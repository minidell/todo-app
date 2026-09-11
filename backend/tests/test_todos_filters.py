"""Filtering and search on ``GET /api/todos`` (master §6.3, spec B7.4).

One seeded world (``world``) is shared by every case so the assertions read as
"which of these twelve todos comes back", which is how a filter bug actually
shows up. Dates are relative to a **fixed** ``today`` passed on every request:
a suite that filtered against the server's clock would fail once a day at
midnight and nowhere else.
"""

from datetime import date, timedelta

import pytest
from httpx import AsyncClient

TODOS = "/api/todos"
LISTS = "/api/lists"

TODAY = date(2026, 9, 2)
YESTERDAY = TODAY - timedelta(days=1)
IN_THREE_DAYS = TODAY + timedelta(days=3)
IN_SIX_DAYS = TODAY + timedelta(days=6)
IN_SEVEN_DAYS = TODAY + timedelta(days=7)
IN_TEN_DAYS = TODAY + timedelta(days=10)

CONFLICT_BODY = {
    "detail": "Use either a due preset or a due date range, not both",
    "code": "conflicting_due_filters",
}


@pytest.fixture
async def world(client: AsyncClient) -> dict:
    """Twelve todos across two lists, three priorities, tags, dates, states."""
    work = (await client.post(LISTS, json={"name": "Work"})).json()

    rows = [
        # title, priority, due_date, tags, completed, list
        ("Buy milk", "high", YESTERDAY, ["home", "errand"], False, None),
        ("Pay rent", "high", YESTERDAY, ["home"], True, None),
        ("Call dentist", "medium", TODAY, ["health"], False, None),
        ("Standup notes", "low", TODAY, ["work"], True, work["id"]),
        ("Write report", "high", IN_THREE_DAYS, ["work", "home"], False, work["id"]),
        ("Book flights", "medium", IN_SIX_DAYS, ["travel"], False, None),
        ("Renew passport", "low", IN_SEVEN_DAYS, ["travel"], False, None),
        ("Plan holiday", "medium", IN_TEN_DAYS, [], False, None),
        ("Water plants", "low", None, ["home"], False, None),
        ("Read 100% of the manual", "medium", None, [], False, work["id"]),
        ("Archive old_files", "low", None, ["work"], True, work["id"]),
        ("Tidy desk", "medium", None, ["work", "home"], False, work["id"]),
    ]

    created: dict[str, dict] = {}
    for title, priority, due, tags, completed, list_id in rows:
        payload: dict = {"title": title, "priority": priority, "tags": tags}
        if due is not None:
            payload["due_date"] = due.isoformat()
        if list_id is not None:
            payload["list_id"] = list_id
        todo = (await client.post(TODOS, json=payload)).json()
        if completed:
            todo = (
                await client.patch(f"{TODOS}/{todo['id']}", json={"completed": True})
            ).json()
        created[title] = todo

    # One subtask, to prove filters apply to parents only.
    await client.post(
        f"{TODOS}/{created['Write report']['id']}/subtasks",
        json={"title": "Collect numbers", "priority": "low", "tags": ["work"]},
    )
    return {"todos": created, "work_list": work}


async def titles(client: AsyncClient, **params) -> list[str]:
    params.setdefault("today", TODAY.isoformat())
    response = await client.get(TODOS, params=params)
    assert response.status_code == 200, response.text
    return [row["title"] for row in response.json()]


async def test_no_filters_returns_every_top_level_todo(
    client: AsyncClient, world: dict
) -> None:
    result = await titles(client)
    assert len(result) == 12
    assert "Collect numbers" not in result


# --- list_id ---------------------------------------------------------------
async def test_filter_by_list(client: AsyncClient, world: dict) -> None:
    result = await titles(client, list_id=world["work_list"]["id"])
    assert sorted(result) == sorted(
        ["Standup notes", "Write report", "Read 100% of the manual",
         "Archive old_files", "Tidy desk"]
    )


# --- status ----------------------------------------------------------------
async def test_filter_by_status(client: AsyncClient, world: dict) -> None:
    active = await titles(client, status="active")
    completed = await titles(client, status="completed")

    assert "Buy milk" in active and "Pay rent" not in active
    assert sorted(completed) == ["Archive old_files", "Pay rent", "Standup notes"]
    assert len(active) + len(completed) == 12


async def test_unknown_status_is_422(client: AsyncClient, world: dict) -> None:
    response = await client.get(TODOS, params={"status": "half-done"})
    assert response.status_code == 422


# --- priority --------------------------------------------------------------
async def test_filter_by_priority_repeated_and_comma_separated(
    client: AsyncClient, world: dict
) -> None:
    high = await titles(client, priority="high")
    assert sorted(high) == ["Buy milk", "Pay rent", "Write report"]

    comma = await titles(client, priority="high,low")
    repeated = await titles(client, priority=["high", "low"])
    assert sorted(comma) == sorted(repeated)
    assert "Water plants" in comma  # low
    assert "Call dentist" not in comma  # medium


async def test_priority_combines_with_status(client: AsyncClient, world: dict) -> None:
    result = await titles(client, status="active", priority="high")
    # "Pay rent" is high but completed.
    assert sorted(result) == ["Buy milk", "Write report"]


async def test_unknown_priority_is_422(client: AsyncClient, world: dict) -> None:
    response = await client.get(TODOS, params={"priority": "urgent"})
    assert response.status_code == 422
    body = response.json()
    assert isinstance(body["detail"], list)
    assert body["detail"][0]["loc"] == ["query", "priority"]


# --- tags ------------------------------------------------------------------
async def test_single_tag_filter(client: AsyncClient, world: dict) -> None:
    result = await titles(client, tag="travel")
    assert sorted(result) == ["Book flights", "Renew passport"]


async def test_multiple_tags_are_ANDed_not_ORed(
    client: AsyncClient, world: dict
) -> None:
    """§6.3: the todo must carry *all* of them."""
    result = await titles(client, tag=["home", "work"])
    assert sorted(result) == ["Tidy desk", "Write report"]


async def test_tag_filter_is_normalized(client: AsyncClient, world: dict) -> None:
    assert await titles(client, tag="  HOME ") == await titles(client, tag="home")


async def test_unknown_tag_yields_an_empty_result_not_404(
    client: AsyncClient, world: dict
) -> None:
    response = await client.get(
        TODOS, params={"tag": "nonexistent", "today": TODAY.isoformat()}
    )
    assert response.status_code == 200
    assert response.json() == []
    assert response.headers["X-Total-Count"] == "0"


async def test_repeated_tag_values_collapse_instead_of_multiplying_the_query(
    client: AsyncClient, world: dict
) -> None:
    """Each distinct tag adds an EXISTS subquery, so the distinct count is the
    size of the generated SQL. Unbounded, that was a denial of service: 500
    repeats of one name took seconds and 1000 exceeded SQLite's expression-tree
    limit with a 500."""
    once = await titles(client, tag="home")

    for repeats in (500, 1000):
        response = await client.get(
            TODOS,
            params={"tag": ["home"] * repeats, "today": TODAY.isoformat()},
        )
        assert response.status_code == 200, repeats
        assert [row["title"] for row in response.json()] == once


async def test_tag_de_duplication_happens_after_normalization(
    client: AsyncClient, world: dict
) -> None:
    """One filter, spelled four ways — a client repeating a name is answered,
    not rejected."""
    spellings = ["home", "Home", "  HOME  ", "home"]
    assert await titles(client, tag=spellings) == await titles(client, tag="home")


async def test_at_most_ten_distinct_tags_may_be_combined(
    client: AsyncClient, world: dict
) -> None:
    ten = [f"tag{index}" for index in range(10)]
    assert (
        await client.get(TODOS, params={"tag": ten, "today": TODAY.isoformat()})
    ).status_code == 200

    eleven = [f"tag{index}" for index in range(11)]
    response = await client.get(
        TODOS, params={"tag": eleven, "today": TODAY.isoformat()}
    )
    assert response.status_code == 422
    body = response.json()
    assert body["detail"][0]["loc"] == ["query", "tag"]
    assert "code" not in body  # D-E1: a 422 keeps FastAPI's shape

    # The cap counts *distinct* names, so padding with repeats stays under it.
    padded = ten + ten
    assert (
        await client.get(TODOS, params={"tag": padded, "today": TODAY.isoformat()})
    ).status_code == 200


async def test_an_over_long_tag_filter_value_is_422(
    client: AsyncClient, world: dict
) -> None:
    """30 characters is the stored column width; longer is a malformed filter,
    not a search that happens to match nothing."""
    assert (
        await client.get(
            TODOS, params={"tag": "a" * 30, "today": TODAY.isoformat()}
        )
    ).status_code == 200

    response = await client.get(
        TODOS, params={"tag": "a" * 31, "today": TODAY.isoformat()}
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][:2] == ["query", "tag"]


async def test_a_blank_tag_value_is_ignored_rather_than_matched(
    client: AsyncClient, world: dict
) -> None:
    assert await titles(client, tag=["", "   "]) == await titles(client)


async def test_a_tag_only_on_a_subtask_does_not_surface_the_parent(
    client: AsyncClient, world: dict
) -> None:
    """Filters apply to parents; a parent's subtasks ride along unfiltered."""
    parent = (await client.post(TODOS, json={"title": "Parent"})).json()
    await client.post(
        f"{TODOS}/{parent['id']}/subtasks",
        json={"title": "Child", "tags": ["subtask-only"]},
    )
    assert await titles(client, tag="subtask-only") == []


async def test_another_users_tag_name_does_not_match(
    anon_client: AsyncClient, client: AsyncClient, world: dict, other_user_auth_headers
) -> None:
    await anon_client.post(
        TODOS, json={"title": "B's", "tags": ["home"]}, headers=other_user_auth_headers
    )
    b_view = await anon_client.get(
        TODOS, params={"tag": "home"}, headers=other_user_auth_headers
    )
    assert [row["title"] for row in b_view.json()] == ["B's"]


# --- due presets -----------------------------------------------------------
async def test_due_overdue_excludes_completed_todos(
    client: AsyncClient, world: dict
) -> None:
    """A finished todo is not overdue: the deadline stopped mattering."""
    result = await titles(client, due="overdue")
    assert result == ["Buy milk"]  # "Pay rent" is also past due but done


async def test_due_today(client: AsyncClient, world: dict) -> None:
    assert sorted(await titles(client, due="today")) == [
        "Call dentist",
        "Standup notes",
    ]


async def test_due_week_boundaries_are_inclusive_today_and_plus_six(
    client: AsyncClient, world: dict
) -> None:
    result = await titles(client, due="week")
    assert "Call dentist" in result  # today
    assert "Book flights" in result  # +6
    assert "Renew passport" not in result  # +7
    assert "Buy milk" not in result  # yesterday


async def test_due_none(client: AsyncClient, world: dict) -> None:
    assert sorted(await titles(client, due="none")) == [
        "Archive old_files",
        "Read 100% of the manual",
        "Tidy desk",
        "Water plants",
    ]


async def test_due_presets_follow_the_caller_supplied_today(
    client: AsyncClient, world: dict
) -> None:
    """The caller sends its *local* date, so "today" matches the user's
    calendar rather than the server's timezone."""
    shifted = await titles(client, due="today", today=IN_THREE_DAYS.isoformat())
    assert shifted == ["Write report"]


async def test_unknown_due_preset_is_422(client: AsyncClient, world: dict) -> None:
    assert (await client.get(TODOS, params={"due": "someday"})).status_code == 422


# --- explicit ranges -------------------------------------------------------
async def test_due_from_and_due_to_are_inclusive(
    client: AsyncClient, world: dict
) -> None:
    result = await titles(
        client, due_from=TODAY.isoformat(), due_to=IN_SIX_DAYS.isoformat()
    )
    assert sorted(result) == sorted(
        ["Book flights", "Call dentist", "Standup notes", "Write report"]
    )

    only_edges = await titles(
        client, due_from=IN_SIX_DAYS.isoformat(), due_to=IN_SIX_DAYS.isoformat()
    )
    assert only_edges == ["Book flights"]


async def test_a_range_alone_excludes_undated_todos(
    client: AsyncClient, world: dict
) -> None:
    result = await titles(client, due_from=YESTERDAY.isoformat())
    assert "Water plants" not in result


async def test_combining_a_preset_with_a_range_is_400(
    client: AsyncClient, world: dict
) -> None:
    for params in (
        {"due": "today", "due_from": TODAY.isoformat()},
        {"due": "week", "due_to": TODAY.isoformat()},
        {"due": "none", "due_from": TODAY.isoformat(), "due_to": TODAY.isoformat()},
    ):
        response = await client.get(TODOS, params=params)
        assert response.status_code == 400, params
        assert response.json() == CONFLICT_BODY

    # ``due=any`` is the default, so a range on its own is fine.
    fine = await client.get(
        TODOS, params={"due": "any", "due_from": TODAY.isoformat()}
    )
    assert fine.status_code == 200


async def test_malformed_dates_are_422(client: AsyncClient, world: dict) -> None:
    for params in (
        {"due_from": "not-a-date"},
        {"due_to": "2026-13-01"},
        {"today": "yesterday"},
    ):
        assert (await client.get(TODOS, params=params)).status_code == 422, params


# --- search ----------------------------------------------------------------
async def test_search_is_case_insensitive_over_the_title(
    client: AsyncClient, world: dict
) -> None:
    assert await titles(client, q="MILK") == ["Buy milk"]
    assert await titles(client, q="milk") == ["Buy milk"]


async def test_search_matches_the_description_too(
    client: AsyncClient, world: dict
) -> None:
    await client.post(
        TODOS,
        json={"title": "Opaque title", "description": "mentions asparagus somewhere"},
    )
    assert await titles(client, q="asparagus") == ["Opaque title"]


async def test_search_treats_percent_and_underscore_literally(
    client: AsyncClient, world: dict
) -> None:
    """An unescaped ``%`` would match every row — a silently wrong result."""
    assert await titles(client, q="100%") == ["Read 100% of the manual"]
    assert await titles(client, q="old_files") == ["Archive old_files"]
    # ``_`` is the single-character wildcard; unescaped, "old_files" would also
    # match "oldXfiles". Prove it does not.
    await client.post(TODOS, json={"title": "Archive oldXfiles"})
    assert await titles(client, q="old_files") == ["Archive old_files"]


async def test_search_finds_nothing_without_erroring(
    client: AsyncClient, world: dict
) -> None:
    assert await titles(client, q="zzz-not-here") == []


async def test_search_combines_with_other_filters(
    client: AsyncClient, world: dict
) -> None:
    assert await titles(client, q="e", status="completed", priority="low") == [
        "Standup notes",
        "Archive old_files",
    ]
    assert await titles(client, q="archive", status="completed", priority="low") == [
        "Archive old_files"
    ]
    # The same search with a priority that excludes it returns nothing.
    assert await titles(client, q="archive", priority="high") == []


async def test_empty_and_over_long_search_terms_are_422(
    client: AsyncClient, world: dict
) -> None:
    assert (await client.get(TODOS, params={"q": ""})).status_code == 422
    assert (await client.get(TODOS, params={"q": "x" * 101})).status_code == 422


# --- pagination ------------------------------------------------------------
async def test_limit_and_offset_page_through_the_matches(
    client: AsyncClient, world: dict
) -> None:
    everything = await titles(client, sort="title", order="asc")
    first_page = await titles(client, sort="title", order="asc", limit=5)
    second_page = await titles(client, sort="title", order="asc", limit=5, offset=5)

    assert first_page == everything[:5]
    assert second_page == everything[5:10]


async def test_x_total_count_reports_the_unpaginated_match_count(
    client: AsyncClient, world: dict
) -> None:
    response = await client.get(
        TODOS, params={"today": TODAY.isoformat(), "limit": 3, "status": "active"}
    )
    assert len(response.json()) == 3
    assert response.headers["X-Total-Count"] == "9"


async def test_out_of_range_limits_are_422(client: AsyncClient, world: dict) -> None:
    for params in ({"limit": 0}, {"limit": 501}, {"offset": -1}):
        assert (await client.get(TODOS, params=params)).status_code == 422, params
    assert (await client.get(TODOS, params={"limit": 500})).status_code == 200


async def test_unknown_query_parameters_are_ignored(
    client: AsyncClient, world: dict
) -> None:
    """FastAPI's default, and the contract (§6.3): slice-1 clients keep working."""
    response = await client.get(TODOS, params={"nonsense": "1"})
    assert response.status_code == 200
    assert len(response.json()) == 12
