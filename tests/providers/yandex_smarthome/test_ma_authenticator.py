"""Tests for the cached-token dialogs authenticator."""

from __future__ import annotations

from unittest import mock

import pytest
from music_assistant_models.errors import LoginFailed

from music_assistant.providers.yandex_smarthome.ma_authenticator import make_authenticator


async def test_cached_token_refreshes_cookies_and_yields_session() -> None:
    """The authenticator exchanges its one input token for dialogs cookies."""
    fake_session = mock.MagicMock()
    client = mock.MagicMock()
    client._session = fake_session
    client.refresh_passport_cookies = mock.AsyncMock()
    client_cm = mock.MagicMock()
    client_cm.__aenter__ = mock.AsyncMock(return_value=client)
    client_cm.__aexit__ = mock.AsyncMock(return_value=False)

    with mock.patch("ya_passport_auth.PassportClient.create", return_value=client_cm):
        async with make_authenticator(cached_x_token="x-token")() as session:
            assert session is fake_session

    token = client.refresh_passport_cookies.await_args.args[0]
    assert token.get_secret() == "x-token"


async def test_rejected_cached_token_is_not_replaced_by_device_flow() -> None:
    """A rejected token propagates so the setup flow can choose its retry policy."""
    client = mock.MagicMock()
    client.refresh_passport_cookies = mock.AsyncMock(side_effect=RuntimeError("rejected"))
    client.start_device_login = mock.AsyncMock()
    client_cm = mock.MagicMock()
    client_cm.__aenter__ = mock.AsyncMock(return_value=client)
    client_cm.__aexit__ = mock.AsyncMock(return_value=False)

    with (
        mock.patch("ya_passport_auth.PassportClient.create", return_value=client_cm),
        pytest.raises(LoginFailed, match="Yandex Music"),
    ):
        async with make_authenticator(cached_x_token="expired")():
            pass

    client.start_device_login.assert_not_awaited()


@pytest.mark.parametrize("token", [None, ""])
async def test_missing_token_requires_yandex_music_reauthentication(
    token: str | None,
) -> None:
    """Missing borrowed credentials fail with owner-specific recovery guidance."""
    authenticator = make_authenticator(cached_x_token=token)

    with pytest.raises(LoginFailed, match="Yandex Music"):
        async with authenticator():
            pass
