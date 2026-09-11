"""Repository protocols + the dataclasses they exchange (master §4.2).

``TodoQuery`` already carries **every** filter/sort field of master §6.3 with
safe defaults. Slice 1 honours only the defaults; slice 3 populates the rest.
Keeping the full shape here means the protocol never changes across slices.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Literal, Protocol, Sequence
from uuid import UUID

from app.db.models import Priority, Tag, Todo, TodoList, User


class _Unset(Enum):
    """Sentinel distinguishing "don't touch" from an explicit ``None``."""

    UNSET = "UNSET"

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return False

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "UNSET"


UNSET = _Unset.UNSET

TodoStatus = Literal["all", "active", "completed"]
DuePreset = Literal["any", "overdue", "today", "week", "none"]
TodoSort = Literal["created_at", "updated_at", "due_date", "priority", "title"]
SortOrder = Literal["asc", "desc"]


@dataclass(frozen=True, slots=True)
class TodoQuery:
    """Read filters for ``list_todos`` — full master §6.3 surface."""

    list_id: UUID | None = None
    status: TodoStatus = "all"
    priorities: tuple[Priority, ...] = ()
    tags: tuple[str, ...] = ()
    due: DuePreset = "any"
    due_from: date | None = None
    due_to: date | None = None
    today: date | None = None
    q: str | None = None
    sort: TodoSort = "created_at"
    order: SortOrder = "asc"
    limit: int = 200
    offset: int = 0


@dataclass(frozen=True, slots=True)
class TodoCreateData:
    """Write payload for ``create``."""

    title: str
    list_id: UUID | None = None
    parent_id: UUID | None = None
    description: str | None = None
    priority: Priority = Priority.MEDIUM
    due_date: date | None = None
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TodoUpdateData:
    """Write payload for ``update``; every field defaults to ``UNSET``.

    ``UNSET`` means "leave alone"; an explicit ``None`` clears the column.
    """

    title: str | _Unset = UNSET
    description: str | None | _Unset = UNSET
    completed: bool | _Unset = UNSET
    priority: Priority | _Unset = UNSET
    due_date: date | None | _Unset = UNSET
    list_id: UUID | _Unset = UNSET
    parent_id: UUID | None | _Unset = UNSET
    tags: Sequence[str] | _Unset = field(default=UNSET)

    def has_changes(self) -> bool:
        return any(
            getattr(self, name) is not UNSET
            for name in (
                "title",
                "description",
                "completed",
                "priority",
                "due_date",
                "list_id",
                "parent_id",
                "tags",
            )
        )


class TodoRepository(Protocol):
    async def list_todos(
        self, user_id: UUID, query: TodoQuery
    ) -> tuple[list[Todo], int]: ...

    async def get(self, user_id: UUID, todo_id: UUID) -> Todo | None: ...

    async def create(self, user_id: UUID, data: TodoCreateData) -> Todo: ...

    async def update(
        self, user_id: UUID, todo_id: UUID, data: TodoUpdateData
    ) -> Todo | None: ...

    async def delete(self, user_id: UUID, todo_id: UUID) -> bool: ...

    def take_created_tags(self) -> list[Tag]: ...  # iteration 4, see TagRepository


class ListRepository(Protocol):
    async def list_lists(self, user_id: UUID) -> list[TodoList]: ...

    async def get(self, user_id: UUID, list_id: UUID) -> TodoList | None: ...

    async def get_default(self, user_id: UUID) -> TodoList: ...

    async def create(self, user_id: UUID, name: str) -> TodoList: ...

    async def rename(
        self, user_id: UUID, list_id: UUID, name: str
    ) -> TodoList | None: ...

    async def delete(self, user_id: UUID, list_id: UUID) -> bool: ...


class TagRepository(Protocol):
    """Declared now, implemented in slice 3 (master §4.2)."""

    async def list_tags(self, user_id: UUID) -> list[Tag]: ...

    async def get_or_create_many(
        self, user_id: UUID, names: Sequence[str]
    ) -> list[Tag]: ...

    async def rename(self, user_id: UUID, tag_id: UUID, name: str) -> Tag | None: ...

    async def delete(self, user_id: UUID, tag_id: UUID) -> bool: ...

    # iteration 4 (IT3-1): drains the tags this *request* actually inserted, so
    # the router can stage one ``tag.created`` frame each. Returns [] and is
    # idempotent when nothing was created. ``TodoRepository`` gets the same
    # method, delegating to the tag repository it owns — tags are created
    # inside the todo write path.
    def take_created_tags(self) -> list[Tag]: ...


class UserRepository(Protocol):
    async def get_by_email(self, email: str) -> User | None: ...

    async def get(self, user_id: UUID) -> User | None: ...

    async def create(
        self, email: str, password_hash: str, display_name: str | None
    ) -> User: ...
