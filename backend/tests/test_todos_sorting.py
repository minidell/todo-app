"""Sorting on ``GET /api/todos`` (master §6.3, spec B7.5).

Three rules here are easy to get subtly wrong and impossible to notice from a
happy-path test, so each has its own case:

* undated todos sort **last in both directions** — not "first when reversed";
* ``priority=asc`` means high → medium → low, which is neither the alphabetical
  nor the storage order of the VARCHAR column;
* every sort ends with ``created_at, id``, without which a page boundary can
  drop or duplicate a row between two identical requests.
"""

from datetime import date, timedelta

import pytest
from httpx import AsyncClient

TODOS = "/api/todos"

TODAY = date(2026, 9, 2)


@pytest.fixture
async def sortable(client: AsyncClient) -> list[dict]:
    """Six todos whose fields disagree, so every sort reorders them."""
    rows = [
        # title, priority, due offset (None = no due date)
        ("banana", "low", 2),
        ("Apple", "high", None),
        ("cherry", "medium", 1),
        ("date", "high", None),
        ("Elderberry", "low", 5),
        ("fig", "medium", None),
    ]
    created = []
    for title, priority, offset in rows:
        payload: dict = {"title": title, "priority": priority}
        if offset is not None:
            payload["due_date"] = (TODAY + timedelta(days=offset)).isoformat()
        created.append((await client.post(TODOS, json=payload)).json())
    return created


async def titles(client: AsyncClient, **params) -> list[str]:
    response = await client.get(TODOS, params={"today": TODAY.isoformat(), **params})
    assert response.status_code == 200, response.text
    return [row["title"] for row in response.json()]


# --- created_at / updated_at ----------------------------------------------
async def test_default_sort_is_created_at_ascending(
    client: AsyncClient, sortable: list[dict]
) -> None:
    assert await titles(client) == [row["title"] for row in sortable]
    assert await titles(client, sort="created_at", order="asc") == await titles(client)


async def test_created_at_descending_reverses(
    client: AsyncClient, sortable: list[dict]
) -> None:
    assert await titles(client, sort="created_at", order="desc") == [
        row["title"] for row in reversed(sortable)
    ]


async def test_updated_at_sorts_by_the_latest_edit(
    client: AsyncClient, sortable: list[dict]
) -> None:
    touched = sortable[1]
    await client.patch(f"{TODOS}/{touched['id']}", json={"priority": "low"})

    assert (await titles(client, sort="updated_at", order="desc"))[0] == touched["title"]


# --- due_date --------------------------------------------------------------
async def test_due_date_ascending_puts_undated_todos_last(
    client: AsyncClient, sortable: list[dict]
) -> None:
    result = await titles(client, sort="due_date", order="asc")
    assert result[:3] == ["cherry", "banana", "Elderberry"]
    assert set(result[3:]) == {"Apple", "date", "fig"}


async def test_due_date_descending_also_puts_undated_todos_last(
    client: AsyncClient, sortable: list[dict]
) -> None:
    """Not merely the reverse of ascending: nulls stay at the bottom (§6.3)."""
    result = await titles(client, sort="due_date", order="desc")
    assert result[:3] == ["Elderberry", "banana", "cherry"]
    assert set(result[3:]) == {"Apple", "date", "fig"}


async def test_undated_todos_keep_the_stable_tiebreak(
    client: AsyncClient, sortable: list[dict]
) -> None:
    for order in ("asc", "desc"):
        result = await titles(client, sort="due_date", order=order)
        # Among the undated three, creation order decides — always the same.
        assert result[3:] == ["Apple", "date", "fig"]


# --- priority --------------------------------------------------------------
async def test_priority_ascending_is_high_to_low(
    client: AsyncClient, sortable: list[dict]
) -> None:
    response = await client.get(
        TODOS, params={"sort": "priority", "order": "asc", "today": TODAY.isoformat()}
    )
    assert [row["priority"] for row in response.json()] == [
        "high",
        "high",
        "medium",
        "medium",
        "low",
        "low",
    ]
    # Alphabetically 'high' < 'low' < 'medium', so a naive VARCHAR sort would
    # have produced high, high, low, low, medium, medium.
    assert [row["title"] for row in response.json()] == [
        "Apple",
        "date",
        "cherry",
        "fig",
        "banana",
        "Elderberry",
    ]


async def test_priority_descending_is_low_to_high(
    client: AsyncClient, sortable: list[dict]
) -> None:
    response = await client.get(
        TODOS, params={"sort": "priority", "order": "desc", "today": TODAY.isoformat()}
    )
    assert [row["priority"] for row in response.json()] == [
        "low",
        "low",
        "medium",
        "medium",
        "high",
        "high",
    ]


# --- title -----------------------------------------------------------------
async def test_title_sorts_case_insensitively(
    client: AsyncClient, sortable: list[dict]
) -> None:
    assert await titles(client, sort="title", order="asc") == [
        "Apple",
        "banana",
        "cherry",
        "date",
        "Elderberry",
        "fig",
    ]
    assert await titles(client, sort="title", order="desc") == [
        "fig",
        "Elderberry",
        "date",
        "cherry",
        "banana",
        "Apple",
    ]


# --- stability -------------------------------------------------------------
async def test_ordering_is_stable_across_identical_requests(
    client: AsyncClient, sortable: list[dict]
) -> None:
    for sort in ("created_at", "updated_at", "due_date", "priority", "title"):
        for order in ("asc", "desc"):
            runs = [await titles(client, sort=sort, order=order) for _ in range(3)]
            assert runs[0] == runs[1] == runs[2], (sort, order)


async def test_ties_break_by_created_at_then_id(client: AsyncClient) -> None:
    """Six todos identical in every sort key but creation order."""
    created = [
        (await client.post(TODOS, json={"title": "same", "priority": "medium"})).json()
        for _ in range(6)
    ]
    expected = [row["id"] for row in created]

    for sort in ("priority", "title", "due_date"):
        response = await client.get(TODOS, params={"sort": sort, "order": "desc"})
        assert [row["id"] for row in response.json()] == expected, sort


async def test_pagination_does_not_drop_or_repeat_a_row(client: AsyncClient) -> None:
    """The tiebreak is what makes paging safe when the sort key repeats."""
    for _ in range(10):
        await client.post(TODOS, json={"title": "same", "priority": "medium"})

    seen: list[str] = []
    for offset in (0, 4, 8):
        response = await client.get(
            TODOS, params={"sort": "priority", "limit": 4, "offset": offset}
        )
        seen.extend(row["id"] for row in response.json())

    assert len(seen) == 10
    assert len(set(seen)) == 10


async def test_unknown_sort_or_order_is_422(client: AsyncClient) -> None:
    assert (await client.get(TODOS, params={"sort": "colour"})).status_code == 422
    assert (await client.get(TODOS, params={"order": "sideways"})).status_code == 422


async def test_subtasks_are_always_ordered_by_creation(client: AsyncClient) -> None:
    """The parent's sort never reorders the children (§6.3)."""
    parent = (await client.post(TODOS, json={"title": "Parent"})).json()
    ids = [
        (
            await client.post(
                f"{TODOS}/{parent['id']}/subtasks",
                json={"title": title, "priority": priority},
            )
        ).json()["id"]
        for title, priority in (("z", "low"), ("a", "high"), ("m", "medium"))
    ]

    for sort in ("title", "priority", "due_date"):
        response = await client.get(TODOS, params={"sort": sort, "order": "desc"})
        assert [sub["id"] for sub in response.json()[0]["subtasks"]] == ids, sort
