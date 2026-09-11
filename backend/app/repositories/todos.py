"""SQLAlchemy ``TodoRepository`` — owner-scoped, eager-loading.

Ownership is a *filter*, not a check: every statement carries
``Todo.user_id == user_id``, so another user's row is indistinguishable from a
missing one and the router turns it into a 404 (D-E2).

The session is committed by ``get_session`` (app/deps.py), never here; the
repository only flushes so generated ids are available.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.util import identity_key

from app.db.models import Tag, Todo, TodoList
from app.db.types import utcnow
from app.errors import (
    ListNotFound,
    SubtaskDepthExceeded,
    SubtaskListMismatch,
    TodoNotFound,
)
from app.query import build_todo_query
from app.repositories.lists import SqlAlchemyListRepository
from app.repositories.protocols import UNSET, TodoCreateData, TodoQuery, TodoUpdateData
from app.repositories.tags import SqlAlchemyTagRepository


def _loader_options() -> tuple:
    """Eager-load everything a ``TodoResponse`` touches.

    A lazy load in an async context raises ``MissingGreenlet`` (risk R2), so
    these options are mandatory rather than an optimisation.

    Exactly one level of ``subtasks`` is loaded: a subtask's own ``subtasks``
    can never be non-empty (invariant I1), and ``TodoResponse`` serialises it as
    ``[]`` without touching the relationship, so a second level would be one
    wasted SELECT per request.
    """
    return (
        selectinload(Todo.tags),
        selectinload(Todo.subtasks).selectinload(Todo.tags),
    )


class SqlAlchemyTodoRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._lists = SqlAlchemyListRepository(session)
        self._tags = SqlAlchemyTagRepository(session)

    def take_created_tags(self) -> list[Tag]:
        """The tags this request inserted while writing a todo (IT3-1).

        A todo write is the only way a tag comes into existence (D-A3), so the
        router that announces the todo is also the one that has to announce the
        tags — and it can only learn about them from the tag repository this
        one owns. Delegation rather than a second outbox: one list, one drain.
        """
        return self._tags.take_created_tags()

    # -- reads --------------------------------------------------------------
    async def list_todos(
        self, user_id: UUID, query: TodoQuery
    ) -> tuple[list[Todo], int]:
        """Filtered, sorted, paginated top-level todos + the unpaginated total.

        The filter/sort translation lives in ``app/query.py`` so it can be unit
        tested without a repository, and so the count and the row select can
        never drift apart — they are built from one set of clauses.
        """
        rows_stmt, count_stmt = build_todo_query(user_id, query)
        total = int((await self._session.execute(count_stmt)).scalar_one())
        rows_stmt = rows_stmt.options(*_loader_options())
        rows = list((await self._session.execute(rows_stmt)).scalars().unique().all())
        return rows, total

    async def get(self, user_id: UUID, todo_id: UUID) -> Todo | None:
        stmt = (
            select(Todo)
            .where(Todo.user_id == user_id, Todo.id == todo_id)
            .options(*_loader_options())
        )
        return (await self._session.execute(stmt)).scalars().unique().one_or_none()

    # -- writes -------------------------------------------------------------
    async def _require_own_list(self, user_id: UUID, list_id: UUID) -> TodoList:
        """Resolve a caller-supplied ``list_id`` **within the caller's lists**.

        Without this, a client could file a todo into — or move one out into —
        another user's list by guessing an id: the todo row would carry the
        attacker's ``user_id`` but the victim's ``list_id``, so deleting that
        list would cascade away the attacker's rows and the victim's list
        counts would include them. Another user's list is reported as simply
        missing (404 ``list_not_found``, D-E2) — never 403.
        """
        todo_list = await self._lists.get(user_id, list_id)
        if todo_list is None:
            raise ListNotFound()
        return todo_list

    async def _require_valid_parent(self, user_id: UUID, parent_id: UUID) -> Todo:
        """Resolve a parent todo, enforcing invariant I1 (one level deep).

        A subtask may not itself have subtasks: the response embeds exactly one
        level, so a deeper tree would silently disappear from every read. An
        unknown or someone else's parent is a 404 ``todo_not_found`` (D-E2);
        an existing parent that is itself a subtask is a 400
        ``subtask_depth_exceeded`` (§6.3), because that is a real, reportable
        client mistake rather than a guess about ids.
        """
        parent = await self.get(user_id, parent_id)
        if parent is None:
            raise TodoNotFound()
        if parent.parent_id is not None:
            raise SubtaskDepthExceeded()
        return parent

    async def create(self, user_id: UUID, data: TodoCreateData) -> Todo:
        parent: Todo | None = None
        if data.parent_id is not None:
            parent = await self._require_valid_parent(user_id, data.parent_id)

        if parent is not None:
            # I2/I3: a subtask inherits its parent's owner and list. A caller
            # that *also* named a list is telling us two different things, so
            # the disagreement is reported rather than resolved silently —
            # filing the row under the parent's list and answering 201 would
            # look like compliance. Ownership is resolved first, so a list that
            # is not the caller's is 404 (D-E2) whether or not it also
            # conflicts: the error must not reveal that the id exists.
            if data.list_id is not None:
                requested = await self._require_own_list(user_id, data.list_id)
                if requested.id != parent.list_id:
                    raise SubtaskListMismatch()
            list_id = parent.list_id
        elif data.list_id is not None:
            list_id = (await self._require_own_list(user_id, data.list_id)).id
        else:
            list_id = (await self._lists.get_default(user_id)).id

        todo = Todo(
            user_id=user_id,
            list_id=list_id,
            parent_id=data.parent_id,
            title=data.title,
            description=data.description,
            completed=False,
            completed_at=None,
            priority=data.priority,
            due_date=data.due_date,
        )
        # A new row has no subtasks; marking the collection loaded keeps
        # serialising the result from triggering a lazy load (risk R2).
        set_committed_value(todo, "subtasks", [])
        # Assigning the tag entities both initialises the collection (so it is
        # never "unloaded") and schedules the todo_tags rows.
        todo.tags = await self._tags.get_or_create_many(user_id, data.tags)

        if parent is not None:
            # Append through the relationship rather than setting parent_id, so
            # a parent already loaded in this session sees its new subtask.
            parent.subtasks.append(todo)
        else:
            self._session.add(todo)
        await self._session.flush()
        return todo

    def _identity_mapped(self, todo_id: UUID | None) -> Todo | None:
        """The instance already in this session, if any — never a query."""
        if todo_id is None:
            return None
        return self._session.identity_map.get(identity_key(Todo, (todo_id,)))

    def _forget_subtasks(self, parent: Todo | None) -> None:
        """Drop a parent's cached ``subtasks`` after its children changed.

        SQLAlchemy does not update a loaded collection when the child's foreign
        key is changed (or the child deleted) behind it, and a subsequent query
        does not overwrite attributes an identity-mapped object already has.
        Without this, a promoted or deleted subtask keeps appearing inside its
        old parent for the rest of the session — visible to anything that reads
        the parent afterwards, which slice 4's "broadcast the parent" rule does
        on every subtask mutation.
        """
        if parent is not None and "subtasks" not in sa_inspect(parent).unloaded:
            self._session.expire(parent, ["subtasks"])

    async def _move_to_list(self, user_id: UUID, todo: Todo, list_id: UUID) -> None:
        """Move a todo — and everything bound to it by I3 — to another list.

        A subtask may not live in a different list from its parent, so a move
        requested on a subtask moves the whole family rather than quietly
        breaking the invariant (or quietly ignoring the request).
        """
        target = await self._require_own_list(user_id, list_id)

        root = todo
        if todo.parent_id is not None:
            parent = await self.get(user_id, todo.parent_id)
            if parent is not None:  # pragma: no branch - FK guarantees it
                root = parent

        root.list_id = target.id
        for subtask in root.subtasks:
            subtask.list_id = target.id

    async def _reparent(
        self, user_id: UUID, todo: Todo, parent_id: UUID | None
    ) -> None:
        """Attach a todo to a parent, or promote a subtask to top level (I1)."""
        if parent_id is None:
            # Promotion keeps the row's list: it is already the parent's, which
            # is a list the caller owns. The FK is set directly — going through
            # the collection would trip its delete-orphan cascade and *delete*
            # the todo instead of promoting it — and the old parent's cached
            # collection is then dropped so it stops claiming this child.
            old_parent = self._identity_mapped(todo.parent_id)
            todo.parent_id = None
            self._forget_subtasks(old_parent)
            return

        if parent_id == todo.id:
            # Its own parent: a cycle, and a second level of nesting the moment
            # anything else points at it.
            raise SubtaskDepthExceeded()

        parent = await self._require_valid_parent(user_id, parent_id)
        if todo.subtasks:
            # Demoting a todo that has its own subtasks would create a second
            # level (I1).
            raise SubtaskDepthExceeded()
        if todo.parent_id == parent.id:
            return

        # Append through the relationship so a parent loaded in this session
        # sees its new child; I3 then pulls the todo into the parent's list.
        old_parent = self._identity_mapped(todo.parent_id)
        parent.subtasks.append(todo)
        todo.list_id = parent.list_id
        if old_parent is not parent:
            self._forget_subtasks(old_parent)

    async def _sync_tags(self, user_id: UUID, todo: Todo, names) -> None:
        """Replace the todo's tags with ``names``, touching only the difference.

        Add/remove rather than clear-and-reinsert: rewriting every association
        row on an unrelated edit would churn the join table and, on PostgreSQL,
        take row locks the request never needed.
        """
        desired: dict[UUID, Tag] = {
            tag.id: tag for tag in await self._tags.get_or_create_many(user_id, names)
        }
        current: dict[UUID, Tag] = {tag.id: tag for tag in todo.tags}

        for tag_id, tag in current.items():
            if tag_id not in desired:
                todo.tags.remove(tag)
        for tag_id, tag in desired.items():
            if tag_id not in current:
                todo.tags.append(tag)

    async def update(
        self, user_id: UUID, todo_id: UUID, data: TodoUpdateData
    ) -> Todo | None:
        todo = await self.get(user_id, todo_id)
        if todo is None:
            return None

        if data.title is not UNSET:
            todo.title = data.title
        if data.description is not UNSET:
            todo.description = data.description
        if data.priority is not UNSET:
            todo.priority = data.priority
        if data.due_date is not UNSET:
            todo.due_date = data.due_date
        if data.parent_id is not UNSET:
            # **Before** the list move, not after. Re-parenting decides whether
            # this todo is still a subtask, and that decides what a list move
            # means: promoting a subtask *and* moving it used to move the whole
            # family, dragging the ex-parent and every ex-sibling along to a
            # list nobody asked about. Promoted first, the move applies to the
            # one todo the request named.
            await self._reparent(user_id, todo, data.parent_id)
        if data.list_id is not UNSET:
            # The *requested* parent, not ``todo.parent_id``: appending through
            # the relationship does not sync the foreign-key attribute until
            # the next flush, so reading it here would still say "top level"
            # and quietly take the family-move branch.
            if data.parent_id is not UNSET and data.parent_id is not None:
                # Both fields supplied and the todo is now a subtask: its list
                # is its parent's (I3), so a different one is a contradiction
                # the client has to resolve — same rule as ``create`` and the
                # same 404-before-400 ordering (D-E2).
                requested = await self._require_own_list(user_id, data.list_id)
                if requested.id != todo.list_id:
                    raise SubtaskListMismatch()
            else:
                await self._move_to_list(user_id, todo, data.list_id)
        if data.tags is not UNSET:
            await self._sync_tags(user_id, todo, data.tags)
        if data.completed is not UNSET:
            # D-A2: completing a parent deliberately does not cascade — the UI
            # shows "2/5 done" instead of pretending the work is finished.
            _apply_completion(todo, data.completed)

        todo.updated_at = utcnow()
        await self._session.flush()
        return todo

    async def delete(self, user_id: UUID, todo_id: UUID) -> bool:
        todo = await self.get(user_id, todo_id)
        if todo is None:
            return False
        parent = self._identity_mapped(todo.parent_id)
        await self._session.delete(todo)
        await self._session.flush()
        # A deleted subtask must not survive inside a parent already loaded in
        # this session.
        self._forget_subtasks(parent)
        return True


def _apply_completion(todo: Todo, completed: bool, *, now: datetime | None = None) -> None:
    """Invariant I4: ``completed_at`` follows the ``completed`` transition."""
    if completed and not todo.completed:
        todo.completed_at = now or utcnow()
    elif not completed and todo.completed:
        todo.completed_at = None
    todo.completed = completed
