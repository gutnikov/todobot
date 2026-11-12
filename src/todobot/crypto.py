"""Secrets kept in the database, encrypted with `APP_SECRET_KEY` (TRL: the users' Trello tokens).

Fernet (the `cryptography` package): AES-128-CBC with HMAC-SHA256, a fresh IV each time. The key is
`APP_SECRET_KEY`, 32 random bytes in url-safe base64, as `Fernet.generate_key()` makes it; `config` checks it at start.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class SecretBox:
    """Seals a secret into text for the database and opens it again. Its repr shows nothing of the key."""

    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode("ascii"))

    def __repr__(self) -> str:
        return "SecretBox()"

    def seal(self, secret: str) -> str:
        return self._fernet.encrypt(secret.encode("utf-8")).decode("ascii")

    def open(self, sealed: str) -> str | None:
        """The secret, or None when `sealed` was not sealed with this key (APP_SECRET_KEY changed) or was altered."""
        try:
            return self._fernet.decrypt(sealed.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError):
            return None
