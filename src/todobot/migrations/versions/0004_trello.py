"""trello: users' connections, OAuth requests on their way, and each task's card (TRL)

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-12

Safe while the previous version still serves (Railway runs `migrate` before the new version takes traffic): it only
adds. Two new tables, which the previous version never reads or writes, and two nullable columns without a default on
`tasks` (a catalog change, no table rewrite). The previous version's INSERTs into `tasks` leave them NULL, so its
tasks wait for their cards like any other (TRL#3: tasks recorded before become cards once Trello is connected). The
unique constraint on `card_id` is built on a column that is NULL everywhere; Postgres counts NULLs as distinct.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("card_id", sa.Text(), nullable=True, comment="its Trello card (TRL#3); NULL until the card is made"),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "card_requested_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="when Trello was last asked to make its card; set while card_id is NULL: a card may exist already",
        ),
    )
    op.create_unique_constraint("uq_tasks_card_id", "tasks", ["card_id"])
    op.create_table(
        "trello_connections",
        sa.Column("user_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column(
            "access",
            sa.Text(),
            nullable=False,
            comment="the Trello access token and its secret, encrypted (Fernet)",
        ),
        sa.Column("board_id", sa.Text(), nullable=True, comment="the board «todobot»; NULL until it is found or made"),
        sa.Column(
            "list_id", sa.Text(), nullable=True, comment="its list «Записана», where cards go; NULL: set the board up"
        ),
        sa.Column("connected_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_trello_connections_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_trello_connections"),
    )
    op.create_table(
        "trello_requests",
        sa.Column("token", sa.Text(), nullable=False, comment="the OAuth request token Trello issued"),
        sa.Column("secret", sa.Text(), nullable=False, comment="the request token's secret, encrypted (Fernet)"),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_trello_requests_user_id_users", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("token", name="pk_trello_requests"),
    )
    op.create_index("ix_trello_requests_user_id", "trello_requests", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_trello_requests_user_id", table_name="trello_requests")
    op.drop_table("trello_requests")
    op.drop_table("trello_connections")
    op.drop_constraint("uq_tasks_card_id", "tasks", type_="unique")
    op.drop_column("tasks", "card_requested_at")
    op.drop_column("tasks", "card_id")
