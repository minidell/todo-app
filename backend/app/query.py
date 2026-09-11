"""``TodoQuery`` → SQL (master §6.3, slice-3 spec B5).

One function builds **both** statements a listing needs — the page of rows and
the count of everything that matched — from a single list of clauses. That is
the point: ``X-Total-Count`` disagreeing with the rows it describes is the
classic filtered-pagination bug, and it cannot happen if the two selects can
never carry different filters.

Portability (D-T1) constrains three things here:

* no ``NULLS LAST`` — SQLite has no such clause, so null ordering is expressed
  as a leading ``CASE`` column that both engines sort identically;
* priority order is a ``CASE``, not the enum's storage order — the column is a
  ``VARCHAR`` and ``'high' < 'low' < 'medium'`` alphabetically, which is
  meaningless;
* search is ``ILIKE`` on ``title``/``description``, which SQLAlchemy compiles to
  a native ``ILIKE`` on PostgreSQL and ``lower() LIKE lower()`` on SQLite.
"""

from __future__ import annotations

from datetime import date, timedelta
from uuid import UUID

from sqlalchemy import ColumnElement, Select, case, exists, func, or_, select

from app.db.models import Priority, Tag, Todo, todo_tags
from app.db.types import utcnow
from app.repositories.protocols import TodoQuery
from app.repositories.sql import LIKE_ESCAPE, escape_like
from app.schemas.tags import normalize_tag

#: ``due=week`` is "today plus the next six days" — seven calendar days
#: including today (§6.3).
WEEK_SPAN_DAYS = 6

#: ``priority`` ascending means most urgent first (§6.3), which is the opposite
#: of any lexical order the storage might suggest.
PRIORITY_RANK = {Priority.HIGH: 0, Priority.MEDIUM: 1, Priority.LOW: 2}


def _has_tag_clause(user_id: UUID, name: str) -> ColumnElement[bool]:
    """One correlated ``EXISTS`` per requested tag → **AND** semantics.

    ``tag=home&tag=work`` means "todos that have both", not "either" (§6.3).
    A single ``IN`` over the join would produce OR; one EXISTS per name is the
    smallest construct that says AND and stays readable in the generated SQL.

    The tag is matched on ``Tag.user_id`` as well as its name so a shared name
    can never reach across accounts.
    """
    return exists(
        select(1)
        .select_from(todo_tags)
        .join(Tag, Tag.id == todo_tags.c.tag_id)
        .where(
            todo_tags.c.todo_id == Todo.id,
            Tag.user_id == user_id,
            Tag.name == name,
        )
        .correlate(Todo)
    )


def _due_clauses(query: TodoQuery, today: date) -> list[ColumnElement[bool]]:
    """The ``due`` preset and the explicit ``due_from``/``due_to`` range.

    The router rejects the combination with 400 ``conflicting_due_filters``
    before we get here, so at most one of the two branches contributes.
    """
    clauses: list[ColumnElement[bool]] = []
    if query.due == "overdue":
        # A completed todo is never "overdue": the deadline stopped mattering
        # when the work was done.
        clauses.append(Todo.due_date < today)
        clauses.append(Todo.completed.is_(False))
    elif query.due == "today":
        clauses.append(Todo.due_date == today)
    elif query.due == "week":
        clauses.append(Todo.due_date >= today)
        clauses.append(Todo.due_date <= today + timedelta(days=WEEK_SPAN_DAYS))
    elif query.due == "none":
        clauses.append(Todo.due_date.is_(None))

    if query.due_from is not None:
        clauses.append(Todo.due_date >= query.due_from)
    if query.due_to is not None:
        clauses.append(Todo.due_date <= query.due_to)
    return clauses


def _search_clause(term: str) -> ColumnElement[bool]:
    """Case-insensitive substring over ``title`` **or** ``description``.

    The term is escaped and the escape character declared, so a search for
    ``100%`` looks for the three characters ``100%`` instead of matching every
    row — a wildcard the user never asked for is a silently wrong result, not a
    crash, which is exactly the kind of bug that survives to production.
    """
    pattern = f"%{escape_like(term)}%"
    return or_(
        Todo.title.ilike(pattern, escape=LIKE_ESCAPE),
        Todo.description.ilike(pattern, escape=LIKE_ESCAPE),
    )


def build_filters(user_id: UUID, query: TodoQuery) -> list[ColumnElement[bool]]:
    """Every WHERE clause implied by ``query``, owner scoping included."""
    clauses: list[ColumnElement[bool]] = [
        Todo.user_id == user_id,
        # Subtasks travel inside their parent; they are never rows of their own
        # in a listing (§6.3).
        Todo.parent_id.is_(None),
    ]

    if query.list_id is not None:
        clauses.append(Todo.list_id == query.list_id)
    if query.status == "active":
        clauses.append(Todo.completed.is_(False))
    elif query.status == "completed":
        clauses.append(Todo.completed.is_(True))
    if query.priorities:
        clauses.append(Todo.priority.in_(query.priorities))

    for raw_name in query.tags:
        name = normalize_tag(raw_name)
        if not name:
            continue
        # An unknown tag name simply matches nothing — an empty result, not a
        # 404: the caller asked "which todos have this tag", and "none" is a
        # truthful answer that does not leak whether the tag exists.
        clauses.append(_has_tag_clause(user_id, name))

    clauses.extend(_due_clauses(query, query.today or utcnow().date()))

    if query.q:
        clauses.append(_search_clause(query.q))
    return clauses


def _sort_column(sort: str):
    if sort == "due_date":
        return Todo.due_date
    if sort == "priority":
        return case(
            *((Todo.priority == member, rank) for member, rank in PRIORITY_RANK.items()),
            else_=len(PRIORITY_RANK),
        )
    if sort == "title":
        # Case-insensitive, so "apple" and "Apple" sort together rather than
        # in two separate ASCII blocks.
        return func.lower(Todo.title)
    if sort == "updated_at":
        return Todo.updated_at
    return Todo.created_at


def build_order_by(query: TodoQuery) -> list:
    """The ORDER BY terms, ending in the stable ``created_at, id`` tiebreak.

    Without the tiebreak, two todos with the same priority (or the same null
    due date) could swap places between two identical requests — and with
    ``limit``/``offset`` that means a row appearing on two pages or on none.
    """
    terms = []
    if query.sort == "due_date":
        # ``NULLS LAST`` is not portable (D-T1): a leading 0/1 CASE column puts
        # undated todos last in *both* directions, which is what §6.3 requires.
        terms.append(case((Todo.due_date.is_(None), 1), else_=0).asc())

    column = _sort_column(query.sort)
    terms.append(column.desc() if query.order == "desc" else column.asc())
    terms.extend([Todo.created_at.asc(), Todo.id.asc()])
    return terms


def build_todo_query(user_id: UUID, query: TodoQuery) -> tuple[Select, Select]:
    """``(rows, count)`` — the page of todos and the unpaginated match count."""
    clauses = build_filters(user_id, query)

    rows = (
        select(Todo)
        .where(*clauses)
        .order_by(*build_order_by(query))
        .limit(query.limit)
        .offset(query.offset)
    )
    # limit/offset deliberately absent: X-Total-Count reports how many todos
    # match, not how many are on this page (§6.3).
    count = select(func.count()).select_from(Todo).where(*clauses)
    return rows, count
