"""Subtasks: exactly one level deep (spec B4/B7.3, invariants I1–I4).

The depth rule is not cosmetic: ``TodoResponse`` embeds exactly one level of
``subtasks``, so a grandchild would exist in the database and be invisible in
every read. Every route that could create one is tested to refuse.
"""

from uuid import uuid4

from httpx import AsyncClient

TODOS = "/api/todos"
LISTS = "/api/lists"

DEPTH_BODY = {
    "detail": "Subtasks can only be one level deep",
    "code": "subtask_depth_exceeded",
}
TODO_NOT_FOUND_BODY = {"detail": "Todo not found", "code": "todo_not_found"}


async def _parent(client: AsyncClient, title: str = "Parent") -> dict:
    return (await client.post(TODOS, json={"title": title})).json()


# --- creating --------------------------------------------------------------
async def test_subtask_route_creates_a_child_of_the_path_todo(
    client: AsyncClient,
) -> None:
    parent = await _parent(client)

    response = await client.post(
        f"{TODOS}/{parent['id']}/subtasks",
        json={"title": "Step one", "priority": "high", "tags": ["home"]},
    )
    assert response.status_code == 201
    body = response.json()

    assert body["parent_id"] == parent["id"]
    assert body["title"] == "Step one"
    assert body["priority"] == "high"
    assert body["tags"] == ["home"]
    assert body["subtasks"] == []
    # I2/I3: the owner and the list are inherited, never supplied.
    assert body["list_id"] == parent["list_id"]


async def test_post_todos_with_parent_id_is_the_same_code_path(
    client: AsyncClient,
) -> None:
    parent = await _parent(client)

    via_body = (
        await client.post(TODOS, json={"title": "Child", "parent_id": parent["id"]})
    ).json()
    via_route = (
        await client.post(f"{TODOS}/{parent['id']}/subtasks", json={"title": "Other"})
    ).json()

    assert via_body["parent_id"] == via_route["parent_id"] == parent["id"]
    assert via_body["list_id"] == via_route["list_id"] == parent["list_id"]


async def test_a_subtask_inherits_the_parents_list_not_the_default(
    client: AsyncClient,
) -> None:
    work = (await client.post(LISTS, json={"name": "Work"})).json()
    parent = (
        await client.post(TODOS, json={"title": "Parent", "list_id": work["id"]})
    ).json()

    child = (
        await client.post(f"{TODOS}/{parent['id']}/subtasks", json={"title": "Child"})
    ).json()
    assert child["list_id"] == work["id"]


async def test_a_conflicting_list_id_is_reported_not_discarded(
    client: AsyncClient,
) -> None:
    """``parent_id`` + a different ``list_id`` is two instructions, not one.

    Silently letting the parent win filed the todo somewhere the caller did not
    ask for and answered 201 as though it had complied.
    """
    work = (await client.post(LISTS, json={"name": "Work"})).json()
    parent = await _parent(client)
    assert parent["list_id"] != work["id"]

    response = await client.post(
        TODOS,
        json={"title": "Child", "parent_id": parent["id"], "list_id": work["id"]},
    )
    assert response.status_code == 400
    assert response.json() == {
        "detail": "A subtask always belongs to its parent's list",
        "code": "subtask_list_mismatch",
    }
    # Nothing was written.
    assert (await client.get(f"{TODOS}/{parent['id']}")).json()["subtasks"] == []


async def test_a_list_id_matching_the_parents_list_is_accepted(
    client: AsyncClient,
) -> None:
    """Agreeing with the parent is not a contradiction."""
    parent = await _parent(client)
    response = await client.post(
        TODOS,
        json={
            "title": "Child",
            "parent_id": parent["id"],
            "list_id": parent["list_id"],
        },
    )
    assert response.status_code == 201
    assert response.json()["list_id"] == parent["list_id"]


async def test_a_foreign_list_id_with_a_parent_is_404_not_the_mismatch_error(
    anon_client: AsyncClient, client: AsyncClient, other_user_auth_headers: dict
) -> None:
    """Ownership is resolved first: the error must not reveal that the id
    exists (D-E2)."""
    b_list = (
        await anon_client.post(
            LISTS, json={"name": "B's list"}, headers=other_user_auth_headers
        )
    ).json()
    parent = await _parent(client)

    response = await client.post(
        TODOS,
        json={"title": "Child", "parent_id": parent["id"], "list_id": b_list["id"]},
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "List not found", "code": "list_not_found"}


async def test_subtasks_never_appear_as_top_level_rows(client: AsyncClient) -> None:
    parent = await _parent(client)
    child = (
        await client.post(f"{TODOS}/{parent['id']}/subtasks", json={"title": "Child"})
    ).json()

    response = await client.get(TODOS)
    listed = response.json()
    assert [row["id"] for row in listed] == [parent["id"]]
    assert [sub["id"] for sub in listed[0]["subtasks"]] == [child["id"]]
    # The subtask does not inflate the match count either.
    assert response.headers["X-Total-Count"] == "1"


async def test_subtasks_are_ordered_by_creation(client: AsyncClient) -> None:
    parent = await _parent(client)
    ids = [
        (
            await client.post(
                f"{TODOS}/{parent['id']}/subtasks", json={"title": f"step {index}"}
            )
        ).json()["id"]
        for index in range(4)
    ]

    fetched = (await client.get(f"{TODOS}/{parent['id']}")).json()
    assert [sub["id"] for sub in fetched["subtasks"]] == ids


# --- depth (I1) ------------------------------------------------------------
async def test_a_subtask_cannot_take_a_subtask_via_the_route(
    client: AsyncClient,
) -> None:
    parent = await _parent(client)
    child = (
        await client.post(f"{TODOS}/{parent['id']}/subtasks", json={"title": "Child"})
    ).json()

    response = await client.post(
        f"{TODOS}/{child['id']}/subtasks", json={"title": "Grandchild"}
    )
    assert response.status_code == 400
    assert response.json() == DEPTH_BODY


async def test_a_subtask_cannot_take_a_subtask_via_parent_id(
    client: AsyncClient,
) -> None:
    parent = await _parent(client)
    child = (
        await client.post(f"{TODOS}/{parent['id']}/subtasks", json={"title": "Child"})
    ).json()

    response = await client.post(
        TODOS, json={"title": "Grandchild", "parent_id": child["id"]}
    )
    assert response.status_code == 400
    assert response.json() == DEPTH_BODY


async def test_patching_parent_id_onto_a_subtask_is_rejected(
    client: AsyncClient,
) -> None:
    parent = await _parent(client)
    child = (
        await client.post(f"{TODOS}/{parent['id']}/subtasks", json={"title": "Child"})
    ).json()
    loose = (await client.post(TODOS, json={"title": "Loose"})).json()

    response = await client.patch(
        f"{TODOS}/{loose['id']}", json={"parent_id": child["id"]}
    )
    assert response.status_code == 400
    assert response.json() == DEPTH_BODY


async def test_demoting_a_todo_that_has_subtasks_is_rejected(
    client: AsyncClient,
) -> None:
    parent = await _parent(client)
    await client.post(f"{TODOS}/{parent['id']}/subtasks", json={"title": "Child"})
    target = (await client.post(TODOS, json={"title": "Target"})).json()

    response = await client.patch(
        f"{TODOS}/{parent['id']}", json={"parent_id": target["id"]}
    )
    assert response.status_code == 400
    assert response.json() == DEPTH_BODY


async def test_a_todo_cannot_become_its_own_parent(client: AsyncClient) -> None:
    todo = await _parent(client, "Self")
    response = await client.patch(
        f"{TODOS}/{todo['id']}", json={"parent_id": todo["id"]}
    )
    assert response.status_code == 400
    assert response.json() == DEPTH_BODY


# --- re-parenting ----------------------------------------------------------
async def test_a_top_level_todo_can_be_demoted_and_promoted_again(
    client: AsyncClient,
) -> None:
    parent = await _parent(client)
    loose = (await client.post(TODOS, json={"title": "Loose"})).json()

    demoted = await client.patch(
        f"{TODOS}/{loose['id']}", json={"parent_id": parent["id"]}
    )
    assert demoted.status_code == 200
    assert demoted.json()["parent_id"] == parent["id"]
    assert [row["id"] for row in (await client.get(TODOS)).json()] == [parent["id"]]

    promoted = await client.patch(f"{TODOS}/{loose['id']}", json={"parent_id": None})
    assert promoted.status_code == 200
    assert promoted.json()["parent_id"] is None
    # Promotion must not have deleted the row (delete-orphan cascade trap).
    assert sorted(row["id"] for row in (await client.get(TODOS)).json()) == sorted(
        [parent["id"], loose["id"]]
    )


async def test_promoting_a_subtask_with_a_list_move_moves_only_that_todo(
    client: AsyncClient,
) -> None:
    """``parent_id`` is applied before ``list_id``.

    The other order moved the whole family first — while the todo was still a
    subtask — so promoting one child dragged its ex-parent and every ex-sibling
    into a list nobody asked about.
    """
    work = (await client.post(LISTS, json={"name": "Work"})).json()
    parent = await _parent(client)
    inbox_id = parent["list_id"]
    promoted = (
        await client.post(f"{TODOS}/{parent['id']}/subtasks", json={"title": "Leaving"})
    ).json()
    sibling = (
        await client.post(f"{TODOS}/{parent['id']}/subtasks", json={"title": "Staying"})
    ).json()

    response = await client.patch(
        f"{TODOS}/{promoted['id']}", json={"parent_id": None, "list_id": work["id"]}
    )
    assert response.status_code == 200
    assert response.json()["parent_id"] is None
    assert response.json()["list_id"] == work["id"]

    # The family it left is exactly where it was.
    remaining = (await client.get(f"{TODOS}/{parent['id']}")).json()
    assert remaining["list_id"] == inbox_id
    assert [(sub["title"], sub["list_id"]) for sub in remaining["subtasks"]] == [
        ("Staying", inbox_id)
    ]
    assert (await client.get(f"{TODOS}/{sibling['id']}")).json()["list_id"] == inbox_id


async def test_demoting_with_a_conflicting_list_id_is_reported(
    client: AsyncClient,
) -> None:
    work = (await client.post(LISTS, json={"name": "Work"})).json()
    parent = await _parent(client)
    loose = (await client.post(TODOS, json={"title": "Loose"})).json()

    response = await client.patch(
        f"{TODOS}/{loose['id']}",
        json={"parent_id": parent["id"], "list_id": work["id"]},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "subtask_list_mismatch"

    # Neither half was applied.
    unchanged = (await client.get(f"{TODOS}/{loose['id']}")).json()
    assert unchanged["parent_id"] is None
    assert unchanged["list_id"] == parent["list_id"]


async def test_demoting_with_the_parents_own_list_id_is_accepted(
    client: AsyncClient,
) -> None:
    parent = await _parent(client)
    loose = (await client.post(TODOS, json={"title": "Loose"})).json()

    response = await client.patch(
        f"{TODOS}/{loose['id']}",
        json={"parent_id": parent["id"], "list_id": parent["list_id"]},
    )
    assert response.status_code == 200
    assert response.json()["parent_id"] == parent["id"]


# --- ownership -------------------------------------------------------------
async def test_unknown_or_malformed_parent_is_404(client: AsyncClient) -> None:
    for todo_id in (str(uuid4()), "not-a-uuid"):
        response = await client.post(
            f"{TODOS}/{todo_id}/subtasks", json={"title": "Child"}
        )
        assert response.status_code == 404
        assert response.json() == TODO_NOT_FOUND_BODY

    body_route = await client.post(
        TODOS, json={"title": "Child", "parent_id": str(uuid4())}
    )
    assert body_route.status_code == 404
    assert body_route.json() == TODO_NOT_FOUND_BODY


async def test_another_users_todo_cannot_be_a_parent(
    anon_client: AsyncClient, client: AsyncClient, other_user_auth_headers: dict
) -> None:
    b_todo = (
        await anon_client.post(
            TODOS, json={"title": "B's task"}, headers=other_user_auth_headers
        )
    ).json()

    via_route = await client.post(
        f"{TODOS}/{b_todo['id']}/subtasks", json={"title": "Trespass"}
    )
    via_body = await client.post(
        TODOS, json={"title": "Trespass", "parent_id": b_todo["id"]}
    )
    assert [via_route.status_code, via_body.status_code] == [404, 404]
    assert via_route.json() == TODO_NOT_FOUND_BODY

    # B's todo gained nothing.
    b_view = await anon_client.get(
        f"{TODOS}/{b_todo['id']}", headers=other_user_auth_headers
    )
    assert b_view.json()["subtasks"] == []


async def test_subtask_creation_requires_authentication(
    anon_client: AsyncClient, client: AsyncClient
) -> None:
    parent = await _parent(client)
    response = await anon_client.post(
        f"{TODOS}/{parent['id']}/subtasks", json={"title": "Child"}
    )
    assert response.status_code == 401


# --- lifecycle -------------------------------------------------------------
async def test_deleting_a_parent_deletes_its_subtasks(client: AsyncClient) -> None:
    parent = await _parent(client)
    child = (
        await client.post(f"{TODOS}/{parent['id']}/subtasks", json={"title": "Child"})
    ).json()

    assert (await client.delete(f"{TODOS}/{parent['id']}")).status_code == 204
    assert (await client.get(f"{TODOS}/{child['id']}")).status_code == 404


async def test_deleting_a_subtask_leaves_the_parent(client: AsyncClient) -> None:
    parent = await _parent(client)
    child = (
        await client.post(f"{TODOS}/{parent['id']}/subtasks", json={"title": "Child"})
    ).json()

    assert (await client.delete(f"{TODOS}/{child['id']}")).status_code == 204
    remaining = (await client.get(f"{TODOS}/{parent['id']}")).json()
    assert remaining["subtasks"] == []


async def test_completing_a_parent_does_not_complete_its_subtasks(
    client: AsyncClient,
) -> None:
    """D-A2: subtask completion is independent; the UI shows ``2/5 done``."""
    parent = await _parent(client)
    for title in ("one", "two"):
        await client.post(f"{TODOS}/{parent['id']}/subtasks", json={"title": title})

    done = await client.patch(f"{TODOS}/{parent['id']}", json={"completed": True})
    assert done.json()["completed"] is True
    assert [sub["completed"] for sub in done.json()["subtasks"]] == [False, False]


async def test_completing_a_subtask_does_not_complete_its_parent(
    client: AsyncClient,
) -> None:
    parent = await _parent(client)
    child = (
        await client.post(f"{TODOS}/{parent['id']}/subtasks", json={"title": "Child"})
    ).json()

    await client.patch(f"{TODOS}/{child['id']}", json={"completed": True})

    refreshed = (await client.get(f"{TODOS}/{parent['id']}")).json()
    assert refreshed["completed"] is False
    assert [sub["completed"] for sub in refreshed["subtasks"]] == [True]


async def test_subtask_route_rejects_list_id_and_parent_id_in_the_body(
    client: AsyncClient,
) -> None:
    """The parent is the path; there is nothing for the body to disagree with."""
    parent = await _parent(client)
    other = (await client.post(LISTS, json={"name": "Work"})).json()

    for payload in (
        {"title": "Child", "list_id": other["id"]},
        {"title": "Child", "parent_id": parent["id"]},
    ):
        response = await client.post(f"{TODOS}/{parent['id']}/subtasks", json=payload)
        assert response.status_code == 422, payload
