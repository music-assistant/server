"""Unit tests for auth.py (ya-passport-auth cookie login + token maintenance)."""

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
from ya_passport_auth.exceptions import (
    NetworkError as PassportNetworkError,
)

# Import via the namespace set up by conftest.py (avoids relative-import issues)
from music_assistant.providers.yandex_station.auth import (
    login_with_cookies,
    refresh_credentials_via_passport,
    refresh_music_token,
    validate_x_token,
)

# mock target prefix: the module as seen in sys.modules
_MOD = "music_assistant.providers.yandex_station.auth"


# -- helpers -------------------------------------------------------------------


def _make_credentials(
    x_token: str = "test_x_token",  # noqa: S107
    music_token: str | None = "test_music_token",  # noqa: S107
    refresh_token: str | None = "test_refresh_token",  # noqa: S107
) -> Credentials:
    """Build a Credentials dataclass for testing."""
    return Credentials(
        x_token=SecretStr(x_token),
        music_token=SecretStr(music_token) if music_token else None,
        refresh_token=SecretStr(refresh_token) if refresh_token else None,
    )


# -- refresh_music_token -------------------------------------------------------


async def test_refresh_music_token_success() -> None:
    """Successful refresh returns a SecretStr."""
    mock_client = mock.AsyncMock()
    mock_client.refresh_music_token.return_value = SecretStr("new_music_token")

    with mock.patch(f"{_MOD}.PassportClient.create") as mock_create:
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        result = await refresh_music_token(SecretStr("my_x_token"))

    assert result.get_secret() == "new_music_token"
    mock_client.refresh_music_token.assert_awaited_once()


async def test_refresh_music_token_auth_error_raises_login_failed() -> None:
    """Auth failure during refresh is mapped to LoginFailed."""
    mock_client = mock.AsyncMock()
    mock_client.refresh_music_token.side_effect = InvalidCredentialsError("bad token")

    with mock.patch(f"{_MOD}.PassportClient.create") as mock_create:
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        with pytest.raises(LoginFailed, match="Music token refresh was rejected"):
            await refresh_music_token(SecretStr("bad_x_token"))


@pytest.mark.parametrize(
    "transient_err",
    [PassportNetworkError("socket reset"), RateLimitedError("429")],
)
async def test_refresh_music_token_transient_raises_provider_unavailable(
    transient_err: Exception,
) -> None:
    """Network/rate-limit failures must NOT be mapped to LoginFailed (would wipe creds)."""
    mock_client = mock.AsyncMock()
    mock_client.refresh_music_token.side_effect = transient_err

    with mock.patch(f"{_MOD}.PassportClient.create") as mock_create:
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)
        with pytest.raises(ResourceTemporarilyUnavailable):
            await refresh_music_token(SecretStr("x_token"))


# -- validate_x_token ----------------------------------------------------------


async def test_validate_x_token_valid() -> None:
    """Valid x_token returns True."""
    mock_client = mock.AsyncMock()
    mock_client.validate_x_token.return_value = True

    with mock.patch(f"{_MOD}.PassportClient.create") as mock_create:
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        result = await validate_x_token(SecretStr("good_token"))

    assert result is True


async def test_validate_x_token_error_returns_false() -> None:
    """A terminal YaPassportError returns False; transient errors re-raise."""
    mock_client = mock.AsyncMock()
    mock_client.validate_x_token.side_effect = YaPassportError("rejected")

    with mock.patch(f"{_MOD}.PassportClient.create") as mock_create:
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        result = await validate_x_token(SecretStr("some_token"))

    assert result is False


# -- refresh_credentials_via_passport ------------------------------------------


async def test_refresh_credentials_via_passport_success() -> None:
    """Successful refresh returns full Credentials triple."""
    new_creds = _make_credentials(
        x_token="new_x",
        music_token="new_music",
        refresh_token="new_refresh",
    )
    mock_client = mock.AsyncMock()
    mock_client.refresh_credentials.return_value = new_creds

    with mock.patch(f"{_MOD}.PassportClient.create") as mock_create:
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        result = await refresh_credentials_via_passport(
            SecretStr("old_x"), SecretStr("old_refresh")
        )

    assert result.x_token.get_secret() == "new_x"
    assert result.music_token is not None
    assert result.music_token.get_secret() == "new_music"
    assert result.refresh_token is not None
    assert result.refresh_token.get_secret() == "new_refresh"
    mock_client.refresh_credentials.assert_awaited_once()


async def test_refresh_credentials_via_passport_error_raises_login_failed() -> None:
    """Auth failure during credential refresh is mapped to LoginFailed."""
    mock_client = mock.AsyncMock()
    mock_client.refresh_credentials.side_effect = InvalidCredentialsError("dead")

    with mock.patch(f"{_MOD}.PassportClient.create") as mock_create:
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        with pytest.raises(LoginFailed, match="Credential refresh was rejected"):
            await refresh_credentials_via_passport(SecretStr("bad_x"), SecretStr("bad_refresh"))


@pytest.mark.parametrize(
    "transient_err",
    [PassportNetworkError("socket reset"), RateLimitedError("429")],
)
async def test_refresh_credentials_via_passport_transient_raises_provider_unavailable(
    transient_err: Exception,
) -> None:
    """Network/rate-limit failures must NOT be mapped to LoginFailed (would wipe creds)."""
    mock_client = mock.AsyncMock()
    mock_client.refresh_credentials.side_effect = transient_err

    with mock.patch(f"{_MOD}.PassportClient.create") as mock_create:
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)
        with pytest.raises(ResourceTemporarilyUnavailable):
            await refresh_credentials_via_passport(SecretStr("x"), SecretStr("r"))


# -- login_with_cookies --------------------------------------------------------


async def test_login_with_cookies_raw_string() -> None:
    """Raw cookie string auth returns (x_token, music_token)."""
    creds = _make_credentials(x_token="cookie_x_token", music_token="cookie_music_token")

    mock_client = mock.AsyncMock()
    mock_client.login_cookies.return_value = creds

    with mock.patch(f"{_MOD}.PassportClient.create") as mock_create:
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        x_token, music_token = await login_with_cookies("Session_id=abc123; yandexuid=456")

    assert x_token == "cookie_x_token"
    assert music_token == "cookie_music_token"
    mock_client.login_cookies.assert_awaited_once_with("Session_id=abc123; yandexuid=456")


async def test_login_with_cookies_json_format() -> None:
    """JSON cookie array is converted to semicolon string and passed to library."""
    cookies_json = json.dumps(
        [
            {"name": "Session_id", "value": "abc123", "domain": ".yandex.ru"},
            {"name": "yandexuid", "value": "456", "domain": ".yandex.ru"},
        ]
    )

    creds = _make_credentials(x_token="json_x_token", music_token="json_music_token")

    mock_client = mock.AsyncMock()
    mock_client.login_cookies.return_value = creds

    with mock.patch(f"{_MOD}.PassportClient.create") as mock_create:
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        x_token, music_token = await login_with_cookies(cookies_json)

    assert x_token == "json_x_token"
    assert music_token == "json_music_token"
    mock_client.login_cookies.assert_awaited_once_with("Session_id=abc123; yandexuid=456")


async def test_login_with_cookies_empty_raises() -> None:
    """Empty cookie string raises InvalidDataError (validation failure)."""
    with pytest.raises(InvalidDataError, match="Empty cookies"):
        await login_with_cookies("")


async def test_login_with_cookies_invalid_format_raises() -> None:
    """Cookie string without '=' raises InvalidDataError (validation failure)."""
    with pytest.raises(InvalidDataError, match="Invalid cookie format"):
        await login_with_cookies("no_equals_sign_here")


async def test_login_with_cookies_auth_error_raises_login_failed() -> None:
    """InvalidCredentialsError from library is mapped to LoginFailed."""
    mock_client = mock.AsyncMock()
    mock_client.login_cookies.side_effect = InvalidCredentialsError("bad cookies")

    with mock.patch(f"{_MOD}.PassportClient.create") as mock_create:
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        with pytest.raises(LoginFailed, match="Cookie authentication"):
            await login_with_cookies("Session_id=expired; yandexuid=456")
