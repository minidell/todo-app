"""SQLAlchemy ORM entities — persistence only (master spec §3.1/§3.2).

Portability rules (D-T1) are load-bearing here: generic ``Uuid``, non-native
enums, a join table instead of an array column, Python-side timestamp
defaults and the ``UtcDateTime`` decorator. The identical schema therefore
builds on PostgreSQL and on the SQLite test lane.
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    Enum,
    ForeignKey,
    Index,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import UtcDateTime, utcnow


class Priority(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


#: Stored as VARCHAR on both engines — never a native PG enum type (D-T1).
#: The value CHECK is declared explicitly on the table rather than left to the
#: type: Alembic skips *type-bound* check constraints when autogenerating, so a
#: type-generated CHECK would show up as a permanent phantom diff in
#: ``alembic check``.
PriorityType = Enum(
    Priority,
    native_enum=False,
    length=6,
    validate_strings=True,
    create_constraint=False,
    name="priority",
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
)

PRIORITY_CHECK_SQL = "priority IN ('low', 'medium', 'high')"


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    lists: Mapped[list[TodoList]] = relationship(
        "TodoList", back_populates="user", cascade="all, delete-orphan"
    )


class TodoList(Base):
    __tablename__ = "todo_lists"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_todo_lists_user_id_name"),
        Index("ix_todo_lists_user_id", "user_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    user: Mapped[User] = relationship("User", back_populates="lists")
    todos: Mapped[list[Todo]] = relationship(
        "Todo", back_populates="todo_list", cascade="all, delete-orphan"
    )


class Tag(Base):
    __tablename__ = "tags"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_tags_user_id_name"),
        Index("ix_tags_user_id", "user_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(30), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow
    )


todo_tags = Table(
    "todo_tags",
    Base.metadata,
    Column(
        "todo_id",
        Uuid,
        ForeignKey("todos.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "tag_id",
        Uuid,
        ForeignKey("tags.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Index("ix_todo_tags_tag_id", "tag_id"),
)


class Todo(Base):
    __tablename__ = "todos"
    __table_args__ = (
        CheckConstraint(PRIORITY_CHECK_SQL, name="priority"),
        Index("ix_todos_user_id_list_id", "user_id", "list_id"),
        Index("ix_todos_parent_id", "parent_id"),
        Index("ix_todos_user_id_due_date", "user_id", "due_date"),
        Index("ix_todos_user_id_created_at", "user_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    # The owner is denormalised onto every row so an ownership check never
    # needs a join (master §3.1).
    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    list_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("todo_lists.id", ondelete="CASCADE"), nullable=False
    )
    parent_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("todos.id", ondelete="CASCADE"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    priority: Mapped[Priority] = mapped_column(
        PriorityType, nullable=False, default=Priority.MEDIUM, server_default="medium"
    )
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    todo_list: Mapped[TodoList] = relationship("TodoList", back_populates="todos")
    parent: Mapped[Todo | None] = relationship(
        "Todo", back_populates="subtasks", remote_side="Todo.id"
    )
    subtasks: Mapped[list[Todo]] = relationship(
        "Todo",
        back_populates="parent",
        cascade="all, delete-orphan",
        order_by="(Todo.created_at, Todo.id)",
    )
    tags: Mapped[list[Tag]] = relationship(
        "Tag", secondary=todo_tags, order_by="Tag.name", lazy="selectin"
    )
