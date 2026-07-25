"""Tests for the Sendspin player interactive pairing setup flow (run_setup_flow)."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest import mock

import pytest
from aiosendspin.models.core import PairMethodDescriptor
from aiosendspin.models.types import PairAbortReason, PairMethod
from aiosendspin.noise.pairing import RemotePairingAbortError
from music_assistant_models.enums import FlowStepType

from music_assistant.models.setup_flow import AbortFlow, SetupFlowContext, SetupSession
from music_assistant.providers.sendspin.constants import (
    CONF_PAIRING_METHOD,
    CONF_PAIRING_PIN,
    CONF_PAIRING_TOKEN,
    PAIR_METHOD_PIN,
    PAIR_METHOD_TOKEN,
)
from music_assistant.providers.sendspin.helpers import SecurityActionError
from music_assistant.providers.sendspin.player import SendspinBasePlayer

if TYPE_CHECKING:
    from aiosendspin.server.client import SendspinClient
    from music_assistant_models.setup_flow import SetupFlowStep

    from music_assistant.providers.sendspin.provider import SendspinProvider


def _desc(method: PairMethod, *, locked_out: bool = False) -> PairMethodDescriptor:
    return PairMethodDescriptor(method=method, locked_out=locked_out)


class _FakePinSession:
    """Minimal PinPairingSession stand-in the fake provider hands back to the flow."""

    def __init__(self, *, awaiting_gesture: bool = False) -> None:
        self.pin_request_event = asyncio.Event()
        if not awaiting_gesture:
            self.pin_request_event.set()
        self.awaiting_pin = True
        self.finished = False
        self.error: Exception | None = None
        self.can_retry = False
        # None so the flow's post-submit "confirming" wait is skipped in tests.
        self.task: asyncio.Task[None] | None = None

    @property
    def awaiting_gesture(self) -> bool:
        return not self.pin_request_event.is_set()


class _FakeApi:
    """Fake SendspinClient exposing just what the flow reads (hello + security + roles)."""

    def __init__(self, methods: list[PairMethodDescriptor], *, active_roles: tuple[str, ...] = ()):
        self.info_or_none = SimpleNamespace(supported_pair_methods=list(methods))
        self.connection_security: object | None = object()
        self.active_roles = active_roles


class _FakeProvider:
    """Scripts the pairing primitives run_setup_flow drives, recording every call."""

    def __init__(
        self,
        api: _FakeApi,
        *,
        gesture: bool = False,
        submit_outcomes: list[str] | None = None,
        token_errors: list[Exception] | None = None,
    ) -> None:
        self.api = api
        self.session: _FakePinSession | None = None
        self.start_calls = 0
        self.static: bool | None = None
        self.submitted_pins: list[str] = []
        self.tokens: list[str] = []
        self.cancel_calls = 0
        self._gesture = gesture
        self._submit_outcomes = deque(submit_outcomes or [])
        self._token_errors = deque(token_errors or [])

    def pairing_config_snapshot(self, client_id: str) -> None:
        return None

    def get_pin_session(self, client_id: str) -> _FakePinSession | None:
        return self.session

    async def start_pin_pairing(
        self, client_id: str, *, verify: bool = False, static: bool = False
    ) -> _FakePinSession:
        self.start_calls += 1
        self.static = static
        if self.session is not None and self.session.can_retry:
            # A retryable session resumes in place, past the gesture, awaiting a PIN again.
            self.session.can_retry = False
            self.session.error = None
            self.session.awaiting_pin = True
            self.session.pin_request_event.set()
            return self.session
        self.session = _FakePinSession(awaiting_gesture=self._gesture)
        return self.session

    async def submit_pin(self, client_id: str, pin: str) -> None:
        assert self.session is not None
        self.submitted_pins.append(pin)
        outcome = self._submit_outcomes.popleft() if self._submit_outcomes else "success"
        if outcome == "success":
            self.session.finished = True
            self.session.error = None
            self.session.awaiting_pin = False
            self.api.active_roles = ("player",)
        elif outcome == "retry":
            self.session.can_retry = True
            self.session.error = RemotePairingAbortError(PairAbortReason.PIN_MISMATCH)

    async def cancel_pin_pairing(self, client_id: str) -> None:
        self.cancel_calls += 1
        self.session = None

    async def pair_with_token(self, client_id: str, token: str) -> None:
        self.tokens.append(token)
        if self._token_errors:
            raise self._token_errors.popleft()
        self.api.active_roles = ("player",)


def _make_player(api: _FakeApi, provider: _FakeProvider) -> SendspinBasePlayer:
    player = SendspinBasePlayer.__new__(SendspinBasePlayer)
    player._player_id = "client-1"
    player._provider = cast("SendspinProvider", provider)
    player.api = cast("SendspinClient", api)
    return player


def _make_session(finish_handler: Any) -> tuple[SetupSession, mock.Mock]:
    mass = mock.Mock()
    context = SetupFlowContext(kind="setup", reason="user", domain="sendspin", player_id="client-1")
    return SetupSession(mass, "flow-test", context, finish_handler), mass


async def _ok_finish(_session: SetupSession, _values: dict[str, Any]) -> dict[str, str]:
    """Finish handler stand-in that accepts any values and reports the player id."""
    return {"player_id": "client-1"}


def _published_steps(mass: mock.Mock) -> list[Any]:
    return [call.kwargs["data"] for call in mass.signal_event.call_args_list]


async def _wait_for(predicate: Any, timeout: float = 5.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


async def _wait_step(
    session: SetupSession,
    *,
    step_type: FlowStepType | None = None,
    step_id: str | None = None,
    with_errors: bool = False,
) -> SetupFlowStep:
    def _match() -> SetupFlowStep | None:
        step = session.current_step
        if step is None:
            return None
        if step_type is not None and step.type != step_type:
            return None
        if step_id is not None and step.step_id != step_id:
            return None
        if with_errors and not step.errors:
            return None
        return step

    return cast("SetupFlowStep", await _wait_for(_match))


async def test_select_method_pin_gesture_submit_success() -> None:
    """Select PIN, wait through the gesture, submit the PIN, succeed, and finish with {}."""
    collected: dict[str, Any] = {}

    async def finish(_s: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected["values"] = values
        return {"player_id": "client-1"}

    api = _FakeApi([_desc(PairMethod.DYNAMIC_PIN), _desc(PairMethod.PAIRING_PSK)])
    provider = _FakeProvider(api, gesture=True)
    session, mass = _make_session(finish)
    player = _make_player(api, provider)

    task = asyncio.create_task(player.run_setup_flow(session))
    step = await _wait_step(session, step_type=FlowStepType.FORM, step_id="select_method")
    assert {option.value for option in step.entries[0].options} == {
        PAIR_METHOD_PIN,
        PAIR_METHOD_TOKEN,
    }
    session.handle_submit({CONF_PAIRING_METHOD: PAIR_METHOD_PIN})

    await _wait_step(session, step_type=FlowStepType.PROGRESS, step_id="awaiting_gesture")
    assert provider.session is not None
    provider.session.pin_request_event.set()

    await _wait_step(session, step_type=FlowStepType.FORM, step_id="enter_pin")
    session.handle_submit({CONF_PAIRING_PIN: "123456"})

    await _wait_for(lambda: session.finished)
    await task

    assert collected["values"] == {}
    assert provider.submitted_pins == ["123456"]
    assert provider.static is False
    assert provider.cancel_calls == 0
    steps = _published_steps(mass)
    assert [s.step_id for s in steps if s.type == FlowStepType.PROGRESS] == ["awaiting_gesture"]
    assert steps[-1].type == FlowStepType.FINISH


async def test_single_pin_method_skips_select() -> None:
    """A device offering one usable PIN method goes straight to the PIN form, no method select."""
    api = _FakeApi([_desc(PairMethod.DYNAMIC_PIN)])
    provider = _FakeProvider(api)
    session, mass = _make_session(_ok_finish)
    player = _make_player(api, provider)

    task = asyncio.create_task(player.run_setup_flow(session))
    await _wait_step(session, step_type=FlowStepType.FORM, step_id="enter_pin")
    assert not any(s.step_id == "select_method" for s in _published_steps(mass))
    session.handle_submit({CONF_PAIRING_PIN: "123456"})
    await _wait_for(lambda: session.finished)
    await task
    assert provider.submitted_pins == ["123456"]


async def test_pin_mismatch_retries_in_place_then_succeeds() -> None:
    """A mismatch re-renders the PIN form with a base error; the retry resumes and succeeds."""
    api = _FakeApi([_desc(PairMethod.DYNAMIC_PIN)])
    provider = _FakeProvider(api, submit_outcomes=["retry", "success"])
    session, _mass = _make_session(_ok_finish)
    player = _make_player(api, provider)

    task = asyncio.create_task(player.run_setup_flow(session))
    await _wait_step(session, step_type=FlowStepType.FORM, step_id="enter_pin")
    session.handle_submit({CONF_PAIRING_PIN: "000000"})

    error_step = await _wait_step(
        session, step_type=FlowStepType.FORM, step_id="enter_pin", with_errors=True
    )
    assert error_step.errors == {"base": "pairing_error_pin_mismatch"}
    session.handle_submit({CONF_PAIRING_PIN: "123456"})

    await _wait_for(lambda: session.finished)
    await task
    assert provider.submitted_pins == ["000000", "123456"]
    assert provider.start_calls == 2
    assert provider.cancel_calls == 0


async def test_abort_mid_pairing_runs_cleanup() -> None:
    """Cancelling the flow while a PIN session is in flight tears it down in the finally."""
    api = _FakeApi([_desc(PairMethod.DYNAMIC_PIN)])
    provider = _FakeProvider(api)
    session, _mass = _make_session(_ok_finish)
    player = _make_player(api, provider)

    task = asyncio.create_task(player.run_setup_flow(session))
    await _wait_step(session, step_type=FlowStepType.FORM, step_id="enter_pin")
    assert provider.session is not None

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert provider.cancel_calls == 1
    assert not session.finished


async def test_token_pairing_success() -> None:
    """A token-only device drives the token form and pairs on submit."""
    collected: dict[str, Any] = {}

    async def finish(_s: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected["values"] = values
        return {"player_id": "client-1"}

    api = _FakeApi([_desc(PairMethod.PAIRING_PSK)])
    provider = _FakeProvider(api)
    session, mass = _make_session(finish)
    player = _make_player(api, provider)

    task = asyncio.create_task(player.run_setup_flow(session))
    await _wait_step(session, step_type=FlowStepType.FORM, step_id="enter_token")
    assert not any(s.step_id == "select_method" for s in _published_steps(mass))
    session.handle_submit({CONF_PAIRING_TOKEN: "tok-123"})

    await _wait_for(lambda: session.finished)
    await task
    assert provider.tokens == ["tok-123"]
    assert collected["values"] == {}


async def test_token_invalid_re_renders_then_succeeds() -> None:
    """An invalid token re-renders the token form with a base error, then pairs on retry."""
    api = _FakeApi([_desc(PairMethod.PAIRING_PSK)])
    provider = _FakeProvider(api, token_errors=[SecurityActionError("pairing_error_token_invalid")])
    session, _mass = _make_session(_ok_finish)
    player = _make_player(api, provider)

    task = asyncio.create_task(player.run_setup_flow(session))
    await _wait_step(session, step_type=FlowStepType.FORM, step_id="enter_token")
    session.handle_submit({CONF_PAIRING_TOKEN: "bad"})

    error_step = await _wait_step(
        session, step_type=FlowStepType.FORM, step_id="enter_token", with_errors=True
    )
    assert error_step.errors == {"base": "pairing_error_token_invalid"}
    session.handle_submit({CONF_PAIRING_TOKEN: "good"})

    await _wait_for(lambda: session.finished)
    await task
    assert provider.tokens == ["bad", "good"]


async def test_no_pair_methods_aborts() -> None:
    """A device offering nothing usable aborts with the no_pair_methods reason."""
    api = _FakeApi([_desc(PairMethod.DYNAMIC_PIN, locked_out=True)])
    provider = _FakeProvider(api)
    session, _mass = _make_session(_ok_finish)
    player = _make_player(api, provider)

    with pytest.raises(AbortFlow) as excinfo:
        await player.run_setup_flow(session)
    assert excinfo.value.reason == "no_pair_methods"
    assert provider.start_calls == 0


async def test_unencrypted_connection_aborts() -> None:
    """An unencrypted (legacy) connection has nothing to pair and aborts."""
    api = _FakeApi([_desc(PairMethod.DYNAMIC_PIN)])
    api.connection_security = None
    provider = _FakeProvider(api)
    session, _mass = _make_session(_ok_finish)
    player = _make_player(api, provider)

    with pytest.raises(AbortFlow) as excinfo:
        await player.run_setup_flow(session)
    assert excinfo.value.reason == "nothing_to_configure"


def test_pairing_method_options_derivation() -> None:
    """Static PIN is only offered when both PIN methods are usable; token is independent."""
    api = _FakeApi([_desc(PairMethod.DYNAMIC_PIN), _desc(PairMethod.STATIC_PIN)])
    provider = _FakeProvider(api)
    player = _make_player(api, provider)
    assert player._pairing_method_options(cast("SendspinProvider", provider)) == [
        PAIR_METHOD_PIN,
        "static_pin",
    ]

    api_single = _FakeApi([_desc(PairMethod.STATIC_PIN), _desc(PairMethod.PAIRING_PSK)])
    provider_single = _FakeProvider(api_single)
    player_single = _make_player(api_single, provider_single)
    assert player_single._pairing_method_options(cast("SendspinProvider", provider_single)) == [
        PAIR_METHOD_PIN,
        PAIR_METHOD_TOKEN,
    ]
