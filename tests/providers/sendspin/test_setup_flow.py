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
from aiosendspin.noise.trust_store import PskCategory
from music_assistant_models.enums import ConfigEntryType, FlowStepType

from music_assistant.models.setup_flow import (
    FINISH_STEP_SILENT,
    AbortFlow,
    SetupFlowContext,
    SetupSession,
    StepExpiredError,
)
from music_assistant.providers.sendspin import player as player_module
from music_assistant.providers.sendspin.constants import (
    CONF_PAIR_DEVICE,
    CONF_PAIRING_METHOD,
    CONF_PAIRING_PIN,
    CONF_PAIRING_TOKEN,
    CONF_SOURCE_APPROVAL_DISMISSED,
    CONF_SOURCE_INPUT_ACTION,
    PAIR_METHOD_DYNAMIC_PIN,
    PAIR_METHOD_PIN,
    PAIR_METHOD_STATIC_PIN,
    PAIR_METHOD_TOKEN,
    SOURCE_INPUT_DISMISS,
    SOURCE_INPUT_PAIR,
)
from music_assistant.providers.sendspin.helpers import SecurityActionError
from music_assistant.providers.sendspin.player import SendspinBasePlayer
from tests.common import collect_loop_errors

if TYPE_CHECKING:
    from aiosendspin.server.client import SendspinClient
    from music_assistant_models.setup_flow import SetupFlowStep

    from music_assistant.providers.sendspin.provider import SendspinProvider


def _desc(
    method: PairMethod,
    *,
    locations: list[str] | None = None,
    out_channels: list[str] | None = None,
) -> PairMethodDescriptor:
    return PairMethodDescriptor(method=method, locations=locations, out_channels=out_channels)


class _FakePinSession:
    """Minimal PinPairingSession stand-in the fake provider hands back to the flow."""

    def __init__(
        self,
        *,
        awaiting_gesture: bool = False,
        verify: bool = False,
        method: PairMethod = PairMethod.DYNAMIC_PIN,
    ) -> None:
        self.pin_request_event = asyncio.Event()
        self.gesture_event = asyncio.Event()
        if awaiting_gesture:
            # A gesture-gated device reports pair-pending before asking for the PIN.
            self.gesture_event.set()
        else:
            self.pin_request_event.set()
        self.awaiting_pin = True
        self.finished = False
        self.error: Exception | None = None
        self.can_retry = False
        self.verify = verify
        self.method = method
        self.pin_length: int | None = 6 if method is PairMethod.DYNAMIC_PIN else None
        # None so the flow's post-submit "confirming" wait is skipped in tests.
        self.task: asyncio.Task[None] | None = None

    @property
    def awaiting_first_message(self) -> bool:
        return not self.gesture_event.is_set() and not self.pin_request_event.is_set()

    @property
    def awaiting_gesture(self) -> bool:
        return self.gesture_event.is_set() and not self.pin_request_event.is_set()

    async def wait_first_message(self) -> None:
        await self.pin_request_event.wait()

    async def wait_pin_request(self) -> None:
        await self.pin_request_event.wait()


class _FakeApi:
    """Fake SendspinClient exposing just what the flow reads (hello + security + roles)."""

    def __init__(
        self,
        methods: list[PairMethodDescriptor],
        *,
        active_roles: tuple[str, ...] = (),
        psk_category: PskCategory = PskCategory.SENTINEL,
        unpaired_access: bool = False,
    ):
        self.info_or_none = SimpleNamespace(
            supported_pair_methods=list(methods),
            unpaired_access=SimpleNamespace(enabled=unpaired_access),
        )
        self.connection_security: Any = SimpleNamespace(psk_category=psk_category)
        self.active_roles = active_roles
        self.negotiated_role_ids: list[str] = []

    def roles_by_family(self, family: str) -> list[str]:
        return [role for role in self.active_roles if role.startswith(f"{family}@")]


class _FakePairingStore:
    """Pairing-store stand-in serving a scripted record and unpaired-trust state."""

    def __init__(self, record: Any = None, trusted: Any = None) -> None:
        self.record = record
        self.trusted = trusted

    async def record_by_client_id(self, client_id: str) -> Any:
        return self.record

    async def trusted_unpaired(self, client_id: str) -> Any:
        return self.trusted


class _FakeProvider:
    """Scripts the pairing primitives run_setup_flow drives, recording every call."""

    def __init__(
        self,
        api: _FakeApi,
        *,
        gesture: bool = False,
        submit_outcomes: list[str] | None = None,
        token_errors: list[Exception] | None = None,
        record: Any = None,
        trusted: Any = None,
    ) -> None:
        self.api = api
        self.server_api = SimpleNamespace(pairing_store=_FakePairingStore(record, trusted))
        self.session: _FakePinSession | None = None
        self.start_calls = 0
        self.static: bool | None = None
        self.verify: bool | None = None
        self.submitted_pins: list[str] = []
        self.tokens: list[str] = []
        self.cancel_calls = 0
        self.clear_calls = 0
        self.trust_calls: list[bool] = []
        self._gesture = gesture
        self._submit_outcomes = deque(submit_outcomes or [])
        self._token_errors = deque(token_errors or [])

    def pairing_config_snapshot(self, client_id: str) -> None:
        return None

    def get_pin_session(self, client_id: str) -> _FakePinSession | None:
        return self.session

    def clear_pin_session(self, client_id: str) -> None:
        self.clear_calls += 1
        if self.session is not None and self.session.finished:
            self.session = None

    async def start_pin_pairing(
        self, client_id: str, *, verify: bool = False, static: bool = False
    ) -> _FakePinSession:
        self.start_calls += 1
        self.static = static
        self.verify = verify
        if self.session is not None and self.session.can_retry:
            # A retryable session resumes in place, past the gesture, awaiting a PIN again.
            self.session.can_retry = False
            self.session.error = None
            self.session.awaiting_pin = True
            self.session.pin_request_event.set()
            return self.session
        offered = {d.method for d in self.api.info_or_none.supported_pair_methods}
        dynamic_offered = PairMethod.DYNAMIC_PIN in offered
        self.session = _FakePinSession(
            awaiting_gesture=self._gesture,
            verify=verify,
            method=(
                PairMethod.DYNAMIC_PIN if dynamic_offered and not static else PairMethod.STATIC_PIN
            ),
        )
        return self.session

    def submit_pin(self, client_id: str, pin: str) -> None:
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
        elif outcome == "session_lost":
            self.session = None
            raise SecurityActionError("pairing_error_no_pin_session")

    async def cancel_pin_pairing(self, client_id: str) -> None:
        self.cancel_calls += 1
        self.session = None

    async def set_trusted_unpaired(self, client_id: str, enabled: bool) -> None:
        self.trust_calls.append(enabled)
        if enabled:
            self.server_api.pairing_store.trusted = object()

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

    api = _FakeApi([_desc(PairMethod.DYNAMIC_PIN), _desc(PairMethod.STATIC_PIN)])
    provider = _FakeProvider(api, gesture=True)
    session, mass = _make_session(finish)
    player = _make_player(api, provider)

    task = asyncio.create_task(player.run_setup_flow(session))
    step = await _wait_step(session, step_type=FlowStepType.FORM, step_id="select_method")
    assert {option.value for option in step.entries[0].options} == {
        PAIR_METHOD_DYNAMIC_PIN,
        PAIR_METHOD_STATIC_PIN,
    }
    # the method is rendered as an expanded (radio) list with nothing preselected
    assert step.entries[0].expanded_options is True
    assert step.entries[0].default_value is None
    session.handle_submit({CONF_PAIRING_METHOD: PAIR_METHOD_DYNAMIC_PIN})

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
    assert provider.verify is False
    assert provider.cancel_calls == 0
    assert provider.clear_calls == 1
    steps = _published_steps(mass)
    assert [s.step_id for s in steps if s.type == FlowStepType.PROGRESS] == ["awaiting_gesture"]
    assert steps[-1].type == FlowStepType.FINISH


async def test_confirming_wait_failure_after_deadline_logs_no_loop_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pairing attempt failing after the confirming step expired is not reported to the loop."""
    release = asyncio.Event()

    async def _failing_attempt() -> None:
        await release.wait()
        raise RuntimeError("refreshing the player failed")

    monkeypatch.setattr(player_module, "PAIR_CONFIRM_TIMEOUT", 0.01)
    api = _FakeApi([_desc(PairMethod.DYNAMIC_PIN)])
    provider = _FakeProvider(api)
    session, _mass = _make_session(_ok_finish)
    player = _make_player(api, provider)

    with collect_loop_errors() as reported:
        flow = asyncio.create_task(player.run_setup_flow(session))
        await _wait_step(session, step_type=FlowStepType.FORM, step_id="enter_pin")
        assert provider.session is not None
        attempt = asyncio.create_task(_failing_attempt())
        provider.session.task = attempt
        session.handle_submit({CONF_PAIRING_PIN: "123456"})

        # let the attempt fail only once the confirming step has expired and the flow has
        # moved on, so the failure reliably lands after the flow stopped waiting for it
        await _wait_for(lambda: session.finished)
        await flow
        release.set()
        with pytest.raises(RuntimeError, match="refreshing the player failed"):
            await attempt

    assert reported == []


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


async def test_trusted_unpaired_pin_mismatch_still_retries() -> None:
    """With unpaired access already trusted, a mismatch must not be misreported as success."""
    api = _FakeApi(
        [_desc(PairMethod.DYNAMIC_PIN)],
        active_roles=("player",),
        unpaired_access=True,
    )
    provider = _FakeProvider(api, submit_outcomes=["retry", "success"], trusted=object())
    session, mass = _make_session(_ok_finish)
    player = _make_player(api, provider)

    task = asyncio.create_task(player.run_setup_flow(session))
    # trusted-unpaired devices are not offered the unpaired option again
    await _wait_step(session, step_type=FlowStepType.FORM, step_id="enter_pin")
    assert not any(s.step_id == "select_method" for s in _published_steps(mass))
    session.handle_submit({CONF_PAIRING_PIN: "000000"})

    error_step = await _wait_step(
        session, step_type=FlowStepType.FORM, step_id="enter_pin", with_errors=True
    )
    assert error_step.errors == {"base": "pairing_error_pin_mismatch"}
    session.handle_submit({CONF_PAIRING_PIN: "123456"})

    await _wait_for(lambda: session.finished)
    await task
    assert provider.submitted_pins == ["000000", "123456"]
    assert provider.trust_calls == []


async def test_consent_step_grants_trust() -> None:
    """Submitting the consent step without opting into pairing allows unpaired playback."""
    api = _FakeApi([_desc(PairMethod.DYNAMIC_PIN)], unpaired_access=True)
    provider = _FakeProvider(api)
    session, _mass = _make_session(_ok_finish)
    player = _make_player(api, provider)

    task = asyncio.create_task(player.run_setup_flow(session))
    step = await _wait_step(session, step_type=FlowStepType.FORM, step_id="approve_device")
    assert [entry.key for entry in step.entries] == [CONF_PAIR_DEVICE]
    session.handle_submit({})

    await _wait_for(lambda: session.finished)
    await task
    assert provider.trust_calls == [True]
    assert provider.start_calls == 0
    assert provider.tokens == []
    # a one-click allow closes the dialog without a success screen
    assert session.finish_step_id == FINISH_STEP_SILENT


async def test_consent_without_pair_methods_still_asks() -> None:
    """The unpaired grant is never automatic: a device without pair methods still asks."""
    api = _FakeApi([], unpaired_access=True)
    provider = _FakeProvider(api)
    session, _mass = _make_session(_ok_finish)
    player = _make_player(api, provider)

    task = asyncio.create_task(player.run_setup_flow(session))
    step = await _wait_step(session, step_type=FlowStepType.FORM, step_id="approve_device")
    assert step.entries == []
    session.handle_submit({})

    await _wait_for(lambda: session.finished)
    await task
    assert provider.trust_calls == [True]


async def test_consent_on_combo_declines_the_input_in_one_click() -> None:
    """A plain allow on a combo also declines the pending audio input, one submit total."""
    api = _FakeApi([_desc(PairMethod.DYNAMIC_PIN)], unpaired_access=True)
    api.negotiated_role_ids = ["player@v1", "source@v1"]
    provider = _FakeProvider(api)
    session, _mass = _make_session(_ok_finish)
    player = _make_player(api, provider)
    mass = _attach_mass(player)

    task = asyncio.create_task(player.run_setup_flow(session))
    step = await _wait_step(session, step_type=FlowStepType.FORM, step_id="approve_device_source")
    assert [entry.key for entry in step.entries] == [CONF_PAIR_DEVICE]
    assert step.last_step is True
    session.handle_submit({})

    await _wait_for(lambda: session.finished)
    await task
    assert provider.trust_calls == [True]
    mass.config.set_raw_player_config_value.assert_called_once_with(
        "client-1", CONF_SOURCE_APPROVAL_DISMISSED, True
    )


async def test_consent_opting_into_pairing_pairs_instead() -> None:
    """Ticking the pairing opt-in continues into the pair-method selection, granting nothing."""
    api = _FakeApi(
        [_desc(PairMethod.DYNAMIC_PIN), _desc(PairMethod.STATIC_PIN)], unpaired_access=True
    )
    provider = _FakeProvider(api)
    session, _mass = _make_session(_ok_finish)
    player = _make_player(api, provider)

    task = asyncio.create_task(player.run_setup_flow(session))
    await _wait_step(session, step_type=FlowStepType.FORM, step_id="approve_device")
    session.handle_submit({CONF_PAIR_DEVICE: True})

    step = await _wait_step(session, step_type=FlowStepType.FORM, step_id="select_method")
    assert {option.value for option in step.entries[0].options} == {
        PAIR_METHOD_DYNAMIC_PIN,
        PAIR_METHOD_STATIC_PIN,
    }
    session.handle_submit({CONF_PAIRING_METHOD: PAIR_METHOD_DYNAMIC_PIN})

    await _wait_step(session, step_type=FlowStepType.FORM, step_id="enter_pin")
    session.handle_submit({CONF_PAIRING_PIN: "123456"})

    await _wait_for(lambda: session.finished)
    await task
    assert provider.submitted_pins == ["123456"]
    assert provider.trust_calls == []


def _attach_mass(player: SendspinBasePlayer, *, dismissed: bool = False) -> mock.MagicMock:
    """Give a bare test player the mass surface the approval paths touch."""
    player.mass = mock.MagicMock()
    player.mass.config.get_raw_player_config_value = mock.Mock(return_value=dismissed)
    player.mass.config.save_player_config = mock.AsyncMock()
    player.update_state = mock.Mock()  # type: ignore[method-assign, misc]
    return player.mass


def _combo_api_with_pending_source() -> _FakeApi:
    api = _FakeApi(
        [_desc(PairMethod.DYNAMIC_PIN)], active_roles=("player@v1",), unpaired_access=True
    )
    api.negotiated_role_ids = ["player@v1", "source@v1"]
    return api


async def test_guest_device_with_an_input_consents_and_keeps_guest_access() -> None:
    """
    A guest device with a pending input consents on the approval step, not the input picker.

    Guest access already carries playback, so the only choice left is the optional upgrade
    to a pairing; finishing keeps guest access and leaves the input off.
    """
    api = _combo_api_with_pending_source()
    provider = _FakeProvider(api)
    session, _mass = _make_session(_ok_finish)
    player = _make_player(api, provider)
    mass = _attach_mass(player)

    task = asyncio.create_task(player.run_setup_flow(session))
    step = await _wait_step(session, step_type=FlowStepType.FORM, step_id="approve_device_source")
    assert [entry.key for entry in step.entries] == [CONF_PAIR_DEVICE]
    assert step.last_step is True
    session.handle_submit({CONF_PAIR_DEVICE: False})

    await _wait_for(lambda: session.finished)
    await task
    mass.config.set_raw_player_config_value.assert_called_once_with(
        "client-1", CONF_SOURCE_APPROVAL_DISMISSED, True
    )
    assert provider.trust_calls == [True]
    assert provider.start_calls == 0
    assert session.finish_step_id == FINISH_STEP_SILENT


async def test_input_picker_serves_a_device_that_withdrew_guest_access() -> None:
    """Without guest access on offer, a pending input still gets the pair-or-decline picker."""
    api = _combo_api_with_pending_source()
    api.info_or_none.unpaired_access = SimpleNamespace(enabled=False)
    provider = _FakeProvider(api)
    session, _mass = _make_session(_ok_finish)
    player = _make_player(api, provider)
    mass = _attach_mass(player)

    task = asyncio.create_task(player.run_setup_flow(session))
    step = await _wait_step(session, step_type=FlowStepType.FORM, step_id="source_input")
    assert [option.value for option in step.entries[0].options] == [
        SOURCE_INPUT_PAIR,
        SOURCE_INPUT_DISMISS,
    ]
    session.handle_submit({CONF_SOURCE_INPUT_ACTION: SOURCE_INPUT_DISMISS})

    await _wait_for(lambda: session.finished)
    await task
    mass.config.set_raw_player_config_value.assert_called_once_with(
        "client-1", CONF_SOURCE_APPROVAL_DISMISSED, True
    )
    assert provider.trust_calls == []
    assert session.finish_step_id == FINISH_STEP_SILENT


async def test_opting_into_pairing_for_the_input_offers_only_pair_methods() -> None:
    """Ticking the pairing box on the approval step never re-offers unpaired access or ignore."""
    api = _combo_api_with_pending_source()
    api.info_or_none.supported_pair_methods = [
        _desc(PairMethod.DYNAMIC_PIN),
        _desc(PairMethod.STATIC_PIN),
    ]
    provider = _FakeProvider(api)
    session, _mass = _make_session(_ok_finish)
    player = _make_player(api, provider)
    _attach_mass(player)

    task = asyncio.create_task(player.run_setup_flow(session))
    await _wait_step(session, step_type=FlowStepType.FORM, step_id="approve_device_source")
    session.handle_submit({CONF_PAIR_DEVICE: True})

    step = await _wait_step(session, step_type=FlowStepType.FORM, step_id="select_method")
    assert {option.value for option in step.entries[0].options} == {
        PAIR_METHOD_DYNAMIC_PIN,
        PAIR_METHOD_STATIC_PIN,
    }
    session.handle_submit({CONF_PAIRING_METHOD: PAIR_METHOD_DYNAMIC_PIN})

    await _wait_step(session, step_type=FlowStepType.FORM, step_id="enter_pin")
    session.handle_submit({CONF_PAIRING_PIN: "123456"})

    await _wait_for(lambda: session.finished)
    await task
    assert provider.submitted_pins == ["123456"]


async def test_verify_presence_on_paired_device() -> None:
    """Re-running the flow on a paired device runs the dynamic-PIN presence verification."""
    api = _FakeApi([_desc(PairMethod.DYNAMIC_PIN)], psk_category=PskCategory.LONG_TERM)
    record = SimpleNamespace(pair_methods=[PairMethod.STATIC_PIN])
    provider = _FakeProvider(api, record=record)
    session, _mass = _make_session(_ok_finish)
    player = _make_player(api, provider)

    task = asyncio.create_task(player.run_setup_flow(session))
    await _wait_step(session, step_type=FlowStepType.FORM, step_id="verify_pin")
    assert provider.verify is True
    assert provider.static is False
    session.handle_submit({CONF_PAIRING_PIN: "123456"})

    await _wait_for(lambda: session.finished)
    await task
    assert provider.submitted_pins == ["123456"]


async def test_paired_device_without_verification_aborts() -> None:
    """A paired device whose presence verification would add nothing aborts as already paired."""
    api = _FakeApi([_desc(PairMethod.DYNAMIC_PIN)], psk_category=PskCategory.LONG_TERM)
    record = SimpleNamespace(pair_methods=[PairMethod.DYNAMIC_PIN])
    provider = _FakeProvider(api, record=record)
    session, _mass = _make_session(_ok_finish)
    player = _make_player(api, provider)

    with pytest.raises(AbortFlow) as excinfo:
        await player.run_setup_flow(session)
    assert excinfo.value.reason == "already_paired"
    assert provider.start_calls == 0


async def test_submit_pin_session_lost_rerenders() -> None:
    """A session that ends underneath the submit re-renders the PIN form and starts afresh."""
    api = _FakeApi([_desc(PairMethod.DYNAMIC_PIN)])
    provider = _FakeProvider(api, submit_outcomes=["session_lost", "success"])
    session, _mass = _make_session(_ok_finish)
    player = _make_player(api, provider)

    task = asyncio.create_task(player.run_setup_flow(session))
    await _wait_step(session, step_type=FlowStepType.FORM, step_id="enter_pin")
    session.handle_submit({CONF_PAIRING_PIN: "000000"})

    error_step = await _wait_step(
        session, step_type=FlowStepType.FORM, step_id="enter_pin", with_errors=True
    )
    assert error_step.errors == {"base": "pairing_error_no_pin_session"}
    session.handle_submit({CONF_PAIRING_PIN: "123456"})

    await _wait_for(lambda: session.finished)
    await task
    assert provider.submitted_pins == ["000000", "123456"]
    assert provider.start_calls == 2


async def test_gesture_timeout_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """An expired gesture wait propagates (timed_out abort) and tears the session down."""
    monkeypatch.setattr("music_assistant.providers.sendspin.player.SERVER_GESTURE_TIMEOUT_S", 0.05)
    api = _FakeApi([_desc(PairMethod.DYNAMIC_PIN)])
    provider = _FakeProvider(api, gesture=True)
    session, _mass = _make_session(_ok_finish)
    player = _make_player(api, provider)

    with pytest.raises(StepExpiredError):
        await player.run_setup_flow(session)
    assert provider.cancel_calls == 1


async def test_pin_form_expiry_retries_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unanswered PIN form re-renders with a timeout error rather than dropping the flow."""
    monkeypatch.setattr("music_assistant.providers.sendspin.player.PAIR_PIN_ENTRY_TIMEOUT", 0.05)
    api = _FakeApi([_desc(PairMethod.DYNAMIC_PIN)])
    provider = _FakeProvider(api)
    session, _mass = _make_session(_ok_finish)
    player = _make_player(api, provider)

    task = asyncio.create_task(player.run_setup_flow(session))
    await _wait_step(session, step_type=FlowStepType.FORM, step_id="enter_pin")
    retry = await _wait_step(
        session, step_type=FlowStepType.FORM, step_id="enter_pin", with_errors=True
    )
    assert retry.errors == {"base": "pairing_error_timeout"}
    session.handle_submit({CONF_PAIRING_PIN: "123456"})
    await _wait_for(lambda: session.finished)
    await task


async def test_pin_form_encodes_the_negotiated_length() -> None:
    """The PIN field renders as a pairing-code box matching the negotiated digit count."""
    api = _FakeApi([_desc(PairMethod.DYNAMIC_PIN)])
    provider = _FakeProvider(api)
    session, _mass = _make_session(_ok_finish)
    player = _make_player(api, provider)

    task = asyncio.create_task(player.run_setup_flow(session))
    step = await _wait_step(session, step_type=FlowStepType.FORM, step_id="enter_pin")
    pin_entry = next(entry for entry in step.entries if entry.key == CONF_PAIRING_PIN)
    assert pin_entry.type is ConfigEntryType.PAIRING_CODE
    assert pin_entry.format == "###-###"
    session.handle_submit({CONF_PAIRING_PIN: "123456"})
    await _wait_for(lambda: session.finished)
    await task


async def test_pin_form_accepts_a_separator_in_the_submitted_pin() -> None:
    """A PIN submitted with the format's separator still pairs (parse_value strips it)."""
    api = _FakeApi([_desc(PairMethod.DYNAMIC_PIN)])
    provider = _FakeProvider(api)
    session, _mass = _make_session(_ok_finish)
    player = _make_player(api, provider)

    task = asyncio.create_task(player.run_setup_flow(session))
    await _wait_step(session, step_type=FlowStepType.FORM, step_id="enter_pin")
    session.handle_submit({CONF_PAIRING_PIN: "123-456"})

    await _wait_for(lambda: session.finished)
    await task
    assert provider.submitted_pins == ["123456"]


async def test_pin_form_rejects_a_short_pin() -> None:
    """A PIN shorter than the negotiated length re-serves the form with a field error."""
    api = _FakeApi([_desc(PairMethod.DYNAMIC_PIN)])
    provider = _FakeProvider(api)
    session, _mass = _make_session(_ok_finish)
    player = _make_player(api, provider)

    task = asyncio.create_task(player.run_setup_flow(session))
    await _wait_step(session, step_type=FlowStepType.FORM, step_id="enter_pin")
    step = session.handle_submit({CONF_PAIRING_PIN: "123"})
    assert step is not None
    assert step.errors == {CONF_PAIRING_PIN: "invalid_value"}
    assert provider.submitted_pins == []

    session.handle_submit({CONF_PAIRING_PIN: "123456"})
    await _wait_for(lambda: session.finished)
    await task


async def test_static_pin_form_hints_where_the_pin_lives() -> None:
    """A static-PIN form surfaces the device's own hint about where its PIN is printed."""
    api = _FakeApi([_desc(PairMethod.STATIC_PIN, locations=["device", "bogus"])])
    provider = _FakeProvider(api)
    session, _mass = _make_session(_ok_finish)
    player = _make_player(api, provider)

    task = asyncio.create_task(player.run_setup_flow(session))
    step = await _wait_step(session, step_type=FlowStepType.FORM, step_id="enter_pin")
    # The unknown location is ignored rather than rendered as a missing translation.
    assert [entry.key for entry in step.entries] == [
        "static_pin_location_device",
        CONF_PAIRING_PIN,
    ]
    # A static PIN is always exactly 8 digits (enforced by aiosendspin).
    pin_entry = next(entry for entry in step.entries if entry.key == CONF_PAIRING_PIN)
    assert pin_entry.type is ConfigEntryType.PAIRING_CODE
    assert pin_entry.format == "####-####"
    session.handle_submit({CONF_PAIRING_PIN: "12345678"})
    await _wait_for(lambda: session.finished)
    await task


async def test_dynamic_pin_form_hints_how_the_pin_arrives() -> None:
    """A dynamic-PIN form surfaces the device's own hint about the channel carrying the PIN."""
    api = _FakeApi([_desc(PairMethod.DYNAMIC_PIN, out_channels=["speaker", "other"])])
    provider = _FakeProvider(api)
    session, _mass = _make_session(_ok_finish)
    player = _make_player(api, provider)

    task = asyncio.create_task(player.run_setup_flow(session))
    step = await _wait_step(session, step_type=FlowStepType.FORM, step_id="enter_pin")
    # "other" says nothing an operator can act on, so it renders no hint.
    assert [entry.key for entry in step.entries] == [
        "dynamic_pin_channel_speaker",
        CONF_PAIRING_PIN,
    ]
    session.handle_submit({CONF_PAIRING_PIN: "123456"})
    await _wait_for(lambda: session.finished)
    await task


async def test_token_form_hints_where_the_token_lives() -> None:
    """A token form surfaces the device's own hint about where its pairing secret is printed."""
    api = _FakeApi([_desc(PairMethod.PAIRING_PSK, locations=["leaflet"])])
    provider = _FakeProvider(api)
    session, _mass = _make_session(_ok_finish)
    player = _make_player(api, provider)

    task = asyncio.create_task(player.run_setup_flow(session))
    step = await _wait_step(session, step_type=FlowStepType.FORM, step_id="enter_token")
    assert [entry.key for entry in step.entries] == [
        "pairing_psk_location_leaflet",
        CONF_PAIRING_TOKEN,
    ]
    session.handle_submit({CONF_PAIRING_TOKEN: "tok-1"})
    await _wait_for(lambda: session.finished)
    await task


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


async def test_token_hidden_when_the_device_can_pair_by_pin() -> None:
    """A device offering both goes straight to its PIN, never showing the token as a choice."""
    api = _FakeApi([_desc(PairMethod.DYNAMIC_PIN), _desc(PairMethod.PAIRING_PSK)])
    provider = _FakeProvider(api)
    session, mass = _make_session(_ok_finish)
    player = _make_player(api, provider)

    task = asyncio.create_task(player.run_setup_flow(session))
    await _wait_step(session, step_type=FlowStepType.FORM, step_id="enter_pin")
    assert not any(s.step_id == "select_method" for s in _published_steps(mass))
    session.handle_submit({CONF_PAIRING_PIN: "123456"})

    await _wait_for(lambda: session.finished)
    await task
    assert provider.tokens == []


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
    api = _FakeApi([])
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
    """Static PIN needs both PIN methods usable; the token yields to any usable PIN."""
    api = _FakeApi([_desc(PairMethod.DYNAMIC_PIN), _desc(PairMethod.STATIC_PIN)])
    provider = _FakeProvider(api)
    player = _make_player(api, provider)
    # Opposite the static option the generic "pin" gives way to the dynamic-specific value,
    # so each option can describe itself.
    assert player._pairing_method_options(cast("SendspinProvider", provider)) == [
        PAIR_METHOD_DYNAMIC_PIN,
        PAIR_METHOD_STATIC_PIN,
    ]

    api_single = _FakeApi(
        [_desc(PairMethod.STATIC_PIN), _desc(PairMethod.PAIRING_PSK)], unpaired_access=True
    )
    provider_single = _FakeProvider(api_single)
    player_single = _make_player(api_single, provider_single)
    assert player_single._pairing_method_options(cast("SendspinProvider", provider_single)) == [
        PAIR_METHOD_PIN
    ]

    # Without a PIN to fall back on the token is the only way in, so it returns to the list.
    api_token = _FakeApi([_desc(PairMethod.PAIRING_PSK)], unpaired_access=True)
    provider_token = _FakeProvider(api_token)
    player_token = _make_player(api_token, provider_token)
    assert player_token._pairing_method_options(cast("SendspinProvider", provider_token)) == [
        PAIR_METHOD_TOKEN
    ]
