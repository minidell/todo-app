"""``TodoResponse`` conversion rules — master §3.3 and risk R2.

R2: a lazy load in an async context raises ``MissingGreenlet``, which would be
a 500 on real data. The serializer refuses to guess: an un-eager-loaded
relationship is a caller bug and must fail in tests, not degrade to ``[]`` in
production.
"""

from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.db.models import Priority, Todo
from app.repositories.protocols import TodoCreateData, TodoQuery
from app.repositories.todos import SqlAlchemyTodoRepository
from app.schemas.ai import (
    CompletedChange,
    DueDateChange,
    EditTodoChangeSet,
    EditTodoContext,
    EditTodoResponse,
    PriorityChange,
    TagsChange,
    TextChange,
    TitleChange,
)
from app.schemas.todos import TodoResponse


async def test_serialising_without_loader_options_raises(
    db_session, user
) -> None:
    repo = SqlAlchemyTodoRepository(db_session)
    todo = await repo.create(user.id, TodoCreateData(title="Task"))
    await db_session.commit()
    db_session.expunge_all()

    # Deliberately fetched without the repository's loader options — exactly
    # what a future caller writing its own query would get wrong.
    bare = (
        await db_session.execute(select(Todo).where(Todo.id == todo.id))
    ).scalar_one()

    with pytest.raises(RuntimeError, match=r"Todo\.subtasks is not loaded"):
        TodoResponse.model_validate(bare)


async def test_the_error_names_the_fix(db_session, user) -> None:
    repo = SqlAlchemyTodoRepository(db_session)
    todo = await repo.create(user.id, TodoCreateData(title="Task"))
    await db_session.commit()
    db_session.expunge_all()

    bare = (
        await db_session.execute(select(Todo).where(Todo.id == todo.id))
    ).scalar_one()

    with pytest.raises(RuntimeError) as excinfo:
        TodoResponse.model_validate(bare)
    assert "selectinload(Todo.subtasks)" in str(excinfo.value)


async def test_repository_results_serialise_outside_the_session_scope(
    db_session, user
) -> None:
    """The repository's loader options are sufficient: no lazy load happens
    even after the objects are detached."""
    repo = SqlAlchemyTodoRepository(db_session)
    parent = await repo.create(user.id, TodoCreateData(title="Parent"))
    await repo.create(
        user.id, TodoCreateData(title="Child", parent_id=parent.id)
    )
    await db_session.commit()
    db_session.expunge_all()

    rows, _ = await repo.list_todos(user.id, TodoQuery())
    db_session.expunge_all()

    responses = [TodoResponse.model_validate(row) for row in rows]
    assert len(responses) == 1
    assert len(responses[0].subtasks) == 1
    # A subtask carries no subtasks of its own (I1).
    assert responses[0].subtasks[0].subtasks == []
    assert responses[0].tags == []


async def test_tag_names_are_sorted_ascending(db_session, user) -> None:
    from app.db.models import Tag

    repo = SqlAlchemyTodoRepository(db_session)
    todo = await repo.create(user.id, TodoCreateData(title="Task"))
    for name in ("work", "errand", "home"):
        todo.tags.append(Tag(user_id=user.id, name=name))
    await db_session.flush()

    assert TodoResponse.model_validate(todo).tags == ["errand", "home", "work"]


# --- the ``from`` wire key of the AI change set (iteration 5, risk R7) ------
#
# ``from`` is a Python keyword, so the field is ``previous`` in Python and only
# an alias puts the documented key on the wire. Get that wrong and the endpoint
# still answers 200 with a well-formed body — the frontend simply renders every
# diff empty, which no backend test would otherwise notice.


@pytest.mark.parametrize(
    ("model", "kwargs"),
    [
        (TitleChange, {"previous": "Dentist", "to": "Call the dentist"}),
        (TextChange, {"previous": "note", "to": None}),
        (PriorityChange, {"previous": Priority.MEDIUM, "to": Priority.HIGH}),
        (DueDateChange, {"previous": None, "to": date(2026, 9, 11)}),
        (CompletedChange, {"previous": False, "to": True}),
        (
            TagsChange,
            {"previous": ["home"], "to": ["home", "health"], "added": ["health"], "removed": []},
        ),
    ],
)
def test_every_change_model_serialises_from_not_previous(model, kwargs) -> None:
    dumped = model(**kwargs).model_dump()

    assert "from" in dumped
    assert "previous" not in dumped
    assert dumped["from"] == kwargs["previous"]


def test_a_change_set_puts_from_on_the_wire_through_its_parent() -> None:
    """Nested serialisation is the case that actually ships: the alias has to
    survive being dumped as part of ``EditTodoResponse``."""
    response = EditTodoResponse(
        todo_id=uuid4(),
        empty=False,
        context=EditTodoContext(subtasks_truncated=False),
        change_set=EditTodoChangeSet(
            title=TitleChange(previous="Dentist", to="Call the dentist")
        ),
    )

    assert response.model_dump()["change_set"]["title"] == {
        "from": "Dentist",
        "to": "Call the dentist",
    }


def test_a_change_model_still_accepts_the_wire_key_back() -> None:
    assert TitleChange.model_validate({"from": "a", "to": "b"}).previous == "a"
