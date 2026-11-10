"""users and inbox

Revision ID: 0001
Revises:
Create Date: 2026-11-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), autoincrement=False, nullable=False, comment="Telegram user id"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
    )
    op.create_table(
        "inbox",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("file_id", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("kind IN ('text', 'voice', 'photo', 'other')", name="ck_inbox_kind"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_inbox_user_id_users", ondelete="CASCADE"),
    )
    op.create_index("ix_inbox_user_id_received_at", "inbox", ["user_id", "received_at"])


def downgrade() -> None:
    op.drop_index("ix_inbox_user_id_received_at", table_name="inbox")
    op.drop_table("inbox")
    op.drop_table("users")
