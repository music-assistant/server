"""
Music-Assistant Device-Flow authenticator for ya-dialogs-api.

Adapter that wraps the shared ``ya_passport_auth.ma`` Device Flow behind
the :data:`ya_dialogs_api.AuthenticatorCM` Protocol — a no-arg
async-context-manager factory yielding an authorized
``aiohttp.ClientSession``.

UX:
- The MA-hosted activation page (localized, tap-to-copy code, honest
  countdown, failure reasons) comes from ``ya_passport_auth.ma``; Yandex's
  ya.ru/device strips query params on the redirect-to-login, so the code
  cannot be pre-filled there.
- The page opens in a popup via ``music_assistant.helpers.auth.AuthenticationHelper``.

Cache fast-path:
- If a valid cached ``x_token`` is provided, we skip Device Flow entirely
  and call ``refresh_passport_cookies`` directly. On any failure during
  refresh we fall back to a fresh Device Flow.

The dialogs API session keeps its own :class:`ya_passport_auth.PassportClient`
(with ``dialogs.yandex.ru`` in the allowed hosts) — the login flow only
supplies the credentials; the cookies are then minted into this client.
"""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    import aiohttp
    from ya_dialogs_api import AuthenticatorCM

    from music_assistant.mass import MusicAssistant


_LOGGER = logging.getLogger(__name__)

# Hard cap on how long we'll wait for the user to enter the code.
DEVICE_FLOW_TIMEOUT_SECONDS = 300.0

_SAFE_SESSION_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")


def make_authenticator(
    *,
    mass: MusicAssistant,
    session_id: str,
    timeout: float = DEVICE_FLOW_TIMEOUT_SECONDS,
    cached_x_token: str | None = None,
    on_token_obtained: Callable[[str], None] | None = None,
    allow_device_flow: bool = True,
) -> AuthenticatorCM:
    """
    Build an :data:`AuthenticatorCM` for ``ya_dialogs_api.auto_create_skill``.

    The returned no-arg callable produces an ``aiohttp.ClientSession``
    context manager. On ``__aenter__`` it either:

    1. Reuses ``cached_x_token`` (``refresh_passport_cookies`` fast-path), or
    2. Runs the full Device Flow via the shared ``ya_passport_auth.ma``
       layer (MA-hosted activation page + ``AuthenticationHelper`` popup),
       then refreshes passport cookies.

    After a successful Device Flow, ``on_token_obtained(x_token_str)`` is
    called so the caller can persist the new token for the next run.
    Callback failures are logged but never break authentication.

    :param mass: MusicAssistant runtime — used for ``mass.webserver``
        route registration and ``AuthenticationHelper`` popup
        management.
    :param session_id: Frontend-supplied session id (matches what
        ``AuthenticationHelper`` listens on for popup open/close).
        Must be safe for URL paths.
    :param timeout: Hard cap on Device Flow polling (seconds). Default
        5 min.
    :param cached_x_token: Optional Yandex Passport ``x_token`` from a
        prior Device Flow. If still valid, skips Device Flow entirely.
    :param on_token_obtained: Optional callback invoked with the fresh
        ``x_token`` (plain ``str``, unwrapped from ``SecretStr``)
        after a successful Device Flow. Use to persist into MA config
        so the next run can use the cache.
    :param allow_device_flow: When False (borrowed credentials from a
        linked Yandex Music instance), a missing or rejected
        ``cached_x_token`` raises ``LoginFailed`` with re-authentication
        guidance instead of falling back to a Device Flow — a fallback
        would create the second token family borrow mode exists to avoid.
    :raises ValueError: ``session_id`` doesn't match the safe
        character set.
    """
    if not _SAFE_SESSION_ID_RE.match(session_id):
        msg = "invalid session_id for device authentication"
        raise ValueError(msg)

    @asynccontextmanager
    async def _cm() -> AsyncIterator[aiohttp.ClientSession]:
        # Imports kept inline so the module can be imported without MA in test envs.
        from ya_passport_auth import ClientConfig, PassportClient  # noqa: PLC0415
        from ya_passport_auth.config import DEFAULT_ALLOWED_HOSTS  # noqa: PLC0415
        from ya_passport_auth.credentials import SecretStr as PpSecretStr  # noqa: PLC0415
        from ya_passport_auth.ma import DevicePageConfig, run_device_flow  # noqa: PLC0415

        allowed = DEFAULT_ALLOWED_HOSTS | frozenset({"dialogs.yandex.ru"})
        config = ClientConfig(allowed_hosts=allowed)

        async with PassportClient.create(config=config) as client:
            # Cache fast-path: try cached x_token first. If the token is
            # still valid Yandex returns fresh session cookies and we skip
            # Device Flow.
            if cached_x_token:
                try:
                    await client.refresh_passport_cookies(PpSecretStr(cached_x_token))
                    _LOGGER.info(
                        "auto-skill: reused cached Yandex Passport x_token (no Device Flow needed)"
                    )
                    yield client._session
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not allow_device_flow:
                        from music_assistant_models.errors import LoginFailed  # noqa: PLC0415

                        raise LoginFailed(
                            "The linked Yandex Music account's session token was "
                            "rejected by Yandex. Re-authenticate the Yandex Music "
                            "provider and retry."
                        ) from exc
                    _LOGGER.info(
                        "auto-skill: cached x_token rejected (%s) — "
                        "falling back to fresh Device Flow",
                        exc,
                    )

            if not allow_device_flow:
                from music_assistant_models.errors import LoginFailed  # noqa: PLC0415

                raise LoginFailed(
                    "The linked Yandex Music account has no session token. "
                    "Authenticate the Yandex Music provider (with Remember "
                    "session enabled) and retry."
                )

            # Device Flow via the shared layer: it hosts the activation page
            # under /yandex_smarthome/device_code/<session_id>, opens the
            # popup and polls until confirmation.
            _LOGGER.warning(
                "auto-skill: opening device-code popup at "
                "/yandex_smarthome/device_code/%s — if the popup does not "
                "open or points at an unreachable address, open the path "
                "directly in your browser (the page displays the user_code) "
                "or fix Settings → Core → Webserver → Base URL",
                session_id,
            )
            result = await run_device_flow(
                mass,
                session_id,
                DevicePageConfig(
                    domain="yandex_smarthome",
                    title={"en": "Login to Yandex Smart Home", "ru": "Вход в Яндекс Умный Дом"},
                ),
                total_timeout=timeout,
            )
            creds = result.credentials

            await client.refresh_passport_cookies(creds.x_token)

            # Persist the new x_token so subsequent auto-create runs can skip
            # Device Flow. Best-effort: a callback failure must not break auth.
            # Unwrap SecretStr → str so the callback can store it via the MA
            # config plumbing (SECURE_STRING serialiser expects a plain str).
            if on_token_obtained is not None:
                try:
                    on_token_obtained(creds.x_token.get_secret())
                except Exception:
                    _LOGGER.exception(
                        "auto-skill: on_token_obtained callback failed; x_token will not be cached"
                    )

            yield client._session

    return _cm
