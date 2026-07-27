"""
Yandex Passport authentication helpers.

Thin wrappers over the shared Music Assistant auth layer in
``ya_passport_auth.ma``. One helper is exposed to the rest of the plugin:

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

if TYPE_CHECKING:
    from ya_passport_auth import SecretStr


async def refresh_music_token(x_token: SecretStr) -> SecretStr:
    """
    Exchange an x_token for a fresh music-scoped OAuth token.

    Transient Passport failures (network, rate limiting) raise
    ``ResourceTemporarilyUnavailable`` so callers retry later instead of
    treating a hiccup as dead credentials; only an explicit rejection
    raises ``LoginFailed``.
    """
    return await _refresh_music_token(x_token)
