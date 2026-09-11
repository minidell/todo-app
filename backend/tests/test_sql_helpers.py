"""LIKE/ILIKE escaping, and who a get-or-create says inserted the row."""

import pytest
from sqlalchemy import select

from app.db.models import Tag
from app.repositories.lists import SqlAlchemyListRepository
from app.repositories.sql import (
    LIKE_ESCAPE,
    escape_like,
    get_or_create,
    get_or_create_reporting,
)


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [
        ("Work", "Work"),
        ("%", r"\%"),
        ("_", r"\_"),
        ("100%_done", r"100\%\_done"),
        ("back\\slash", r"back\\slash"),
        # The backslash is escaped first, so its escape is not re-escaped.
        ("\\%", r"\\\%"),
        ("", ""),
    ],
)
def test_escape_like(raw: str, escaped: str) -> None:
    assert escape_like(raw) == escaped


def test_like_escape_is_a_single_backslash() -> None:
    assert LIKE_ESCAPE == "\\"


async def test_find_by_name_treats_wildcards_literally(db_session, user) -> None:
    lists = SqlAlchemyListRepository(db_session)
    await lists.create(user.id, "Work")
    await lists.create(user.id, "Home")

    # Without escaping these would each match an unrelated list.
    assert await lists.find_by_name(user.id, "%") is None
    assert await lists.find_by_name(user.id, "W%") is None
    assert await lists.find_by_name(user.id, "____") is None


async def test_find_by_name_still_matches_exactly_and_case_insensitively(
    db_session, user
) -> None:
    lists = SqlAlchemyListRepository(db_session)
    created = await lists.create(user.id, "Work")

    assert (await lists.find_by_name(user.id, "Work")).id == created.id
    assert (await lists.find_by_name(user.id, "  wOrK  ")).id == created.id
    assert await lists.find_by_name(user.id, "Wor") is None


async def test_find_by_name_matches_a_literal_wildcard_name(
    db_session, user
) -> None:
    lists = SqlAlchemyListRepository(db_session)
    created = await lists.create(user.id, "100% done")

    assert (await lists.find_by_name(user.id, "100% done")).id == created.id
    assert await lists.find_by_name(user.id, "100X done") is None


async def test_find_by_name_is_owner_scoped(db_session, user) -> None:
    from app.repositories.users import SqlAlchemyUserRepository

    other = await SqlAlchemyUserRepository(db_session).create("c@example.com", "!", None)
    lists = SqlAlchemyListRepository(db_session)
    await lists.create(other.id, "Secret")

    assert await lists.find_by_name(user.id, "Secret") is None
    assert await lists.find_by_name(user.id, "%") is None


# --- get_or_create_reporting (iteration 4, IT3-1) --------------------------
#
# ``created_by_us`` is what the ``tag.created`` outbox is built on: a request
# may only announce a row it actually inserted.


async def _find_tag(session, user_id, name: str):
    result = await session.execute(
        select(Tag).where(Tag.user_id == user_id, Tag.name == name)
    )
    return result.scalars().first()


async def test_reporting_says_created_for_a_genuine_insert(db_session, user) -> None:
    row, created = await get_or_create_reporting(
        db_session,
        find=lambda: _find_tag(db_session, user.id, "home"),
        build=lambda: Tag(user_id=user.id, name="home"),
    )

    assert created is True
    assert row.name == "home"


async def test_reporting_says_not_created_when_the_row_already_existed(
    db_session, user
) -> None:
    first, _ = await get_or_create_reporting(
        db_session,
        find=lambda: _find_tag(db_session, user.id, "home"),
        build=lambda: Tag(user_id=user.id, name="home"),
    )

    second, created = await get_or_create_reporting(
        db_session,
        find=lambda: _find_tag(db_session, user.id, "home"),
        build=lambda: Tag(user_id=user.id, name="home"),
    )

    assert created is False
    assert second.id == first.id


async def test_reporting_says_not_created_when_the_race_is_lost(
    db_session, user
) -> None:
    """The savepoint path: somebody else inserted between find() and flush().

    ``find`` is made to miss the winner's row exactly once, which is what a
    concurrent insert looks like from inside this call; the insert then trips
    the unique index, the savepoint rolls back, and the winner's row comes back
    — as somebody else's, so ``created`` must be False.
    """
    winner = Tag(user_id=user.id, name="home")
    db_session.add(winner)
    await db_session.flush()

    misses = {"left": 1}

    async def find():
        if misses["left"]:
            misses["left"] -= 1
            return None
        return await _find_tag(db_session, user.id, "home")

    row, created = await get_or_create_reporting(
        db_session, find=find, build=lambda: Tag(user_id=user.id, name="home")
    )

    assert created is False
    assert row.id == winner.id


async def test_get_or_create_is_the_row_half_of_reporting(db_session, user) -> None:
    created = await get_or_create(
        db_session,
        find=lambda: _find_tag(db_session, user.id, "work"),
        build=lambda: Tag(user_id=user.id, name="work"),
    )
    again = await get_or_create(
        db_session,
        find=lambda: _find_tag(db_session, user.id, "work"),
        build=lambda: Tag(user_id=user.id, name="work"),
    )

    assert again.id == created.id
