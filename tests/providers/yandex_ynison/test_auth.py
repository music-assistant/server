"""Unit tests for provider/auth.py (ya-passport-auth wrapper)."""

from __future__ import annotations

from unittest import mock

import pytest
from music_assistant_models.errors import LoginFailed
from ya_passport_auth import SecretStr
from ya_passport_auth.exceptions import InvalidCredentialsError

from music_assistant.providers.yandex_ynison.auth import refresh_music_token

# ---------------------------------------------------------------
# refresh_music_token
# ---------------------------------------------------------------


async def test_refresh_music_token_success() -> None:
    """Successful refresh returns a SecretStr."""
    mock_client = mock.AsyncMock()
    mock_client.refresh_music_token.return_value = SecretStr("new_music_token")

    with mock.patch(
        "music_assistant.providers.yandex_ynison.auth.PassportClient.create"
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
        "music_assistant.providers.yandex_ynison.auth.PassportClient.create"
    ) as mock_create:
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        with pytest.raises(LoginFailed, match="Music token refresh was rejected"):
            await refresh_music_token(SecretStr("bad_x_token"))
