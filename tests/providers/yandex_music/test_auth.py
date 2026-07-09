"""Unit tests for auth.py (ya-passport-auth QR + Device Flow)."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator, Iterator
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
    YaPassportError,
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
        "ya_passport_auth.ma.routes.asyncio.sleep",
        new=mock.AsyncMock(),
    ) as patched:
        yield patched


@pytest.fixture(autouse=True)
async def drain_teardown_tasks(
    skip_grace_sleep: mock.AsyncMock,  # noqa: ARG001 — orders teardown before the patch exits
) -> AsyncGenerator[None]:
    """
    Settle deferred route-teardown tasks before the grace-sleep patch exits.

    Cancels leftovers instead of awaiting them so a test that failed before
    releasing a hung grace sleep cannot deadlock the whole run.
    """
    yield
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


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


class _FakeWebserver:
    """Dict-backed webserver stub mirroring MA's dynamic-route semantics."""

    base_url = "http://ma.local:8095"

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], object] = {}
        self.unregister_calls: list[str] = []
        self.unregister_error: Exception | None = None

    def register_dynamic_route(self, path: str, handler: object, method: str = "*") -> None:
        key = (path, method)
        if key in self.routes:
            raise RuntimeError(f"Route {path} already registered.")
        self.routes[key] = handler

    def unregister_dynamic_route(self, path: str, method: str = "*") -> None:
        self.unregister_calls.append(path)
        if self.unregister_error is not None:
            error, self.unregister_error = self.unregister_error, None
            raise error
        self.routes.pop((path, method), None)


def _make_mass(webserver: _FakeWebserver | None = None) -> mock.MagicMock:
    """Build a mass mock whose create_task actually schedules coroutines."""
    mock_mass = mock.MagicMock()
    if webserver is not None:
        mock_mass.webserver = webserver
    else:
        mock_mass.webserver.base_url = "http://ma.local:8095"
    mock_mass.create_task.side_effect = asyncio.create_task
    return mock_mass


@contextlib.contextmanager
def _patched_flow(mock_client: mock.AsyncMock, mock_auth_helper: mock.AsyncMock) -> Iterator[None]:
    """Patch PassportClient.create + AuthenticationHelper around a flow call."""
    with (
        mock.patch(
            "music_assistant.providers.yandex_music.auth.PassportClient.create",
        ) as mock_create,
        mock.patch(
            "music_assistant.helpers.auth.AuthenticationHelper",
            return_value=mock_auth_helper,
        ),
    ):
        mock_create.return_value.__aenter__ = mock.AsyncMock(return_value=mock_client)
        mock_create.return_value.__aexit__ = mock.AsyncMock(return_value=False)
        yield


async def _drain_background_tasks() -> None:
    """Await every task except the current one (route-teardown tasks)."""
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def _render_device_page(
    mock_mass: mock.MagicMock,
    session: DeviceCodeSession | None = None,
) -> str:
    """Run a successful device flow and return the intermediate page HTML."""
    session = session or _make_device_session()
    creds = _make_credentials()
    mock_client = mock.AsyncMock()
    mock_client.start_device_login.return_value = session
    mock_client.poll_device_until_confirmed.return_value = creds
    mock_auth_helper = mock.AsyncMock()

    with _patched_flow(mock_client, mock_auth_helper):
        await perform_device_auth(mock_mass, "session_page")

    page_call = next(
        c
        for c in mock_mass.webserver.register_dynamic_route.call_args_list
        if not c.args[0].endswith("/status")
    )
    handler = page_call.args[1]
    response = await handler(mock.MagicMock())
    await _drain_background_tasks()
    body = response.text
    assert isinstance(body, str)
    return body


# -- perform_device_auth -------------------------------------------------------


async def test_perform_device_auth_returns_three_tokens() -> None:
    """Device flow returns (x_token, music_token, refresh_token)."""
    session = _make_device_session()
    creds = _make_credentials()
    mock_client = mock.AsyncMock()
    mock_client.start_device_login.return_value = session
    mock_client.poll_device_until_confirmed.return_value = creds

    mock_mass = _make_mass()
    mock_auth_helper = mock.AsyncMock()

    with _patched_flow(mock_client, mock_auth_helper):
        x_token, music_token, refresh_token = await perform_device_auth(mock_mass, "session_1")

    assert x_token == "test_x_token"
    assert music_token == "test_music_token"
    assert refresh_token == "test_refresh_token"
    mock_client.start_device_login.assert_awaited_once()
    mock_client.poll_device_until_confirmed.assert_awaited_once_with(session, total_timeout=None)


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

    mock_mass = _make_mass()
    mock_auth_helper = mock.AsyncMock()

    with _patched_flow(mock_client, mock_auth_helper):
        await perform_device_auth(mock_mass, "session_1")

    # Route teardown is deferred to a background task (grace period for the
    # page's final status poll) — drain it before asserting unregistration.
    await _drain_background_tasks()

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
    """
    The status endpoint reports state=done after the device flow completes.

    Without this the popup window (opened via target=_blank) has no signal to
    close itself after the user confirms the code.
    """
    session = _make_device_session()
    creds = _make_credentials()
    mock_client = mock.AsyncMock()
    mock_client.start_device_login.return_value = session
    mock_client.poll_device_until_confirmed.return_value = creds

    mock_mass = _make_mass()
    mock_auth_helper = mock.AsyncMock()

    with _patched_flow(mock_client, mock_auth_helper):
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


async def test_perform_device_auth_returns_without_grace_delay(
    skip_grace_sleep: mock.AsyncMock,
) -> None:
    """
    The flow returns as soon as auth completes — the grace period must not block it.

    The grace sleep is made to hang forever: the caller must still get its
    tokens immediately, while the routes stay registered until the deferred
    teardown finally runs.
    """
    release = asyncio.Event()

    async def _hang(*_args: object, **_kwargs: object) -> None:
        await release.wait()

    skip_grace_sleep.side_effect = _hang

    session = _make_device_session()
    creds = _make_credentials()
    mock_client = mock.AsyncMock()
    mock_client.start_device_login.return_value = session
    mock_client.poll_device_until_confirmed.return_value = creds

    webserver = _FakeWebserver()
    mock_mass = _make_mass(webserver)
    mock_auth_helper = mock.AsyncMock()

    with _patched_flow(mock_client, mock_auth_helper):
        tokens = await asyncio.wait_for(perform_device_auth(mock_mass, "session_1"), timeout=1.0)

    assert tokens == ("test_x_token", "test_music_token", "test_refresh_token")
    # Teardown is still pending — the page can keep polling the final state.
    assert ("/yandex_music/device_code/session_1", "GET") in webserver.routes
    assert ("/yandex_music/device_code/session_1/status", "GET") in webserver.routes

    release.set()
    await _drain_background_tasks()
    assert webserver.routes == {}


async def test_perform_device_auth_immediate_retry_reuses_session_path(
    skip_grace_sleep: mock.AsyncMock,
) -> None:
    """
    A retry with the same session id must succeed while teardown is still pending.

    The MA frontend can reuse the config-flow session id for a rapid second
    login attempt; the previous attempt's routes (awaiting their grace-period
    teardown) must be taken over, not collide with a RuntimeError.
    """
    release = asyncio.Event()

    async def _hang(*_args: object, **_kwargs: object) -> None:
        await release.wait()

    skip_grace_sleep.side_effect = _hang

    session = _make_device_session()
    creds = _make_credentials()
    webserver = _FakeWebserver()
    mock_mass = _make_mass(webserver)

    try:
        for _attempt in range(2):
            mock_client = mock.AsyncMock()
            mock_client.start_device_login.return_value = session
            mock_client.poll_device_until_confirmed.return_value = creds
            mock_auth_helper = mock.AsyncMock()
            with _patched_flow(mock_client, mock_auth_helper):
                tokens = await perform_device_auth(mock_mass, "session_retry")
            assert tokens[0] == "test_x_token"
    finally:
        release.set()

    await _drain_background_tasks()
    assert webserver.routes == {}


async def test_route_teardown_attempts_every_path_despite_errors() -> None:
    """
    A failing unregister for one route must not skip the remaining routes.

    Teardown runs in a detached task (e.g. during MA shutdown the webserver
    may already be gone) — one failure must not leak the other route or
    surface as an unretrieved task exception.
    """
    session = _make_device_session()
    creds = _make_credentials()
    mock_client = mock.AsyncMock()
    mock_client.start_device_login.return_value = session
    mock_client.poll_device_until_confirmed.return_value = creds

    webserver = _FakeWebserver()
    mock_mass = _make_mass(webserver)
    mock_auth_helper = mock.AsyncMock()

    with _patched_flow(mock_client, mock_auth_helper):
        await perform_device_auth(mock_mass, "session_err")

    webserver.unregister_error = RuntimeError("Dynamic routes are not enabled")
    baseline = len(webserver.unregister_calls)
    await _drain_background_tasks()
    assert len(webserver.unregister_calls) - baseline == 2


async def test_device_code_page_countdown_reflects_elapsed_time() -> None:
    """
    The countdown shows the time actually left, not the full code lifetime.

    The popup can open (or be reloaded) long after the code was issued; the
    page must be rendered with the remaining seconds at request time.
    """
    session = _make_device_session(expires_in=543)
    fake_time = mock.MagicMock()
    fake_time.monotonic.side_effect = [1000.0, 1100.0]
    with mock.patch("ya_passport_auth.ma.routes.time", fake_time, create=True):
        body = await _render_device_page(_make_page_mass(), session=session)
    assert "443" in body


@pytest.mark.parametrize(
    ("exc", "expected_reason", "expected_match"),
    [
        (DeviceCodeTimeoutError("expired"), "expired", "timed out"),
        (InvalidCredentialsError("denied"), "denied", "denied"),
        (YaPassportError("boom"), "error", "Device authentication failed"),
    ],
    ids=["expired", "denied", "error"],
)
async def test_perform_device_auth_status_reports_failure_reason(
    exc: Exception, expected_reason: str, expected_match: str
) -> None:
    """
    When poll fails, the status endpoint reports failed plus a machine-readable reason.

    The page uses the reason to tell the user what to do next — an expired
    code and a rejected login require different actions.
    """
    session = _make_device_session()
    mock_client = mock.AsyncMock()
    mock_client.start_device_login.return_value = session
    mock_client.poll_device_until_confirmed.side_effect = exc

    mock_mass = _make_mass()
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
        _patched_flow(mock_client, mock_auth_helper),
        pytest.raises(LoginFailed, match=expected_match),
    ):
        await perform_device_auth(mock_mass, "session_fail")

    assert status_handlers, "status handler should have been registered"
    response = await status_handlers[0](mock.MagicMock())
    assert isinstance(response.body, bytes)
    payload = json.loads(response.body)
    assert payload == {"state": "failed", "reason": expected_reason}
    await _drain_background_tasks()


async def test_perform_device_auth_does_not_mark_cancellation_as_failure() -> None:
    """
    CancelledError must propagate without marking state as 'failed'.

    Routes must still be torn down eventually so a cancelled login doesn't
    leak webserver routes.
    """
    session = _make_device_session()
    mock_client = mock.AsyncMock()
    mock_client.start_device_login.return_value = session
    mock_client.poll_device_until_confirmed.side_effect = asyncio.CancelledError()

    mock_mass = _make_mass()
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

    with _patched_flow(mock_client, mock_auth_helper), pytest.raises(asyncio.CancelledError):
        await perform_device_auth(mock_mass, "session_cancel")

    assert status_handlers, "status handler should have been registered"
    response = await status_handlers[0](mock.MagicMock())
    assert isinstance(response.body, bytes)
    payload = json.loads(response.body)
    assert payload["state"] == "pending"

    await _drain_background_tasks()
    unregistered = [c.args for c in mock_mass.webserver.unregister_dynamic_route.call_args_list]
    assert ("/yandex_music/device_code/session_cancel", "GET") in unregistered
    assert ("/yandex_music/device_code/session_cancel/status", "GET") in unregistered


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

    mock_mass = _make_mass()
    mock_auth_helper = mock.AsyncMock()

    with _patched_flow(mock_client, mock_auth_helper):
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

    mock_mass = _make_mass()
    mock_auth_helper = mock.AsyncMock()

    with (
        _patched_flow(mock_client, mock_auth_helper),
        pytest.raises(LoginFailed, match="timed out"),
    ):
        await perform_device_auth(mock_mass, "session_1")

    await _drain_background_tasks()
    unregistered_paths = [
        c.args for c in mock_mass.webserver.unregister_dynamic_route.call_args_list
    ]
    assert ("/yandex_music/device_code/session_1", "GET") in unregistered_paths
    assert ("/yandex_music/device_code/session_1/status", "GET") in unregistered_paths


async def test_perform_device_auth_ya_passport_error_raises_login_failed() -> None:
    """Generic YaPassportError from library is mapped to LoginFailed."""
    mock_client = mock.AsyncMock()
    mock_client.start_device_login.side_effect = YaPassportError("misc")

    mock_mass = _make_mass()
    mock_auth_helper = mock.AsyncMock()

    with (
        _patched_flow(mock_client, mock_auth_helper),
        pytest.raises(LoginFailed, match="Device authentication failed"),
    ):
        await perform_device_auth(mock_mass, "session_1")


async def test_perform_device_auth_no_music_token_raises_login_failed() -> None:
    """Credentials without music_token raises LoginFailed."""
    session = _make_device_session()
    creds = _make_credentials(music_token=None)
    mock_client = mock.AsyncMock()
    mock_client.start_device_login.return_value = session
    mock_client.poll_device_until_confirmed.return_value = creds

    mock_mass = _make_mass()
    mock_auth_helper = mock.AsyncMock()

    with (
        _patched_flow(mock_client, mock_auth_helper),
        pytest.raises(LoginFailed, match="no music token"),
    ):
        await perform_device_auth(mock_mass, "session_1")


async def test_perform_device_auth_no_refresh_token_raises_login_failed() -> None:
    """Credentials without refresh_token raises LoginFailed."""
    session = _make_device_session()
    creds = _make_credentials(refresh_token=None)
    mock_client = mock.AsyncMock()
    mock_client.start_device_login.return_value = session
    mock_client.poll_device_until_confirmed.return_value = creds

    mock_mass = _make_mass()
    mock_auth_helper = mock.AsyncMock()

    with (
        _patched_flow(mock_client, mock_auth_helper),
        pytest.raises(LoginFailed, match="no refresh token"),
    ):
        await perform_device_auth(mock_mass, "session_1")


# -- device-code page content ---------------------------------------------------


def _make_page_mass(locale: object = None) -> mock.MagicMock:
    """Build a mass mock for page-rendering tests, optionally with a locale."""
    mock_mass = _make_mass()
    if locale is not None:
        mock_mass.metadata.locale = locale
    return mock_mass


async def test_device_code_page_copy_targets_code_block() -> None:
    """
    The code block itself is the copy target; no standalone copy button.

    ``document.execCommand`` must be present as the fallback because the
    Clipboard API is unavailable on plain-HTTP MA deployments.
    """
    body = await _render_device_page(_make_page_mass())
    assert 'id="copy"' not in body
    assert "execCommand" in body
    assert 'role="button"' in body


async def test_device_code_page_shows_countdown_and_terminal_states() -> None:
    """The page embeds the code lifetime and treats a 404 status as terminal."""
    session = _make_device_session(expires_in=543)
    body = await _render_device_page(_make_page_mass(), session=session)
    assert "543" in body
    assert "404" in body


async def test_device_code_page_shows_verification_url_as_text() -> None:
    """The verification URL is visible as text, not only hidden in the link href."""
    body = await _render_device_page(_make_page_mass())
    assert body.count("https://oauth.yandex.ru/device") >= 2


async def test_device_code_page_localized_russian() -> None:
    """A Russian MA locale renders the page in Russian."""
    body = await _render_device_page(_make_page_mass(locale="ru_RU"))
    assert 'lang="ru"' in body
    assert "Скопируйте" in body


async def test_device_code_page_defaults_to_english_for_unknown_locale() -> None:
    """A non-string locale (or non-Russian) falls back to English."""
    body = await _render_device_page(_make_page_mass(locale=mock.MagicMock()))
    assert 'lang="en"' in body
    assert "Tap the code" in body
    assert "Скопируйте" not in body


async def test_device_code_page_supports_dark_theme() -> None:
    """The page adapts to the user's dark colour scheme."""
    body = await _render_device_page(_make_page_mass())
    assert "prefers-color-scheme: dark" in body


async def test_device_code_page_uses_translated_strings() -> None:
    """Strings resolved by the MA translations controller reach the page."""
    mock_mass = _make_page_mass(locale="de_DE")
    mock_mass.translations.ensure_locale_loaded = mock.AsyncMock()
    mock_mass.translations.get_translation = mock.Mock(
        side_effect=lambda key, **_kw: (
            "Anmeldung bei Yandex Music" if key.endswith(".title") else None
        )
    )
    body = await _render_device_page(mock_mass)
    assert "Anmeldung bei Yandex Music" in body


async def test_device_code_page_falls_back_without_translation() -> None:
    """Unresolved keys fall back to the in-code table in the page language."""
    mock_mass = _make_page_mass(locale="ru_RU")
    mock_mass.translations.ensure_locale_loaded = mock.AsyncMock()
    mock_mass.translations.get_translation = mock.Mock(return_value=None)
    body = await _render_device_page(mock_mass)
    assert "Скопируйте" in body


async def test_device_code_page_survives_missing_translations_controller() -> None:
    """An MA build without the translations controller renders the page as today."""
    mock_mass = _make_page_mass()
    del mock_mass.translations
    body = await _render_device_page(mock_mass)
    assert "Tap the code" in body


# -- perform_qr_auth ----------------------------------------------------------


async def test_perform_qr_auth_success() -> None:
    """QR flow returns (x_token, music_token) as plain strings."""
    qr = _make_qr_session()
    creds = _make_credentials()
    mock_client = mock.AsyncMock()
    mock_client.start_qr_login.return_value = qr
    mock_client.poll_qr_until_confirmed.return_value = creds

    mock_mass = _make_mass()
    mock_auth_helper = mock.AsyncMock()

    with _patched_flow(mock_client, mock_auth_helper):
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

    mock_mass = _make_mass()
    mock_auth_helper = mock.AsyncMock()

    with _patched_flow(mock_client, mock_auth_helper):
        await perform_qr_auth(mock_mass, "session_1")

    mock_auth_helper.__aenter__.return_value.send_url.assert_called_once_with(qr.qr_url)


async def test_perform_qr_auth_timeout_raises_login_failed() -> None:
    """QRTimeoutError from library is mapped to LoginFailed."""
    qr = _make_qr_session()
    mock_client = mock.AsyncMock()
    mock_client.start_qr_login.return_value = qr
    mock_client.poll_qr_until_confirmed.side_effect = QRTimeoutError("timed out")

    mock_mass = _make_mass()
    mock_auth_helper = mock.AsyncMock()

    with (
        _patched_flow(mock_client, mock_auth_helper),
        pytest.raises(LoginFailed, match="timed out"),
    ):
        await perform_qr_auth(mock_mass, "session_1")


async def test_perform_qr_auth_passport_error_raises_login_failed() -> None:
    """Generic YaPassportError is mapped to LoginFailed."""
    mock_client = mock.AsyncMock()
    mock_client.start_qr_login.side_effect = YaPassportError("misc")

    mock_mass = _make_mass()
    mock_auth_helper = mock.AsyncMock()

    with (
        _patched_flow(mock_client, mock_auth_helper),
        pytest.raises(LoginFailed, match="QR authentication failed"),
    ):
        await perform_qr_auth(mock_mass, "session_1")


async def test_perform_qr_auth_no_music_token_raises() -> None:
    """Credentials without music_token raises LoginFailed."""
    qr = _make_qr_session()
    creds = _make_credentials(music_token=None)
    mock_client = mock.AsyncMock()
    mock_client.start_qr_login.return_value = qr
    mock_client.poll_qr_until_confirmed.return_value = creds

    mock_mass = _make_mass()
    mock_auth_helper = mock.AsyncMock()

    with (
        _patched_flow(mock_client, mock_auth_helper),
        pytest.raises(LoginFailed, match="no music token"),
    ):
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


async def test_perform_device_auth_error_does_not_leak_library_payload() -> None:
    """Errors raised from device-flow must not include library str()."""
    mock_client = mock.AsyncMock()
    mock_client.start_device_login.side_effect = PassportNetworkError(_SECRET_PAYLOAD)
    mock_mass = _make_mass()
    mock_auth_helper = mock.AsyncMock()

    with (
        _patched_flow(mock_client, mock_auth_helper),
        pytest.raises(ResourceTemporarilyUnavailable) as exc_info,
    ):
        await perform_device_auth(mock_mass, "session_1")

    assert _SECRET_PAYLOAD not in str(exc_info.value)
    assert "ABC_TOKEN_LEAK" not in str(exc_info.value)


async def test_perform_qr_auth_error_does_not_leak_library_payload() -> None:
    """Errors raised from QR flow must not include library str()."""
    mock_client = mock.AsyncMock()
    mock_client.start_qr_login.side_effect = PassportNetworkError(_SECRET_PAYLOAD)
    mock_mass = _make_mass()
    mock_auth_helper = mock.AsyncMock()

    with (
        _patched_flow(mock_client, mock_auth_helper),
        pytest.raises(ResourceTemporarilyUnavailable) as exc_info,
    ):
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
