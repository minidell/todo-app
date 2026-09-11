"""The slice-3 write surface: full create + partial PATCH (spec B7.1/B7.2).

Master R11 is the scheduled contract change under test here: ``PATCH`` becomes
partial, and ``{"completed": …}`` — the only body slices 1–2 accepted — has to
keep working byte for byte, because the frontend's ``setCompleted()`` call site
was never meant to move.
"""

from datetime import date, timedelta

from httpx import AsyncClient

TODOS = "/api/todos"
LISTS = "/api/lists"

EMPTY_UPDATE_BODY = {"detail": "No fields to update", "code": "empty_update"}


# --- create ----------------------------------------------------------------
async def test_create_accepts_every_field(client: AsyncClient) -> None:
    due = (date.today() + timedelta(days=3)).isoformat()
    work = (await client.post(LISTS, json={"name": "Work"})).json()

    response = await client.post(
        TODOS,
        json={
            "title": "  Buy milk  ",
            "list_id": work["id"],
            "description": "  2% please  ",
            "priority": "high",
            "due_date": due,
            "tags": ["errand", "home"],
        },
    )
    assert response.status_code == 201
    body = response.json()

    assert body["title"] == "Buy milk"
    assert body["list_id"] == work["id"]
    assert body["description"] == "2% please"
    assert body["priority"] == "high"
    assert body["due_date"] == due
    # Tag names come back sorted (§3.3).
    assert body["tags"] == ["errand", "home"]
    assert body["parent_id"] is None
    assert body["subtasks"] == []


async def test_create_with_only_a_title_uses_the_documented_defaults(
    client: AsyncClient,
) -> None:
    body = (await client.post(TODOS, json={"title": "Task"})).json()
    assert body["description"] is None
    assert body["priority"] == "medium"
    assert body["due_date"] is None
    assert body["tags"] == []
    assert body["completed"] is False


async def test_blank_description_is_stored_as_null(client: AsyncClient) -> None:
    body = (
        await client.post(TODOS, json={"title": "Task", "description": "   "})
    ).json()
    assert body["description"] is None


async def test_create_rejects_an_over_long_description(client: AsyncClient) -> None:
    ok = await client.post(
        TODOS, json={"title": "Task", "description": "a" * 2000}
    )
    assert ok.status_code == 201

    too_long = await client.post(
        TODOS, json={"title": "Task", "description": "a" * 2001}
    )
    assert too_long.status_code == 422


async def test_create_rejects_more_than_ten_distinct_tags(client: AsyncClient) -> None:
    ten = [f"tag{index}" for index in range(10)]
    assert (await client.post(TODOS, json={"title": "Task", "tags": ten})).status_code == 201

    eleven = [f"other{index}" for index in range(11)]
    response = await client.post(TODOS, json={"title": "Task", "tags": eleven})
    assert response.status_code == 422


async def test_the_tag_cap_counts_tags_after_de_duplication(
    client: AsyncClient,
) -> None:
    """Spec B2 counts what the todo ends up carrying.

    Counting the raw list first would reject a body for tags it does not
    actually have — twelve spellings of two words is two tags.
    """
    spellings = ["Home", "home", "  HOME  ", "Work", "work", "WORK"] * 2
    response = await client.post(TODOS, json={"title": "Task", "tags": spellings})
    assert response.status_code == 201
    assert response.json()["tags"] == ["home", "work"]

    # Eleven distinct names, padded with repeats, is still eleven.
    padded = [f"tag{index}" for index in range(11)] * 2
    assert (
        await client.post(TODOS, json={"title": "Task", "tags": padded})
    ).status_code == 422


async def test_the_tag_cap_applies_to_patch_and_subtasks_too(
    client: AsyncClient,
) -> None:
    created = (await client.post(TODOS, json={"title": "Task"})).json()
    eleven = [f"tag{index}" for index in range(11)]

    assert (
        await client.patch(f"{TODOS}/{created['id']}", json={"tags": eleven})
    ).status_code == 422
    assert (
        await client.post(
            f"{TODOS}/{created['id']}/subtasks", json={"title": "Child", "tags": eleven}
        )
    ).status_code == 422

    ten_spellings = [f"Tag{index}" for index in range(10)] * 3
    patched = await client.patch(
        f"{TODOS}/{created['id']}", json={"tags": ten_spellings}
    )
    assert patched.status_code == 200
    assert len(patched.json()["tags"]) == 10


async def test_create_rejects_invalid_tag_names(client: AsyncClient) -> None:
    for name in ("Bad!Tag", "", "   ", "-nope", "a" * 31, "emoji 🙂"):
        response = await client.post(TODOS, json={"title": "Task", "tags": [name]})
        assert response.status_code == 422, name


async def test_invalid_tag_error_names_the_offending_value(
    client: AsyncClient,
) -> None:
    response = await client.post(TODOS, json={"title": "Task", "tags": ["Bad!Tag"]})
    assert response.status_code == 422
    assert "Bad!Tag" in response.text


async def test_tags_are_normalized_and_deduplicated(client: AsyncClient) -> None:
    body = (
        await client.post(
            TODOS, json={"title": "Task", "tags": ["  Home  ", "home", "HOME"]}
        )
    ).json()
    assert body["tags"] == ["home"]


async def test_create_rejects_an_invalid_priority(client: AsyncClient) -> None:
    response = await client.post(TODOS, json={"title": "Task", "priority": "urgent"})
    assert response.status_code == 422


async def test_create_rejects_an_unknown_key(client: AsyncClient) -> None:
    response = await client.post(TODOS, json={"title": "Task", "colour": "red"})
    assert response.status_code == 422


# --- partial PATCH (master R11) -------------------------------------------
async def test_each_field_can_be_patched_on_its_own(client: AsyncClient) -> None:
    created = (await client.post(TODOS, json={"title": "Task"})).json()
    todo_id = created["id"]
    due = (date.today() + timedelta(days=5)).isoformat()

    cases = [
        ({"title": "Renamed"}, "title", "Renamed"),
        ({"description": "notes"}, "description", "notes"),
        ({"priority": "low"}, "priority", "low"),
        ({"due_date": due}, "due_date", due),
        ({"tags": ["home"]}, "tags", ["home"]),
        ({"completed": True}, "completed", True),
    ]
    for payload, field, expected in cases:
        response = await client.patch(f"{TODOS}/{todo_id}", json=payload)
        assert response.status_code == 200, payload
        assert response.json()[field] == expected

    # Every earlier change survived the later ones: a partial PATCH touches
    # only the keys it was given.
    final = (await client.get(f"{TODOS}/{todo_id}")).json()
    assert final["title"] == "Renamed"
    assert final["description"] == "notes"
    assert final["priority"] == "low"
    assert final["due_date"] == due
    assert final["tags"] == ["home"]
    assert final["completed"] is True


async def test_patch_completed_still_works_exactly_as_before(
    client: AsyncClient,
) -> None:
    """Master R11 regression: the slice-1/2 body must not have moved."""
    created = (await client.post(TODOS, json={"title": "Task"})).json()

    done = await client.patch(f"{TODOS}/{created['id']}", json={"completed": True})
    assert done.status_code == 200
    assert done.json()["completed"] is True
    assert done.json()["completed_at"] is not None

    reopened = await client.patch(f"{TODOS}/{created['id']}", json={"completed": False})
    assert reopened.json()["completed"] is False
    assert reopened.json()["completed_at"] is None


async def test_empty_patch_body_is_400_empty_update(client: AsyncClient) -> None:
    created = (await client.post(TODOS, json={"title": "Task"})).json()
    response = await client.patch(f"{TODOS}/{created['id']}", json={})
    assert response.status_code == 400
    assert response.json() == EMPTY_UPDATE_BODY


async def test_explicit_null_clears_the_clearable_fields(client: AsyncClient) -> None:
    created = (
        await client.post(
            TODOS,
            json={
                "title": "Task",
                "description": "notes",
                "due_date": date.today().isoformat(),
            },
        )
    ).json()
    todo_id = created["id"]

    cleared = await client.patch(
        f"{TODOS}/{todo_id}", json={"description": None, "due_date": None}
    )
    assert cleared.status_code == 200
    assert cleared.json()["description"] is None
    assert cleared.json()["due_date"] is None


async def test_empty_tag_list_removes_every_tag(client: AsyncClient) -> None:
    created = (
        await client.post(TODOS, json={"title": "Task", "tags": ["home", "work"]})
    ).json()

    response = await client.patch(f"{TODOS}/{created['id']}", json={"tags": []})
    assert response.status_code == 200
    assert response.json()["tags"] == []


async def test_patching_tags_replaces_rather_than_appends(client: AsyncClient) -> None:
    created = (
        await client.post(TODOS, json={"title": "Task", "tags": ["home", "work"]})
    ).json()

    response = await client.patch(
        f"{TODOS}/{created['id']}", json={"tags": ["work", "urgent"]}
    )
    assert response.json()["tags"] == ["urgent", "work"]


async def test_null_for_a_non_clearable_field_is_422(client: AsyncClient) -> None:
    created = (await client.post(TODOS, json={"title": "Task"})).json()

    for payload in (
        {"title": None},
        {"completed": None},
        {"priority": None},
        {"tags": None},
        {"list_id": None},
    ):
        response = await client.patch(f"{TODOS}/{created['id']}", json=payload)
        assert response.status_code == 422, payload


async def test_patch_rejects_an_unknown_key(client: AsyncClient) -> None:
    created = (await client.post(TODOS, json={"title": "Task"})).json()
    response = await client.patch(f"{TODOS}/{created['id']}", json={"colour": "red"})
    assert response.status_code == 422


async def test_patch_moves_a_todo_and_its_subtasks_to_another_list(
    client: AsyncClient,
) -> None:
    work = (await client.post(LISTS, json={"name": "Work"})).json()
    parent = (await client.post(TODOS, json={"title": "Parent"})).json()
    child = (
        await client.post(f"{TODOS}/{parent['id']}/subtasks", json={"title": "Child"})
    ).json()
    assert child["list_id"] == parent["list_id"]

    moved = await client.patch(f"{TODOS}/{parent['id']}", json={"list_id": work["id"]})
    assert moved.status_code == 200
    assert moved.json()["list_id"] == work["id"]
    # I3: the subtask went with it, in the same transaction.
    assert [sub["list_id"] for sub in moved.json()["subtasks"]] == [work["id"]]
    assert (await client.get(f"{TODOS}/{child['id']}")).json()["list_id"] == work["id"]


async def test_moving_a_subtask_moves_its_whole_family(client: AsyncClient) -> None:
    """A subtask may not live in a different list from its parent (I3), so the
    move is applied to the family rather than silently breaking the invariant."""
    work = (await client.post(LISTS, json={"name": "Work"})).json()
    parent = (await client.post(TODOS, json={"title": "Parent"})).json()
    child = (
        await client.post(f"{TODOS}/{parent['id']}/subtasks", json={"title": "Child"})
    ).json()

    moved = await client.patch(f"{TODOS}/{child['id']}", json={"list_id": work["id"]})
    assert moved.status_code == 200
    assert moved.json()["list_id"] == work["id"]
    assert (await client.get(f"{TODOS}/{parent['id']}")).json()["list_id"] == work["id"]


async def test_patch_updates_updated_at_but_not_created_at(
    client: AsyncClient,
) -> None:
    created = (await client.post(TODOS, json={"title": "Task"})).json()
    patched = (
        await client.patch(f"{TODOS}/{created['id']}", json={"title": "Renamed"})
    ).json()

    assert patched["created_at"] == created["created_at"]
    assert patched["updated_at"] >= created["updated_at"]
