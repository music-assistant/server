"""Tests for the NetEase Cloud Music interactive setup flow."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import Mock, patch

from music_assistant_models.enums import FlowStepType

from music_assistant.models.setup_flow import (
    SetupFlowContext,
    SetupFlowError,
    SetupSession,
)
from music_assistant.providers.neteasecloudmusic import setup_flow as ncm_flow
from music_assistant.providers.neteasecloudmusic.constants import (
    CONF_API_BASE_URL,
    CONF_COOKIE,
    CONF_UID,
)

_QR_IMAGE = "data:image/png;base64,QR-IMAGE-DATA"


class _FakeClient:
    """Canned NeteaseCloudMusicApi client that walks a QR login to confirmation."""

    def __init__(self, *_args: Any, check_codes: list[int] | None = None) -> None:
        # default: one expiry (800) then confirmation (803), exercising the refresh loop
        self._check_codes = check_codes if check_codes is not None else [800, 803]
        self._check_idx = 0
        self.calls: list[str] = []

    async def get(self, path: str, **_kwargs: Any) -> dict[str, Any]:
        """Return a canned payload keyed by request path."""
        self.calls.append(path)
        if path == "/login/qr/key":
            return {"code": 200, "data": {"unikey": "unikey-123"}}
        if path == "/login/qr/create":
            return {"code": 200, "data": {"qrimg": _QR_IMAGE}}
        if path == "/login/qr/check":
            code = self._check_codes[min(self._check_idx, len(self._check_codes) - 1)]
            self._check_idx += 1
            if code == 803:
                return {"code": 803, "cookie": "MUSIC_U=secret-cookie"}
            return {"code": code}
        if path == "/login/status":
            return {"code": 200, "data": {"profile": {"userId": 42}}}
        raise AssertionError(f"unexpected path {path}")


def _make_session(finish_handler: Any) -> tuple[SetupSession, Mock]:
    """Build a SetupSession backed by a Mock mass for driving run_setup directly."""
    mass = Mock()
    context = SetupFlowContext(kind="setup", reason="user", domain="neteasecloudmusic")
    session = SetupSession(mass, "flow-test", context, finish_handler)
    return session, mass


def _published_steps(mass: Mock) -> list[Any]:
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


async def test_run_setup_qr_refresh_and_finish() -> None:
    """The QR flow collects the backend, refreshes an expired QR, then stores cookie/uid."""
    collected: dict[str, Any] = {}

    async def finish_handler(_session: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"instance_id": "neteasecloudmusic--test"}

    session, mass = _make_session(finish_handler)
    fake_client = _FakeClient()

    with patch.object(ncm_flow, "NcmApiClient", return_value=fake_client):
        task = asyncio.create_task(ncm_flow.run_setup(session))
        # first step is the backend-url form
        user_step = await _wait_for(
            lambda: (
                session.current_step
                if session.current_step and session.current_step.type == FlowStepType.FORM
                else None
            )
        )
        assert user_step.step_id == "user"
        session.handle_submit({CONF_API_BASE_URL: "http://127.0.0.1:3000"})
        await _wait_for(lambda: session.finished)
        await task

    # cookie + uid resolved and the chosen backend url are persisted together
    assert collected == {
        CONF_API_BASE_URL: "http://127.0.0.1:3000",
        CONF_COOKIE: "MUSIC_U=secret-cookie",
        CONF_UID: "42",
    }
    # the expired code triggered a second key/create/check round (refresh loop)
    assert fake_client.calls.count("/login/qr/key") == 2
    assert fake_client.calls.count("/login/qr/check") == 2
    # the QR image is emitted verbatim on the scan_qr progress step(s)
    progress_steps = [step for step in _published_steps(mass) if step.type == FlowStepType.PROGRESS]
    assert progress_steps
    assert all(step.step_id == "scan_qr" for step in progress_steps)
    assert all(step.image == _QR_IMAGE for step in progress_steps)


async def test_run_setup_retries_form_on_finish_error() -> None:
    """A finish failure re-renders the user form with the error, then succeeds on retry."""
    attempts = {"count": 0}

    async def finish_handler(_session: SetupSession, _values: dict[str, Any]) -> dict[str, str]:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise SetupFlowError("bad backend", translation_key="login_failed")
        return {"instance_id": "neteasecloudmusic--test"}

    session, _mass = _make_session(finish_handler)

    # a fresh client per instantiation so each attempt confirms immediately
    with patch.object(
        ncm_flow, "NcmApiClient", side_effect=lambda *_a, **_k: _FakeClient(check_codes=[803])
    ):
        task = asyncio.create_task(ncm_flow.run_setup(session))
        await _wait_for(
            lambda: session.current_step and session.current_step.type == FlowStepType.FORM
        )
        session.handle_submit({CONF_API_BASE_URL: "http://127.0.0.1:3000"})
        # after the failed finish the flow loops back to the user form with the error
        error_form = await _wait_for(
            lambda: (
                session.current_step
                if session.current_step
                and session.current_step.type == FlowStepType.FORM
                and session.current_step.errors
                else None
            )
        )
        assert error_form.errors == {"base": "login_failed"}
        session.handle_submit({CONF_API_BASE_URL: "http://127.0.0.1:3000"})
        await _wait_for(lambda: session.finished)
        await task

    assert attempts["count"] == 2
