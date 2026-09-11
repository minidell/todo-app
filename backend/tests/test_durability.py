"""Every mutating endpoint commits its own work (QA item 3).

``test_session_scope.py`` proves the *ordering* is right and
``test_live_server.py`` proves it over a real socket. This module closes the
remaining hole: a handler that simply forgets ``await session.commit()``. Now
that the session dependency no longer commits in its teardown, such a handler
would answer 201 and quietly discard the row when the session closes.

Catching that needs a **second connection**: the request's own session sees its
uncommitted writes, and so does anything sharing its connection. That is why
this module is PostgreSQL-only — the SQLite lane runs every request through one
``StaticPool`` connection, where an uncommitted row is indistinguishable from a
committed one. Each assertion here therefore opens a *separate engine* and
looks for the row from outside the request's transaction entirely.
"""

from datetime import date
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.db.models import Priority, Tag, Todo, TodoList, User, todo_tags
from app.db.session import create_engine_from_url, create_sessionmaker
from app.main import create_app
from app.repositories.lists import SqlAlchemyListRepository
from app.repositories.users import SqlAlchemyUserRepository
from app.security import create_access_token, hash_password
from tests.conftest import TEST_PASSWORD

pytestmark = pytest.mark.postgres


@pytest.fixture
async def outsider(db_url: str):
    """A connection pool of its own: it can only see committed rows."""
    engine = create_engine_from_url(db_url)
    sessionmaker = create_sessionmaker(engine)

    async def query(statement):
        async with sessionmaker() as session:
            return (await session.execute(statement)).scalars().all()

    try:
        yield query
    finally:
        await engine.dispose()


@pytest.fixture
async def api(engine, test_settings):
    """The app with its **real** per-request sessions (no override)."""
    app = create_app(test_settings)
    app.state.engine = engine
    app.state.sessionmaker = create_sessionmaker(engine)

    sessionmaker = create_sessionmaker(engine)
    async with sessionmaker() as session:
        user = await SqlAlchemyUserRepository(session).create(
            "durable@example.com", await hash_password(TEST_PASSWORD), None
        )
        await SqlAlchemyListRepository(session).create(
            user.id, "Inbox", is_default=True
        )
        user_id = user.id
        await session.commit()

    token, _ = create_access_token(user_id, test_settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield client


async def test_a_created_todo_is_committed(api, outsider) -> None:
    created = await api.post("/api/todos", json={"title": "Durable"})
    assert created.status_code == 201

    rows = await outsider(select(Todo).where(Todo.id == UUID(created.json()["id"])))
    assert [row.title for row in rows] == ["Durable"]


async def test_a_toggled_todo_is_committed(api, outsider) -> None:
    todo_id = (await api.post("/api/todos", json={"title": "Toggle"})).json()["id"]

    patched = await api.patch(f"/api/todos/{todo_id}", json={"completed": True})
    assert patched.status_code == 200

    rows = await outsider(select(Todo).where(Todo.id == UUID(todo_id)))
    assert rows[0].completed is True
    assert rows[0].completed_at is not None


async def test_a_deleted_todo_is_committed(api, outsider) -> None:
    todo_id = (await api.post("/api/todos", json={"title": "Delete me"})).json()["id"]

    assert (await api.delete(f"/api/todos/{todo_id}")).status_code == 204

    assert await outsider(select(Todo).where(Todo.id == UUID(todo_id))) == []


async def test_a_created_list_is_committed(api, outsider) -> None:
    created = await api.post("/api/lists", json={"name": "Work"})
    assert created.status_code == 201

    rows = await outsider(
        select(TodoList).where(TodoList.id == UUID(created.json()["id"]))
    )
    assert [row.name for row in rows] == ["Work"]


async def test_a_renamed_list_is_committed(api, outsider) -> None:
    list_id = (await api.post("/api/lists", json={"name": "Work"})).json()["id"]

    assert (
        await api.patch(f"/api/lists/{list_id}", json={"name": "Job"})
    ).status_code == 200

    rows = await outsider(select(TodoList).where(TodoList.id == UUID(list_id)))
    assert [row.name for row in rows] == ["Job"]


async def test_a_deleted_list_is_committed(api, outsider) -> None:
    list_id = (await api.post("/api/lists", json={"name": "Work"})).json()["id"]

    assert (await api.delete(f"/api/lists/{list_id}")).status_code == 204

    assert await outsider(select(TodoList).where(TodoList.id == UUID(list_id))) == []


async def test_a_promoted_default_list_is_committed(api, outsider) -> None:
    """The delete cascade *and* the promotion of the oldest survivor."""
    inbox = (await api.get("/api/lists")).json()[0]
    work = (await api.post("/api/lists", json={"name": "Work"})).json()

    assert (await api.delete(f"/api/lists/{inbox['id']}")).status_code == 204

    rows = await outsider(select(TodoList).where(TodoList.id == UUID(work["id"])))
    assert rows[0].is_default is True


async def test_a_registration_is_committed(engine, test_settings, outsider) -> None:
    """Both rows: an account without its Inbox could not hold a todo."""
    app = create_app(test_settings)
    app.state.engine = engine
    app.state.sessionmaker = create_sessionmaker(engine)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/auth/register",
            json={"email": "fresh@example.com", "password": TEST_PASSWORD},
        )
        assert response.status_code == 201

    user_id = UUID(response.json()["id"])
    users = await outsider(select(User).where(User.id == user_id))
    lists = await outsider(select(TodoList).where(TodoList.user_id == user_id))
    assert [user.email for user in users] == ["fresh@example.com"]
    assert [(row.name, row.is_default) for row in lists] == [("Inbox", True)]


async def test_a_rehashed_password_is_committed(
    engine, test_settings, outsider, monkeypatch
) -> None:
    from argon2 import PasswordHasher

    # This suite runs the fast argon2 profile (risk R3), under which login
    # deliberately skips the upgrade — rewriting there would *downgrade* a
    # production-strength hash to test strength. The profile check is pinned to
    # "off", which is what a real deployment reports, so the durability of the
    # upgrade is what gets tested rather than the profile gate.
    monkeypatch.setattr("app.routers.auth.using_fast_hashing", lambda: False)

    sessionmaker = create_sessionmaker(engine)
    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(TEST_PASSWORD)
    async with sessionmaker() as session:
        user = await SqlAlchemyUserRepository(session).create(
            "legacy@example.com", weak, None
        )
        user_id = user.id
        await session.commit()

    app = create_app(test_settings)
    app.state.engine = engine
    app.state.sessionmaker = create_sessionmaker(engine)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/auth/login",
            json={"email": "legacy@example.com", "password": TEST_PASSWORD},
        )
        assert response.status_code == 200

    rows = await outsider(select(User).where(User.id == user_id))
    assert rows[0].password_hash != weak


# --- slice 3: enrichment, tags and subtasks --------------------------------
async def test_a_todo_created_with_tags_is_committed_with_its_tags(
    api, outsider
) -> None:
    """Two tables and a join row, one transaction: an outsider must see all of
    it or none of it — never a todo whose tags arrive later."""
    created = await api.post(
        "/api/todos",
        json={"title": "Tagged", "priority": "high", "tags": ["home", "errand"]},
    )
    assert created.status_code == 201
    todo_id = UUID(created.json()["id"])

    # The schema is fresh per test and the ``api`` fixture created exactly one
    # account, so every tag row here belongs to it.
    tags = await outsider(select(Tag))
    assert sorted(tag.name for tag in tags) == ["errand", "home"]

    links = await outsider(
        select(todo_tags.c.tag_id).where(todo_tags.c.todo_id == todo_id)
    )
    assert len(links) == 2


async def test_a_partial_patch_is_committed(api, outsider) -> None:
    todo_id = (await api.post("/api/todos", json={"title": "Enrich"})).json()["id"]

    patched = await api.patch(
        f"/api/todos/{todo_id}",
        json={"description": "notes", "priority": "low", "due_date": "2026-09-05"},
    )
    assert patched.status_code == 200

    rows = await outsider(select(Todo).where(Todo.id == UUID(todo_id)))
    assert rows[0].description == "notes"
    assert rows[0].priority is Priority.LOW
    assert rows[0].due_date == date(2026, 9, 5)


async def test_a_tag_sync_on_patch_is_committed(api, outsider) -> None:
    """The association *diff* must land, not just the todo row."""
    todo_id = (
        await api.post("/api/todos", json={"title": "Retag", "tags": ["home", "work"]})
    ).json()["id"]

    patched = await api.patch(f"/api/todos/{todo_id}", json={"tags": ["work"]})
    assert patched.status_code == 200

    links = await outsider(
        select(todo_tags.c.tag_id).where(todo_tags.c.todo_id == UUID(todo_id))
    )
    assert len(links) == 1

    cleared = await api.patch(f"/api/todos/{todo_id}", json={"tags": []})
    assert cleared.status_code == 200
    assert (
        await outsider(
            select(todo_tags.c.tag_id).where(todo_tags.c.todo_id == UUID(todo_id))
        )
        == []
    )


async def test_a_created_subtask_is_committed(api, outsider) -> None:
    parent_id = (await api.post("/api/todos", json={"title": "Parent"})).json()["id"]

    created = await api.post(
        f"/api/todos/{parent_id}/subtasks", json={"title": "Step", "tags": ["home"]}
    )
    assert created.status_code == 201

    rows = await outsider(select(Todo).where(Todo.parent_id == UUID(parent_id)))
    assert [row.title for row in rows] == ["Step"]


async def test_a_reparented_todo_is_committed(api, outsider) -> None:
    parent_id = (await api.post("/api/todos", json={"title": "Parent"})).json()["id"]
    loose_id = (await api.post("/api/todos", json={"title": "Loose"})).json()["id"]

    assert (
        await api.patch(f"/api/todos/{loose_id}", json={"parent_id": parent_id})
    ).status_code == 200
    rows = await outsider(select(Todo).where(Todo.id == UUID(loose_id)))
    assert rows[0].parent_id == UUID(parent_id)

    # Promotion is the branch that writes the FK directly rather than through
    # the collection, so it gets its own assertion.
    assert (
        await api.patch(f"/api/todos/{loose_id}", json={"parent_id": None})
    ).status_code == 200
    rows = await outsider(select(Todo).where(Todo.id == UUID(loose_id)))
    assert rows[0].parent_id is None


async def test_a_list_move_is_committed_for_the_whole_family(api, outsider) -> None:
    """I3 moves the subtasks too; all of it must be durable together."""
    work_id = (await api.post("/api/lists", json={"name": "Work"})).json()["id"]
    parent_id = (await api.post("/api/todos", json={"title": "Parent"})).json()["id"]
    child_id = (
        await api.post(f"/api/todos/{parent_id}/subtasks", json={"title": "Step"})
    ).json()["id"]

    moved = await api.patch(f"/api/todos/{parent_id}", json={"list_id": work_id})
    assert moved.status_code == 200

    for todo_id in (parent_id, child_id):
        rows = await outsider(select(Todo).where(Todo.id == UUID(todo_id)))
        assert rows[0].list_id == UUID(work_id), todo_id


async def test_a_renamed_tag_is_committed(api, outsider) -> None:
    await api.post("/api/todos", json={"title": "Tagged", "tags": ["errand"]})
    tag_id = (await api.get("/api/tags")).json()[0]["id"]

    renamed = await api.patch(f"/api/tags/{tag_id}", json={"name": "errands"})
    assert renamed.status_code == 200

    rows = await outsider(select(Tag).where(Tag.id == UUID(tag_id)))
    assert rows[0].name == "errands"


async def test_a_deleted_tag_is_committed_with_its_associations(
    api, outsider
) -> None:
    todo_id = (
        await api.post("/api/todos", json={"title": "Tagged", "tags": ["errand"]})
    ).json()["id"]
    tag_id = (await api.get("/api/tags")).json()[0]["id"]

    assert (await api.delete(f"/api/tags/{tag_id}")).status_code == 204

    assert await outsider(select(Tag).where(Tag.id == UUID(tag_id))) == []
    assert (
        await outsider(
            select(todo_tags.c.tag_id).where(todo_tags.c.todo_id == UUID(todo_id))
        )
        == []
    )
    # The todo itself survives — deleting a tag is not deleting the work.
    assert await outsider(select(Todo).where(Todo.id == UUID(todo_id))) != []


async def test_a_failed_mutation_commits_nothing(api, outsider) -> None:
    """Rollback still works: a 404 must not leave a half-written row behind."""
    before = await outsider(select(func.count()).select_from(Todo))

    response = await api.post(
        "/api/todos", json={"title": "Trespass", "list_id": str(UUID(int=0))}
    )
    assert response.status_code == 404

    after = await outsider(select(func.count()).select_from(Todo))
    assert after == before
