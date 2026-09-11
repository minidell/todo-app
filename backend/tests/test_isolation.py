"""Cross-user isolation matrix (slice-2 spec B9.4, decisions D-E2 / C1).

The rule under test: **A can neither observe nor affect anything of B's, and
every attempt looks exactly like "it does not exist".** 403 would confirm the
resource exists and who owns it; 200 would be a breach. So the assertion is
always 404 — and, after every attempt, that B's data is untouched.
"""

import pytest
from httpx import AsyncClient

from app.db.models import User

TODOS = "/api/todos"
LISTS = "/api/lists"
TAGS = "/api/tags"

TODO_NOT_FOUND_BODY = {"detail": "Todo not found", "code": "todo_not_found"}
LIST_NOT_FOUND_BODY = {"detail": "List not found", "code": "list_not_found"}
TAG_NOT_FOUND_BODY = {"detail": "Tag not found", "code": "tag_not_found"}


@pytest.fixture
async def b_data(
    anon_client: AsyncClient, other_user: User, other_user_auth_headers: dict
) -> dict:
    """A list, a tagged todo and a subtask owned by user B."""
    headers = other_user_auth_headers
    todo_list = (
        await anon_client.post(LISTS, json={"name": "B's list"}, headers=headers)
    ).json()
    todo = (
        await anon_client.post(
            TODOS,
            json={
                "title": "B's secret",
                "list_id": todo_list["id"],
                "tags": ["confidential"],
            },
            headers=headers,
        )
    ).json()
    subtask = (
        await anon_client.post(
            f"{TODOS}/{todo['id']}/subtasks",
            json={"title": "B's step"},
            headers=headers,
        )
    ).json()
    tag = (await anon_client.get(TAGS, headers=headers)).json()[0]
    return {
        "headers": headers,
        "list": todo_list,
        "todo": todo,
        "subtask": subtask,
        "tag": tag,
    }


# --- todos -----------------------------------------------------------------
async def test_a_does_not_see_b_todos(client: AsyncClient, b_data: dict) -> None:
    response = await client.get(TODOS)
    assert response.status_code == 200
    assert response.json() == []
    assert response.headers["X-Total-Count"] == "0"


async def test_a_cannot_read_b_todo(client: AsyncClient, b_data: dict) -> None:
    response = await client.get(f"{TODOS}/{b_data['todo']['id']}")
    assert response.status_code == 404
    assert response.json() == TODO_NOT_FOUND_BODY


async def test_a_cannot_patch_b_todo(
    anon_client: AsyncClient, client: AsyncClient, b_data: dict
) -> None:
    response = await client.patch(
        f"{TODOS}/{b_data['todo']['id']}", json={"completed": True}
    )
    assert response.status_code == 404
    assert response.json() == TODO_NOT_FOUND_BODY

    unchanged = await anon_client.get(
        f"{TODOS}/{b_data['todo']['id']}", headers=b_data["headers"]
    )
    assert unchanged.json()["completed"] is False


async def test_a_cannot_delete_b_todo(
    anon_client: AsyncClient, client: AsyncClient, b_data: dict
) -> None:
    response = await client.delete(f"{TODOS}/{b_data['todo']['id']}")
    assert response.status_code == 404
    assert response.json() == TODO_NOT_FOUND_BODY

    survivor = await anon_client.get(
        f"{TODOS}/{b_data['todo']['id']}", headers=b_data["headers"]
    )
    assert survivor.status_code == 200


async def test_a_cannot_create_a_todo_in_b_list(
    anon_client: AsyncClient, client: AsyncClient, b_data: dict
) -> None:
    """A guessed list id must not let A file rows into B's list — the row would
    carry A's owner but B's list, so B deleting the list would cascade it away
    and B's counts would include it."""
    response = await client.post(
        TODOS, json={"title": "Trespass", "list_id": b_data["list"]["id"]}
    )
    assert response.status_code == 404
    assert response.json() == LIST_NOT_FOUND_BODY

    b_todos = await anon_client.get(
        TODOS, params={"list_id": b_data["list"]["id"]}, headers=b_data["headers"]
    )
    assert [row["title"] for row in b_todos.json()] == ["B's secret"]


async def test_a_cannot_list_todos_of_b_list(client: AsyncClient, b_data: dict) -> None:
    response = await client.get(TODOS, params={"list_id": b_data["list"]["id"]})
    assert response.status_code == 404
    assert response.json() == LIST_NOT_FOUND_BODY


async def test_a_cannot_add_a_subtask_to_b_todo(
    anon_client: AsyncClient, client: AsyncClient, b_data: dict
) -> None:
    response = await client.post(
        f"{TODOS}/{b_data['todo']['id']}/subtasks", json={"title": "Trespass"}
    )
    assert response.status_code == 404
    assert response.json() == TODO_NOT_FOUND_BODY

    b_view = await anon_client.get(
        f"{TODOS}/{b_data['todo']['id']}", headers=b_data["headers"]
    )
    assert [sub["title"] for sub in b_view.json()["subtasks"]] == ["B's step"]


async def test_a_cannot_reach_b_subtask_directly(
    client: AsyncClient, b_data: dict
) -> None:
    subtask_id = b_data["subtask"]["id"]
    attempts = [
        await client.get(f"{TODOS}/{subtask_id}"),
        await client.patch(f"{TODOS}/{subtask_id}", json={"title": "Hacked"}),
        await client.delete(f"{TODOS}/{subtask_id}"),
    ]
    assert [response.status_code for response in attempts] == [404, 404, 404]


async def test_a_cannot_adopt_b_todo_as_a_parent(
    client: AsyncClient, b_data: dict
) -> None:
    mine = (await client.post(TODOS, json={"title": "Mine"})).json()
    response = await client.patch(
        f"{TODOS}/{mine['id']}", json={"parent_id": b_data["todo"]["id"]}
    )
    assert response.status_code == 404
    assert response.json() == TODO_NOT_FOUND_BODY


# --- tags ------------------------------------------------------------------
async def test_a_does_not_see_b_tags(client: AsyncClient, b_data: dict) -> None:
    assert (await client.get(TAGS)).json() == []


async def test_a_cannot_rename_or_delete_b_tag(
    anon_client: AsyncClient, client: AsyncClient, b_data: dict
) -> None:
    tag_id = b_data["tag"]["id"]
    rename = await client.patch(f"{TAGS}/{tag_id}", json={"name": "hacked"})
    delete = await client.delete(f"{TAGS}/{tag_id}")

    assert [rename.status_code, delete.status_code] == [404, 404]
    assert rename.json() == TAG_NOT_FOUND_BODY

    b_tags = (await anon_client.get(TAGS, headers=b_data["headers"])).json()
    assert [tag["name"] for tag in b_tags] == ["confidential"]


async def test_a_filtering_by_b_tag_name_matches_nothing(
    client: AsyncClient, b_data: dict
) -> None:
    """Same name, different owner: the EXISTS clause is scoped to the caller."""
    await client.post(TODOS, json={"title": "A's own", "tags": ["mine"]})
    response = await client.get(TODOS, params={"tag": "confidential"})
    assert response.status_code == 200
    assert response.json() == []


# --- lists -----------------------------------------------------------------
async def test_a_does_not_see_b_lists(client: AsyncClient, b_data: dict) -> None:
    names = [row["name"] for row in (await client.get(LISTS)).json()]
    assert names == ["Inbox"]
    assert "B's list" not in names


async def test_a_cannot_rename_b_list(
    anon_client: AsyncClient, client: AsyncClient, b_data: dict
) -> None:
    response = await client.patch(
        f"{LISTS}/{b_data['list']['id']}", json={"name": "Hacked"}
    )
    assert response.status_code == 404
    assert response.json() == LIST_NOT_FOUND_BODY

    b_lists = (await anon_client.get(LISTS, headers=b_data["headers"])).json()
    assert "B's list" in [row["name"] for row in b_lists]
    assert "Hacked" not in [row["name"] for row in b_lists]


async def test_a_cannot_delete_b_list(
    anon_client: AsyncClient, client: AsyncClient, b_data: dict
) -> None:
    response = await client.delete(f"{LISTS}/{b_data['list']['id']}")
    assert response.status_code == 404
    assert response.json() == LIST_NOT_FOUND_BODY

    b_lists = (await anon_client.get(LISTS, headers=b_data["headers"])).json()
    assert "B's list" in [row["name"] for row in b_lists]


async def test_the_same_list_name_is_free_for_each_user(
    anon_client: AsyncClient, client: AsyncClient, b_data: dict
) -> None:
    """Uniqueness is per user; B owning "Work" must not block A."""
    assert (
        await anon_client.post(
            LISTS, json={"name": "Work"}, headers=b_data["headers"]
        )
    ).status_code == 201
    assert (await client.post(LISTS, json={"name": "Work"})).status_code == 201


async def test_no_response_ever_carries_a_password_hash(
    anon_client: AsyncClient, client: AsyncClient, b_data: dict
) -> None:
    todo = (await client.post(TODOS, json={"title": "Mine"})).json()
    responses = [
        await client.get("/api/auth/me"),
        await client.get(LISTS),
        await client.get(TODOS),
        await client.get(f"{TODOS}/{todo['id']}"),
    ]
    for response in responses:
        assert "password" not in response.text.lower(), response.request.url


async def test_b_still_sees_everything_after_a_full_attack_run(
    anon_client: AsyncClient, client: AsyncClient, b_data: dict
) -> None:
    """The whole matrix in one pass, then B's world must be exactly as it was."""
    todo_id, list_id = b_data["todo"]["id"], b_data["list"]["id"]

    tag_id, subtask_id = b_data["tag"]["id"], b_data["subtask"]["id"]

    attempts = [
        await client.get(f"{TODOS}/{todo_id}"),
        await client.patch(f"{TODOS}/{todo_id}", json={"completed": True}),
        await client.patch(f"{TODOS}/{todo_id}", json={"tags": ["hacked"]}),
        await client.delete(f"{TODOS}/{todo_id}"),
        await client.get(TODOS, params={"list_id": list_id}),
        await client.post(TODOS, json={"title": "x", "list_id": list_id}),
        await client.post(TODOS, json={"title": "x", "parent_id": todo_id}),
        await client.post(f"{TODOS}/{todo_id}/subtasks", json={"title": "x"}),
        await client.patch(f"{TODOS}/{subtask_id}", json={"completed": True}),
        await client.delete(f"{TODOS}/{subtask_id}"),
        await client.patch(f"{LISTS}/{list_id}", json={"name": "Hacked"}),
        await client.delete(f"{LISTS}/{list_id}"),
        await client.patch(f"{TAGS}/{tag_id}", json={"name": "hacked"}),
        await client.delete(f"{TAGS}/{tag_id}"),
    ]
    assert [response.status_code for response in attempts] == [404] * len(attempts)
    assert all(response.status_code != 403 for response in attempts)

    b_todos = (
        await anon_client.get(
            TODOS, params={"list_id": list_id}, headers=b_data["headers"]
        )
    ).json()
    assert [(row["title"], row["completed"]) for row in b_todos] == [
        ("B's secret", False)
    ]
    assert [sub["title"] for sub in b_todos[0]["subtasks"]] == ["B's step"]
    assert b_todos[0]["tags"] == ["confidential"]
    b_lists = (await anon_client.get(LISTS, headers=b_data["headers"])).json()
    assert sorted(row["name"] for row in b_lists) == ["B's list", "Inbox"]
    b_tags = (await anon_client.get(TAGS, headers=b_data["headers"])).json()
    assert [tag["name"] for tag in b_tags] == ["confidential"]
