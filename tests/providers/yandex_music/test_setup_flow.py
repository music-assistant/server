"""Tests for the Yandex Music interactive setup flow (run_setup)."""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any
from unittest import mock
from urllib.parse import unquote

import pytest
from music_assistant_models.enums import ConfigEntryType, FlowStepType
from ya_passport_auth import Credentials, DeviceCodeSession, QrSession, SecretStr
from ya_passport_auth.exceptions import DeviceCodeTimeoutError, QRTimeoutError

from music_assistant.models.setup_flow import SetupFlowContext, SetupSession, StepExpiredError
from music_assistant.providers.yandex_music import setup_flow as ym_flow
from music_assistant.providers.yandex_music.constants import (
    CONF_REFRESH_TOKEN,
    CONF_REMEMBER_SESSION,
    CONF_TOKEN,
    CONF_X_TOKEN,
)


class _FakeClient:
    """Canned PassportClient that confirms a QR/device login (optionally after one expiry)."""

    def __init__(
        self,
        creds: Credentials,
        *,
        qr_fail_first: bool = False,
        device_fail_first: bool = False,
    ) -> None:
        self._creds = creds
        self.qr_starts = 0
        self._qr_polls = 0
        self._qr_fail_first = qr_fail_first
        self.device_starts = 0
        self._device_polls = 0
        self._device_fail_first = device_fail_first

    async def start_qr_login(self) -> QrSession:
        self.qr_starts += 1
        return QrSession(track_id="t", csrf_token="c", qr_url="https://passport.yandex.ru/qr/abc")

    async def poll_qr_until_confirmed(self, _qr: QrSession, **_kwargs: Any) -> Credentials:
        self._qr_polls += 1
        if self._qr_fail_first and self._qr_polls == 1:
            raise QRTimeoutError("expired")
        return self._creds

    async def start_device_login(self, **_kwargs: Any) -> DeviceCodeSession:
        self.device_starts += 1
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
        self._device_polls += 1
        if self._device_fail_first and self._device_polls == 1:
            raise DeviceCodeTimeoutError("expired")
        return self._creds


class _HangingClient(_FakeClient):
    """Passport client whose confirmation polls never finish on their own."""

    async def poll_qr_until_confirmed(self, _qr: QrSession, **_kwargs: Any) -> Credentials:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def poll_device_until_confirmed(
        self, _session: DeviceCodeSession, **_kwargs: Any
    ) -> Credentials:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


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


async def _assert_login_has_hard_timeout(
    login: Callable[[SetupSession], Awaitable[Credentials]],
) -> None:
    """Assert an abandoned login expires even while Yandex polling remains pending."""
    creds = Credentials(x_token=SecretStr("XT"), music_token=SecretStr("MT"))
    client = _HangingClient(creds)
    session = mock.Mock(spec=SetupSession)

    async def progress_until(awaitable: Awaitable[Credentials], **_kwargs: Any) -> Credentials:
        return await awaitable

    session.progress_until = mock.AsyncMock(side_effect=progress_until)
    with (
        mock.patch.object(ym_flow, "_AUTH_FLOW_TIMEOUT_SECONDS", 0.01),
        mock.patch.object(ym_flow, "PassportClient") as passport_client,
    ):
        passport_client.create.return_value = _async_cm(client)
        with pytest.raises(StepExpiredError):
            await asyncio.wait_for(login(session), timeout=0.2)


def test_qr_image_has_opaque_white_quiet_zone() -> None:
    """The QR remains high-contrast against Music Assistant's dark theme."""
    image = ym_flow._qr_image("https://passport.yandex.ru/qr/test")
    svg = unquote(image.split(",", 1)[1])

    assert "<path fill='#fff' d='M0 0h37v37h-37z'/>" in svg
    assert "<path class='qrline' stroke='#000' d='M4 4.5" in svg


async def test_qr_login_has_hard_timeout() -> None:
    """An abandoned QR login cannot refresh codes forever."""
    await _assert_login_has_hard_timeout(ym_flow._qr_login)


async def test_device_login_has_hard_timeout() -> None:
    """An abandoned Device Code login cannot refresh codes forever."""
    await _assert_login_has_hard_timeout(ym_flow._device_login)


async def test_device_countdown_respects_hard_timeout() -> None:
    """The displayed Device Code lifetime cannot exceed the whole flow lifetime."""
    creds = Credentials(x_token=SecretStr("XT"), music_token=SecretStr("MT"))
    client = _FakeClient(creds)
    session = mock.Mock(spec=SetupSession)
    shown_expiry: float | None = None

    async def progress_until(
        awaitable: Awaitable[Credentials], *, expires_in: float, **_kwargs: Any
    ) -> Credentials:
        nonlocal shown_expiry
        shown_expiry = expires_in
        return await awaitable

    session.progress_until = mock.AsyncMock(side_effect=progress_until)
    with (
        mock.patch.object(ym_flow, "_AUTH_FLOW_TIMEOUT_SECONDS", 10),
        mock.patch.object(ym_flow, "PassportClient") as passport_client,
    ):
        passport_client.create.return_value = _async_cm(client)
        await ym_flow._device_login(session)

    assert shown_expiry is not None
    assert 0 < shown_expiry <= 10


def test_device_image_makes_verification_address_prominent() -> None:
    """The non-clickable fallback clearly tells users where to enter the code."""
    image = ym_flow._device_image("ABCD-1234", "https://ya.ru/device")
    svg = base64.b64decode(image.split(",", 1)[1]).decode("utf-8")

    assert "Open this address in a browser" in svg
    assert ">ya.ru/device</text>" in svg
    assert "https://ya.ru/device" not in svg
    address = svg.split(">ya.ru/device</text>", 1)[0].rsplit("<text", 1)[1]
    assert 'font-size="24"' in address


async def test_manual_token_is_only_shown_after_selecting_its_method() -> None:
    """QR and Device Code never render the secure token input on their method form."""

    async def finish(_s: SetupSession, _values: dict[str, Any]) -> dict[str, str]:
        raise AssertionError("form inspection must not finish the flow")

    session, _mass = _make_session(finish)
    task = asyncio.create_task(ym_flow.run_setup(session))
    try:
        form = await _wait_for(
            lambda: (
                session.current_step
                if session.current_step and session.current_step.type == FlowStepType.FORM
                else None
            )
        )
        method_entry = next(entry for entry in form.entries if entry.key == ym_flow.CONF_METHOD)
        assert {option.value for option in method_entry.options} >= {"qr", "device", "token"}
        assert method_entry.default_value == ym_flow.METHOD_QR
        assert CONF_TOKEN not in {entry.key for entry in form.entries}

        session.handle_submit({ym_flow.CONF_METHOD: "token", CONF_REMEMBER_SESSION: True})
        token_form = await _wait_for(
            lambda: (
                session.current_step
                if session.current_step
                and session.current_step.type == FlowStepType.FORM
                and session.current_step.step_id == "token_login"
                else None
            ),
            timeout=0.5,
        )
        token_entry = next(entry for entry in token_form.entries if entry.key == CONF_TOKEN)
        assert token_entry.type == ConfigEntryType.SECURE_STRING
        assert token_entry.required is True
        assert {entry.key for entry in token_form.entries} == {CONF_TOKEN}
    finally:
        task.cancel()
        with suppress(BaseException):
            await task


async def test_manual_token_login_persists_only_submitted_token() -> None:
    """Manual login bypasses Passport and clears credentials from a previous session."""
    creds = Credentials(x_token=SecretStr("unused-XT"), music_token=SecretStr("unused-MT"))
    client = _FakeClient(creds)
    collected: dict[str, Any] = {}

    async def finish(_s: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"instance_id": "yandex_music--1"}

    session, _mass = _make_session(finish)
    with mock.patch.object(ym_flow, "PassportClient") as passport_client:
        passport_client.create.return_value = _async_cm(client)
        task = asyncio.create_task(ym_flow.run_setup(session))
        await _wait_for(lambda: session.current_step and session.current_step.step_id == "user")
        session.handle_submit({ym_flow.CONF_METHOD: "token", CONF_REMEMBER_SESSION: True})
        await _wait_for(
            lambda: session.current_step and session.current_step.step_id == "token_login",
            timeout=0.5,
        )
        session.handle_submit({CONF_TOKEN: "manual-token"})
        await _wait_for(lambda: session.finished)
        await task

    assert collected == {
        CONF_TOKEN: "manual-token",
        CONF_X_TOKEN: None,
        CONF_REFRESH_TOKEN: None,
    }
    passport_client.create.assert_not_called()


async def test_manual_token_login_rejects_empty_token() -> None:
    """Selecting manual login without a token re-renders the form with a field error."""

    async def finish(_s: SetupSession, _values: dict[str, Any]) -> dict[str, str]:
        raise AssertionError("an empty manual token must not finish the flow")

    session, _mass = _make_session(finish)
    task = asyncio.create_task(ym_flow.run_setup(session))
    try:
        await _wait_for(lambda: session.current_step and session.current_step.step_id == "user")
        session.handle_submit({ym_flow.CONF_METHOD: "token", CONF_REMEMBER_SESSION: True})
        await _wait_for(
            lambda: session.current_step and session.current_step.step_id == "token_login",
            timeout=0.5,
        )
        session.handle_submit({CONF_TOKEN: ""})
        await _wait_for(
            lambda: (
                session.current_step and session.current_step.errors.get(CONF_TOKEN) == "required"
            ),
            timeout=0.5,
        )
        assert session.current_step is not None
        assert session.current_step.errors == {CONF_TOKEN: "required"}
        assert not task.done()
    finally:
        task.cancel()
        with suppress(BaseException):
            await task


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
        form = await _wait_for(
            lambda: (
                session.current_step
                if session.current_step and session.current_step.type == FlowStepType.FORM
                else None
            )
        )
        method_entry = next(entry for entry in form.entries if entry.key == ym_flow.CONF_METHOD)
        assert method_entry.default_value == ym_flow.METHOD_QR
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


async def test_device_login_refreshes_expired_code() -> None:
    """An expired Device Code is minted afresh and the login still completes."""
    creds = Credentials(x_token=SecretStr("XT"), music_token=SecretStr("MT"))
    collected: dict[str, Any] = {}

    async def finish(_s: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"instance_id": "yandex_music--1"}

    session, _mass = _make_session(finish)
    client = _FakeClient(creds, device_fail_first=True)
    with mock.patch.object(ym_flow, "PassportClient") as passport_client:
        passport_client.create.return_value = _async_cm(client)
        task = asyncio.create_task(ym_flow.run_setup(session))
        await _drive(
            session, {ym_flow.CONF_METHOD: ym_flow.METHOD_DEVICE, CONF_REMEMBER_SESSION: True}
        )
        await task

    assert collected[CONF_TOKEN] == "MT"
    assert client.device_starts == 2
