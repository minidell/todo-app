"""Cold-start races on get-or-create.

``get_default`` outlived the slice-1 bootstrap user: an account whose lists
were all deleted, or a burst of parallel requests arriving before the first one
commits its ``Inbox``, still has several transactions trying to insert the same
``(user_id, 'Inbox')`` row. Before the savepoint retry that produced
``uq_todo_lists_user_id_name`` 500s.

PostgreSQL lane only: the SQLite test engine holds a single connection behind
``StaticPool``, so these calls serialise there and the race cannot occur.
"""

import asyncio
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.db.models import Tag, TodoList, User
from app.db.session import create_sessionmaker
from app.deps import get_session
from app.main import create_app
from app.repositories.lists import SqlAlchemyListRepository
from app.repositories.users import SqlAlchemyUserRepository
from app.security import create_access_token, hash_password
from tests.conftest import TEST_PASSWORD

pytestmark = pytest.mark.postgres

CONCURRENCY = 20


async def _count(session, entity) -> int:
    return int((await session.execute(select(func.count()).select_from(entity))).scalar_one())


async def _make_user(sessionmaker, email: str) -> UUID:
    """An account with **no lists** — the state that makes get_default race."""
    async with sessionmaker() as session:
        user = await SqlAlchemyUserRepository(session).create(
            email, await hash_password(TEST_PASSWORD), None
        )
        user_id = user.id
        await session.commit()
    return user_id


async def test_concurrent_get_default_for_a_listless_user(engine) -> None:
    sessionmaker = create_sessionmaker(engine)
    user_id = await _make_user(sessionmaker, "race@example.com")

    async def get_default() -> UUID:
        async with sessionmaker() as session:
            todo_list = await SqlAlchemyListRepository(session).get_default(user_id)
            await session.commit()
            return todo_list.id

    list_ids = await asyncio.gather(*(get_default() for _ in range(CONCURRENCY)))

    assert len(set(list_ids)) == 1, "every caller must get the same Inbox"
    async with sessionmaker() as session:
        assert await _count(session, TodoList) == 1


async def test_concurrent_requests_on_a_cold_account_all_succeed(
    engine, test_settings
) -> None:
    """The reported symptom, end to end: parallel API calls for one account
    whose default list does not exist yet."""
    sessionmaker = create_sessionmaker(engine)
    user_id = await _make_user(sessionmaker, "cold@example.com")
    token, _ = create_access_token(user_id, test_settings)

    app = create_app(test_settings)
    app.state.engine = engine
    app.state.sessionmaker = sessionmaker
    # No get_session override here: every request gets its own session, which
    # is the whole point of the test.
    assert get_session not in app.dependency_overrides

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        responses = await asyncio.gather(
            *(client.get("/api/todos") for _ in range(CONCURRENCY)),
            *(
                client.post("/api/todos", json={"title": f"todo {index}"})
                for index in range(5)
            ),
        )

    statuses = [response.status_code for response in responses]
    assert set(statuses) <= {200, 201}, statuses

    async with sessionmaker() as session:
        assert await _count(session, User) == 1
        assert await _count(session, TodoList) == 1


async def test_concurrent_registrations_of_one_email_produce_one_account(
    engine, test_settings
) -> None:
    """The UNIQUE index decides the winner; the losers get 409, never a 500."""
    app = create_app(test_settings)
    app.state.engine = engine
    app.state.sessionmaker = create_sessionmaker(engine)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        payload = {"email": "duplicate@example.com", "password": TEST_PASSWORD}
        # The auth limiter allows 10 attempts per (address, email).
        responses = await asyncio.gather(
            *(client.post("/api/auth/register", json=payload) for _ in range(10))
        )

    statuses = sorted(response.status_code for response in responses)
    assert statuses.count(201) == 1, statuses
    assert set(statuses) <= {201, 409}, statuses

    async with create_sessionmaker(engine)() as session:
        assert await _count(session, User) == 1
        # Exactly one Inbox: a failed registration must not leave a list behind.
        assert await _count(session, TodoList) == 1


async def test_concurrent_use_of_a_new_tag_creates_it_once(
    engine, test_settings
) -> None:
    """Tags are created implicitly (D-A3), so the first use of a word races.

    Twenty todos tagged ``home`` at once means twenty transactions that all see
    no ``home`` yet. Without the savepoint retry in ``get_or_create``, nineteen
    of them would die on ``uq_tags_user_id_name`` — and the user would just see
    failed saves the first time they used a tag.
    """
    sessionmaker = create_sessionmaker(engine)
    user_id = await _make_user(sessionmaker, "tags@example.com")
    token, _ = create_access_token(user_id, test_settings)

    app = create_app(test_settings)
    app.state.engine = engine
    app.state.sessionmaker = sessionmaker

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        responses = await asyncio.gather(
            *(
                client.post(
                    "/api/todos",
                    json={"title": f"todo {index}", "tags": ["home", "urgent"]},
                )
                for index in range(CONCURRENCY)
            )
        )

    assert {response.status_code for response in responses} == {201}
    assert all(response.json()["tags"] == ["home", "urgent"] for response in responses)

    async with sessionmaker() as session:
        assert await _count(session, Tag) == 2


async def test_a_genuine_integrity_error_still_propagates(engine) -> None:
    """The retry must not swallow unrelated constraint violations."""
    sessionmaker = create_sessionmaker(engine)
    async with sessionmaker() as session:
        users = SqlAlchemyUserRepository(session)
        await users.create("taken@example.com", "!", None)
        await session.commit()

    async with sessionmaker() as session:
        users = SqlAlchemyUserRepository(session)
        with pytest.raises(IntegrityError) as excinfo:
            # A different user id, but an email that is already taken: not the
            # race get_or_create retries, so it must surface.
            await users.create("taken@example.com", "!", None)
        assert "uq_users_email" in str(excinfo.value)
