"""Tests for the Yandex Station interactive setup flow (run_setup)."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest import mock

from music_assistant_models.enums import FlowStepType
from ya_passport_auth import Credentials, DeviceCodeSession, QrSession, SecretStr
from ya_passport_auth.ma import BORROW_SOURCE_OWN

from music_assistant.models.setup_flow import SetupFlowContext, SetupSession
from music_assistant.providers.yandex_station import setup_flow as station_flow
from music_assistant.providers.yandex_station.constants import (
    CONF_COOKIES,
    CONF_MUSIC_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_REMEMBER_SESSION,
    CONF_X_TOKEN,
    CONF_YM_INSTANCE,
)


class _FakeClient:
    """Canned PassportClient that confirms a QR/device login."""

    def __init__(self, creds: Credentials) -> None:
        self._creds = creds

    async def start_qr_login(self) -> QrSession:
        return QrSession(track_id="t", csrf_token="c", qr_url="https://passport.yandex.ru/qr/abc")

    async def poll_qr_until_confirmed(self, _qr: QrSession, **_kwargs: Any) -> Credentials:
        return self._creds

    async def start_device_login(self, **_kwargs: Any) -> DeviceCodeSession:
        return DeviceCodeSession(
            device_code=SecretStr("dc"),
            user_code="ABCD-1234",
            verification_url="https://ya.ru/device",
            expires_in=300,
            interval=5,
        )

    async def poll_device_until_confirmed(
        self, _session: DeviceCodeSession, **_kwargs: Any
    ) -> Credentials:
        return self._creds


def _async_cm(client: _FakeClient) -> mock.MagicMock:
    """Wrap a fake client as the async context manager PassportClient.create returns."""
    ctx = mock.MagicMock()
    ctx.__aenter__ = mock.AsyncMock(return_value=client)
    ctx.__aexit__ = mock.AsyncMock(return_value=False)
    return ctx


def _make_session(
    finish_handler: Any, providers: dict[str, Any] | None = None
) -> tuple[SetupSession, mock.Mock]:
    """Build a real SetupSession backed by a Mock mass listing the given YM providers."""
    mass = mock.Mock()
    mass.config.get = mock.Mock(return_value=providers or {})
    context = SetupFlowContext(kind="setup", reason="user", domain="yandex_station")
    return SetupSession(mass, "flow-test", context, finish_handler), mass


async def _wait_for(predicate: Any, timeout: float = 5.0) -> Any:
    """Wait until the predicate returns truthy (or fail the test)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


async def _wait_form(session: SetupSession, step_id: str) -> None:
    """Wait until the given FORM step is presented."""
    await _wait_for(
        lambda: (
            session.current_step
            and session.current_step.type == FlowStepType.FORM
            and session.current_step.step_id == step_id
        )
    )


async def test_borrow_mode_finishes_with_instance_only() -> None:
    """Selecting a linked Yandex Music instance persists only that instance id."""
    collected: dict[str, Any] = {}

    async def finish(_s: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"instance_id": "yandex_station"}

    session, _mass = _make_session(
        finish, providers={"ym-a": {"domain": "yandex_music", "name": "Main"}}
    )
    task = asyncio.create_task(station_flow.run_setup(session))
    await _wait_form(session, "user")
    session.handle_submit({CONF_YM_INSTANCE: "ym-a"})
    await _wait_for(lambda: session.finished)
    await task

    assert collected == {CONF_YM_INSTANCE: "ym-a"}


async def test_own_device_login_persists_full_triple() -> None:
    """Own credentials via device login persist music + x + refresh tokens under OWN."""
    creds = Credentials(
        x_token=SecretStr("XT"),
        music_token=SecretStr("MT"),
        refresh_token=SecretStr("RT"),
        display_login="alice",
    )
    collected: dict[str, Any] = {}

    async def finish(_s: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"instance_id": "yandex_station"}

    session, _mass = _make_session(finish)
    client = _FakeClient(creds)
    with mock.patch.object(station_flow, "PassportClient") as pc:
        pc.create.return_value = _async_cm(client)
        task = asyncio.create_task(station_flow.run_setup(session))
        await _wait_form(session, "user")
        session.handle_submit({CONF_YM_INSTANCE: BORROW_SOURCE_OWN})
        await _wait_form(session, "method")
        session.handle_submit(
            {station_flow.CONF_METHOD: station_flow.METHOD_DEVICE, CONF_REMEMBER_SESSION: True}
        )
        await _wait_for(lambda: session.finished)
        await task

    assert collected == {
        CONF_YM_INSTANCE: BORROW_SOURCE_OWN,
        CONF_MUSIC_TOKEN: "MT",
        CONF_X_TOKEN: "XT",
        CONF_REFRESH_TOKEN: "RT",
    }


async def test_own_cookie_login_persists_tokens() -> None:
    """Own credentials via cookies persist music + x token (no refresh) under OWN."""
    collected: dict[str, Any] = {}

    async def finish(_s: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"instance_id": "yandex_station"}

    session, _mass = _make_session(finish)
    with mock.patch.object(
        station_flow,
        "login_with_cookies",
        new=mock.AsyncMock(return_value=("XT", "MT")),
    ) as cookie_login:
        task = asyncio.create_task(station_flow.run_setup(session))
        await _wait_form(session, "user")
        session.handle_submit({CONF_YM_INSTANCE: BORROW_SOURCE_OWN})
        await _wait_form(session, "method")
        session.handle_submit(
            {station_flow.CONF_METHOD: station_flow.METHOD_COOKIES, CONF_REMEMBER_SESSION: True}
        )
        await _wait_form(session, "cookies")
        session.handle_submit({CONF_COOKIES: "Session_id=abc; yandexuid=1"})
        await _wait_for(lambda: session.finished)
        await task

    cookie_login.assert_awaited_once_with("Session_id=abc; yandexuid=1")
    assert collected == {
        CONF_YM_INSTANCE: BORROW_SOURCE_OWN,
        CONF_MUSIC_TOKEN: "MT",
        CONF_X_TOKEN: "XT",
        CONF_REFRESH_TOKEN: None,
    }


async def test_own_qr_without_remember_clears_long_lived_tokens() -> None:
    """QR own login with remember off stores only the music token."""
    creds = Credentials(x_token=SecretStr("XT"), music_token=SecretStr("MT"))
    collected: dict[str, Any] = {}

    async def finish(_s: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"instance_id": "yandex_station"}

    session, _mass = _make_session(finish)
    client = _FakeClient(creds)
    with mock.patch.object(station_flow, "PassportClient") as pc:
        pc.create.return_value = _async_cm(client)
        task = asyncio.create_task(station_flow.run_setup(session))
        await _wait_form(session, "user")
        session.handle_submit({CONF_YM_INSTANCE: BORROW_SOURCE_OWN})
        await _wait_form(session, "method")
        session.handle_submit(
            {station_flow.CONF_METHOD: station_flow.METHOD_QR, CONF_REMEMBER_SESSION: False}
        )
        await _wait_for(lambda: session.finished)
        await task

    assert collected == {
        CONF_YM_INSTANCE: BORROW_SOURCE_OWN,
        CONF_MUSIC_TOKEN: "MT",
        CONF_X_TOKEN: None,
        CONF_REFRESH_TOKEN: None,
    }
