"""Authentication manager base for the 24-7 (247e) music backend."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from music_assistant.helpers.util import lock, try_parse_int
from music_assistant.providers.music247e.api_client import JsonLike

if TYPE_CHECKING:
    from music_assistant.providers.music247e.provider import Music247eProvider


class Music247eAccessToken:
    """24-7 (247e) access token wrapper."""

    def __init__(self, access_token: str) -> None:
        """Initialize Music247eAccessToken."""
        self._access_token = access_token
        self._token_parts = self._parse_access_token(access_token)

    def is_expired(self) -> bool:
        """Return True if token is expired."""
        expires_at = try_parse_int(self._token_parts.get("ExpiresOn", 0))
        return not expires_at or expires_at <= time.time()

    def __str__(self) -> str:
        """Return string representation of the access token."""
        return self._access_token

    def _parse_access_token(self, token: str) -> JsonLike:
        return dict(part.split("=", 1) for part in token.split("&") if "=" in part)


class Music247eAuthManager:
    """24-7 (247e) authentication manager base; concrete providers implement auth_token."""

    def __init__(self, provider: Music247eProvider):
        """Initialize Music247eAuthManager."""
        self._access_token: Music247eAccessToken | None = None
        self._refresh_token: str | None = None
        self.mass = provider.mass
        self.provider = provider
        self.logger = provider.logger

    def invalidate(self) -> None:
        """Invalidate current access token."""
        self._access_token = None

    @lock
    async def auth_token(self) -> Music247eAccessToken | None:
        """Authenticate and return access token."""
        raise NotImplementedError
