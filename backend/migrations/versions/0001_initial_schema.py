"""initial schema — full iteration-2 target schema (master §3.1, decision D-M1)

This baseline creates every table of the iteration-2 data model in one go,
including the columns that only slices 2–4 write to. Unused-yet columns are
inert; the payoff is that no later slice has to rewrite a table and the
``TodoResponse`` shape is stable from slice 1.

Portability (D-T1): generic ``sa.Uuid``, a non-native ``priority`` enum
(VARCHAR + CHECK, so there is no PostgreSQL type to create or drop), a real
join table for tags and ``timestamptz`` columns via ``UtcDateTime``.

``downgrade()`` drops everything in reverse dependency order and is exercised
by ``alembic upgrade head -> downgrade base -> upgrade head`` in CI.

Revision ID: 0001
Revises:
Create Date: 2026-09-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

import app.db.types

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=True),
        sa.Column("created_at", app.db.types.UtcDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", app.db.types.UtcDateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )
    op.create_table(
        "tags",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=30), nullable=False),
        sa.Column("created_at", app.db.types.UtcDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_tags_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tags")),
        sa.UniqueConstraint("user_id", "name", name="uq_tags_user_id_name"),
    )
    op.create_index("ix_tags_user_id", "tags", ["user_id"], unique=False)
    op.create_table(
        "todo_lists",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("created_at", app.db.types.UtcDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", app.db.types.UtcDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_todo_lists_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_todo_lists")),
        sa.UniqueConstraint("user_id", "name", name="uq_todo_lists_user_id_name"),
    )
    op.create_index("ix_todo_lists_user_id", "todo_lists", ["user_id"], unique=False)
    op.create_table(
        "todos",
        sa.Column("id", sa.Uuid(), nullable=False),
        # The owner is denormalised onto every todo so an ownership filter
        # never needs a join (master §3.1).
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("list_id", sa.Uuid(), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("completed", sa.Boolean(), nullable=False),
        sa.Column(
            "completed_at", app.db.types.UtcDateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "priority",
            sa.Enum(
                "low",
                "medium",
                "high",
                name="priority",
                native_enum=False,
                create_constraint=False,
            ),
            server_default="medium",
            nullable=False,
        ),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("created_at", app.db.types.UtcDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", app.db.types.UtcDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["list_id"],
            ["todo_lists.id"],
            name=op.f("fk_todos_list_id_todo_lists"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["todos.id"],
            name=op.f("fk_todos_parent_id_todos"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_todos_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "priority IN ('low', 'medium', 'high')", name=op.f("ck_todos_priority")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_todos")),
    )
    op.create_index("ix_todos_parent_id", "todos", ["parent_id"], unique=False)
    op.create_index(
        "ix_todos_user_id_created_at", "todos", ["user_id", "created_at"], unique=False
    )
    op.create_index(
        "ix_todos_user_id_due_date", "todos", ["user_id", "due_date"], unique=False
    )
    op.create_index(
        "ix_todos_user_id_list_id", "todos", ["user_id", "list_id"], unique=False
    )
    op.create_table(
        "todo_tags",
        sa.Column("todo_id", sa.Uuid(), nullable=False),
        sa.Column("tag_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tag_id"],
            ["tags.id"],
            name=op.f("fk_todo_tags_tag_id_tags"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["todo_id"],
            ["todos.id"],
            name=op.f("fk_todo_tags_todo_id_todos"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("todo_id", "tag_id", name=op.f("pk_todo_tags")),
    )
    op.create_index("ix_todo_tags_tag_id", "todo_tags", ["tag_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_todo_tags_tag_id", table_name="todo_tags")
    op.drop_table("todo_tags")
    op.drop_index("ix_todos_user_id_list_id", table_name="todos")
    op.drop_index("ix_todos_user_id_due_date", table_name="todos")
    op.drop_index("ix_todos_user_id_created_at", table_name="todos")
    op.drop_index("ix_todos_parent_id", table_name="todos")
    op.drop_table("todos")
    op.drop_index("ix_todo_lists_user_id", table_name="todo_lists")
    op.drop_table("todo_lists")
    op.drop_index("ix_tags_user_id", table_name="tags")
    op.drop_table("tags")
    op.drop_table("users")
