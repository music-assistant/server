"""Yandex Passport token-refresh helper."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ya_passport_auth.ma import refresh_music_token as _refresh_music_token

if TYPE_CHECKING:
    from ya_passport_auth import SecretStr


async def refresh_music_token(x_token: SecretStr) -> SecretStr:
    """Exchange an x-token for a temporary music-scoped OAuth token."""
    return await _refresh_music_token(x_token)
