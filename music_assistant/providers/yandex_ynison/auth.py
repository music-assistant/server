"""
Yandex Passport authentication helpers.

Thin wrappers over the shared Music Assistant auth layer in
``ya_passport_auth.ma``. Two helpers are exposed to the rest of the plugin:

* :func:`perform_qr_auth` — full QR login (UI popup → tokens), used by the
  own-mode config flow when the user clicks "Login with QR code".
* :func:`refresh_music_token` — exchange ``x_token`` for a fresh music-scoped
  OAuth token. Called both in borrow mode (against the linked yandex_music
  provider's x_token) and in own mode (against the plugin's own stored
  x_token, when the user opted in to "Remember session").
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# PassportClient is re-imported here (not used directly) so the test-suite's
# established patch target `<this module>.PassportClient.create` keeps
# working — patching the classmethod mutates the class object shared with
# the ya_passport_auth.ma flows.
from ya_passport_auth import PassportClient  # noqa: F401
from ya_passport_auth.ma import refresh_music_token as _refresh_music_token
from ya_passport_auth.ma import require_music_token, run_qr_flow

if TYPE_CHECKING:
    from ya_passport_auth import SecretStr

    from music_assistant.mass import MusicAssistant


async def perform_qr_auth(mass: MusicAssistant, session_id: str) -> tuple[str, str, str | None]:
    """
    Run a QR login flow and return ``(x_token, music_token, display_login)``.

    Opens the QR popup in the MA frontend, polls the Yandex Passport
    endpoint until the user confirms the scan in the Yandex app, then
    returns the resulting tokens as plain strings (suitable for MA config
    storage).

    ``display_login`` is the Yandex login name when the server returns it
    (used by the config UI to render "Logged in as X"); may be ``None``.
    """
    result = await run_qr_flow(mass, session_id)
    creds = result.credentials
    music_token = require_music_token(creds, flow="QR")
    return (creds.x_token.get_secret(), music_token, result.display_login or None)


async def refresh_music_token(x_token: SecretStr) -> SecretStr:
    """
    Exchange an x_token for a fresh music-scoped OAuth token.

    Transient Passport failures (network, rate limiting) raise
    ``ResourceTemporarilyUnavailable`` so callers retry later instead of
    treating a hiccup as dead credentials; only an explicit rejection
    raises ``LoginFailed``.
    """
    return await _refresh_music_token(x_token)
