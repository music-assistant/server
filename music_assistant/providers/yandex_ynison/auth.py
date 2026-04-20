"""Yandex Passport authentication helper.

Delegates Passport interactions to the ``ya-passport-auth`` library. Only one
helper is exposed: :func:`refresh_music_token`, used in borrow mode to obtain
a fresh music-scoped OAuth token from a session token (``x_token``) read from
the linked ``yandex_music`` provider's config.
"""

from __future__ import annotations

from music_assistant_models.errors import LoginFailed
from ya_passport_auth import PassportClient, SecretStr
from ya_passport_auth.exceptions import YaPassportError


async def refresh_music_token(x_token: SecretStr) -> SecretStr:
    """Exchange an x_token for a fresh music-scoped OAuth token."""
    try:
        async with PassportClient.create() as client:
            return await client.refresh_music_token(x_token)
    except YaPassportError as err:
        raise LoginFailed(f"Failed to refresh music token: {err}") from err
