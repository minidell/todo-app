"""Request validation: title rules, extra="forbid" (C3/D-E3), 422 body shape."""

from httpx import AsyncClient


async def test_create_todo_strips_padded_title(client: AsyncClient) -> None:
    response = await client.post("/api/todos", json={"title": "  padded  "})
    assert response.status_code == 201
    assert response.json()["title"] == "padded"


async def test_create_todo_invalid_titles_return_422(client: AsyncClient) -> None:
    invalid_payloads = [
        {"title": ""},
        {"title": "   "},
        {},
        {"title": 123},
        {"title": "a" * 201},
    ]
    for payload in invalid_payloads:
        response = await client.post("/api/todos", json=payload)
        assert response.status_code == 422, payload


async def test_create_todo_with_200_char_title_returns_201(client: AsyncClient) -> None:
    title = "a" * 200
    response = await client.post("/api/todos", json={"title": title})
    assert response.status_code == 201
    assert response.json()["title"] == title


async def test_patch_with_no_fields_returns_400_empty_update(
    client: AsyncClient,
) -> None:
    """Master R11: ``PATCH`` is partial from slice 3, so ``{}`` is no longer a
    schema error (missing ``completed``) but a request with nothing to do."""
    created = (await client.post("/api/todos", json={"title": "Task"})).json()

    response = await client.patch(f"/api/todos/{created['id']}", json={})
    assert response.status_code == 400
    assert response.json() == {"detail": "No fields to update", "code": "empty_update"}


async def test_patch_non_boolean_completed_returns_422(client: AsyncClient) -> None:
    created = (await client.post("/api/todos", json={"title": "Task"})).json()
    response = await client.patch(
        f"/api/todos/{created['id']}", json={"completed": "maybe"}
    )
    assert response.status_code == 422


async def test_create_todo_rejects_extra_keys_with_422(client: AsyncClient) -> None:
    """C3 / D-E3: this reverses iteration-1 D4, where extra keys were ignored.

    Rejecting them means a client can never believe it set a field the server
    silently dropped — notably ``id`` and ``completed``, which are server-owned.
    """
    response = await client.post("/api/todos", json={"title": "Buy milk", "nope": 1})
    assert response.status_code == 422

    for payload in (
        {"title": "Buy milk", "id": "client-supplied-id"},
        # ``completed`` is server-owned even now that ``priority``, ``due_date``
        # and friends became writable in slice 3.
        {"title": "Buy milk", "completed": True},
        {"title": "Buy milk", "completed_at": "1970-01-01T00:00:00Z"},
        {"title": "Buy milk", "unexpected": {"nested": "value"}},
    ):
        assert (await client.post("/api/todos", json=payload)).status_code == 422, payload


async def test_patch_rejects_extra_keys_with_422(client: AsyncClient) -> None:
    created = (await client.post("/api/todos", json={"title": "Task"})).json()
    todo_id = created["id"]

    for payload in (
        {"completed": True, "x": 1},
        {"completed": True, "created_at": "1970-01-01T00:00:00Z"},
        {"completed": True, "completed_at": "1970-01-01T00:00:00Z"},
        {"completed": True, "id": "client-supplied-id"},
        {"completed": True, "user_id": "someone-else"},
    ):
        response = await client.patch(f"/api/todos/{todo_id}", json=payload)
        assert response.status_code == 422, payload

    # The whole body is rejected, so the *valid* key next to the unknown one is
    # not applied either.
    unchanged = (await client.get(f"/api/todos/{todo_id}")).json()
    assert unchanged["completed"] is False
    assert unchanged["title"] == created["title"]


async def test_422_body_shape_matches_fastapi_default(client: AsyncClient) -> None:
    """D-E1: validation errors keep FastAPI's array body and carry no `code`."""
    response = await client.post("/api/todos", json={"title": ""})
    assert response.status_code == 422
    body = response.json()
    assert isinstance(body["detail"], list)
    assert len(body["detail"]) >= 1
    assert "code" not in body
    for error in body["detail"]:
        assert "loc" in error
        assert "msg" in error
        assert "type" in error
