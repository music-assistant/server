"""Tests for the Yandex Ynison interactive setup flow (run_setup)."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest import mock

from music_assistant_models.enums import FlowStepType
from ya_passport_auth import Credentials, QrSession, SecretStr

from music_assistant.models.setup_flow import SetupFlowContext, SetupSession
from music_assistant.providers.yandex_ynison import setup_flow as yn_flow
from music_assistant.providers.yandex_ynison.constants import (
    CONF_ACCOUNT_LOGIN,
    CONF_REMEMBER_SESSION,
    CONF_TOKEN,
    CONF_X_TOKEN,
    CONF_YM_INSTANCE,
    YM_INSTANCE_OWN,
)


class _FakeClient:
    """Canned PassportClient that confirms a QR login."""

    def __init__(self, creds: Credentials) -> None:
        self._creds = creds
        self.qr_starts = 0

    async def start_qr_login(self) -> QrSession:
        self.qr_starts += 1
        return QrSession(track_id="t", csrf_token="c", qr_url="https://passport.yandex.ru/qr/abc")

    async def poll_qr_until_confirmed(self, _qr: QrSession, **_kwargs: Any) -> Credentials:
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
    context = SetupFlowContext(kind="setup", reason="user", domain="yandex_ynison")
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


async def _await_user_form(session: SetupSession) -> None:
    """Wait until the user form is presented."""
    await _wait_for(lambda: session.current_step and session.current_step.type == FlowStepType.FORM)


async def test_borrow_mode_finishes_with_instance_only() -> None:
    """Selecting a linked Yandex Music instance persists only that instance id."""
    collected: dict[str, Any] = {}

    async def finish(_s: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"instance_id": "yandex_ynison--1"}

    session, _mass = _make_session(
        finish, providers={"ym-a": {"domain": "yandex_music", "name": "Main"}}
    )
    task = asyncio.create_task(yn_flow.run_setup(session))
    await _await_user_form(session)
    session.handle_submit({CONF_YM_INSTANCE: "ym-a", CONF_REMEMBER_SESSION: True})
    await _wait_for(lambda: session.finished)
    await task

    assert collected == {CONF_YM_INSTANCE: "ym-a"}


async def test_own_mode_qr_persists_tokens_and_login() -> None:
    """Own-mode QR login persists music token, x_token (remember on) and display login."""
    creds = Credentials(x_token=SecretStr("XT"), music_token=SecretStr("MT"), display_login="alice")
    collected: dict[str, Any] = {}

    async def finish(_s: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"instance_id": "yandex_ynison--1"}

    session, mass = _make_session(finish)
    client = _FakeClient(creds)
    with mock.patch.object(yn_flow, "PassportClient") as pc:
        pc.create.return_value = _async_cm(client)
        task = asyncio.create_task(yn_flow.run_setup(session))
        await _await_user_form(session)
        session.handle_submit({CONF_YM_INSTANCE: YM_INSTANCE_OWN, CONF_REMEMBER_SESSION: True})
        await _wait_for(lambda: session.finished)
        await task

    assert collected == {
        CONF_YM_INSTANCE: YM_INSTANCE_OWN,
        CONF_TOKEN: "MT",
        CONF_X_TOKEN: "XT",
        CONF_ACCOUNT_LOGIN: "alice",
    }
    scan_steps = [s for s in _published_steps(mass) if s.step_id == "scan_qr"]
    assert scan_steps
    assert all(s.image and s.image.startswith("data:image/svg+xml") for s in scan_steps)


async def test_own_mode_without_remember_clears_x_token() -> None:
    """Own-mode QR login with remember off stores the music token but no x_token."""
    creds = Credentials(x_token=SecretStr("XT"), music_token=SecretStr("MT"), display_login="bob")
    collected: dict[str, Any] = {}

    async def finish(_s: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"instance_id": "yandex_ynison--1"}

    session, _mass = _make_session(finish)
    client = _FakeClient(creds)
    with mock.patch.object(yn_flow, "PassportClient") as pc:
        pc.create.return_value = _async_cm(client)
        task = asyncio.create_task(yn_flow.run_setup(session))
        await _await_user_form(session)
        session.handle_submit({CONF_YM_INSTANCE: YM_INSTANCE_OWN, CONF_REMEMBER_SESSION: False})
        await _wait_for(lambda: session.finished)
        await task

    assert collected[CONF_TOKEN] == "MT"
    assert collected[CONF_X_TOKEN] is None
