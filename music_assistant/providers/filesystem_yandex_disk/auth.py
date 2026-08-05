"""OAuth Device Flow and token maintenance for Yandex Disk."""

from __future__ import annotations

import secrets
import string
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

import aiohttp
from music_assistant_models.errors import LoginFailed, ProviderUnavailableError

from .constants import OAUTH_DEVICE_CODE_URL, OAUTH_SCOPE, OAUTH_TOKEN_URL

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant import MusicAssistant

_DEVICE_ID_ALPHABET = string.ascii_letters + string.digits


class OAuthProtocolError(Exception):
    """Yandex returned an invalid or terminal OAuth response."""


class OAuthTransportError(Exception):
    """Yandex OAuth is temporarily unavailable."""


class DeviceCodeExpired(OAuthProtocolError):
    """The active Device Flow code expired."""


class DeviceCodeDenied(OAuthProtocolError):
    """The user denied the active Device Flow request."""


class DevicePollState(StrEnum):
    """Non-terminal Device Flow polling states."""

    PENDING = "authorization_pending"
    SLOW_DOWN = "slow_down"


@dataclass(frozen=True, slots=True, repr=False)
class DeviceCodeGrant:
    """Active Yandex Device Flow grant."""

    device_code: str
    user_code: str
    verification_url: str
    expires_in: int
    interval: int

    def __repr__(self) -> str:
        """Return a representation without either device code."""
        return (
            f"DeviceCodeGrant(verification_url={self.verification_url!r}, "
            f"expires_in={self.expires_in}, interval={self.interval})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class OAuthTokens:
    """Access and refresh tokens returned by Yandex OAuth."""

    access_token: str
    refresh_token: str
    expires_in: int

    def __repr__(self) -> str:
        """Return a representation without token values."""
        return f"OAuthTokens(access_token='***', refresh_token='***', expires_in={self.expires_in})"


async def request_device_code(
    session: aiohttp.ClientSession,
    client_id: str,
) -> DeviceCodeGrant:
    """
    Start Yandex OAuth Device Flow.

    :param session: Shared Music Assistant HTTP session.
    :param client_id: User-owned Yandex OAuth application ID.
    """
    status, payload = await _post_oauth_json(
        session,
        OAUTH_DEVICE_CODE_URL,
        {
            "client_id": client_id,
            "device_id": "".join(secrets.choice(_DEVICE_ID_ALPHABET) for _ in range(10)),
            "device_name": "Music Assistant - Yandex Disk",
            "scope": OAUTH_SCOPE,
        },
    )
    if status >= 400 or isinstance(payload.get("error"), str):
        error = payload.get("error")
        suffix = f" ({error})" if isinstance(error, str) else ""
        raise OAuthProtocolError(f"Yandex rejected the device-code request{suffix}")

    device_code = payload.get("device_code")
    user_code = payload.get("user_code")
    verification_url = payload.get("verification_url")
    expires_in = payload.get("expires_in")
    interval = payload.get("interval")
    if (
        not isinstance(device_code, str)
        or not device_code
        or not isinstance(user_code, str)
        or not user_code
        or not isinstance(verification_url, str)
        or not verification_url
        or isinstance(expires_in, bool)
        or not isinstance(expires_in, int)
        or expires_in <= 0
        or isinstance(interval, bool)
        or not isinstance(interval, int)
        or interval <= 0
    ):
        raise OAuthProtocolError("Yandex returned an invalid device-code response")
    return DeviceCodeGrant(
        device_code=device_code,
        user_code=user_code,
        verification_url=verification_url,
        expires_in=expires_in,
        interval=interval,
    )


async def poll_device_token(
    session: aiohttp.ClientSession,
    grant: DeviceCodeGrant,
    client_id: str,
    client_secret: str,
) -> OAuthTokens | DevicePollState:
    """
    Poll Yandex once for completion of a Device Flow grant.

    :param session: Shared Music Assistant HTTP session.
    :param grant: Active Device Flow grant.
    :param client_id: User-owned Yandex OAuth application ID.
    :param client_secret: User-owned Yandex OAuth application secret.
    """
    _status, payload = await _post_oauth_json(
        session,
        OAUTH_TOKEN_URL,
        {
            "grant_type": "device_code",
            "code": grant.device_code,
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    if "access_token" in payload:
        return _parse_tokens(payload)
    error = payload.get("error")
    if error == DevicePollState.PENDING:
        return DevicePollState.PENDING
    if error == DevicePollState.SLOW_DOWN:
        return DevicePollState.SLOW_DOWN
    if error == "expired_token":
        raise DeviceCodeExpired("Yandex device code expired")
    if error == "access_denied":
        raise DeviceCodeDenied("Yandex device login was denied")
    suffix = f" ({error})" if isinstance(error, str) else ""
    raise OAuthProtocolError(f"Yandex returned an invalid device-token response{suffix}")


async def refresh_oauth_tokens(
    session: aiohttp.ClientSession,
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> OAuthTokens:
    """
    Exchange a refresh token for a new Yandex OAuth token pair.

    :param session: Shared Music Assistant HTTP session.
    :param client_id: User-owned Yandex OAuth application ID.
    :param client_secret: User-owned Yandex OAuth application secret.
    :param refresh_token: Previously issued refresh token.
    """
    try:
        _status, payload = await _post_oauth_json(
            session,
            OAUTH_TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
    except OAuthTransportError as err:
        raise ProviderUnavailableError("Yandex OAuth is temporarily unavailable") from err

    if payload.get("error") == "invalid_grant":
        raise LoginFailed("Yandex Disk authorization was revoked or expired")
    if isinstance(payload.get("error"), str):
        raise ProviderUnavailableError("Yandex OAuth returned a temporary error")
    try:
        return _parse_tokens(payload)
    except OAuthProtocolError as err:
        raise ProviderUnavailableError("Yandex OAuth returned an invalid response") from err


class MAYandexDiskAuth:
    """Provide Yandex Disk access tokens using a stored refresh token."""

    def __init__(
        self,
        mass: MusicAssistant,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        persist_refresh_token: Callable[[str], None] | None = None,
    ) -> None:
        """
        Initialize the auth helper.

        :param mass: Music Assistant instance.
        :param client_id: User-owned Yandex OAuth application ID.
        :param client_secret: User-owned Yandex OAuth application secret.
        :param refresh_token: Stored refresh token.
        :param persist_refresh_token: Callback for a rotated refresh token.
        """
        self.mass = mass
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._persist_refresh_token = persist_refresh_token
        self._access_token: str | None = None
        self._expires_at = 0.0

    async def async_get_access_token(self) -> str:
        """Return a currently valid access token."""
        if not self._refresh_token:
            raise LoginFailed("Yandex Disk is not authorized")
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        return await self._refresh()

    async def _refresh(self) -> str:
        """Refresh and cache the access token."""
        tokens = await refresh_oauth_tokens(
            self.mass.http_session,
            self._client_id,
            self._client_secret,
            self._refresh_token,
        )
        self._access_token = tokens.access_token
        self._expires_at = time.time() + tokens.expires_in
        if tokens.refresh_token != self._refresh_token:
            self._refresh_token = tokens.refresh_token
            if self._persist_refresh_token is not None:
                self._persist_refresh_token(tokens.refresh_token)
        return tokens.access_token


async def _post_oauth_json(
    session: aiohttp.ClientSession,
    url: str,
    data: dict[str, str],
) -> tuple[int, dict[str, object]]:
    try:
        async with session.post(url, data=data) as response:
            status = response.status
            try:
                payload = await response.json(content_type=None)
            except (aiohttp.ClientError, ValueError) as err:
                if status == 429 or status >= 500:
                    raise OAuthTransportError("Yandex OAuth returned an HTTP error") from err
                raise OAuthProtocolError("Yandex OAuth returned malformed JSON") from err
    except OAuthProtocolError, OAuthTransportError:
        raise
    except (aiohttp.ClientError, TimeoutError) as err:
        raise OAuthTransportError("Unable to reach Yandex OAuth") from err
    if status == 429 or status >= 500:
        raise OAuthTransportError("Yandex OAuth returned an HTTP error")
    if not isinstance(payload, dict):
        raise OAuthProtocolError("Yandex OAuth returned a non-object response")
    return status, payload


def _parse_tokens(payload: dict[str, object]) -> OAuthTokens:
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    expires_in = payload.get("expires_in")
    if (
        not isinstance(access_token, str)
        or not access_token
        or not isinstance(refresh_token, str)
        or not refresh_token
        or isinstance(expires_in, bool)
        or not isinstance(expires_in, int)
        or expires_in <= 0
    ):
        raise OAuthProtocolError("Yandex OAuth returned an invalid token response")
    return OAuthTokens(access_token, refresh_token, expires_in)
