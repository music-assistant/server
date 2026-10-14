"""Unit tests for auth.py cookie login and token-maintenance wrappers."""

from __future__ import annotations

import json
from unittest import mock

import pytest
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    ResourceTemporarilyUnavailable,
)
from ya_passport_auth import Credentials, SecretStr
from ya_passport_auth.exceptions import (
    InvalidCredentialsError,
    RateLimitedError,
    YaPassportError,
)
from ya_passport_auth.exceptions import NetworkError as PassportNetworkError

from music_assistant.providers.yandex_station.auth import (
    login_with_cookies,
    refresh_credentials_via_passport,
    refresh_music_token,
    validate_x_token,
)

_MOD = "music_assistant.providers.yandex_station.auth"


def _make_credentials(
    x_token: str = "test_x_token",  # noqa: S107
    music_token: str | None = "test_music_token",  # noqa: S107
    refresh_token: str | None = "test_refresh_token",  # noqa: S107
) -> Credentials:
    """Build credentials for a wrapper-boundary test."""
    return Credentials(
        x_token=SecretStr(x_token),
        music_token=SecretStr(music_token) if music_token else None,
        refresh_token=SecretStr(refresh_token) if refresh_token else None,
    )


async def test_refresh_music_token_success() -> None:
    """Successful refresh returns the new token."""
    client = mock.AsyncMock()
    client.refresh_music_token.return_value = SecretStr("new_music_token")
    with mock.patch(f"{_MOD}.PassportClient.create") as create:
        create.return_value.__aenter__ = mock.AsyncMock(return_value=client)
        create.return_value.__aexit__ = mock.AsyncMock(return_value=False)
        result = await refresh_music_token(SecretStr("my_x_token"))
    assert result.get_secret() == "new_music_token"


async def test_refresh_music_token_auth_error_raises_login_failed() -> None:
    """Rejected x-token is surfaced as a terminal login failure."""
    client = mock.AsyncMock()
    client.refresh_music_token.side_effect = InvalidCredentialsError("bad token")
    with mock.patch(f"{_MOD}.PassportClient.create") as create:
        create.return_value.__aenter__ = mock.AsyncMock(return_value=client)
        create.return_value.__aexit__ = mock.AsyncMock(return_value=False)
        with pytest.raises(LoginFailed, match="Music token refresh was rejected"):
            await refresh_music_token(SecretStr("bad_x_token"))


@pytest.mark.parametrize(
    "transient_error",
    [PassportNetworkError("socket reset"), RateLimitedError("429")],
)
async def test_refresh_music_token_preserves_transient_failures(
    transient_error: Exception,
) -> None:
    """Network and rate-limit failures remain retryable."""
    client = mock.AsyncMock()
    client.refresh_music_token.side_effect = transient_error
    with mock.patch(f"{_MOD}.PassportClient.create") as create:
        create.return_value.__aenter__ = mock.AsyncMock(return_value=client)
        create.return_value.__aexit__ = mock.AsyncMock(return_value=False)
        with pytest.raises(ResourceTemporarilyUnavailable):
            await refresh_music_token(SecretStr("x_token"))


async def test_validate_x_token_valid() -> None:
    """Accepted x-token returns true."""
    client = mock.AsyncMock()
    client.validate_x_token.return_value = True
    with mock.patch(f"{_MOD}.PassportClient.create") as create:
        create.return_value.__aenter__ = mock.AsyncMock(return_value=client)
        create.return_value.__aexit__ = mock.AsyncMock(return_value=False)
        result = await validate_x_token(SecretStr("good_token"))
    assert result is True


async def test_validate_x_token_rejection_returns_false() -> None:
    """Terminal Passport rejection returns false."""
    client = mock.AsyncMock()
    client.validate_x_token.side_effect = YaPassportError("rejected")
    with mock.patch(f"{_MOD}.PassportClient.create") as create:
        create.return_value.__aenter__ = mock.AsyncMock(return_value=client)
        create.return_value.__aexit__ = mock.AsyncMock(return_value=False)
        result = await validate_x_token(SecretStr("some_token"))
    assert result is False


async def test_refresh_credentials_returns_rotated_triple() -> None:
    """Refresh-token rotation returns all new credentials."""
    credentials = _make_credentials("new_x", "new_music", "new_refresh")
    client = mock.AsyncMock()
    client.refresh_credentials.return_value = credentials
    with mock.patch(f"{_MOD}.PassportClient.create") as create:
        create.return_value.__aenter__ = mock.AsyncMock(return_value=client)
        create.return_value.__aexit__ = mock.AsyncMock(return_value=False)
        result = await refresh_credentials_via_passport(
            SecretStr("old_x"), SecretStr("old_refresh")
        )
    assert result.x_token.get_secret() == "new_x"
    assert result.music_token is not None
    assert result.music_token.get_secret() == "new_music"
    assert result.refresh_token is not None
    assert result.refresh_token.get_secret() == "new_refresh"


async def test_refresh_credentials_rejection_raises_login_failed() -> None:
    """Rejected refresh token is surfaced as a terminal login failure."""
    client = mock.AsyncMock()
    client.refresh_credentials.side_effect = InvalidCredentialsError("dead")
    with mock.patch(f"{_MOD}.PassportClient.create") as create:
        create.return_value.__aenter__ = mock.AsyncMock(return_value=client)
        create.return_value.__aexit__ = mock.AsyncMock(return_value=False)
        with pytest.raises(LoginFailed, match="Credential refresh was rejected"):
            await refresh_credentials_via_passport(SecretStr("bad_x"), SecretStr("bad_refresh"))


@pytest.mark.parametrize(
    "transient_error",
    [PassportNetworkError("socket reset"), RateLimitedError("429")],
)
async def test_refresh_credentials_preserves_transient_failures(
    transient_error: Exception,
) -> None:
    """Transient rotation failures remain retryable."""
    client = mock.AsyncMock()
    client.refresh_credentials.side_effect = transient_error
    with mock.patch(f"{_MOD}.PassportClient.create") as create:
        create.return_value.__aenter__ = mock.AsyncMock(return_value=client)
        create.return_value.__aexit__ = mock.AsyncMock(return_value=False)
        with pytest.raises(ResourceTemporarilyUnavailable):
            await refresh_credentials_via_passport(SecretStr("x"), SecretStr("r"))


@pytest.mark.parametrize(
    ("cookies", "x_token", "music_token"),
    [
        ("Session_id=abc123; yandexuid=456", "cookie_x", "cookie_music"),
        (
            json.dumps(
                [
                    {"name": "Session_id", "value": "abc123", "domain": ".yandex.ru"},
                    {"name": "yandexuid", "value": "456", "domain": ".yandex.ru"},
                ]
            ),
            "json_x",
            "json_music",
        ),
    ],
)
async def test_login_with_cookies_returns_tokens(
    cookies: str, x_token: str, music_token: str
) -> None:
    """Raw and JSON cookie inputs produce the provider token pair."""
    client = mock.AsyncMock()
    client.login_cookies.return_value = _make_credentials(x_token, music_token)
    with mock.patch(f"{_MOD}.PassportClient.create") as create:
        create.return_value.__aenter__ = mock.AsyncMock(return_value=client)
        create.return_value.__aexit__ = mock.AsyncMock(return_value=False)
        result = await login_with_cookies(cookies)
    assert result == (x_token, music_token)


@pytest.mark.parametrize("cookies", ["", "no_equals_sign_here"])
async def test_login_with_cookies_rejects_invalid_input(cookies: str) -> None:
    """Malformed cookie input fails validation before authentication."""
    with pytest.raises(InvalidDataError):
        await login_with_cookies(cookies)


async def test_login_with_cookies_rejection_raises_login_failed() -> None:
    """Rejected cookies are surfaced as a terminal login failure."""
    client = mock.AsyncMock()
    client.login_cookies.side_effect = InvalidCredentialsError("bad cookies")
    with mock.patch(f"{_MOD}.PassportClient.create") as create:
        create.return_value.__aenter__ = mock.AsyncMock(return_value=client)
        create.return_value.__aexit__ = mock.AsyncMock(return_value=False)
        with pytest.raises(LoginFailed, match="Cookie authentication"):
            await login_with_cookies("Session_id=expired; yandexuid=456")
