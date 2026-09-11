"""Todo-list wire models (master §3.3, §6.2).

``todo_count``/``active_count`` are **not** columns: they are computed per
request from a single grouped query (never N+1) and injected here, so a list
entity that has not been counted cannot silently serialise as zero.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints

from app.db.models import TodoList

ListName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
]


class ListCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: ListName


class ListUpdate(BaseModel):
    """``PATCH /api/lists/{id}`` — renaming is the only supported change.

    ``is_default`` is deliberately absent: which list is the default is derived
    (the first one created, or the oldest survivor after a delete), not client
    input, so there is no way to end up with two defaults.
    """

    model_config = ConfigDict(extra="forbid")

    name: ListName


class ListResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    is_default: bool
    #: Top-level todos only — subtasks travel inside their parent (§6.2).
    todo_count: int
    active_count: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_entity(
        cls, entity: TodoList, counts: tuple[int, int] = (0, 0)
    ) -> "ListResponse":
        total, active = counts
        return cls(
            id=entity.id,
            name=entity.name,
            is_default=entity.is_default,
            todo_count=total,
            active_count=active,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )
