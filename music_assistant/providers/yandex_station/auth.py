"""
Yandex Passport authentication flows.

Thin provider-side wrappers over the shared Music Assistant auth layer in
``ya_passport_auth.ma`` (device-code page, QR flow, cookie login, token
maintenance with unified error mapping). This module only pins the
provider-specific page branding and the tuple-shaped return values the
config flow expects.

Three user-facing login paths:

* **Device Flow** — :func:`perform_device_auth` serves a short user code on an
  MA-hosted intermediate page and polls Passport until confirmation. Yields
  the full ``(x_token, music_token, refresh_token)`` triple so the provider
  can silently refresh credentials when the music token (or x_token) expires.
* **QR flow** — :func:`perform_qr_auth` opens a QR popup via the MA frontend
  and polls Passport until the user scans/confirms. Yields
  ``(x_token, music_token)``.
* **Cookies fallback** — :func:`login_with_cookies` accepts a JSON array or
  raw cookie string exported from the browser. Yields
  ``(x_token, music_token)``.

Token maintenance helpers (:func:`refresh_music_token`,
:func:`refresh_credentials_via_passport`, :func:`validate_x_token`) live
alongside the login flows.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from music_assistant_models.errors import LoginFailed

# PassportClient is re-imported here (not used directly) so the test-suite's
# established patch target `<this module>.PassportClient.create` keeps
# working — patching the classmethod mutates the class object shared with
# the ya_passport_auth.ma flows.
from ya_passport_auth import PassportClient  # noqa: F401
from ya_passport_auth.ma import (
    DevicePageConfig,
    require_music_token,
    run_device_flow,
    run_qr_flow,
)
from ya_passport_auth.ma import (
    login_with_cookies as _login_with_cookies,
)
from ya_passport_auth.ma import (
    refresh_credentials as _refresh_credentials,
)
from ya_passport_auth.ma import (
    refresh_music_token as _refresh_music_token,
)
from ya_passport_auth.ma import (
    validate_x_token as _validate_x_token,
)

if TYPE_CHECKING:
    from ya_passport_auth import Credentials, SecretStr

    from music_assistant import MusicAssistant

_LOGGER = logging.getLogger(__name__)

# Provider-specific branding of the shared device-code page; everything else
# (layout, localization, countdown, failure reasons) comes from the library
# and is translated via this provider's strings.json (page.device_code.*).
PAGE_CONFIG = DevicePageConfig(
    domain="yandex_station",
    title={"en": "Login to Yandex Station", "ru": "Вход в Яндекс Станцию"},
)


async def perform_device_auth(mass: MusicAssistant, session_id: str) -> tuple[str, str, str]:
    """
    Perform Yandex OAuth Device Flow and return credential tokens.

    Asks Yandex for a device code, presents it to the user via an
    MA-hosted intermediate page, then polls until the user confirms or the
    code expires. Returns (or raises) as soon as the outcome is known.

    Returns (x_token, music_token, refresh_token) as plain strings for MA
    config storage.
    """
    result = await run_device_flow(mass, session_id, PAGE_CONFIG)
    creds = result.credentials
    music_token = require_music_token(creds, flow="Device")
    refresh_token = creds.refresh_token
    if refresh_token is None:
        raise LoginFailed("Device auth succeeded but no refresh token was returned")
    _LOGGER.debug("Device flow complete, obtained full credential triple")
    return (creds.x_token.get_secret(), music_token, refresh_token.get_secret())


async def perform_qr_auth(mass: MusicAssistant, session_id: str) -> tuple[str, str]:
    """
    Perform full QR authentication flow.

    Opens a QR code popup via MA frontend, polls for scan confirmation,
    then returns tokens as plain strings for MA config storage.

    Returns (x_token, music_token).
    """
    result = await run_qr_flow(mass, session_id)
    creds = result.credentials
    music_token = require_music_token(creds, flow="QR")
    _LOGGER.debug("QR auth complete, obtained both tokens")
    return creds.x_token.get_secret(), music_token


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
