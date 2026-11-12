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
    ForeignKeyConstraint,
    Identity,
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

# A task (CAP#1): a plain text message (not a command) becomes one when its inbox item is stored, in the same
# transaction, so Telegram's repeat of the update (DUP#1), which stores no item, makes no task either. Its inbox item
# is (user_id, message_id), DUP#1's key, and one message is one task. Inbox items stored before CAP have no task.
# TRL#3: its card in Trello, made by the background loop (`todobot.trello.sync`), never a second one: `card_id` once it
# is made, and `card_requested_at` before each request, so a request whose outcome is unknown (a crash, a lost answer)
# is looked for on the board before another is made.
tasks = Table(
    "tasks",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("user_id", BigInteger, nullable=False),
    Column("message_id", BigInteger, nullable=False, comment="the inbox item it was made from: (user_id, message_id)"),
    Column("text", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("card_id", Text, nullable=True, comment="its Trello card (TRL#3); NULL until the card is made"),
    Column(
        "card_requested_at",
        DateTime(timezone=True),
        nullable=True,
        comment="when Trello was last asked to make its card; set while card_id is NULL: a card may exist already",
    ),
    CheckConstraint("status IN ('recorded')", name="status"),
    ForeignKeyConstraint(["user_id", "message_id"], ["inbox.user_id", "inbox.message_id"], ondelete="CASCADE"),
    UniqueConstraint("user_id", "message_id"),
    UniqueConstraint("card_id"),
    Index(None, "user_id", "created_at"),
)

# TRL#1: a user's access to their own Trello. The access token and its secret are encrypted with APP_SECRET_KEY
# (`todobot.crypto`); `/trello` again replaces the row. TRL#2: the board «todobot» and its list «Записана» once they
# are set up; `list_id` NULL means the board is to be set up (again).
trello_connections = Table(
    "trello_connections",
    metadata,
    Column("user_id", BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, autoincrement=False),
    Column("access", Text, nullable=False, comment="the Trello access token and its secret, encrypted (Fernet)"),
    Column("board_id", Text, nullable=True, comment="the board «todobot»; NULL until it is found or made"),
    Column("list_id", Text, nullable=True, comment="its list «Записана», where cards go; NULL: set the board up"),
    Column("connected_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

# TRL#1: an OAuth request on its way: `/trello` got a request token from Trello and sent its link to the user. Trello's
# callback brings the token back; it finds the user here (never from the callback's URL) once, before `expires_at`.
trello_requests = Table(
    "trello_requests",
    metadata,
    Column("token", Text, primary_key=True, comment="the OAuth request token Trello issued"),
    Column("secret", Text, nullable=False, comment="the request token's secret, encrypted (Fernet)"),
    Column("user_id", BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Index(None, "user_id"),
)
