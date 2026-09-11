"""SQLAlchemy ``ListRepository`` — owner-scoped todo lists.

Only ``get_default`` is exercised in slice 1 (every todo needs a list); the
remaining methods are the frozen protocol surface that slice 2 exposes over
``/api/lists``.
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Todo, TodoList
from app.errors import CannotDeleteLastList
from app.repositories.sql import LIKE_ESCAPE, escape_like, get_or_create

DEFAULT_LIST_NAME = "Inbox"


class SqlAlchemyListRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_lists(self, user_id: UUID) -> list[TodoList]:
        stmt = (
            select(TodoList)
            .where(TodoList.user_id == user_id)
            .order_by(TodoList.created_at.asc(), TodoList.id.asc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get(self, user_id: UUID, list_id: UUID) -> TodoList | None:
        stmt = select(TodoList).where(
            TodoList.user_id == user_id, TodoList.id == list_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _find_default(self, user_id: UUID) -> TodoList | None:
        stmt = (
            select(TodoList)
            .where(TodoList.user_id == user_id)
            .order_by(
                TodoList.is_default.desc(),
                TodoList.created_at.asc(),
                TodoList.id.asc(),
            )
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def get_default(self, user_id: UUID) -> TodoList:
        """The user's default list, falling back to the oldest, else a new Inbox.

        "Exactly one default list per user" is an application invariant — a
        partial unique index is not portable (D-T1) — so the query is ordered
        deterministically instead of relying on uniqueness.

        Creation is idempotent: two requests racing on a user with no lists
        both try to insert ``Inbox``, one loses on ``uq_todo_lists_user_id_name``
        and returns the winner's row instead of failing. This survives slice 2,
        which keeps ``get_default`` after deleting the bootstrap user.
        """
        return await get_or_create(
            self._session,
            find=lambda: self._find_default(user_id),
            build=lambda: TodoList(
                user_id=user_id, name=DEFAULT_LIST_NAME, is_default=True
            ),
        )

    async def create(
        self, user_id: UUID, name: str, *, is_default: bool | None = None
    ) -> TodoList:
        """Create a list. ``is_default=None`` means "default iff it is the first".

        A user must always have exactly one default list — it is where a todo
        without a ``list_id`` lands — so the very first list is it, and callers
        that know better (the registration ``Inbox``) say so explicitly.
        """
        if is_default is None:
            is_default = await self.count(user_id) == 0
        todo_list = TodoList(
            user_id=user_id, name=name.strip(), is_default=is_default
        )
        self._session.add(todo_list)
        await self._session.flush()
        return todo_list

    async def find_by_name(self, user_id: UUID, name: str) -> TodoList | None:
        """Exact, case-insensitive lookup.

        ``ilike`` compiles on both engines (D-T1), but it is a *pattern* match:
        the name is escaped so a value like ``"%"`` cannot widen it into
        "return any list of this user".
        """
        stmt = select(TodoList).where(
            TodoList.user_id == user_id,
            TodoList.name.ilike(escape_like(name.strip()), escape=LIKE_ESCAPE),
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def rename(
        self, user_id: UUID, list_id: UUID, name: str
    ) -> TodoList | None:
        todo_list = await self.get(user_id, list_id)
        if todo_list is None:
            return None
        todo_list.name = name.strip()
        await self._session.flush()
        return todo_list

    async def delete(self, user_id: UUID, list_id: UUID) -> bool:
        """Delete a list (cascading its todos) and keep a default behind.

        Raises :class:`CannotDeleteLastList` rather than leaving the user with
        nowhere to file a todo. The guard lives here, not only in the router,
        so no future caller can bypass it.
        """
        todo_list = await self.get(user_id, list_id)
        if todo_list is None:
            return False
        if await self.count(user_id) <= 1:
            raise CannotDeleteLastList()

        was_default = todo_list.is_default
        await self._session.delete(todo_list)
        await self._session.flush()
        if was_default:
            # Deleting the default is allowed (§6.2); the oldest survivor takes
            # over, so "exactly one default per user" still holds afterwards.
            await self.promote_default(user_id)
        return True

    async def promote_default(self, user_id: UUID) -> TodoList | None:
        """Make the oldest remaining list this user's default."""
        stmt = (
            select(TodoList)
            .where(TodoList.user_id == user_id)
            .order_by(TodoList.created_at.asc(), TodoList.id.asc())
            .limit(1)
        )
        oldest = (await self._session.execute(stmt)).scalars().first()
        if oldest is None:
            return None
        if not oldest.is_default:
            oldest.is_default = True
            await self._session.flush()
        return oldest

    async def todo_counts(
        self, user_id: UUID, list_ids: Sequence[UUID] | None = None
    ) -> dict[UUID, tuple[int, int]]:
        """``{list_id: (todo_count, active_count)}`` for the caller's lists.

        One grouped query for every list, not one query per list: rendering the
        switcher must not cost N round trips (spec B7). Only top-level rows are
        counted — a subtask is part of its parent, not a separate entry — and
        lists with no todos are simply absent from the mapping, so the caller
        supplies the ``(0, 0)`` default.
        """
        active = func.sum(case((Todo.completed.is_(False), 1), else_=0))
        stmt = (
            select(Todo.list_id, func.count(Todo.id), active)
            .where(Todo.user_id == user_id, Todo.parent_id.is_(None))
            .group_by(Todo.list_id)
        )
        if list_ids is not None:
            if not list_ids:
                return {}
            stmt = stmt.where(Todo.list_id.in_(list_ids))
        rows = (await self._session.execute(stmt)).all()
        return {row[0]: (int(row[1]), int(row[2] or 0)) for row in rows}

    async def top_level_todo_ids(
        self, user_id: UUID, list_id: UUID, *, limit: int
    ) -> list[UUID]:
        """Ids of the top-level todos a list holds — for the delete fan-out.

        Deleting a list cascades its todos away, and slice-4 §7.2 wants one
        ``todo.deleted`` frame per top-level row so a receiver can drop them
        without a refetch. Only ids are selected: the frames carry nothing else,
        and loading whole rows (with their tags and subtasks) to throw them away
        would make deleting a large list cost several queries for nothing.

        Subtasks are excluded because they are not rows a client renders on
        their own — they vanish with the parent's frame.
        """
        stmt = (
            select(Todo.id)
            .where(
                Todo.user_id == user_id,
                Todo.list_id == list_id,
                Todo.parent_id.is_(None),
            )
            .order_by(Todo.created_at.asc(), Todo.id.asc())
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def count(self, user_id: UUID) -> int:
        stmt = select(func.count()).select_from(TodoList).where(
            TodoList.user_id == user_id
        )
        return int((await self._session.execute(stmt)).scalar_one())
