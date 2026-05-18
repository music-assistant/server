"""Yandex Passport authentication helpers.

Delegates Passport interactions to the ``ya-passport-auth`` library. Two
helpers are exposed to the rest of the plugin:

* :func:`perform_qr_auth` — full QR login (UI popup → tokens), used by the
  own-mode config flow when the user clicks "Login with QR code".
* :func:`refresh_music_token` — exchange ``x_token`` for a fresh music-scoped
  OAuth token. Called both in borrow mode (against the linked yandex_music
  provider's x_token) and in own mode (against the plugin's own stored
  x_token, when the user opted in to "Remember session").
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.errors import LoginFailed
from ya_passport_auth import PassportClient, SecretStr
from ya_passport_auth.exceptions import QRTimeoutError, YaPassportError

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


async def perform_qr_auth(mass: MusicAssistant, session_id: str) -> tuple[str, str, str | None]:
    """Run a QR login flow and return ``(x_token, music_token, display_login)``.

    Opens the QR popup in the MA frontend via
    :class:`music_assistant.helpers.auth.AuthenticationHelper`, polls the
    Yandex Passport endpoint until the user confirms the scan in the Yandex
    app, then returns the resulting tokens as plain strings (suitable for
    MA config storage).

    ``display_login`` is the Yandex login name when the server returns it
    (used by the config UI to render "Logged in as X"); may be ``None``.
    """
    # AuthenticationHelper lives in the music_assistant *server* package,
    # which isn't always installed in the plugin's standalone test/dev
    # environment. Lazy import keeps `provider.auth` importable for unit
    # tests that don't exercise this code path.
    from music_assistant.helpers.auth import AuthenticationHelper  # noqa: PLC0415

    try:
        async with PassportClient.create() as client:
            qr = await client.start_qr_login()
            async with AuthenticationHelper(mass, session_id) as auth_helper:
                auth_helper.send_url(qr.qr_url)
                creds = await client.poll_qr_until_confirmed(qr)

            if creds.music_token is None:
                raise LoginFailed("QR auth succeeded but no music token was returned")
            return (
                creds.x_token.get_secret(),
                creds.music_token.get_secret(),
                creds.display_login,
            )
    except QRTimeoutError as err:
        raise LoginFailed("QR authentication timed out. Please try again.") from err
    except YaPassportError as err:
        raise LoginFailed(f"Yandex auth error: {err}") from err


async def refresh_music_token(x_token: SecretStr) -> SecretStr:
    """Exchange an x_token for a fresh music-scoped OAuth token."""
    try:
        async with PassportClient.create() as client:
            return await client.refresh_music_token(x_token)
    except YaPassportError as err:
        raise LoginFailed(f"Failed to refresh music token: {err}") from err
