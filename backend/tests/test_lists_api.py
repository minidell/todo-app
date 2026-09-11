"""API tests for ``/api/lists`` and list-scoped todos (master §6.2, spec B9.5)."""

import re
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.db.models import Todo

LISTS = "/api/lists"
TODOS = "/api/todos"

TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")

LIST_NOT_FOUND_BODY = {"detail": "List not found", "code": "list_not_found"}
NAME_TAKEN_BODY = {
    "detail": "A list with that name already exists",
    "code": "list_name_taken",
}
LAST_LIST_BODY = {
    "detail": "You must keep at least one list",
    "code": "cannot_delete_last_list",
}

LIST_RESPONSE_KEYS = {
    "id",
    "name",
    "is_default",
    "todo_count",
    "active_count",
    "created_at",
    "updated_at",
}


async def create_list(client: AsyncClient, name: str) -> dict:
    response = await client.post(LISTS, json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


# --- read ------------------------------------------------------------------
async def test_a_new_account_has_exactly_one_default_inbox(
    client: AsyncClient,
) -> None:
    response = await client.get(LISTS)
    assert response.status_code == 200
    body = response.json()

    assert len(body) == 1
    assert set(body[0]) == LIST_RESPONSE_KEYS
    assert body[0]["name"] == "Inbox"
    assert body[0]["is_default"] is True
    assert body[0]["todo_count"] == 0
    assert body[0]["active_count"] == 0
    assert TIMESTAMP_PATTERN.match(body[0]["created_at"])
    assert TIMESTAMP_PATTERN.match(body[0]["updated_at"])


async def test_lists_are_ordered_by_creation(client: AsyncClient) -> None:
    for name in ("Work", "Home", "Errands"):
        await create_list(client, name)

    names = [row["name"] for row in (await client.get(LISTS)).json()]
    assert names == ["Inbox", "Work", "Home", "Errands"]


async def test_counts_are_top_level_only_and_per_list(
    client: AsyncClient, db_session, user
) -> None:
    """Subtasks belong to their parent, so they must not inflate either count."""
    from app.repositories.protocols import TodoCreateData
    from app.repositories.todos import SqlAlchemyTodoRepository

    work = await create_list(client, "Work")
    inbox = [row for row in (await client.get(LISTS)).json() if row["is_default"]][0]

    first = await client.post(TODOS, json={"title": "Open", "list_id": work["id"]})
    done = await client.post(TODOS, json={"title": "Done", "list_id": work["id"]})
    await client.patch(f"{TODOS}/{done.json()['id']}", json={"completed": True})

    repo = SqlAlchemyTodoRepository(db_session)
    await repo.create(
        user.id,
        TodoCreateData(title="Subtask", parent_id=UUID(first.json()["id"])),
    )
    await db_session.commit()

    rows = {row["name"]: row for row in (await client.get(LISTS)).json()}
    assert (rows["Work"]["todo_count"], rows["Work"]["active_count"]) == (2, 1)
    assert (rows["Inbox"]["todo_count"], rows["Inbox"]["active_count"]) == (0, 0)
    assert inbox["id"] == rows["Inbox"]["id"]


async def test_counts_are_one_query_not_one_per_list(
    client: AsyncClient, app, engine
) -> None:
    """Guards against an N+1 rendering of the switcher."""
    from sqlalchemy import event

    for name in ("Work", "Home", "Errands", "Someday"):
        await create_list(client, name)

    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        await client.get(LISTS)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)

    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    # user lookup (auth) + the lists + one grouped count query.
    assert len(selects) <= 3, selects


# --- create ----------------------------------------------------------------
async def test_create_list_returns_201(client: AsyncClient) -> None:
    body = await create_list(client, "  Work  ")
    assert body["name"] == "Work"
    assert body["is_default"] is False
    assert body["todo_count"] == 0
    assert body["active_count"] == 0


async def test_the_first_list_of_a_user_is_the_default(
    anon_client: AsyncClient, db_session, test_settings
) -> None:
    from tests.conftest import bearer_headers, create_user

    fresh = await create_user(db_session, "fresh@example.com")
    # Strip the Inbox that registration would have made, to observe the rule.
    from app.repositories.lists import SqlAlchemyListRepository

    lists = SqlAlchemyListRepository(db_session)
    for row in await lists.list_lists(fresh.id):
        await db_session.delete(row)
    await db_session.commit()

    headers = bearer_headers(fresh, test_settings)
    created = await anon_client.post(LISTS, json={"name": "Only"}, headers=headers)
    assert created.json()["is_default"] is True


@pytest.mark.parametrize("duplicate", ["Work", "work", "  WORK  "])
async def test_duplicate_names_are_409_case_insensitively(
    client: AsyncClient, duplicate: str
) -> None:
    await create_list(client, "Work")

    response = await client.post(LISTS, json={"name": duplicate})
    assert response.status_code == 409
    assert response.json() == NAME_TAKEN_BODY


async def test_duplicate_name_race_is_also_409(
    client: AsyncClient, monkeypatch
) -> None:
    """The UNIQUE index is the real guarantee; it must not surface as a 500."""
    from app.repositories.lists import SqlAlchemyListRepository

    await create_list(client, "Work")

    async def blind(self, user_id, name):
        return None

    monkeypatch.setattr(SqlAlchemyListRepository, "find_by_name", blind)

    response = await client.post(LISTS, json={"name": "Work"})
    assert response.status_code == 409
    assert response.json() == NAME_TAKEN_BODY


@pytest.mark.parametrize(
    "payload",
    [
        {"name": ""},
        {"name": "   "},
        {},
        {"name": "x" * 101},
        {"name": 123},
        {"name": "Work", "is_default": True},  # extra="forbid" (D-E3)
        {"name": "Work", "id": str(uuid4())},
    ],
)
async def test_create_validation_failures_are_422(
    client: AsyncClient, payload: dict
) -> None:
    assert (await client.post(LISTS, json=payload)).status_code == 422, payload


async def test_a_100_character_name_is_accepted(client: AsyncClient) -> None:
    body = await create_list(client, "x" * 100)
    assert len(body["name"]) == 100


# --- rename ----------------------------------------------------------------
async def test_rename_returns_the_updated_list(client: AsyncClient) -> None:
    work = await create_list(client, "Work")

    response = await client.patch(f"{LISTS}/{work['id']}", json={"name": "Work stuff"})
    assert response.status_code == 200
    assert response.json()["name"] == "Work stuff"
    assert response.json()["id"] == work["id"]

    names = [row["name"] for row in (await client.get(LISTS)).json()]
    assert "Work stuff" in names and "Work" not in names


async def test_rename_keeps_the_counts(client: AsyncClient) -> None:
    work = await create_list(client, "Work")
    await client.post(TODOS, json={"title": "Task", "list_id": work["id"]})

    renamed = (
        await client.patch(f"{LISTS}/{work['id']}", json={"name": "Job"})
    ).json()
    assert (renamed["todo_count"], renamed["active_count"]) == (1, 1)


async def test_renaming_to_a_different_case_of_its_own_name_is_allowed(
    client: AsyncClient,
) -> None:
    work = await create_list(client, "Work")
    response = await client.patch(f"{LISTS}/{work['id']}", json={"name": "WORK"})
    assert response.status_code == 200
    assert response.json()["name"] == "WORK"


async def test_renaming_onto_another_list_is_409(client: AsyncClient) -> None:
    await create_list(client, "Work")
    home = await create_list(client, "Home")

    response = await client.patch(f"{LISTS}/{home['id']}", json={"name": "work"})
    assert response.status_code == 409
    assert response.json() == NAME_TAKEN_BODY


@pytest.mark.parametrize("list_id", [str(uuid4()), "not-a-uuid"])
async def test_renaming_an_unknown_or_malformed_id_is_404(
    client: AsyncClient, list_id: str
) -> None:
    response = await client.patch(f"{LISTS}/{list_id}", json={"name": "Nope"})
    assert response.status_code == 404
    assert response.json() == LIST_NOT_FOUND_BODY


async def test_rename_validation_failures_are_422(client: AsyncClient) -> None:
    work = await create_list(client, "Work")
    for payload in ({"name": ""}, {}, {"name": "x" * 101}, {"name": "ok", "x": 1}):
        response = await client.patch(f"{LISTS}/{work['id']}", json=payload)
        assert response.status_code == 422, payload


# --- delete ----------------------------------------------------------------
async def test_delete_removes_the_list_and_its_todos(
    client: AsyncClient, db_session
) -> None:
    work = await create_list(client, "Work")
    await client.post(TODOS, json={"title": "Task", "list_id": work["id"]})

    response = await client.delete(f"{LISTS}/{work['id']}")
    assert response.status_code == 204
    assert response.content == b""

    assert [row["name"] for row in (await client.get(LISTS)).json()] == ["Inbox"]
    remaining = await db_session.execute(select(func.count()).select_from(Todo))
    assert remaining.scalar_one() == 0


async def test_deleting_the_only_list_is_409(client: AsyncClient) -> None:
    inbox = (await client.get(LISTS)).json()[0]

    response = await client.delete(f"{LISTS}/{inbox['id']}")
    assert response.status_code == 409
    assert response.json() == LAST_LIST_BODY
    assert len((await client.get(LISTS)).json()) == 1


async def test_deleting_the_default_promotes_the_oldest_survivor(
    client: AsyncClient,
) -> None:
    inbox = (await client.get(LISTS)).json()[0]
    work = await create_list(client, "Work")
    await create_list(client, "Home")

    assert (await client.delete(f"{LISTS}/{inbox['id']}")).status_code == 204

    rows = (await client.get(LISTS)).json()
    assert [(row["name"], row["is_default"]) for row in rows] == [
        ("Work", True),
        ("Home", False),
    ]
    assert rows[0]["id"] == work["id"]


async def test_deleting_a_non_default_list_leaves_the_default_alone(
    client: AsyncClient,
) -> None:
    work = await create_list(client, "Work")

    assert (await client.delete(f"{LISTS}/{work['id']}")).status_code == 204
    rows = (await client.get(LISTS)).json()
    assert [(row["name"], row["is_default"]) for row in rows] == [("Inbox", True)]


@pytest.mark.parametrize("list_id", [str(uuid4()), "not-a-uuid"])
async def test_deleting_an_unknown_or_malformed_id_is_404(
    client: AsyncClient, list_id: str
) -> None:
    response = await client.delete(f"{LISTS}/{list_id}")
    assert response.status_code == 404
    assert response.json() == LIST_NOT_FOUND_BODY


# --- todos scoped by list --------------------------------------------------
async def test_post_todo_without_list_id_lands_in_the_default_list(
    client: AsyncClient,
) -> None:
    inbox = (await client.get(LISTS)).json()[0]
    created = (await client.post(TODOS, json={"title": "Task"})).json()
    assert created["list_id"] == inbox["id"]


async def test_post_todo_with_list_id_lands_there(client: AsyncClient) -> None:
    work = await create_list(client, "Work")
    created = (
        await client.post(TODOS, json={"title": "Task", "list_id": work["id"]})
    ).json()
    assert created["list_id"] == work["id"]


async def test_post_todo_into_an_unknown_list_is_404(client: AsyncClient) -> None:
    response = await client.post(
        TODOS, json={"title": "Task", "list_id": str(uuid4())}
    )
    assert response.status_code == 404
    assert response.json() == LIST_NOT_FOUND_BODY


async def test_post_todo_with_a_malformed_list_id_is_422(client: AsyncClient) -> None:
    """A body field is validated like any other body field (D-E1 keeps the
    default 422 shape); only *path* ids fall back to 404 (D1)."""
    response = await client.post(TODOS, json={"title": "Task", "list_id": "nope"})
    assert response.status_code == 422


async def test_get_todos_filters_by_list(client: AsyncClient) -> None:
    work = await create_list(client, "Work")
    home = await create_list(client, "Home")

    in_work = (
        await client.post(TODOS, json={"title": "Work task", "list_id": work["id"]})
    ).json()
    in_home = (
        await client.post(TODOS, json={"title": "Home task", "list_id": home["id"]})
    ).json()
    in_inbox = (await client.post(TODOS, json={"title": "Inbox task"})).json()

    work_rows = await client.get(TODOS, params={"list_id": work["id"]})
    assert [row["id"] for row in work_rows.json()] == [in_work["id"]]
    assert work_rows.headers["X-Total-Count"] == "1"

    home_rows = (await client.get(TODOS, params={"list_id": home["id"]})).json()
    assert [row["id"] for row in home_rows] == [in_home["id"]]

    everything = (await client.get(TODOS)).json()
    assert {row["id"] for row in everything} == {
        in_work["id"],
        in_home["id"],
        in_inbox["id"],
    }


@pytest.mark.parametrize("list_id", [str(uuid4()), "not-a-uuid", ""])
async def test_get_todos_with_an_unusable_list_id_is_404(
    client: AsyncClient, list_id: str
) -> None:
    """Never an empty array: that would read as "this list is empty"."""
    response = await client.get(TODOS, params={"list_id": list_id})
    assert response.status_code == 404
    assert response.json() == LIST_NOT_FOUND_BODY
