"""API tests for /api/todos — the iteration-1 case list, now against the DB.

The four capabilities and their status codes are unchanged; what changed is
that the rows live in PostgreSQL/SQLite and the response carries the full
iteration-2 ``TodoResponse`` shape (master §3.3).
"""

import re
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

CREATED_AT_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")

NOT_FOUND_BODY = {"detail": "Todo not found", "code": "todo_not_found"}

TODO_RESPONSE_KEYS = {
    "id",
    "list_id",
    "parent_id",
    "title",
    "description",
    "completed",
    "completed_at",
    "priority",
    "due_date",
    "tags",
    "subtasks",
    "created_at",
    "updated_at",
}


async def test_list_todos_empty_store(client: AsyncClient) -> None:
    response = await client.get("/api/todos")
    assert response.status_code == 200
    assert response.json() == []


async def test_create_todo_returns_full_iteration2_shape(client: AsyncClient) -> None:
    response = await client.post("/api/todos", json={"title": "Buy milk"})
    assert response.status_code == 201
    body = response.json()

    assert set(body.keys()) == TODO_RESPONSE_KEYS
    assert body["title"] == "Buy milk"
    assert body["completed"] is False
    assert body["completed_at"] is None
    assert body["parent_id"] is None
    assert body["description"] is None
    assert body["priority"] == "medium"
    assert body["due_date"] is None
    assert body["tags"] == []
    assert body["subtasks"] == []
    # UUIDs are serialised as plain strings.
    assert isinstance(body["id"], str)
    assert isinstance(body["list_id"], str)
    UUID(body["id"])
    UUID(body["list_id"])
    assert CREATED_AT_PATTERN.match(body["created_at"]), body["created_at"]
    assert CREATED_AT_PATTERN.match(body["updated_at"]), body["updated_at"]


async def test_created_todo_is_scoped_to_the_authenticated_user(
    client: AsyncClient, user
) -> None:
    created = (await client.post("/api/todos", json={"title": "Mine"})).json()
    listed = (await client.get("/api/todos")).json()
    assert [t["id"] for t in listed] == [created["id"]]
    assert created["list_id"] is not None


async def test_list_todos_ordered_by_created_at_ascending(client: AsyncClient) -> None:
    titles = ["first", "second", "third"]
    created_ids = []
    for title in titles:
        response = await client.post("/api/todos", json={"title": title})
        created_ids.append(response.json()["id"])

    response = await client.get("/api/todos")
    assert response.status_code == 200
    body = response.json()
    assert [todo["title"] for todo in body] == titles
    assert [todo["id"] for todo in body] == created_ids


async def test_list_todos_sends_x_total_count(client: AsyncClient) -> None:
    empty = await client.get("/api/todos")
    assert empty.headers["X-Total-Count"] == "0"

    for title in ("a", "b", "c"):
        await client.post("/api/todos", json={"title": title})

    response = await client.get("/api/todos")
    assert response.headers["X-Total-Count"] == "3"
    assert len(response.json()) == 3


async def test_x_total_count_counts_top_level_todos_only(
    client: AsyncClient, db_session, user
) -> None:
    """Subtasks travel inside their parent, so they must not inflate the total."""
    from app.repositories.protocols import TodoCreateData
    from app.repositories.todos import SqlAlchemyTodoRepository

    repo = SqlAlchemyTodoRepository(db_session)
    parent = await repo.create(user.id, TodoCreateData(title="Parent"))
    await repo.create(
        user.id, TodoCreateData(title="Child", parent_id=parent.id)
    )
    await db_session.commit()

    response = await client.get("/api/todos")
    assert response.headers["X-Total-Count"] == "1"
    assert len(response.json()) == 1
    assert len(response.json()[0]["subtasks"]) == 1


async def test_get_todo_by_id(client: AsyncClient) -> None:
    created = (await client.post("/api/todos", json={"title": "Task"})).json()

    response = await client.get(f"/api/todos/{created['id']}")
    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


async def test_get_unknown_and_malformed_id_returns_404(client: AsyncClient) -> None:
    response = await client.get(f"/api/todos/{uuid4()}")
    assert response.status_code == 404
    assert response.json() == NOT_FOUND_BODY

    response = await client.get("/api/todos/not-a-uuid")
    assert response.status_code == 404
    assert response.json() == NOT_FOUND_BODY


async def test_patch_toggles_completed_both_directions(client: AsyncClient) -> None:
    created = (await client.post("/api/todos", json={"title": "Task"})).json()
    todo_id = created["id"]

    response = await client.patch(f"/api/todos/{todo_id}", json={"completed": True})
    assert response.status_code == 200
    body = response.json()
    assert body["completed"] is True
    assert body["id"] == created["id"]
    assert body["title"] == created["title"]
    assert body["created_at"] == created["created_at"]
    # Invariant I4: completed_at is stamped on the false -> true transition.
    assert body["completed_at"] is not None
    assert CREATED_AT_PATTERN.match(body["completed_at"]), body["completed_at"]

    response = await client.patch(f"/api/todos/{todo_id}", json={"completed": False})
    assert response.status_code == 200
    body = response.json()
    assert body["completed"] is False
    assert body["completed_at"] is None
    assert body["id"] == created["id"]
    assert body["title"] == created["title"]
    assert body["created_at"] == created["created_at"]


async def test_patch_persists_across_requests(client: AsyncClient) -> None:
    created = (await client.post("/api/todos", json={"title": "Task"})).json()
    await client.patch(f"/api/todos/{created['id']}", json={"completed": True})

    listed = (await client.get("/api/todos")).json()
    assert listed[0]["completed"] is True


async def test_patch_unknown_and_malformed_id_returns_404(client: AsyncClient) -> None:
    response = await client.patch(f"/api/todos/{uuid4()}", json={"completed": True})
    assert response.status_code == 404
    assert response.json() == NOT_FOUND_BODY

    response = await client.patch("/api/todos/not-a-uuid", json={"completed": True})
    assert response.status_code == 404
    assert response.json() == NOT_FOUND_BODY


async def test_delete_existing_todo_removes_it(client: AsyncClient) -> None:
    created = (await client.post("/api/todos", json={"title": "Task"})).json()

    response = await client.delete(f"/api/todos/{created['id']}")
    assert response.status_code == 204
    assert response.content == b""

    assert (await client.get("/api/todos")).json() == []


async def test_delete_twice_and_malformed_id_returns_404(client: AsyncClient) -> None:
    created = (await client.post("/api/todos", json={"title": "Task"})).json()
    todo_id = created["id"]

    assert (await client.delete(f"/api/todos/{todo_id}")).status_code == 204

    second = await client.delete(f"/api/todos/{todo_id}")
    assert second.status_code == 404
    assert second.json() == NOT_FOUND_BODY

    malformed = await client.delete("/api/todos/not-a-uuid")
    assert malformed.status_code == 404
    assert malformed.json() == NOT_FOUND_BODY


async def test_x_client_id_header_is_accepted_and_never_echoed(
    client: AsyncClient,
) -> None:
    """Slice 4 uses it for echo suppression; here it must simply not break."""
    client_id = str(uuid4())
    response = await client.post(
        "/api/todos", json={"title": "Task"}, headers={"X-Client-Id": client_id}
    )
    assert response.status_code == 201
    assert client_id not in response.text
    assert "x-client-id" not in {k.lower() for k in response.headers}

    # A malformed client id is treated as absent, never as a 4xx.
    response = await client.post(
        "/api/todos", json={"title": "Other"}, headers={"X-Client-Id": "nope"}
    )
    assert response.status_code == 201


@pytest.mark.parametrize("method", ["get", "delete"])
async def test_empty_id_paths_do_not_500(client: AsyncClient, method: str) -> None:
    response = await getattr(client, method)("/api/todos/")
    assert response.status_code in (307, 404, 405)
