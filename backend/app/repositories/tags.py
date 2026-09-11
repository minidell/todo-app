"""SQLAlchemy ``TagRepository`` — owner-scoped, get-or-create by name.

Tags have no creation endpoint (D-A3): they come into existence by being used
on a todo, which makes ``get_or_create_many`` the hot path and races real —
two requests can tag two different todos ``home`` at the same moment. The
shared savepoint-based :func:`app.repositories.sql.get_or_create` turns that
race into "both callers get the winner's row" instead of a 500.

Names are always normalized here as well as at the wire edge: a repository is
reachable from tests, fixtures and (in slice 4) the AI path, and a single
un-normalized insert would create a duplicate the ``UNIQUE`` index cannot see.
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Tag, Todo, todo_tags
from app.errors import TagNameTaken
from app.repositories.sql import get_or_create_reporting
from app.schemas.tags import normalize_tag, validate_tag


class SqlAlchemyTagRepository:
    """Owner-scoped tag store with a per-request "what did I insert" outbox.

    The outbox (iteration 4, IT3-1) exists because tags are created deep inside
    the *todo* write path: the router that has to stage a ``tag.created`` frame
    never sees the tag entities, and re-querying "which of these tags are new?"
    after the write is impossible — by then they all exist. So the only place
    that knows is the one that inserted them, and it writes the answer down.

    The instance is request-scoped (``Depends(get_tag_repository)`` builds a new
    one per request), and :meth:`take_created_tags` *drains*, so a frame can
    never be emitted twice or leak into another request.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._created: list[Tag] = []

    # -- reads --------------------------------------------------------------
    async def list_tags(self, user_id: UUID) -> list[Tag]:
        stmt = (
            select(Tag).where(Tag.user_id == user_id).order_by(Tag.name.asc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_with_counts(self, user_id: UUID) -> list[tuple[Tag, int]]:
        """``[(tag, todo_count)]`` ordered by name — one query, never N+1.

        **Top-level todos only**, the same rule as ``ListResponse.todo_count``
        and — decisively — the same rule as the ``tag=`` filter, which matches
        parents and never surfaces a todo for a tag only its subtask carries.
        Counting subtasks here would print a number the user cannot reproduce:
        the chip would claim five todos and filtering by it would list three.

        The outer join keeps tags nobody uses, with a count of 0: a tag orphaned
        by removing it from its last todo stays in the user's vocabulary
        (spec B3).
        """
        stmt = (
            select(Tag, func.count(Todo.id))
            .outerjoin(todo_tags, todo_tags.c.tag_id == Tag.id)
            .outerjoin(
                Todo,
                (Todo.id == todo_tags.c.todo_id) & (Todo.parent_id.is_(None)),
            )
            .where(Tag.user_id == user_id)
            .group_by(Tag.id)
            .order_by(Tag.name.asc())
        )
        rows = (await self._session.execute(stmt)).all()
        return [(row[0], int(row[1] or 0)) for row in rows]

    async def get(self, user_id: UUID, tag_id: UUID) -> Tag | None:
        stmt = select(Tag).where(Tag.user_id == user_id, Tag.id == tag_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def find_by_name(self, user_id: UUID, name: str) -> Tag | None:
        """Exact lookup on the normalized name.

        Equality, not ``ilike``: names are stored normalized, so exact matching
        *is* case-insensitive matching, and there is no pattern for a value like
        ``%`` to widen.
        """
        stmt = select(Tag).where(
            Tag.user_id == user_id, Tag.name == normalize_tag(name)
        )
        return (await self._session.execute(stmt)).scalars().first()

    # -- writes -------------------------------------------------------------
    async def get_or_create_many(
        self, user_id: UUID, names: Sequence[str]
    ) -> list[Tag]:
        """Resolve names to entities, creating the missing ones.

        One ``SELECT ... WHERE name IN (...)`` for the whole batch rather than
        one per name, then an insert only for what is genuinely new. Returns the
        entities in the order the caller asked for, de-duplicated after
        normalization.

        Names are **validated** here, not merely normalized. The wire schema
        already rejects a bad name, but this method is also reachable from
        fixtures and, in slice 4, from the AI path — and an LLM is exactly the
        caller most likely to invent ``"Bad!Tag"``. A repository that trusts its
        input would persist a name no API request could ever have created, and
        the invalid row would then come back out of ``GET /api/tags`` forever.
        """
        wanted = list(
            dict.fromkeys(validate_tag(name) for name in names if normalize_tag(name))
        )
        if not wanted:
            return []

        stmt = select(Tag).where(Tag.user_id == user_id, Tag.name.in_(wanted))
        found = {
            tag.name: tag for tag in (await self._session.execute(stmt)).scalars()
        }

        for name in wanted:
            if name in found:
                continue
            # Bound the closure's ``name`` per iteration: a late-evaluated
            # lambda would otherwise build every tag with the last name.
            created, created_by_us = await get_or_create_reporting(
                self._session,
                find=lambda n=name: self.find_by_name(user_id, n),
                build=lambda n=name: Tag(user_id=user_id, name=n),
            )
            if created_by_us:
                # Only a row *this* request inserted goes in the outbox. A row
                # the concurrent-insert race handed us belongs to the request
                # that won it, and that request announces it.
                self._created.append(created)
            found[created.name] = created

        return [found[name] for name in wanted]

    def take_created_tags(self) -> list[Tag]:
        """The tags this request inserted, in insertion order — and forget them.

        Draining rather than reading is deliberate: the caller stages one frame
        per tag, and a second call must return nothing rather than a repeat.
        Idempotent when nothing was created (``[]``).
        """
        created, self._created = self._created, []
        return created

    async def rename(self, user_id: UUID, tag_id: UUID, name: str) -> Tag | None:
        """Rename a tag, or ``None`` when it is not the caller's.

        Raises :class:`TagNameTaken` when the normalized name already belongs to
        another tag of this user — checked in the application *and* caught from
        the ``UNIQUE`` index, so the race between the two is a 409 rather than a
        500. A name that is not a valid tag raises ``ValueError`` rather than
        being written: the router's schema catches that first, but the rule
        belongs to the store, not to one caller of it.
        """
        tag = await self.get(user_id, tag_id)
        if tag is None:
            return None

        normalized = validate_tag(name)
        clash = await self.find_by_name(user_id, normalized)
        if clash is not None and clash.id != tag.id:
            raise TagNameTaken()

        tag.name = normalized
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise TagNameTaken() from exc
        return tag

    async def delete(self, user_id: UUID, tag_id: UUID) -> bool:
        """Delete a tag; its ``todo_tags`` rows go with the FK cascade.

        The association rows are removed explicitly as well: SQLite only honours
        the cascade with ``PRAGMA foreign_keys=ON`` and, more importantly, a
        ``Todo.tags`` collection already loaded in this session would otherwise
        keep pointing at a deleted row for the rest of the request.
        """
        tag = await self.get(user_id, tag_id)
        if tag is None:
            return False
        await self._session.execute(
            delete(todo_tags).where(todo_tags.c.tag_id == tag.id)
        )
        await self._session.delete(tag)
        await self._session.flush()
        return True
