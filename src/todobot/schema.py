"""Tables. Every table keys its rows by the Telegram user id: each user's data is theirs from the start.

The migrations in `todobot/migrations/versions` create these; `metadata` is what `alembic revision --autogenerate`
compares against.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    func,
)

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)

# A Telegram user who has sent the bot something to keep.
users = Table(
    "users",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=False, comment="Telegram user id"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

# Every message a user sent, as it came in: the contract's columns (SK-2), and the message's identity (DUP#1).
# A message is its chat and its `message_id`; the bot takes in private chats only, whose chat id is the user's id,
# so (user_id, message_id) is the message. Telegram's repeat of an update finds it there and stores nothing.
# Rows stored before DUP have no message_id (NULL); Postgres counts NULLs as distinct, so they never conflict.
inbox = Table(
    "inbox",
    metadata,
    Column("user_id", BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("kind", Text, nullable=False),
    Column("text", Text, nullable=True),
    Column("file_id", Text, nullable=True),
    Column("received_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column(
        "message_id",
        BigInteger,
        nullable=True,
        comment="Telegram message_id in the user's private chat; NULL for rows stored before DUP",
    ),
    CheckConstraint("kind IN ('text', 'voice', 'photo', 'other')", name="kind"),
    Index(None, "user_id", "received_at"),
    UniqueConstraint("user_id", "message_id"),
)
