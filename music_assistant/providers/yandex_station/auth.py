"""
Yandex Passport authentication flows.

Thin provider-side wrappers over the shared Music Assistant auth layer in
``ya_passport_auth.ma`` (cookie login, token maintenance with unified error
mapping). This module only pins the tuple-shaped return values the config
flow expects.

One user-facing login path:

* **Cookies** — :func:`login_with_cookies` accepts a JSON array or
  raw cookie string exported from the browser. Yields
  ``(x_token, music_token)``.

Token maintenance helpers (:func:`refresh_music_token`,
:func:`refresh_credentials_via_passport`, :func:`validate_x_token`) live
alongside the login flow.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

# PassportClient is re-imported here (not used directly) so the test-suite's
# established patch target `<this module>.PassportClient.create` keeps
# working — patching the classmethod mutates the class object shared with
# the ya_passport_auth.ma flows.
from ya_passport_auth import PassportClient  # noqa: F401
from ya_passport_auth.ma import (
    login_with_cookies as _login_with_cookies,
)
from ya_passport_auth.ma import (
    refresh_credentials as _refresh_credentials,
)
from ya_passport_auth.ma import (
    refresh_music_token as _refresh_music_token,
)
from ya_passport_auth.ma import require_music_token
from ya_passport_auth.ma import (
    validate_x_token as _validate_x_token,
)

if TYPE_CHECKING:
    from ya_passport_auth import Credentials, SecretStr

_LOGGER = logging.getLogger(__name__)


async def login_with_cookies(cookies_input: str) -> tuple[str, str]:
    """
    Authenticate using browser cookies from passport.yandex.ru.

    Supports two formats:
    - JSON from "Copy Cookies" Chrome extension: [{"name":"...", "value":"...", "domain":"..."}]
    - Raw cookie string: "key1=value1; key2=value2"

    Returns (x_token, music_token).
    """
    creds = await _login_with_cookies(cookies_input)
    music_token = require_music_token(creds, flow="Cookie")
    _LOGGER.debug("Cookie auth complete, obtained both tokens")
    return creds.x_token.get_secret(), music_token


async def refresh_music_token(x_token: SecretStr) -> SecretStr:
    """
    Exchange an x_token for a fresh music-scoped OAuth token.

    :param x_token: Long-lived Yandex Passport session token.
    :returns: A fresh music-scoped OAuth token.
    :raises ResourceTemporarilyUnavailable: On transient failures (network,
        rate limit) — callers should retry later instead of clearing
        credentials.
    :raises LoginFailed: On real credential failures (x_token
        expired or rejected).
    """
    return await _refresh_music_token(x_token)


async def refresh_credentials_via_passport(
    x_token: SecretStr, refresh_token: SecretStr
) -> Credentials:
    """
    Silently re-issue the full credential triple using a refresh token.

    Only available for accounts authenticated via the Device Flow (QR
    and cookies login do not yield a ``refresh_token``). Rotates both
    ``x_token`` and ``refresh_token`` server-side, so callers must
    persist the returned Credentials.

    :param x_token: Current long-lived Yandex Passport session token.
    :param refresh_token: Refresh token issued during Device Flow.
    :returns: New Credentials with rotated ``x_token`` and
        ``refresh_token``.
    :raises ResourceTemporarilyUnavailable: On transient failures (network,
        rate limit) — callers should retry later instead of clearing
        credentials.
    :raises LoginFailed: On real credential failures (refresh_token
        rejected).
    """
    return await _refresh_credentials(x_token, refresh_token)


async def validate_x_token(x_token: SecretStr) -> bool:
    """
    Return True if *x_token* is still accepted by Yandex Passport.

    A ``False`` return signals "rejected by Passport" — a terminal
    credential failure. Transient network or rate-limit errors are
    re-raised so callers can distinguish them from invalid credentials
    and avoid clearing a good token on a temporary outage.

    :raises NetworkError: Transient network failure reaching Passport.
    :raises RateLimitedError: Passport returned 429.
    """
    return await _validate_x_token(x_token)
