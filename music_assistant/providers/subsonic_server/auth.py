"""Subsonic app passwords: derivation and request verification."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

APP_PASSWORD_LENGTH = 24


def generate_secret() -> str:
    """Return a new random secret to derive app passwords from."""
    return secrets.token_hex(32)


def derive_app_password(secret: str, user_id: str) -> str:
    """
    Return the Subsonic app password of a user.

    :param secret: The plugin secret the passwords are derived from.
    :param user_id: The Music Assistant user id.
    """
    digest = hmac.new(secret.encode(), user_id.encode(), hashlib.sha256).digest()
    return base64.b32encode(digest).decode().rstrip("=").lower()[:APP_PASSWORD_LENGTH]


def verify_credentials(
    expected_password: str,
    password: str | None = None,
    token: str | None = None,
    salt: str | None = None,
) -> bool:
    """
    Check the credentials of a Subsonic request against the expected password.

    Token authentication (``t`` and ``s``) takes precedence over a password (``p``),
    which may be sent plain or hex encoded with an ``enc:`` prefix.

    :param expected_password: The app password of the user.
    :param password: The ``p`` request parameter.
    :param token: The ``t`` request parameter.
    :param salt: The ``s`` request parameter.
    """
    if token is not None and salt is not None:
        # md5 is what the Subsonic protocol prescribes for token authentication
        computed = hashlib.md5(
            (expected_password + salt).encode(), usedforsecurity=False
        ).hexdigest()
        return hmac.compare_digest(computed, token.lower())
    if password is None:
        return False
    if password.startswith("enc:"):
        try:
            password = bytes.fromhex(password[4:]).decode()
        except ValueError:
            return False
    return hmac.compare_digest(password.encode(), expected_password.encode())
