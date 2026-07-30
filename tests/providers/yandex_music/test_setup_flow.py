"""Tests for the Yandex Music interactive setup flow (run_setup)."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest import mock

from music_assistant_models.enums import FlowStepType
from ya_passport_auth import Credentials, DeviceCodeSession, QrSession, SecretStr
from ya_passport_auth.exceptions import QRTimeoutError

from music_assistant.models.setup_flow import SetupFlowContext, SetupSession
from music_assistant.providers.yandex_music import setup_flow as ym_flow
from music_assistant.providers.yandex_music.constants import (
    CONF_REFRESH_TOKEN,
    CONF_REMEMBER_SESSION,
    CONF_TOKEN,
    CONF_X_TOKEN,
)


class _FakeClient:
    """Canned PassportClient that confirms a QR/device login (optionally after one expiry)."""

    def __init__(self, creds: Credentials, *, qr_fail_first: bool = False) -> None:
        self._creds = creds
        self.qr_starts = 0
        self._qr_polls = 0
        self._qr_fail_first = qr_fail_first

    async def start_qr_login(self) -> QrSession:
        self.qr_starts += 1
        return QrSession(track_id="t", csrf_token="c", qr_url="https://passport.yandex.ru/qr/abc")

    async def poll_qr_until_confirmed(self, _qr: QrSession, **_kwargs: Any) -> Credentials:
        self._qr_polls += 1
        if self._qr_fail_first and self._qr_polls == 1:
            raise QRTimeoutError("expired")
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


def _make_session(finish_handler: Any) -> tuple[SetupSession, mock.Mock]:
    """Build a real SetupSession backed by a Mock mass for driving run_setup directly."""
    mass = mock.Mock()
    context = SetupFlowContext(kind="setup", reason="user", domain="yandex_music")
    return SetupSession(mass, "flow-test", context, finish_handler), mass


def _published_steps(mass: mock.Mock) -> list[Any]:
    """Return the flow steps pushed through mass.signal_event, in order."""
    return [call.kwargs["data"] for call in mass.signal_event.call_args_list]


async def _wait_for(predicate: Any, timeout: float = 5.0) -> Any:
    """Wait until the predicate returns truthy (or fail the test)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


async def _drive(session: SetupSession, submit: dict[str, Any]) -> None:
    """Wait for the user form, submit the given values, then wait for finish."""
    await _wait_for(lambda: session.current_step and session.current_step.type == FlowStepType.FORM)
    session.handle_submit(submit)
    await _wait_for(lambda: session.finished)


async def test_device_login_remember_persists_full_triple() -> None:
    """Device login with remember on persists music + x + refresh tokens."""
    creds = Credentials(
        x_token=SecretStr("XT"),
        music_token=SecretStr("MT"),
        refresh_token=SecretStr("RT"),
        display_login="alice",
        uid=1,
    )
    collected: dict[str, Any] = {}

    async def finish(_s: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"instance_id": "yandex_music--1"}

    session, mass = _make_session(finish)
    client = _FakeClient(creds)
    with mock.patch.object(ym_flow, "PassportClient") as pc:
        pc.create.return_value = _async_cm(client)
        task = asyncio.create_task(ym_flow.run_setup(session))
        await _drive(
            session, {ym_flow.CONF_METHOD: ym_flow.METHOD_DEVICE, CONF_REMEMBER_SESSION: True}
        )
        await task

    assert collected == {CONF_TOKEN: "MT", CONF_X_TOKEN: "XT", CONF_REFRESH_TOKEN: "RT"}
    progress = [s for s in _published_steps(mass) if s.type == FlowStepType.PROGRESS]
    assert progress
    assert progress[0].step_id == "device_login"
    assert progress[0].image is not None
    assert progress[0].image.startswith("data:image/svg+xml")


async def test_qr_login_without_remember_stores_music_token_only() -> None:
    """QR login with remember off stores only the music token (x/refresh cleared)."""
    creds = Credentials(x_token=SecretStr("XT"), music_token=SecretStr("MT"), display_login="bob")
    collected: dict[str, Any] = {}

    async def finish(_s: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"instance_id": "yandex_music--1"}

    session, mass = _make_session(finish)
    client = _FakeClient(creds)
    with mock.patch.object(ym_flow, "PassportClient") as pc:
        pc.create.return_value = _async_cm(client)
        task = asyncio.create_task(ym_flow.run_setup(session))
        await _drive(
            session, {ym_flow.CONF_METHOD: ym_flow.METHOD_QR, CONF_REMEMBER_SESSION: False}
        )
        await task

    assert collected == {CONF_TOKEN: "MT", CONF_X_TOKEN: None, CONF_REFRESH_TOKEN: None}
    scan_steps = [s for s in _published_steps(mass) if s.step_id == "scan_qr"]
    assert scan_steps
    assert all(s.image and s.image.startswith("data:image/svg+xml") for s in scan_steps)


async def test_qr_login_refreshes_expired_code() -> None:
    """An expired QR code is minted afresh and the login still completes."""
    creds = Credentials(x_token=SecretStr("XT"), music_token=SecretStr("MT"))
    collected: dict[str, Any] = {}

    async def finish(_s: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"instance_id": "yandex_music--1"}

    session, _mass = _make_session(finish)
    client = _FakeClient(creds, qr_fail_first=True)
    with mock.patch.object(ym_flow, "PassportClient") as pc:
        pc.create.return_value = _async_cm(client)
        task = asyncio.create_task(ym_flow.run_setup(session))
        await _drive(session, {ym_flow.CONF_METHOD: ym_flow.METHOD_QR, CONF_REMEMBER_SESSION: True})
        await task

    assert collected[CONF_TOKEN] == "MT"
    # the expired code triggered a second start_qr_login (refresh loop)
    assert client.qr_starts == 2
