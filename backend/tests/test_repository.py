"""Direct unit tests of the SQLAlchemy repositories.

The important property here is **owner isolation** (carry-over C1, decision
D-E2): user A's repository calls must be unable to observe or mutate user B's
rows, and must report them as simply absent.
"""

from datetime import date, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Priority, Todo, TodoList
from app.errors import (
    CannotDeleteLastList,
    ListNotFound,
    SubtaskDepthExceeded,
    TodoNotFound,
)
from app.repositories.lists import SqlAlchemyListRepository
from app.repositories.protocols import (
    UNSET,
    TodoCreateData,
    TodoQuery,
    TodoUpdateData,
)
from app.repositories.todos import SqlAlchemyTodoRepository
from app.repositories.tags import SqlAlchemyTagRepository
from app.repositories.users import SqlAlchemyUserRepository
from app.schemas.todos import TodoResponse


async def _make_user(session: AsyncSession, email: str):
    return await SqlAlchemyUserRepository(session).create(email, "!", None)


@pytest.fixture
async def repo(db_session: AsyncSession) -> SqlAlchemyTodoRepository:
    return SqlAlchemyTodoRepository(db_session)


@pytest.fixture
async def user_b(db_session: AsyncSession):
    return await _make_user(db_session, "b@example.com")


# --- basics ----------------------------------------------------------------
async def test_create_returns_persisted_todo_with_defaults(repo, user) -> None:
    todo = await repo.create(user.id, TodoCreateData(title="Task"))

    assert todo.id is not None
    assert todo.user_id == user.id
    assert todo.list_id is not None
    assert todo.parent_id is None
    assert todo.title == "Task"
    assert todo.completed is False
    assert todo.completed_at is None
    assert todo.priority is Priority.MEDIUM
    assert todo.due_date is None
    assert todo.tags == []
    assert todo.subtasks == []
    assert todo.created_at.tzinfo is not None


async def test_create_uses_the_users_default_list(repo, user, db_session) -> None:
    todo = await repo.create(user.id, TodoCreateData(title="Task"))
    default_list = await SqlAlchemyListRepository(db_session).get_default(user.id)
    assert todo.list_id == default_list.id
    assert default_list.name == "Inbox"
    assert default_list.is_default is True


async def test_get_default_creates_an_inbox_for_a_user_with_no_lists(
    db_session, user_b
) -> None:
    lists = SqlAlchemyListRepository(db_session)
    created = await lists.get_default(user_b.id)
    assert created.name == "Inbox"
    assert created.is_default is True
    # It is created once, not on every call.
    again = await lists.get_default(user_b.id)
    assert again.id == created.id
    assert await lists.count(user_b.id) == 1


async def test_get_returns_none_for_unknown_id(repo, user) -> None:
    assert await repo.get(user.id, uuid4()) is None


async def test_list_todos_orders_by_created_at_then_id(repo, user) -> None:
    created = [
        await repo.create(user.id, TodoCreateData(title=title))
        for title in ("first", "second", "third")
    ]

    rows, total = await repo.list_todos(user.id, TodoQuery())
    assert [row.id for row in rows] == [todo.id for todo in created]
    assert total == 3


async def test_list_todos_excludes_subtasks_from_the_top_level(
    repo, user, db_session
) -> None:
    parent = await repo.create(user.id, TodoCreateData(title="Parent"))
    child = await repo.create(
        user.id, TodoCreateData(title="Child", parent_id=parent.id)
    )
    await db_session.commit()

    rows, total = await repo.list_todos(user.id, TodoQuery())
    assert [row.id for row in rows] == [parent.id]
    assert total == 1
    assert [sub.id for sub in rows[0].subtasks] == [child.id]
    # A subtask inherits its parent's list and owner (I2/I3).
    assert child.list_id == parent.list_id
    assert child.user_id == parent.user_id


async def test_list_todos_honours_limit_and_offset(repo, user) -> None:
    for index in range(5):
        await repo.create(user.id, TodoCreateData(title=f"t{index}"))

    rows, total = await repo.list_todos(user.id, TodoQuery(limit=2, offset=1))
    assert total == 5
    assert [row.title for row in rows] == ["t1", "t2"]


# --- update / I4 -----------------------------------------------------------
async def test_update_sets_and_clears_completed_at(repo, user) -> None:
    todo = await repo.create(user.id, TodoCreateData(title="Task"))

    completed = await repo.update(
        user.id, todo.id, TodoUpdateData(completed=True)
    )
    assert completed.completed is True
    assert completed.completed_at is not None
    stamped = completed.completed_at

    # Re-applying the same value must not re-stamp it.
    again = await repo.update(user.id, todo.id, TodoUpdateData(completed=True))
    assert again.completed_at == stamped

    reopened = await repo.update(
        user.id, todo.id, TodoUpdateData(completed=False)
    )
    assert reopened.completed is False
    assert reopened.completed_at is None


async def test_update_bumps_updated_at(repo, user) -> None:
    todo = await repo.create(user.id, TodoCreateData(title="Task"))
    before = todo.updated_at

    updated = await repo.update(user.id, todo.id, TodoUpdateData(completed=True))
    assert updated.updated_at >= before
    assert updated.created_at == todo.created_at


async def test_update_returns_none_for_unknown_id(repo, user) -> None:
    assert (
        await repo.update(user.id, uuid4(), TodoUpdateData(completed=True))
        is None
    )


async def test_update_applies_the_scheduled_slice3_columns(repo, user) -> None:
    """The write path is slice 3's, but the column plumbing is exercised now."""
    todo = await repo.create(user.id, TodoCreateData(title="Task"))
    due = date.today() + timedelta(days=3)

    updated = await repo.update(
        user.id,
        todo.id,
        TodoUpdateData(
            title="Renamed", description="notes", priority=Priority.HIGH, due_date=due
        ),
    )
    assert updated.title == "Renamed"
    assert updated.description == "notes"
    assert updated.priority is Priority.HIGH
    assert updated.due_date == due


async def test_unset_fields_are_left_alone(repo, user) -> None:
    todo = await repo.create(
        user.id, TodoCreateData(title="Task", description="keep me")
    )
    updated = await repo.update(user.id, todo.id, TodoUpdateData(completed=True))
    assert updated.description == "keep me"
    assert TodoUpdateData().has_changes() is False
    assert TodoUpdateData(completed=True).has_changes() is True
    assert TodoUpdateData().title is UNSET


# --- delete ----------------------------------------------------------------
async def test_delete_returns_true_then_false(repo, user) -> None:
    todo = await repo.create(user.id, TodoCreateData(title="Task"))

    assert await repo.delete(user.id, todo.id) is True
    assert await repo.get(user.id, todo.id) is None
    assert await repo.delete(user.id, todo.id) is False


async def test_delete_returns_false_for_unknown_id(repo, user) -> None:
    assert await repo.delete(user.id, uuid4()) is False


# --- owner isolation (C1 / D-E2) -------------------------------------------
async def test_user_a_never_sees_user_b_rows(repo, user, user_b) -> None:
    b_todo = await repo.create(user_b.id, TodoCreateData(title="B's secret"))
    a_todo = await repo.create(user.id, TodoCreateData(title="A's task"))

    rows, total = await repo.list_todos(user.id, TodoQuery())
    assert [row.id for row in rows] == [a_todo.id]
    assert total == 1

    assert await repo.get(user.id, b_todo.id) is None
    assert (
        await repo.update(user.id, b_todo.id, TodoUpdateData(completed=True))
        is None
    )
    assert await repo.delete(user.id, b_todo.id) is False

    # B's row survived every one of A's attempts.
    still_there = await repo.get(user_b.id, b_todo.id)
    assert still_there is not None
    assert still_there.completed is False


async def test_cannot_create_a_todo_in_another_users_list(
    repo, db_session, user, user_b
) -> None:
    """A guessed list id must not let A file rows into B's list."""
    b_list = await SqlAlchemyListRepository(db_session).get_default(user_b.id)

    with pytest.raises(ListNotFound):
        await repo.create(
            user.id, TodoCreateData(title="Trespass", list_id=b_list.id)
        )

    rows, total = await repo.list_todos(user_b.id, TodoQuery())
    assert (rows, total) == ([], 0)


async def test_cannot_move_a_todo_into_another_users_list(
    repo, db_session, user, user_b
) -> None:
    user_id = user.id
    todo = await repo.create(user_id, TodoCreateData(title="Mine"))
    todo_id, original_list_id = todo.id, todo.list_id
    await db_session.commit()
    b_list_id = (await SqlAlchemyListRepository(db_session).get_default(user_b.id)).id

    with pytest.raises(ListNotFound):
        await repo.update(user_id, todo_id, TodoUpdateData(list_id=b_list_id))
    # The router's session dependency rolls back on any raised error; after a
    # rollback every loaded instance is expired, so ids are captured up front.
    await db_session.rollback()

    unchanged = await repo.get(user_id, todo_id)
    assert unchanged.list_id == original_list_id


async def test_creating_in_an_unknown_list_is_not_found(repo, user) -> None:
    with pytest.raises(ListNotFound):
        await repo.create(user.id, TodoCreateData(title="x", list_id=uuid4()))


async def test_creating_in_the_callers_own_list_is_allowed(
    repo, db_session, user
) -> None:
    own = await SqlAlchemyListRepository(db_session).create(user.id, "Work")
    todo = await repo.create(
        user.id, TodoCreateData(title="Task", list_id=own.id)
    )
    assert todo.list_id == own.id

    moved = await repo.update(user.id, todo.id, TodoUpdateData(list_id=own.id))
    assert moved.list_id == own.id


async def test_lists_are_owner_scoped(db_session, user, user_b) -> None:
    lists = SqlAlchemyListRepository(db_session)
    a_default = await lists.get_default(user.id)
    b_default = await lists.get_default(user_b.id)

    assert a_default.id != b_default.id
    assert await lists.get(user.id, b_default.id) is None
    assert await lists.rename(user.id, b_default.id, "Hacked") is None
    assert await lists.delete(user.id, b_default.id) is False


# --- list repository (slice 2) ---------------------------------------------
async def test_the_first_list_is_the_default_and_later_ones_are_not(
    db_session, user_b
) -> None:
    lists = SqlAlchemyListRepository(db_session)

    first = await lists.create(user_b.id, "Work")
    second = await lists.create(user_b.id, "Home")

    assert first.is_default is True
    assert second.is_default is False
    assert (await lists.get_default(user_b.id)).id == first.id


async def test_deleting_the_only_list_is_refused(db_session, user_b) -> None:
    lists = SqlAlchemyListRepository(db_session)
    only = await lists.get_default(user_b.id)

    with pytest.raises(CannotDeleteLastList):
        await lists.delete(user_b.id, only.id)

    assert await lists.count(user_b.id) == 1


async def test_deleting_the_default_promotes_the_oldest_survivor(
    db_session, user_b
) -> None:
    lists = SqlAlchemyListRepository(db_session)
    default = await lists.get_default(user_b.id)
    second = await lists.create(user_b.id, "Second")
    third = await lists.create(user_b.id, "Third")

    assert await lists.delete(user_b.id, default.id) is True

    remaining = await lists.list_lists(user_b.id)
    assert [(row.id, row.is_default) for row in remaining] == [
        (second.id, True),
        (third.id, False),
    ]


async def test_deleting_a_non_default_list_does_not_move_the_default(
    db_session, user_b
) -> None:
    lists = SqlAlchemyListRepository(db_session)
    default = await lists.get_default(user_b.id)
    other = await lists.create(user_b.id, "Other")

    assert await lists.delete(user_b.id, other.id) is True
    assert (await lists.get_default(user_b.id)).id == default.id


async def test_todo_counts_exclude_subtasks_and_other_users(
    repo, db_session, user, user_b
) -> None:
    lists = SqlAlchemyListRepository(db_session)
    work = await lists.create(user.id, "Work")

    open_todo = await repo.create(user.id, TodoCreateData(title="Open", list_id=work.id))
    done = await repo.create(user.id, TodoCreateData(title="Done", list_id=work.id))
    await repo.update(user.id, done.id, TodoUpdateData(completed=True))
    await repo.create(
        user.id, TodoCreateData(title="Subtask", parent_id=open_todo.id)
    )
    # B's rows live in B's own list and must not appear in A's counts.
    await repo.create(user_b.id, TodoCreateData(title="B's"))
    await db_session.commit()

    counts = await lists.todo_counts(user.id)
    assert counts[work.id] == (2, 1)
    # A list with no todos is simply absent (the caller defaults it to 0/0).
    default_list = await lists.get_default(user.id)
    assert default_list.id not in counts

    scoped = await lists.todo_counts(user.id, [work.id])
    assert scoped == {work.id: (2, 1)}
    assert await lists.todo_counts(user.id, []) == {}


async def test_user_repository_normalizes_email(db_session) -> None:
    users = SqlAlchemyUserRepository(db_session)
    created = await users.create("  MiXeD@Example.COM ", "!", "  Name  ")
    assert created.email == "mixed@example.com"
    assert created.display_name == "Name"
    assert (await users.get_by_email("MIXED@EXAMPLE.COM")).id == created.id


# --- cascades (needs PRAGMA foreign_keys=ON on SQLite) ---------------------
async def test_deleting_a_list_deletes_its_todos(
    repo, db_session, user
) -> None:
    todo = await repo.create(user.id, TodoCreateData(title="Task"))
    list_id = todo.list_id
    await db_session.commit()

    todo_list = await db_session.get(TodoList, list_id)
    await db_session.delete(todo_list)
    await db_session.commit()

    remaining = await db_session.execute(select(func.count()).select_from(Todo))
    assert remaining.scalar_one() == 0


async def test_deleting_a_parent_deletes_its_subtasks(
    repo, db_session, user
) -> None:
    parent = await repo.create(user.id, TodoCreateData(title="Parent"))
    await repo.create(user.id, TodoCreateData(title="Child", parent_id=parent.id))
    await db_session.commit()

    assert await repo.delete(user.id, parent.id) is True
    await db_session.commit()

    remaining = await db_session.execute(select(func.count()).select_from(Todo))
    assert remaining.scalar_one() == 0


async def test_deleting_a_user_cascades_to_lists_and_todos(
    repo, db_session, user_b
) -> None:
    await repo.create(user_b.id, TodoCreateData(title="B's task"))
    await db_session.commit()

    await db_session.delete(user_b)
    await db_session.commit()

    todos = await db_session.execute(select(func.count()).select_from(Todo))
    lists = await db_session.execute(select(func.count()).select_from(TodoList))
    assert todos.scalar_one() == 0
    assert lists.scalar_one() == 0


# --- invariant I1: subtasks are exactly one level deep ---------------------
async def test_a_subtask_cannot_have_a_subtask(repo, user) -> None:
    """A second level would vanish from every read: the response embeds one."""
    parent = await repo.create(user.id, TodoCreateData(title="Parent"))
    child = await repo.create(
        user.id, TodoCreateData(title="Child", parent_id=parent.id)
    )

    with pytest.raises(SubtaskDepthExceeded):
        await repo.create(
            user.id, TodoCreateData(title="Grandchild", parent_id=child.id)
        )


async def test_unknown_parent_is_todo_not_found(repo, user) -> None:
    with pytest.raises(TodoNotFound):
        await repo.create(
            user.id, TodoCreateData(title="Orphan", parent_id=uuid4())
        )


async def test_another_users_todo_cannot_be_a_parent(repo, user, user_b) -> None:
    b_todo = await repo.create(user_b.id, TodoCreateData(title="B's task"))

    with pytest.raises(TodoNotFound):
        await repo.create(
            user.id, TodoCreateData(title="Trespass", parent_id=b_todo.id)
        )


async def test_update_rejects_a_parent_that_is_itself_a_subtask(
    repo, user
) -> None:
    parent = await repo.create(user.id, TodoCreateData(title="Parent"))
    child = await repo.create(
        user.id, TodoCreateData(title="Child", parent_id=parent.id)
    )
    other = await repo.create(user.id, TodoCreateData(title="Other"))

    with pytest.raises(SubtaskDepthExceeded):
        await repo.update(user.id, other.id, TodoUpdateData(parent_id=child.id))


async def test_update_rejects_demoting_a_todo_that_has_subtasks(
    repo, user
) -> None:
    parent = await repo.create(user.id, TodoCreateData(title="Parent"))
    await repo.create(
        user.id, TodoCreateData(title="Child", parent_id=parent.id)
    )
    target = await repo.create(user.id, TodoCreateData(title="Target"))

    with pytest.raises(SubtaskDepthExceeded):
        await repo.update(user.id, parent.id, TodoUpdateData(parent_id=target.id))


# --- the slice-3 write surface (was NotImplementedError in slices 1–2) -----
async def test_create_and_update_apply_tags(repo, user) -> None:
    todo = await repo.create(
        user.id, TodoCreateData(title="Task", tags=("Home", "  home ", "work"))
    )
    # Normalization collapses the first two into one tag.
    assert sorted(tag.name for tag in todo.tags) == ["home", "work"]

    updated = await repo.update(user.id, todo.id, TodoUpdateData(tags=["work"]))
    assert [tag.name for tag in updated.tags] == ["work"]

    cleared = await repo.update(user.id, todo.id, TodoUpdateData(tags=[]))
    assert cleared.tags == []


async def test_update_reparents_and_promotes(repo, user) -> None:
    todo = await repo.create(user.id, TodoCreateData(title="Task"))
    parent = await repo.create(user.id, TodoCreateData(title="Parent"))

    demoted = await repo.update(user.id, todo.id, TodoUpdateData(parent_id=parent.id))
    assert demoted.parent_id == parent.id
    # I3: it followed its parent's list.
    assert demoted.list_id == parent.list_id

    promoted = await repo.update(user.id, todo.id, TodoUpdateData(parent_id=None))
    assert promoted.parent_id is None
    # Promotion must not delete the row through the delete-orphan cascade.
    assert await repo.get(user.id, todo.id) is not None


async def test_a_todo_cannot_be_its_own_parent(repo, user) -> None:
    todo = await repo.create(user.id, TodoCreateData(title="Task"))
    with pytest.raises(SubtaskDepthExceeded):
        await repo.update(user.id, todo.id, TodoUpdateData(parent_id=todo.id))


# --- the tag store validates its own input ---------------------------------
async def test_the_tag_repository_refuses_to_persist_an_invalid_name(
    db_session, user
) -> None:
    """The wire schema is not the only way in.

    Fixtures and — from slice 4 — the AI path call this directly, and an LLM is
    exactly the caller most likely to invent ``"Bad!Tag"``. A row written here
    could never have been created through the API, and would come back out of
    ``GET /api/tags`` forever.
    """
    tags = SqlAlchemyTagRepository(db_session)

    for invalid in ("Bad!Tag", "-leading", "a" * 31, "emoji 🙂"):
        with pytest.raises(ValueError):
            await tags.get_or_create_many(user.id, [invalid])

    assert await tags.list_tags(user.id) == []


async def test_the_tag_repository_normalizes_what_it_does_accept(
    db_session, user
) -> None:
    tags = SqlAlchemyTagRepository(db_session)
    created = await tags.get_or_create_many(user.id, ["  Home  ", "HOME", "work"])

    assert [tag.name for tag in created] == ["home", "work"]
    assert sorted(tag.name for tag in await tags.list_tags(user.id)) == ["home", "work"]


# --- the tag.created outbox (iteration 4, IT3-1) ---------------------------
async def test_the_outbox_reports_only_the_tags_this_call_inserted(
    db_session, user
) -> None:
    tags = SqlAlchemyTagRepository(db_session)
    await tags.get_or_create_many(user.id, ["home"])
    tags.take_created_tags()

    await tags.get_or_create_many(user.id, ["home", "errand"])

    assert [tag.name for tag in tags.take_created_tags()] == ["errand"]


async def test_the_outbox_keeps_insertion_order(db_session, user) -> None:
    tags = SqlAlchemyTagRepository(db_session)
    await tags.get_or_create_many(user.id, ["zulu", "alpha", "mike"])

    assert [tag.name for tag in tags.take_created_tags()] == ["zulu", "alpha", "mike"]


async def test_taking_the_outbox_drains_it(db_session, user) -> None:
    """A frame is staged once. A second read must be empty by construction."""
    tags = SqlAlchemyTagRepository(db_session)
    await tags.get_or_create_many(user.id, ["home"])

    assert len(tags.take_created_tags()) == 1
    assert tags.take_created_tags() == []


async def test_the_outbox_is_empty_when_nothing_was_created(db_session, user) -> None:
    tags = SqlAlchemyTagRepository(db_session)

    assert tags.take_created_tags() == []
    await tags.get_or_create_many(user.id, [])
    assert tags.take_created_tags() == []


async def test_two_repository_instances_do_not_share_an_outbox(
    db_session, user
) -> None:
    """Risk R1: the outbox is per instance, and instances are per request.

    ``Depends(get_tag_repository)`` builds a new repository for every request,
    so a tag created by one request cannot be announced by the next. Two
    instances over one session is exactly that shape.
    """
    first = SqlAlchemyTagRepository(db_session)
    await first.get_or_create_many(user.id, ["home"])

    second = SqlAlchemyTagRepository(db_session)
    await second.get_or_create_many(user.id, ["home", "errand"])

    assert [tag.name for tag in second.take_created_tags()] == ["errand"]
    # The first instance still holds its own, untouched by the second.
    assert [tag.name for tag in first.take_created_tags()] == ["home"]


async def test_the_todo_repository_delegates_the_outbox(repo, user) -> None:
    """Tags are created inside the todo write path; the router asks the todo
    repository, which has to hand back what its tag repository inserted."""
    await repo.create(user.id, TodoCreateData(title="Task", tags=("home", "work")))

    assert [tag.name for tag in repo.take_created_tags()] == ["home", "work"]
    assert repo.take_created_tags() == []

    await repo.create(user.id, TodoCreateData(title="Another", tags=("home",)))
    assert repo.take_created_tags() == []


async def test_the_outbox_follows_an_update_that_names_a_new_tag(repo, user) -> None:
    todo = await repo.create(user.id, TodoCreateData(title="Task", tags=("home",)))
    repo.take_created_tags()

    await repo.update(user.id, todo.id, TodoUpdateData(tags=["home", "errand"]))

    assert [tag.name for tag in repo.take_created_tags()] == ["errand"]


async def test_the_tag_repository_refuses_an_invalid_rename(db_session, user) -> None:
    tags = SqlAlchemyTagRepository(db_session)
    tag = (await tags.get_or_create_many(user.id, ["home"]))[0]

    with pytest.raises(ValueError):
        await tags.rename(user.id, tag.id, "Bad!Tag")

    # An unknown id still answers None rather than raising: "not yours" is not
    # a validation problem.
    assert await tags.rename(user.id, uuid4(), "fine") is None


async def test_finding_a_tag_by_an_invalid_name_is_a_miss_not_an_error(
    db_session, user
) -> None:
    """A *lookup* takes arbitrary input: asking about an impossible name simply
    finds nothing, which is what the ``tag=`` filter relies on."""
    tags = SqlAlchemyTagRepository(db_session)
    await tags.get_or_create_many(user.id, ["home"])

    assert await tags.find_by_name(user.id, "Bad!Tag") is None
    assert (await tags.find_by_name(user.id, "  HOME ")).name == "home"


# --- N+1 guard (spec B7.9 / acceptance criterion 11) -----------------------
MAX_LIST_STATEMENTS = 5


async def test_listing_enriched_todos_issues_a_bounded_number_of_queries(
    repo, db_session, user
) -> None:
    """20 todos, each with tags and subtasks, must not cost 20 round trips.

    The cost of a lazy relationship is invisible in a unit test and linear in
    production, so the budget is asserted rather than assumed: one select for
    the rows, one for their tags, one for their subtasks, one for the subtasks'
    tags, and one for the count — five, whatever the number of rows.
    """
    for index in range(20):
        parent = await repo.create(
            user.id,
            TodoCreateData(title=f"Task {index}", tags=("home", f"tag{index % 3}")),
        )
        for step in range(2):
            await repo.create(
                user.id,
                TodoCreateData(
                    title=f"Step {step}", parent_id=parent.id, tags=("home",)
                ),
            )
    await db_session.commit()
    db_session.expunge_all()

    statements: list[str] = []

    def count_statement(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    # ``get_bind()`` on an AsyncSession hands back the *sync* engine the async
    # facade drives, which is where DBAPI-level events fire.
    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", count_statement)
    try:
        rows, total = await repo.list_todos(user.id, TodoQuery())
        # Serialising must not trigger a lazy load either (risk R2).
        payload = [TodoResponse.model_validate(row) for row in rows]
    finally:
        event.remove(engine, "before_cursor_execute", count_statement)

    assert total == 20
    assert len(payload) == 20
    assert all(len(item.subtasks) == 2 for item in payload)
    assert all(item.tags for item in payload)
    # The lower bound is not decoration: if the listener ever stopped firing,
    # "<= 5" would pass at zero and this guard would silently stop guarding.
    assert 2 <= len(statements) <= MAX_LIST_STATEMENTS, statements
