"""Todo wire models (master §3.3, slice-1 spec B8).

``TodoResponse`` is emitted in its **full final shape from slice 1 onwards**;
fields whose write path lands in a later slice simply carry their defaults.
The frontend type therefore never changes shape between slices.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    StringConstraints,
    field_validator,
    model_validator,
)
from sqlalchemy import inspect as sa_inspect

from app.db.models import Priority, Tag, Todo
from app.schemas.tags import RawTagName, normalize_tag_list

TodoTitle = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]
#: ``todos.description`` is ``Text``; 2000 is the application limit (§3.1).
MAX_DESCRIPTION_LENGTH = 2000
TodoDescription = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=MAX_DESCRIPTION_LENGTH),
]
#: A todo with more than ten tags is a mis-use of the field, not a taxonomy.
MAX_TAGS_PER_TODO = 10
#: Deliberately *not* ``Field(max_length=...)``: that constraint runs on the raw
#: list, before normalization, so ``["Home", "home", …]`` could be rejected for
#: exceeding a limit it never actually reaches. Spec B2 counts the tags a todo
#: would end up with, so the cap is enforced after de-duplication in
#: :meth:`TodoContent._normalize_tags`, matching the ``tag=`` filter.
TagNameList = list[RawTagName]

#: ``PATCH`` fields that an explicit ``null`` may **not** clear: a todo always
#: has a title, a completion state, a priority, a tag list (possibly empty) and
#: a list. ``description``, ``due_date`` and ``parent_id`` are the clearable
#: three (§6.3).
NON_CLEARABLE_UPDATE_FIELDS = ("title", "completed", "priority", "tags", "list_id")


def _normalized_tags_within_cap(value: list[str]) -> list[str]:
    """Normalize, de-duplicate, then bound — in that order (spec B2).

    Counting before de-duplication would reject a body for tags it does not
    actually carry: ``["Home", "home", "HOME", …]`` is one tag by the time it
    reaches the database.
    """
    names = normalize_tag_list(value)
    if len(names) > MAX_TAGS_PER_TODO:
        raise ValueError(
            f"a todo may carry at most {MAX_TAGS_PER_TODO} distinct tags "
            f"(got {len(names)})"
        )
    return names


def _blank_description_is_null(value: str | None) -> str | None:
    """A description that trims to nothing is absent, not an empty string.

    Otherwise ``""`` and ``null`` would be two ways to say "no description",
    and every consumer would have to test for both (spec B2).
    """
    return value or None


class TodoContent(BaseModel):
    """The fields a client may write on any todo, subtask or not.

    ``extra="forbid"`` (D-E3, reverses iteration-1 D4): an unknown key is a
    client bug and yields 422 rather than being silently dropped.
    """

    model_config = ConfigDict(extra="forbid")

    title: TodoTitle
    description: TodoDescription | None = None
    priority: Priority = Priority.MEDIUM
    due_date: date | None = None
    #: Names, not ids: tags are created implicitly by use (D-A3).
    tags: TagNameList = []

    @field_validator("description")
    @classmethod
    def _normalize_description(cls, value: str | None) -> str | None:
        return _blank_description_is_null(value)

    @field_validator("tags")
    @classmethod
    def _normalize_tags(cls, value: list[str]) -> list[str]:
        return _normalized_tags_within_cap(value)


class SubtaskCreate(TodoContent):
    """Request body for ``POST /api/todos/{id}/subtasks``.

    Deliberately without ``list_id``/``parent_id``: a subtask's parent is the
    path and its list is inherited (invariants I2/I3), so there is nothing for a
    client to disagree with.
    """


class TodoCreate(TodoContent):
    """Request body for ``POST /api/todos``."""

    #: Optional from slice 2 (§6.2/D-A1): omitted means the caller's default
    #: list. A list that is not the caller's is rejected as 404 ``list_not_found``
    #: by the repository; a syntactically invalid uuid is a 422 like any other
    #: malformed body field.
    list_id: UUID | None = None
    #: Present makes this a subtask (§6.3); the same code path as the
    #: ``/subtasks`` convenience route, so both enforce I1–I3 identically.
    parent_id: UUID | None = None


class TodoUpdate(BaseModel):
    """Request body for ``PATCH /api/todos/{id}`` — **partial** from slice 3.

    The scheduled contract change of master R11: every field is optional and
    only the keys actually present are applied. ``{"completed": …}`` — the one
    body slices 1–2 accepted — therefore keeps working unchanged, which is why
    no frontend call site had to move.

    "Absent" and "explicitly null" are different requests, so the sentinel is
    Pydantic's own ``model_fields_set`` rather than a default value: ``{}``
    leaves ``description`` alone, ``{"description": null}`` clears it. A ``null``
    for a field that cannot be cleared is a 422, not a silent no-op.
    """

    model_config = ConfigDict(extra="forbid")

    title: TodoTitle | None = None
    description: TodoDescription | None = None
    completed: bool | None = None
    priority: Priority | None = None
    due_date: date | None = None
    tags: TagNameList | None = None
    list_id: UUID | None = None
    parent_id: UUID | None = None

    @field_validator("description")
    @classmethod
    def _normalize_description(cls, value: str | None) -> str | None:
        return _blank_description_is_null(value)

    @field_validator("tags")
    @classmethod
    def _normalize_tags(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _normalized_tags_within_cap(value)

    @model_validator(mode="after")
    def _reject_null_on_non_clearable_fields(self) -> "TodoUpdate":
        nulled = [
            name
            for name in NON_CLEARABLE_UPDATE_FIELDS
            if name in self.model_fields_set and getattr(self, name) is None
        ]
        if nulled:
            raise ValueError(
                f"{', '.join(nulled)} cannot be null; omit the field to leave it "
                "unchanged"
            )
        return self

    def changes(self) -> dict[str, Any]:
        """Only the keys the client actually sent.

        An empty result is 400 ``empty_update`` at the router — a body with
        nothing to apply is a client mistake worth reporting, not a no-op 200.
        """
        changed: dict[str, Any] = {}
        for name in self.model_fields_set:
            value = getattr(self, name)
            changed[name] = tuple(value) if name == "tags" else value
        return changed


def _require_loaded(unloaded: set[str], relationship: str) -> None:
    if relationship in unloaded:
        raise RuntimeError(
            f"Todo.{relationship} is not loaded, so it cannot be serialised: "
            f"query the todo with selectinload(Todo.{relationship}) (see "
            "app/repositories/todos.py::_loader_options). Reading it here "
            "would lazy-load in an async context and raise MissingGreenlet."
        )


def _tag_names(tags: Any) -> list[str]:
    names = [tag.name if isinstance(tag, Tag) else str(tag) for tag in tags or []]
    return sorted(names)


class TodoResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    list_id: UUID
    parent_id: UUID | None
    title: str
    description: str | None
    completed: bool
    completed_at: datetime | None
    priority: Priority
    due_date: date | None
    tags: list[str]
    subtasks: list[TodoResponse]
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="before")
    @classmethod
    def _from_entity(cls, value: Any) -> Any:
        """Convert a ``Todo`` entity without ever triggering a lazy load.

        A lazy load in an async context raises ``MissingGreenlet`` (risk R2),
        so relationships are read only when the query eager-loaded them. A
        missing loader option is a **bug in the caller**, not an empty
        collection: silently serialising ``[]`` would ship a response that
        quietly drops the user's tags or subtasks, and no test would notice.
        It therefore raises.
        """
        if not isinstance(value, Todo):
            return value
        unloaded = sa_inspect(value).unloaded
        _require_loaded(unloaded, "tags")
        if value.parent_id is None:
            _require_loaded(unloaded, "subtasks")
            subtasks = list(value.subtasks)
        else:
            # Invariant I1 keeps the tree one level deep, so a subtask's own
            # ``subtasks`` is empty *by construction*. Trusting the invariant
            # here rather than eager-loading a level that can never exist saves
            # one SELECT per list request (acceptance criterion 11).
            subtasks = []
        return {
            "id": value.id,
            "list_id": value.list_id,
            "parent_id": value.parent_id,
            "title": value.title,
            "description": value.description,
            "completed": value.completed,
            "completed_at": value.completed_at,
            "priority": value.priority,
            "due_date": value.due_date,
            "tags": _tag_names(value.tags),
            # Always empty inside a subtask (I1), so this recursion terminates
            # after one step.
            "subtasks": subtasks,
            "created_at": value.created_at,
            "updated_at": value.updated_at,
        }
