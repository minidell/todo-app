"""``UtcDateTime`` — the guard that makes the SQLite lane trustworthy (D-T1).

PostgreSQL stores ``timestamptz`` and hands back aware datetimes; SQLite has no
timezone concept at all. Without this decorator the two lanes would disagree
about ``created_at``, and the "…Z" wire-format contract would hold on one
engine only.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import StatementError

from app.db.models import Todo, User
from app.db.types import UtcDateTime, utcnow
from app.repositories.protocols import TodoCreateData
from app.repositories.todos import SqlAlchemyTodoRepository

BERLIN = timezone(timedelta(hours=2))


def test_bind_rejects_a_naive_datetime() -> None:
    with pytest.raises(ValueError, match="naive datetime"):
        UtcDateTime().process_bind_param(datetime(2026, 9, 2, 12, 0, 0), None)


def test_bind_converts_an_aware_datetime_to_utc() -> None:
    value = datetime(2026, 9, 2, 12, 0, 0, tzinfo=BERLIN)
    bound = UtcDateTime().process_bind_param(value, None)
    assert bound == datetime(2026, 9, 2, 10, 0, 0, tzinfo=timezone.utc)
    assert bound.tzinfo is timezone.utc


def test_bind_and_result_pass_none_through() -> None:
    assert UtcDateTime().process_bind_param(None, None) is None
    assert UtcDateTime().process_result_value(None, None) is None


def test_result_attaches_utc_to_a_naive_value() -> None:
    """This is the SQLite case: the driver returns a naive datetime."""
    value = datetime(2026, 9, 2, 10, 0, 0)
    assert UtcDateTime().process_result_value(value, None) == datetime(
        2026, 9, 2, 10, 0, 0, tzinfo=timezone.utc
    )


def test_result_normalises_an_offset_value_to_utc() -> None:
    value = datetime(2026, 9, 2, 12, 0, 0, tzinfo=BERLIN)
    assert UtcDateTime().process_result_value(value, None) == datetime(
        2026, 9, 2, 10, 0, 0, tzinfo=timezone.utc
    )


def test_bind_rejects_a_non_datetime() -> None:
    with pytest.raises(TypeError):
        UtcDateTime().process_bind_param("2026-09-02", None)


async def test_round_trip_returns_an_aware_utc_value(db_session, user) -> None:
    """Runs on whichever engine the lane selected — that is the point."""
    repo = SqlAlchemyTodoRepository(db_session)
    before = utcnow()
    todo = await repo.create(user.id, TodoCreateData(title="Task"))
    await db_session.commit()
    db_session.expunge_all()

    loaded = (
        await db_session.execute(select(Todo).where(Todo.id == todo.id))
    ).scalar_one()
    assert loaded.created_at.tzinfo is not None
    assert loaded.created_at.utcoffset() == timedelta(0)
    assert loaded.created_at >= before


async def test_writing_a_naive_datetime_is_refused_at_the_boundary(
    db_session, user
) -> None:
    user = await db_session.get(User, user.id)
    user.updated_at = datetime(2026, 9, 2, 12, 0, 0)
    # SQLAlchemy wraps the bind-time ValueError in a StatementError.
    with pytest.raises(StatementError, match="naive datetime"):
        await db_session.flush()
    await db_session.rollback()
