"""Shared SQL helpers for the repository layer."""

from typing import Awaitable, Callable, TypeVar

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

#: Passed as ``escape=`` alongside every :func:`escape_like` result.
LIKE_ESCAPE = "\\"

T = TypeVar("T")


async def get_or_create_reporting(
    session: AsyncSession,
    *,
    find: Callable[[], Awaitable[T | None]],
    build: Callable[[], T],
) -> tuple[T, bool]:
    """Idempotent get-or-create, safe against a concurrent identical insert.

    Returns ``(row, created_by_us)``. ``created_by_us`` is ``False`` when
    ``find`` won the race and returned somebody else's row — that row is not
    ours to announce (iteration 4, IT3-1: it is the flag the ``tag.created``
    outbox is built on, and announcing a row this request did not insert would
    put a duplicate frame on another tab's stream).

    ``SELECT`` then ``INSERT`` is a race: two requests arriving on an empty
    database both see nothing and both insert, and one of them dies on the
    unique/primary key. With a cold start that is not a rare edge case — it is
    every request in the first burst.

    The insert therefore runs inside a **savepoint**: if it trips a unique
    constraint, the savepoint (not the whole transaction) is rolled back, the
    row the winner just wrote is re-selected, and the loser returns that. Both
    callers get the same row and neither sees an error.

    A savepoint is used rather than retrying the outer transaction because the
    caller's transaction may already carry work that must survive; it is also
    portable — PostgreSQL and SQLite both implement ``SAVEPOINT``.
    """
    existing = await find()
    if existing is not None:
        return existing, False

    try:
        async with session.begin_nested():
            created = build()
            session.add(created)
            await session.flush()
    except IntegrityError:
        # Someone else inserted the same row between our find() and flush().
        # The savepoint is already rolled back; the winner's row is visible.
        existing = await find()
        if existing is None:
            # A different constraint failed — this is not the race we handle.
            raise
        return existing, False
    return created, True


async def get_or_create(
    session: AsyncSession,
    *,
    find: Callable[[], Awaitable[T | None]],
    build: Callable[[], T],
) -> T:
    """:func:`get_or_create_reporting` for callers that only want the row.

    The savepoint logic lives in exactly one place; this is the thin spelling
    for the call sites that do not care who inserted the row.
    """
    row, _created = await get_or_create_reporting(session, find=find, build=build)
    return row


def escape_like(value: str) -> str:
    """Escape the LIKE/ILIKE metacharacters in a user-supplied string.

    ``%`` and ``_`` are wildcards in a LIKE pattern, so an unescaped value lets
    a caller widen a comparison it was never meant to control — ``"%"`` matches
    every row, turning an exact-match lookup into "does this user have *any*
    row". The backslash itself must be escaped first, or it would consume the
    escapes added afterwards.

    Always pair the result with ``escape=LIKE_ESCAPE``::

        column.ilike(escape_like(value), escape=LIKE_ESCAPE)

    Slice 3's substring search wraps the escaped value in its own ``%…%``.
    """
    return (
        value.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", f"{LIKE_ESCAPE}%")
        .replace("_", f"{LIKE_ESCAPE}_")
    )
