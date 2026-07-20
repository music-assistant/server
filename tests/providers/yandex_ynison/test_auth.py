"""Unit tests for provider/auth.py (ya-passport-auth wrapper)."""

from __future__ import annotations

from unittest import mock

import pytest
from music_assistant_models.errors import LoginFailed
from ya_passport_auth import SecretStr
from ya_passport_auth.exceptions import (
    InvalidCredentialsError,
    QRTimeoutError,
    YaPassportError,
)

from music_assistant.providers.yandex_ynison.auth import perform_qr_auth, refresh_music_token

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


# ---------------------------------------------------------------
# perform_qr_auth
# ---------------------------------------------------------------


def _make_creds(
    *,
    music_token: str | None = "music-tok",  # noqa: S107 — fixture stub, not a credential
    display_login: str | None = "alice",
) -> mock.MagicMock:
    """Build a stub Credentials object with a SecretStr-shaped x_token/music_token."""
    creds = mock.MagicMock()
    creds.x_token = SecretStr("x-tok")
    creds.music_token = SecretStr(music_token) if music_token is not None else None
    creds.display_login = display_login
    return creds


def _patched_passport(client: mock.AsyncMock) -> mock._patch[mock.MagicMock]:
    """Patch PassportClient.create() to yield the given mock client."""
    patcher = mock.patch("music_assistant.providers.yandex_ynison.auth.PassportClient.create")
    mock_create = patcher.start()
    mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=client)
    mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)
    return patcher


def _patched_auth_helper() -> tuple[mock._patch[mock.MagicMock], mock.MagicMock]:
    """
    Patch music_assistant.helpers.auth.AuthenticationHelper at its source.

    perform_qr_auth lazy-imports this symbol inside the function body, so the
    patch target is the original module path — not ``provider.auth.``.
    """
    patcher = mock.patch("music_assistant.helpers.auth.AuthenticationHelper")
    mock_cls = patcher.start()
    helper = mock.MagicMock()
    helper.send_url = mock.MagicMock()
    mock_cls.return_value.__aenter__ = mock.AsyncMock(return_value=helper)
    mock_cls.return_value.__aexit__ = mock.AsyncMock(return_value=False)
    return patcher, helper


async def test_perform_qr_auth_success() -> None:
    """Happy path: returns (x_token, music_token, display_login) and pushes QR URL."""
    qr = mock.MagicMock(qr_url="https://passport.yandex.ru/auth/magic/code/?track_id=abc")
    client = mock.AsyncMock()
    client.start_qr_login.return_value = qr
    client.poll_qr_until_confirmed.return_value = _make_creds()

    p1 = _patched_passport(client)
    p2, helper = _patched_auth_helper()
    try:
        x_token, music_token, login = await perform_qr_auth(mock.MagicMock(), "session-1")
    finally:
        p1.stop()
        p2.stop()

    assert x_token == "x-tok"
    assert music_token == "music-tok"
    assert login == "alice"
    helper.send_url.assert_called_once_with(qr.qr_url)


async def test_perform_qr_auth_no_music_token_raises_login_failed() -> None:
    """If creds.music_token is None we cannot proceed — surface LoginFailed."""
    qr = mock.MagicMock(qr_url="https://example/qr")
    client = mock.AsyncMock()
    client.start_qr_login.return_value = qr
    client.poll_qr_until_confirmed.return_value = _make_creds(music_token=None)

    p1 = _patched_passport(client)
    p2, _helper = _patched_auth_helper()
    try:
        with pytest.raises(LoginFailed, match="no music token"):
            await perform_qr_auth(mock.MagicMock(), "session-1")
    finally:
        p1.stop()
        p2.stop()


async def test_perform_qr_auth_timeout_maps_to_login_failed() -> None:
    """QRTimeoutError from polling becomes a friendly LoginFailed message."""
    qr = mock.MagicMock(qr_url="https://example/qr")
    client = mock.AsyncMock()
    client.start_qr_login.return_value = qr
    client.poll_qr_until_confirmed.side_effect = QRTimeoutError("expired")

    p1 = _patched_passport(client)
    p2, _helper = _patched_auth_helper()
    try:
        with pytest.raises(LoginFailed, match="timed out"):
            await perform_qr_auth(mock.MagicMock(), "session-1")
    finally:
        p1.stop()
        p2.stop()


async def test_perform_qr_auth_passport_error_maps_to_login_failed() -> None:
    """Generic YaPassportError becomes LoginFailed with a context-bearing message."""
    qr = mock.MagicMock(qr_url="https://example/qr")
    client = mock.AsyncMock()
    client.start_qr_login.return_value = qr
    client.poll_qr_until_confirmed.side_effect = YaPassportError("network down")

    p1 = _patched_passport(client)
    p2, _helper = _patched_auth_helper()
    try:
        with pytest.raises(LoginFailed, match="QR authentication failed"):
            await perform_qr_auth(mock.MagicMock(), "session-1")
    finally:
        p1.stop()
        p2.stop()
