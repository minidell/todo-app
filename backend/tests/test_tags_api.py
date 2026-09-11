"""``/api/tags`` — the tag vocabulary (master §6.4, slice-3 spec B7.7).

Tags have no creation endpoint (D-A3), so every test here starts by *using* a
tag on a todo. The properties that matter: names are normalized and per user,
counts are real, and deleting a tag never deletes the work it was attached to.
"""

from uuid import uuid4

from httpx import AsyncClient

TAGS = "/api/tags"
TODOS = "/api/todos"

TAG_NOT_FOUND_BODY = {"detail": "Tag not found", "code": "tag_not_found"}
TAG_TAKEN_BODY = {
    "detail": "A tag with that name already exists",
    "code": "tag_name_taken",
}


async def _todo(client: AsyncClient, title: str, tags: list[str]) -> dict:
    response = await client.post(TODOS, json={"title": title, "tags": tags})
    assert response.status_code == 201, response.text
    return response.json()


async def test_tags_are_created_implicitly_and_listed_with_counts(
    client: AsyncClient,
) -> None:
    await _todo(client, "Milk", ["home", "errand"])
    await _todo(client, "Report", ["work"])
    await _todo(client, "Bread", ["errand"])

    response = await client.get(TAGS)
    assert response.status_code == 200
    body = response.json()

    # Ordered by name ASC (§6.4).
    assert [tag["name"] for tag in body] == ["errand", "home", "work"]
    assert {tag["name"]: tag["todo_count"] for tag in body} == {
        "errand": 2,
        "home": 1,
        "work": 1,
    }
    assert set(body[0]) == {"id", "name", "todo_count"}


async def test_todo_count_counts_top_level_todos_only(client: AsyncClient) -> None:
    """The count must agree with what filtering by the tag returns.

    ``?tag=`` matches parents only, so counting subtasks here would print a
    number the user cannot reproduce — the chip claims two, the filter lists
    one.
    """
    parent = await _todo(client, "Parent", ["home"])
    await client.post(
        f"{TODOS}/{parent['id']}/subtasks",
        json={"title": "Step", "tags": ["home", "detail"]},
    )

    listed = {tag["name"]: tag["todo_count"] for tag in (await client.get(TAGS)).json()}
    assert listed == {"home": 1, "detail": 0}

    # The same numbers the filter produces.
    for name, expected in listed.items():
        filtered = await client.get(TODOS, params={"tag": name})
        assert len(filtered.json()) == expected, name
        assert filtered.headers["X-Total-Count"] == str(expected), name


async def test_a_tag_used_only_by_subtasks_still_appears(client: AsyncClient) -> None:
    """Count 0 is not "gone": the tag is still in the user's vocabulary and
    still attached to real work."""
    parent = await _todo(client, "Parent", [])
    await client.post(
        f"{TODOS}/{parent['id']}/subtasks",
        json={"title": "Step", "tags": ["detail"]},
    )

    listed = (await client.get(TAGS)).json()
    assert [(tag["name"], tag["todo_count"]) for tag in listed] == [("detail", 0)]


async def test_tag_names_are_normalized_before_they_are_stored(
    client: AsyncClient,
) -> None:
    created = await _todo(client, "Task", ["  Home  ", "HOME", "home"])
    # Three spellings, one tag — collapsed silently (spec B2).
    assert created["tags"] == ["home"]

    listed = (await client.get(TAGS)).json()
    assert [tag["name"] for tag in listed] == ["home"]
    assert listed[0]["todo_count"] == 1


async def test_a_tag_orphaned_by_its_last_todo_stays_in_the_vocabulary(
    client: AsyncClient,
) -> None:
    """Decision (spec B3): the tag survives with a count of 0.

    Deleting it as a side effect of an unrelated edit would make it vanish from
    the filter bar while the user was still using it elsewhere.
    """
    todo = await _todo(client, "Task", ["home"])
    await client.patch(f"{TODOS}/{todo['id']}", json={"tags": []})

    listed = (await client.get(TAGS)).json()
    assert [(tag["name"], tag["todo_count"]) for tag in listed] == [("home", 0)]


async def test_rename_updates_every_todo_carrying_the_tag(client: AsyncClient) -> None:
    first = await _todo(client, "Milk", ["errand"])
    second = await _todo(client, "Bread", ["errand"])
    tag = (await client.get(TAGS)).json()[0]

    response = await client.patch(f"{TAGS}/{tag['id']}", json={"name": "  Errands  "})
    assert response.status_code == 200
    assert response.json() == {"id": tag["id"], "name": "errands", "todo_count": 2}

    for todo in (first, second):
        fetched = (await client.get(f"{TODOS}/{todo['id']}")).json()
        assert fetched["tags"] == ["errands"]


async def test_rename_onto_an_existing_name_is_409(client: AsyncClient) -> None:
    await _todo(client, "Task", ["home", "work"])
    home = next(t for t in (await client.get(TAGS)).json() if t["name"] == "home")

    response = await client.patch(f"{TAGS}/{home['id']}", json={"name": "WORK"})
    assert response.status_code == 409
    assert response.json() == TAG_TAKEN_BODY

    # Nothing changed.
    assert [t["name"] for t in (await client.get(TAGS)).json()] == ["home", "work"]


async def test_a_rejected_rename_publishes_nothing(
    app, client: AsyncClient, user
) -> None:
    """§7.2: publish **after** the commit, so a 409 announces nothing at all.

    A frame for a rename that was rolled back would tell every other tab to
    render a name the database never took.
    """
    await _todo(client, "Task", ["home", "work"])
    home = next(t for t in (await client.get(TAGS)).json() if t["name"] == "home")

    async with app.state.broker.subscribe(user.id) as queue:
        conflict = await client.patch(f"{TAGS}/{home['id']}", json={"name": "WORK"})
        assert conflict.status_code == 409
        assert queue.empty()

        # The same subscriber does see a rename that succeeds, so the assertion
        # above is about the rollback and not about a broken subscription.
        assert (
            await client.patch(f"{TAGS}/{home['id']}", json={"name": "house"})
        ).status_code == 200
        assert queue.get_nowait().name == "tag.updated"


async def test_a_failed_tag_delete_publishes_nothing(
    app, client: AsyncClient, user
) -> None:
    async with app.state.broker.subscribe(user.id) as queue:
        assert (await client.delete(f"{TAGS}/{uuid4()}")).status_code == 404
        assert queue.empty()


async def test_renaming_a_tag_to_its_own_name_is_allowed(client: AsyncClient) -> None:
    await _todo(client, "Task", ["home"])
    tag = (await client.get(TAGS)).json()[0]

    response = await client.patch(f"{TAGS}/{tag['id']}", json={"name": "Home"})
    assert response.status_code == 200
    assert response.json()["name"] == "home"


async def test_rename_rejects_an_invalid_name_with_422(client: AsyncClient) -> None:
    await _todo(client, "Task", ["home"])
    tag = (await client.get(TAGS)).json()[0]

    for name in ("Bad!Tag", "", "   ", "-leading-hyphen", "x" * 31):
        response = await client.patch(f"{TAGS}/{tag['id']}", json={"name": name})
        assert response.status_code == 422, name


async def test_rename_unknown_or_malformed_id_is_404(client: AsyncClient) -> None:
    for tag_id in (str(uuid4()), "not-a-uuid"):
        response = await client.patch(f"{TAGS}/{tag_id}", json={"name": "x"})
        assert response.status_code == 404
        assert response.json() == TAG_NOT_FOUND_BODY


async def test_delete_removes_associations_but_never_the_todos(
    client: AsyncClient,
) -> None:
    todo = await _todo(client, "Milk", ["home", "errand"])
    home = next(t for t in (await client.get(TAGS)).json() if t["name"] == "home")

    assert (await client.delete(f"{TAGS}/{home['id']}")).status_code == 204

    survivor = await client.get(f"{TODOS}/{todo['id']}")
    assert survivor.status_code == 200
    assert survivor.json()["tags"] == ["errand"]
    assert [t["name"] for t in (await client.get(TAGS)).json()] == ["errand"]


async def test_delete_twice_and_malformed_id_are_404(client: AsyncClient) -> None:
    await _todo(client, "Task", ["home"])
    tag = (await client.get(TAGS)).json()[0]

    assert (await client.delete(f"{TAGS}/{tag['id']}")).status_code == 204
    repeat = await client.delete(f"{TAGS}/{tag['id']}")
    assert repeat.status_code == 404
    assert repeat.json() == TAG_NOT_FOUND_BODY

    malformed = await client.delete(f"{TAGS}/not-a-uuid")
    assert malformed.status_code == 404


async def test_there_is_no_tag_creation_endpoint(client: AsyncClient) -> None:
    """D-A3: one creation path, so a tag can never be created with a name the
    todo write path would have normalized differently."""
    response = await client.post(TAGS, json={"name": "home"})
    assert response.status_code == 405


async def test_tags_are_per_user(
    anon_client: AsyncClient, client: AsyncClient, other_user_auth_headers: dict
) -> None:
    await _todo(client, "A's task", ["shared"])
    b_todo = await anon_client.post(
        TODOS, json={"title": "B's task", "tags": ["shared"]},
        headers=other_user_auth_headers,
    )
    assert b_todo.status_code == 201

    a_tags = (await client.get(TAGS)).json()
    b_tags = (await anon_client.get(TAGS, headers=other_user_auth_headers)).json()

    # Same name, two rows: uniqueness is per user.
    assert [t["name"] for t in a_tags] == ["shared"]
    assert [t["name"] for t in b_tags] == ["shared"]
    assert a_tags[0]["id"] != b_tags[0]["id"]
    assert a_tags[0]["todo_count"] == 1


async def test_another_users_tag_is_404(
    anon_client: AsyncClient, client: AsyncClient, other_user_auth_headers: dict
) -> None:
    await anon_client.post(
        TODOS, json={"title": "B's task", "tags": ["secret"]},
        headers=other_user_auth_headers,
    )
    b_tag = (await anon_client.get(TAGS, headers=other_user_auth_headers)).json()[0]

    rename = await client.patch(f"{TAGS}/{b_tag['id']}", json={"name": "hacked"})
    delete = await client.delete(f"{TAGS}/{b_tag['id']}")
    assert [rename.status_code, delete.status_code] == [404, 404]
    assert rename.json() == TAG_NOT_FOUND_BODY

    # B's tag is untouched.
    still_there = (await anon_client.get(TAGS, headers=other_user_auth_headers)).json()
    assert [t["name"] for t in still_there] == ["secret"]


async def test_tags_require_authentication(anon_client: AsyncClient) -> None:
    response = await anon_client.get(TAGS)
    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated", "code": "unauthorized"}
