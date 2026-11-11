"""inbox: the message's identity, one item per message (DUP#1)

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-11

Safe while the previous version still serves (Railway runs `migrate` before the new version takes traffic): it only
adds. The nullable column without a default changes the catalog only, no table rewrite; the old code's INSERTs leave
it NULL, and the unique constraint counts NULLs as distinct, so they still succeed. Building the constraint's index
holds the table's lock for as long as it takes on a small table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "inbox",
        sa.Column(
            "message_id",
            sa.BigInteger(),
            nullable=True,
            comment="Telegram message_id in the user's private chat; NULL for rows stored before DUP",
        ),
    )
    op.create_unique_constraint("uq_inbox_user_id_message_id", "inbox", ["user_id", "message_id"])


def downgrade() -> None:
    op.drop_constraint("uq_inbox_user_id_message_id", "inbox", type_="unique")
    op.drop_column("inbox", "message_id")
