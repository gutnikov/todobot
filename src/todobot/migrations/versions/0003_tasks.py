"""tasks: a plain text message becomes a task, tied to its inbox item (CAP#1)

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-11

Safe while the previous version still serves (Railway runs `migrate` before the new version takes traffic): it only
adds a table, which the previous version never reads or writes. Its foreign key locks `inbox` against writes only for
as long as it takes to create, on an empty table. No inbox item stored before becomes a task (CAP#1): nothing is
copied into `tasks`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "message_id",
            sa.BigInteger(),
            nullable=False,
            comment="the inbox item it was made from: (user_id, message_id)",
        ),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('recorded')", name="ck_tasks_status"),
        sa.ForeignKeyConstraint(
            ["user_id", "message_id"],
            ["inbox.user_id", "inbox.message_id"],
            name="fk_tasks_user_id_inbox",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tasks"),
        sa.UniqueConstraint("user_id", "message_id", name="uq_tasks_user_id_message_id"),
    )
    op.create_index("ix_tasks_user_id_created_at", "tasks", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_tasks_user_id_created_at", table_name="tasks")
    op.drop_table("tasks")
