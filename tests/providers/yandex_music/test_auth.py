"""Unit tests for auth.py (ya-passport-auth token maintenance)."""

from __future__ import annotations

from unittest import mock

import pytest
from music_assistant_models.errors import LoginFailed, ResourceTemporarilyUnavailable
from ya_passport_auth import Credentials, SecretStr
from ya_passport_auth.exceptions import InvalidCredentialsError, RateLimitedError
from ya_passport_auth.exceptions import (
    NetworkError as PassportNetworkError,
)

from music_assistant.providers.yandex_music.auth import (
    refresh_credentials_via_passport,
    refresh_music_token,
    validate_x_token,
)

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

    with mock.patch(
        "music_assistant.providers.yandex_music.auth.PassportClient.create",
    ) as mock_create:
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        result = await refresh_music_token(SecretStr("my_x_token"))

    assert result.get_secret() == "new_music_token"
    mock_client.refresh_music_token.assert_awaited_once()


async def test_refresh_music_token_auth_error_raises_login_failed() -> None:
    """Auth failure during refresh is mapped to LoginFailed."""
    mock_client = mock.AsyncMock()
    mock_client.refresh_music_token.side_effect = InvalidCredentialsError("bad token")

    with mock.patch(
        "music_assistant.providers.yandex_music.auth.PassportClient.create",
    ) as mock_create:
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        with pytest.raises(LoginFailed, match="Music token refresh was rejected"):
            await refresh_music_token(SecretStr("bad_x_token"))


@pytest.mark.parametrize(
    "exc",
    [PassportNetworkError("offline"), RateLimitedError("429")],
    ids=["network", "rate_limited"],
)
async def test_refresh_music_token_transient_error_raises_temporarily_unavailable(
    exc: Exception,
) -> None:
    """Transient Passport failures don't masquerade as LoginFailed."""
    mock_client = mock.AsyncMock()
    mock_client.refresh_music_token.side_effect = exc

    with mock.patch(
        "music_assistant.providers.yandex_music.auth.PassportClient.create",
    ) as mock_create:
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        with pytest.raises(ResourceTemporarilyUnavailable, match="temporarily unavailable"):
            await refresh_music_token(SecretStr("my_x_token"))


# -- validate_x_token ----------------------------------------------------------


async def test_validate_x_token_valid() -> None:
    """Valid x_token returns True."""
    mock_client = mock.AsyncMock()
    mock_client.validate_x_token.return_value = True

    with mock.patch(
        "music_assistant.providers.yandex_music.auth.PassportClient.create",
    ) as mock_create:
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        result = await validate_x_token(SecretStr("good_token"))

    assert result is True


async def test_validate_x_token_invalid_returns_false() -> None:
    """A terminal credential error returns False (token rejected by Passport)."""
    mock_client = mock.AsyncMock()
    mock_client.validate_x_token.side_effect = InvalidCredentialsError("token rejected")

    with mock.patch(
        "music_assistant.providers.yandex_music.auth.PassportClient.create",
    ) as mock_create:
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        result = await validate_x_token(SecretStr("some_token"))

    assert result is False


@pytest.mark.parametrize(
    "exc",
    [PassportNetworkError("offline"), RateLimitedError("429")],
    ids=["network", "rate_limited"],
)
async def test_validate_x_token_transient_error_propagates(exc: Exception) -> None:
    """
    Transient Passport failures must not masquerade as "token invalid".

    A network blip or 429 should not cause callers to clear the stored
    credential — re-raise so the caller can distinguish the two.
    """
    mock_client = mock.AsyncMock()
    mock_client.validate_x_token.side_effect = exc

    with mock.patch(
        "music_assistant.providers.yandex_music.auth.PassportClient.create",
    ) as mock_create:
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        with pytest.raises((PassportNetworkError, RateLimitedError)):
            await validate_x_token(SecretStr("some_token"))


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

    with mock.patch(
        "music_assistant.providers.yandex_music.auth.PassportClient.create",
    ) as mock_create:
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

    with mock.patch(
        "music_assistant.providers.yandex_music.auth.PassportClient.create",
    ) as mock_create:
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        with pytest.raises(LoginFailed, match="Credential refresh was rejected"):
            await refresh_credentials_via_passport(SecretStr("bad_x"), SecretStr("bad_refresh"))


@pytest.mark.parametrize(
    "exc",
    [PassportNetworkError("offline"), RateLimitedError("429")],
    ids=["network", "rate_limited"],
)
async def test_refresh_credentials_via_passport_transient_error_raises_temporarily_unavailable(
    exc: Exception,
) -> None:
    """Transient Passport failures don't masquerade as LoginFailed."""
    mock_client = mock.AsyncMock()
    mock_client.refresh_credentials.side_effect = exc

    with mock.patch(
        "music_assistant.providers.yandex_music.auth.PassportClient.create",
    ) as mock_create:
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        with pytest.raises(ResourceTemporarilyUnavailable, match="temporarily unavailable"):
            await refresh_credentials_via_passport(SecretStr("x"), SecretStr("refresh"))


# -- exception-message redaction ----------------------------------------------
#
# These tests guard against leaking the upstream ``ya-passport-auth`` exception
# payload into our own ``LoginFailed`` / ``ResourceTemporarilyUnavailable``
# messages. The library may include token fragments, device codes, or raw
# response bodies in its exception text — none of which should reach MA logs
# or the frontend.

_SECRET_PAYLOAD = "token=ABC_TOKEN_LEAK&csrf=xyz"


@pytest.mark.parametrize(
    ("exc", "expected_exc_type"),
    [
        (PassportNetworkError(_SECRET_PAYLOAD), ResourceTemporarilyUnavailable),
        (RateLimitedError(_SECRET_PAYLOAD), ResourceTemporarilyUnavailable),
        (InvalidCredentialsError(_SECRET_PAYLOAD), LoginFailed),
    ],
    ids=["network", "rate_limited", "invalid_credentials"],
)
async def test_refresh_music_token_error_does_not_leak_library_payload(
    exc: Exception, expected_exc_type: type[Exception]
) -> None:
    """``refresh_music_token`` exceptions must not include library str()."""
    mock_client = mock.AsyncMock()
    mock_client.refresh_music_token.side_effect = exc

    with mock.patch(
        "music_assistant.providers.yandex_music.auth.PassportClient.create",
    ) as mock_create:
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        with pytest.raises(expected_exc_type) as exc_info:
            await refresh_music_token(SecretStr("my_x_token"))

    assert _SECRET_PAYLOAD not in str(exc_info.value)
    assert "ABC_TOKEN_LEAK" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("exc", "expected_exc_type"),
    [
        (PassportNetworkError(_SECRET_PAYLOAD), ResourceTemporarilyUnavailable),
        (RateLimitedError(_SECRET_PAYLOAD), ResourceTemporarilyUnavailable),
        (InvalidCredentialsError(_SECRET_PAYLOAD), LoginFailed),
    ],
    ids=["network", "rate_limited", "invalid_credentials"],
)
async def test_refresh_credentials_via_passport_error_does_not_leak_library_payload(
    exc: Exception, expected_exc_type: type[Exception]
) -> None:
    """``refresh_credentials_via_passport`` exceptions must not include library str()."""
    mock_client = mock.AsyncMock()
    mock_client.refresh_credentials.side_effect = exc

    with mock.patch(
        "music_assistant.providers.yandex_music.auth.PassportClient.create",
    ) as mock_create:
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        with pytest.raises(expected_exc_type) as exc_info:
            await refresh_credentials_via_passport(SecretStr("x"), SecretStr("refresh"))

    assert _SECRET_PAYLOAD not in str(exc_info.value)
    assert "ABC_TOKEN_LEAK" not in str(exc_info.value)
