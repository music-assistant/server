"""Unit tests for auth.py (ya-passport-auth QR + Device Flow)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Generator
from typing import TYPE_CHECKING
from unittest import mock

import pytest
from music_assistant_models.errors import LoginFailed, ResourceTemporarilyUnavailable
from ya_passport_auth import Credentials, DeviceCodeSession, QrSession, SecretStr
from ya_passport_auth.exceptions import (
    DeviceCodeTimeoutError,
    InvalidCredentialsError,
    QRTimeoutError,
    RateLimitedError,
)
from ya_passport_auth.exceptions import (
    NetworkError as PassportNetworkError,
)

from music_assistant.providers.yandex_music.auth import (
    perform_device_auth,
    perform_qr_auth,
    refresh_credentials_via_passport,
    refresh_music_token,
    validate_x_token,
)

if TYPE_CHECKING:
    from aiohttp import web


@pytest.fixture(autouse=True)
def skip_grace_sleep() -> Generator[mock.AsyncMock]:
    """Bypass the post-auth grace ``asyncio.sleep`` so tests run instantly."""
    with mock.patch(
        "music_assistant.providers.yandex_music.auth.asyncio.sleep",
        new=mock.AsyncMock(),
    ) as patched:
        yield patched


# -- helpers -------------------------------------------------------------------


def _make_device_session(
    user_code: str = "ABCD-1234",
    verification_url: str = "https://oauth.yandex.ru/device",
    interval: int = 1,
    expires_in: int = 600,
) -> DeviceCodeSession:
    """Build a DeviceCodeSession for testing."""
    return DeviceCodeSession(
        device_code=SecretStr("dev-code-xyz"),
        user_code=user_code,
        verification_url=verification_url,
        expires_in=expires_in,
        interval=interval,
    )


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


def _make_qr_session() -> QrSession:
    """Build a QrSession for testing."""
    return QrSession(
        track_id="track123",
        csrf_token="csrf_abc",
        qr_url="https://passport.yandex.ru/auth/magic/code/?track_id=track123",
    )


# -- perform_device_auth -------------------------------------------------------


async def test_perform_device_auth_returns_three_tokens() -> None:
    """Device flow returns (x_token, music_token, refresh_token)."""
    session = _make_device_session()
    creds = _make_credentials()
    mock_client = mock.AsyncMock()
    mock_client.start_device_login.return_value = session
    mock_client.poll_device_until_confirmed.return_value = creds

    mock_mass = mock.MagicMock()
    mock_mass.webserver.base_url = "http://ma.local:8095"
    mock_auth_helper = mock.AsyncMock()

    with (
        mock.patch(
            "music_assistant.providers.yandex_music.auth.PassportClient.create",
        ) as mock_create,
        mock.patch(
            "music_assistant.providers.yandex_music.auth.AuthenticationHelper",
            return_value=mock_auth_helper,
        ),
    ):
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        x_token, music_token, refresh_token = await perform_device_auth(mock_mass, "session_1")

    assert x_token == "test_x_token"
    assert music_token == "test_music_token"
    assert refresh_token == "test_refresh_token"
    mock_client.start_device_login.assert_awaited_once()
    mock_client.poll_device_until_confirmed.assert_awaited_once_with(session)


async def test_perform_device_auth_serves_intermediate_page_and_cleans_up() -> None:
    """A temporary HTML page + status endpoint are registered and unregistered after."""
    session = _make_device_session(
        user_code="WXYZ-9999",
        verification_url="https://oauth.yandex.ru/device",
    )
    creds = _make_credentials()
    mock_client = mock.AsyncMock()
    mock_client.start_device_login.return_value = session
    mock_client.poll_device_until_confirmed.return_value = creds

    mock_mass = mock.MagicMock()
    mock_mass.webserver.base_url = "http://ma.local:8095"
    mock_auth_helper = mock.AsyncMock()

    with (
        mock.patch(
            "music_assistant.providers.yandex_music.auth.PassportClient.create",
        ) as mock_create,
        mock.patch(
            "music_assistant.providers.yandex_music.auth.AuthenticationHelper",
            return_value=mock_auth_helper,
        ),
    ):
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        await perform_device_auth(mock_mass, "session_1")

    expected_path = "/yandex_music/device_code/session_1"
    expected_status_path = f"{expected_path}/status"

    registered_paths = [
        (c.args[0], c.args[2]) for c in mock_mass.webserver.register_dynamic_route.call_args_list
    ]
    assert (expected_path, "GET") in registered_paths
    assert (expected_status_path, "GET") in registered_paths

    unregistered_paths = [
        c.args for c in mock_mass.webserver.unregister_dynamic_route.call_args_list
    ]
    assert (expected_path, "GET") in unregistered_paths
    assert (expected_status_path, "GET") in unregistered_paths

    mock_auth_helper.__aenter__.return_value.send_url.assert_called_once_with(
        f"http://ma.local:8095{expected_path}"
    )


async def test_perform_device_auth_status_endpoint_reports_done_after_success() -> None:
    """The status endpoint reports state=done after the device flow completes.

    Without this the popup window (opened via target=_blank) has no signal to
    close itself after the user confirms the code.
    """
    session = _make_device_session()
    creds = _make_credentials()
    mock_client = mock.AsyncMock()
    mock_client.start_device_login.return_value = session
    mock_client.poll_device_until_confirmed.return_value = creds

    mock_mass = mock.MagicMock()
    mock_mass.webserver.base_url = "http://ma.local:8095"
    mock_auth_helper = mock.AsyncMock()

    with (
        mock.patch(
            "music_assistant.providers.yandex_music.auth.PassportClient.create",
        ) as mock_create,
        mock.patch(
            "music_assistant.providers.yandex_music.auth.AuthenticationHelper",
            return_value=mock_auth_helper,
        ),
    ):
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        await perform_device_auth(mock_mass, "session_xyz")

    status_call = next(
        c
        for c in mock_mass.webserver.register_dynamic_route.call_args_list
        if c.args[0].endswith("/status")
    )
    status_handler = status_call.args[1]
    response = await status_handler(mock.MagicMock())
    assert isinstance(response.body, bytes)
    payload = json.loads(response.body)
    assert payload["state"] == "done"


async def test_perform_device_auth_status_reports_failed_on_error(
    skip_grace_sleep: mock.AsyncMock,
) -> None:
    """When poll fails, status endpoint reports failed and grace sleep still fires.

    Otherwise the page would race with route teardown and only ever see 404s
    instead of the 'failed' message.
    """
    session = _make_device_session()
    mock_client = mock.AsyncMock()
    mock_client.start_device_login.return_value = session
    mock_client.poll_device_until_confirmed.side_effect = DeviceCodeTimeoutError("expired")

    mock_mass = mock.MagicMock()
    mock_mass.webserver.base_url = "http://ma.local:8095"
    mock_auth_helper = mock.AsyncMock()

    status_handlers: list[Callable[[web.Request], Awaitable[web.Response]]] = []

    def _capture(
        path: str,
        handler: Callable[[web.Request], Awaitable[web.Response]],
        _method: str,
    ) -> None:
        if path.endswith("/status"):
            status_handlers.append(handler)

    mock_mass.webserver.register_dynamic_route.side_effect = _capture

    with (
        mock.patch(
            "music_assistant.providers.yandex_music.auth.PassportClient.create",
        ) as mock_create,
        mock.patch(
            "music_assistant.providers.yandex_music.auth.AuthenticationHelper",
            return_value=mock_auth_helper,
        ),
    ):
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        with pytest.raises(LoginFailed, match="timed out"):
            await perform_device_auth(mock_mass, "session_fail")

    assert status_handlers, "status handler should have been registered"
    response = await status_handlers[0](mock.MagicMock())
    assert isinstance(response.body, bytes)
    payload = json.loads(response.body)
    assert payload["state"] == "failed"
    # Grace sleep must fire on failure so the page can observe "failed" before teardown.
    skip_grace_sleep.assert_awaited()


async def test_perform_device_auth_does_not_mark_cancellation_as_failure(
    skip_grace_sleep: mock.AsyncMock,
) -> None:
    """CancelledError must propagate without marking state as 'failed' or sleeping."""
    session = _make_device_session()
    mock_client = mock.AsyncMock()
    mock_client.start_device_login.return_value = session
    mock_client.poll_device_until_confirmed.side_effect = asyncio.CancelledError()

    mock_mass = mock.MagicMock()
    mock_mass.webserver.base_url = "http://ma.local:8095"
    mock_auth_helper = mock.AsyncMock()

    status_handlers: list[Callable[[web.Request], Awaitable[web.Response]]] = []

    def _capture(
        path: str,
        handler: Callable[[web.Request], Awaitable[web.Response]],
        _method: str,
    ) -> None:
        if path.endswith("/status"):
            status_handlers.append(handler)

    mock_mass.webserver.register_dynamic_route.side_effect = _capture

    with (
        mock.patch(
            "music_assistant.providers.yandex_music.auth.PassportClient.create",
        ) as mock_create,
        mock.patch(
            "music_assistant.providers.yandex_music.auth.AuthenticationHelper",
            return_value=mock_auth_helper,
        ),
    ):
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        with pytest.raises(asyncio.CancelledError):
            await perform_device_auth(mock_mass, "session_cancel")

    assert status_handlers, "status handler should have been registered"
    response = await status_handlers[0](mock.MagicMock())
    assert isinstance(response.body, bytes)
    payload = json.loads(response.body)
    assert payload["state"] == "pending"
    skip_grace_sleep.assert_not_awaited()


async def test_perform_device_auth_route_handler_renders_code_and_url() -> None:
    """The registered route handler returns HTML containing the code + verification URL."""
    session = _make_device_session(
        user_code="ABCD-1234",
        verification_url="https://oauth.yandex.ru/device",
    )
    creds = _make_credentials()
    mock_client = mock.AsyncMock()
    mock_client.start_device_login.return_value = session
    mock_client.poll_device_until_confirmed.return_value = creds

    mock_mass = mock.MagicMock()
    mock_mass.webserver.base_url = "http://ma.local:8095"
    mock_auth_helper = mock.AsyncMock()

    with (
        mock.patch(
            "music_assistant.providers.yandex_music.auth.PassportClient.create",
        ) as mock_create,
        mock.patch(
            "music_assistant.providers.yandex_music.auth.AuthenticationHelper",
            return_value=mock_auth_helper,
        ),
    ):
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        await perform_device_auth(mock_mass, "session_1")

    page_call = next(
        c
        for c in mock_mass.webserver.register_dynamic_route.call_args_list
        if not c.args[0].endswith("/status")
    )
    handler = page_call.args[1]
    response = await handler(mock.MagicMock())
    body = response.text
    assert body is not None
    assert "ABCD-1234" in body
    assert "https://oauth.yandex.ru/device" in body
    assert response.content_type == "text/html"


async def test_perform_device_auth_timeout_raises_login_failed() -> None:
    """DeviceCodeTimeoutError from library is mapped to LoginFailed and the route is freed."""
    session = _make_device_session()
    mock_client = mock.AsyncMock()
    mock_client.start_device_login.return_value = session
    mock_client.poll_device_until_confirmed.side_effect = DeviceCodeTimeoutError("expired")

    mock_mass = mock.MagicMock()
    mock_mass.webserver.base_url = "http://ma.local:8095"
    mock_auth_helper = mock.AsyncMock()

    with (
        mock.patch(
            "music_assistant.providers.yandex_music.auth.PassportClient.create",
        ) as mock_create,
        mock.patch(
            "music_assistant.providers.yandex_music.auth.AuthenticationHelper",
            return_value=mock_auth_helper,
        ),
    ):
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        with pytest.raises(LoginFailed, match="timed out"):
            await perform_device_auth(mock_mass, "session_1")

    unregistered_paths = [
        c.args for c in mock_mass.webserver.unregister_dynamic_route.call_args_list
    ]
    assert ("/yandex_music/device_code/session_1", "GET") in unregistered_paths
    assert ("/yandex_music/device_code/session_1/status", "GET") in unregistered_paths


async def test_perform_device_auth_ya_passport_error_raises_login_failed() -> None:
    """Generic YaPassportError from library is mapped to LoginFailed."""
    mock_client = mock.AsyncMock()
    mock_client.start_device_login.side_effect = PassportNetworkError("offline")

    mock_mass = mock.MagicMock()
    mock_auth_helper = mock.AsyncMock()

    with (
        mock.patch(
            "music_assistant.providers.yandex_music.auth.PassportClient.create",
        ) as mock_create,
        mock.patch(
            "music_assistant.providers.yandex_music.auth.AuthenticationHelper",
            return_value=mock_auth_helper,
        ),
    ):
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        with pytest.raises(LoginFailed, match="device auth error"):
            await perform_device_auth(mock_mass, "session_1")


async def test_perform_device_auth_no_music_token_raises_login_failed() -> None:
    """Credentials without music_token raises LoginFailed."""
    session = _make_device_session()
    creds = _make_credentials(music_token=None)
    mock_client = mock.AsyncMock()
    mock_client.start_device_login.return_value = session
    mock_client.poll_device_until_confirmed.return_value = creds

    mock_mass = mock.MagicMock()
    mock_auth_helper = mock.AsyncMock()

    with (
        mock.patch(
            "music_assistant.providers.yandex_music.auth.PassportClient.create",
        ) as mock_create,
        mock.patch(
            "music_assistant.providers.yandex_music.auth.AuthenticationHelper",
            return_value=mock_auth_helper,
        ),
    ):
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        with pytest.raises(LoginFailed, match="no music token"):
            await perform_device_auth(mock_mass, "session_1")


async def test_perform_device_auth_no_refresh_token_raises_login_failed() -> None:
    """Credentials without refresh_token raises LoginFailed."""
    session = _make_device_session()
    creds = _make_credentials(refresh_token=None)
    mock_client = mock.AsyncMock()
    mock_client.start_device_login.return_value = session
    mock_client.poll_device_until_confirmed.return_value = creds

    mock_mass = mock.MagicMock()
    mock_auth_helper = mock.AsyncMock()

    with (
        mock.patch(
            "music_assistant.providers.yandex_music.auth.PassportClient.create",
        ) as mock_create,
        mock.patch(
            "music_assistant.providers.yandex_music.auth.AuthenticationHelper",
            return_value=mock_auth_helper,
        ),
    ):
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        with pytest.raises(LoginFailed, match="no refresh token"):
            await perform_device_auth(mock_mass, "session_1")


# -- perform_qr_auth ----------------------------------------------------------


async def test_perform_qr_auth_success() -> None:
    """QR flow returns (x_token, music_token) as plain strings."""
    qr = _make_qr_session()
    creds = _make_credentials()
    mock_client = mock.AsyncMock()
    mock_client.start_qr_login.return_value = qr
    mock_client.poll_qr_until_confirmed.return_value = creds

    mock_mass = mock.MagicMock()
    mock_auth_helper = mock.AsyncMock()

    with (
        mock.patch(
            "music_assistant.providers.yandex_music.auth.PassportClient.create",
        ) as mock_create,
        mock.patch(
            "music_assistant.providers.yandex_music.auth.AuthenticationHelper",
            return_value=mock_auth_helper,
        ),
    ):
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        x_token, music_token = await perform_qr_auth(mock_mass, "session_1")

    assert x_token == "test_x_token"
    assert music_token == "test_music_token"
    mock_client.start_qr_login.assert_awaited_once()
    mock_client.poll_qr_until_confirmed.assert_awaited_once_with(qr)


async def test_perform_qr_auth_sends_qr_url() -> None:
    """QR URL is sent to the AuthenticationHelper."""
    qr = _make_qr_session()
    creds = _make_credentials()
    mock_client = mock.AsyncMock()
    mock_client.start_qr_login.return_value = qr
    mock_client.poll_qr_until_confirmed.return_value = creds

    mock_mass = mock.MagicMock()
    mock_auth_helper = mock.AsyncMock()

    with (
        mock.patch(
            "music_assistant.providers.yandex_music.auth.PassportClient.create",
        ) as mock_create,
        mock.patch(
            "music_assistant.providers.yandex_music.auth.AuthenticationHelper",
            return_value=mock_auth_helper,
        ),
    ):
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        await perform_qr_auth(mock_mass, "session_1")

    mock_auth_helper.__aenter__.return_value.send_url.assert_called_once_with(qr.qr_url)


async def test_perform_qr_auth_timeout_raises_login_failed() -> None:
    """QRTimeoutError from library is mapped to LoginFailed."""
    qr = _make_qr_session()
    mock_client = mock.AsyncMock()
    mock_client.start_qr_login.return_value = qr
    mock_client.poll_qr_until_confirmed.side_effect = QRTimeoutError("timed out")

    mock_mass = mock.MagicMock()
    mock_auth_helper = mock.AsyncMock()

    with (
        mock.patch(
            "music_assistant.providers.yandex_music.auth.PassportClient.create",
        ) as mock_create,
        mock.patch(
            "music_assistant.providers.yandex_music.auth.AuthenticationHelper",
            return_value=mock_auth_helper,
        ),
    ):
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        with pytest.raises(LoginFailed, match="timed out"):
            await perform_qr_auth(mock_mass, "session_1")


async def test_perform_qr_auth_passport_error_raises_login_failed() -> None:
    """Generic YaPassportError is mapped to LoginFailed."""
    mock_client = mock.AsyncMock()
    mock_client.start_qr_login.side_effect = PassportNetworkError("connection lost")

    mock_mass = mock.MagicMock()
    mock_auth_helper = mock.AsyncMock()

    with (
        mock.patch(
            "music_assistant.providers.yandex_music.auth.PassportClient.create",
        ) as mock_create,
        mock.patch(
            "music_assistant.providers.yandex_music.auth.AuthenticationHelper",
            return_value=mock_auth_helper,
        ),
    ):
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        with pytest.raises(LoginFailed, match="Yandex auth error"):
            await perform_qr_auth(mock_mass, "session_1")


async def test_perform_qr_auth_no_music_token_raises() -> None:
    """Credentials without music_token raises LoginFailed."""
    qr = _make_qr_session()
    creds = _make_credentials(music_token=None)
    mock_client = mock.AsyncMock()
    mock_client.start_qr_login.return_value = qr
    mock_client.poll_qr_until_confirmed.return_value = creds

    mock_mass = mock.MagicMock()
    mock_auth_helper = mock.AsyncMock()

    with (
        mock.patch(
            "music_assistant.providers.yandex_music.auth.PassportClient.create",
        ) as mock_create,
        mock.patch(
            "music_assistant.providers.yandex_music.auth.AuthenticationHelper",
            return_value=mock_auth_helper,
        ),
    ):
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        with pytest.raises(LoginFailed, match="no music token"):
            await perform_qr_auth(mock_mass, "session_1")


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

        with pytest.raises(LoginFailed, match="Failed to refresh"):
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
    """Transient Passport failures must not masquerade as "token invalid".

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

        with pytest.raises(LoginFailed, match="Failed to refresh credentials"):
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


async def test_perform_device_auth_error_does_not_leak_library_payload() -> None:
    """``LoginFailed`` raised from device-flow must not include library str()."""
    mock_client = mock.AsyncMock()
    mock_client.start_device_login.side_effect = PassportNetworkError(_SECRET_PAYLOAD)
    mock_mass = mock.MagicMock()
    mock_auth_helper = mock.AsyncMock()

    with (
        mock.patch(
            "music_assistant.providers.yandex_music.auth.PassportClient.create",
        ) as mock_create,
        mock.patch(
            "music_assistant.providers.yandex_music.auth.AuthenticationHelper",
            return_value=mock_auth_helper,
        ),
    ):
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        with pytest.raises(LoginFailed) as exc_info:
            await perform_device_auth(mock_mass, "session_1")

    assert _SECRET_PAYLOAD not in str(exc_info.value)
    assert "ABC_TOKEN_LEAK" not in str(exc_info.value)


async def test_perform_qr_auth_error_does_not_leak_library_payload() -> None:
    """``LoginFailed`` raised from QR flow must not include library str()."""
    mock_client = mock.AsyncMock()
    mock_client.start_qr_login.side_effect = PassportNetworkError(_SECRET_PAYLOAD)
    mock_mass = mock.MagicMock()
    mock_auth_helper = mock.AsyncMock()

    with (
        mock.patch(
            "music_assistant.providers.yandex_music.auth.PassportClient.create",
        ) as mock_create,
        mock.patch(
            "music_assistant.providers.yandex_music.auth.AuthenticationHelper",
            return_value=mock_auth_helper,
        ),
    ):
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        with pytest.raises(LoginFailed) as exc_info:
            await perform_qr_auth(mock_mass, "session_1")

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
