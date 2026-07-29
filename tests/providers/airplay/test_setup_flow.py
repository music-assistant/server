"""Tests for the AirPlay interactive setup (pairing) flow."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import FlowStepType
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.models.setup_flow import AbortFlow, SetupFlowContext, SetupSession
from music_assistant.providers.airplay.constants import (
    AIRPLAY_DISCOVERY_TYPE,
    COMPANION_DISCOVERY_TYPE,
    CONF_AIRPLAY_CREDENTIALS,
    CONF_COMPANION_CREDENTIALS,
    CONF_COMPANION_PAIRING_PIN,
    CONF_PAIR_NOW,
    CONF_PAIRING_PASSWORD,
    CONF_PAIRING_PIN,
    CONF_PASSWORD,
    CONF_RAOP_CREDENTIALS,
)
from music_assistant.providers.airplay.control_player import AirPlayControlPlayer
from music_assistant.providers.airplay.player import AirPlayPlayer

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant_models.setup_flow import SetupFlowStep

# _airplay._tcp features bitmask with the AirPlay 2 feature bits set (bit 38/48).
AP2_FEATURES = "0x4A7FDFD5,0x3C177FDE"
# 192 hex chars, as produced by cliairplay --pair-setup
FAKE_AP2_CREDS = "ab" * 96
FAKE_RAOP_CREDS = "clientid:secret"
_PAIRING_TARGET = "music_assistant.providers.airplay.pairing.AirPlayPairing"
_PYATV_PAIR_TARGET = "music_assistant.providers.airplay.control_player.pyatv.pair"


# --------------------------------------------------------------------------------------
# Harness (direct-drive: run run_setup_flow as a task and pump the session by hand)
# --------------------------------------------------------------------------------------


def _make_session(
    finish_handler: Any, *, player_id: str = "test_player"
) -> tuple[SetupSession, MagicMock]:
    """Build a real SetupSession backed by a Mock mass for driving run_setup_flow directly."""
    mass = MagicMock()
    context = SetupFlowContext(kind="setup", reason="user", domain="airplay", player_id=player_id)
    return SetupSession(mass, "flow-test", context, finish_handler), mass


def _published_steps(mass: MagicMock) -> list[SetupFlowStep]:
    """Return the flow steps pushed through mass.signal_event, in order."""
    return [call.kwargs["data"] for call in mass.signal_event.call_args_list]


async def _wait_for(predicate: Callable[[], Any], timeout: float = 5.0) -> Any:
    """Wait until the predicate returns a truthy value (or fail the test)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


async def _pump(
    session: SetupSession,
    task: asyncio.Task[None],
    responder: Callable[[SetupFlowStep], dict[str, Any]],
    *,
    timeout: float = 5.0,
) -> None:
    """Answer each published FORM step via ``responder`` until the flow ends, then await it."""
    handled: SetupFlowStep | None = None
    while True:

        def _ready(after: SetupFlowStep | None = handled) -> bool:
            step = session.current_step
            if task.done() or session.finished:
                return True
            return step is not None and step is not after and step.type == FlowStepType.FORM

        await _wait_for(_ready, timeout)
        if task.done() or session.finished:
            break
        handled = session.current_step
        assert handled is not None
        session.handle_submit(responder(handled))
    await task


def _pyatv_pairing(credentials: str) -> MagicMock:
    """Return a mock pyatv PairingHandler that completes with the given credentials."""
    pairing = MagicMock()
    pairing.begin = AsyncMock()
    pairing.finish = AsyncMock()
    pairing.close = AsyncMock()
    pairing.has_paired = True
    pairing.service.credentials = credentials
    return pairing


def _service_info(
    service_type: str,
    properties: dict[str, str],
    *,
    address: str = "192.168.1.10",
    port: int = 7000,
) -> MagicMock:
    """Create an mDNS service-info mock usable by the control-player pairing helpers."""
    info = MagicMock()
    info.type = service_type
    info.name = f"test.{service_type}"
    info.port = port
    info.decoded_properties = dict(properties)
    info.properties = {key.encode(): value.encode() for key, value in properties.items()}
    info.addresses = [b"\xc0\xa8\x01\x0a"]
    info.parsed_addresses.return_value = [address]
    return info


def _stub_setup_data(provider: MagicMock, player_id: str, setup_data: dict[str, Any]) -> None:
    """Route the player's setup_data through the mocked mass.config get surface."""

    def _config_get(key: str, default: Any = None) -> Any:
        if key == f"players/{player_id}/setup_data":
            return setup_data
        if key == f"players/{player_id}":
            return {"player_id": player_id}
        return default

    provider.mass.config.get.side_effect = _config_get
    provider.mass.config.decrypt_string.side_effect = lambda value: value
    provider.mass.config.encrypt_string.side_effect = lambda value: value


def _streaming_player(
    *,
    player_id: str = "test_player",
    raop: bool = False,
    setup_data: dict[str, Any] | None = None,
) -> AirPlayPlayer:
    """Create a base AirPlay player that requires PIN pairing (RAOP or AirPlay 2)."""
    provider = MagicMock()
    provider.dacp_id = "0123456789ABCDEF"
    _stub_setup_data(provider, player_id, setup_data or {})
    if raop:
        raop_info = _service_info("_raop._tcp.local.", {"sf": "0x200"}, port=5000)
        airplay_info = None
    else:
        raop_info = None
        airplay_info = _service_info(
            AIRPLAY_DISCOVERY_TYPE, {"features": AP2_FEATURES, "flags": "0x8"}
        )
    return AirPlayPlayer(
        provider=provider,
        player_id=player_id,
        display_name="Test Player",
        address="127.0.0.1",
        manufacturer="Apple" if not raop else "Denon",
        model="Apple TV" if not raop else "AVR",
        raop_discovery_info=raop_info,
        airplay_discovery_info=airplay_info,
    )


def _control_player(
    *, player_id: str = "apctl", setup_data: dict[str, Any] | None = None
) -> AirPlayControlPlayer:
    """Create a control-capable Apple player requiring streaming PIN + Companion pairing."""
    provider = MagicMock()
    provider.instance_id = "airplay"
    provider.dacp_id = "0123456789ABCDEF"
    provider.logger = logging.getLogger("test.airplay.flow")
    config = MagicMock()
    config.get_value.side_effect = lambda _key, default=None: default
    provider.mass.config.get_base_player_config.return_value = config
    _stub_setup_data(provider, player_id, setup_data or {})
    airplay_info = _service_info(
        AIRPLAY_DISCOVERY_TYPE,
        {
            "deviceid": "AA:BB:CC:DD:EE:FF",
            "features": AP2_FEATURES,
            "model": "AppleTV11,1",
            "osvers": "26.0",
            "flags": "0x8",  # require streaming PIN pairing
        },
    )
    companion_info = _service_info(COMPANION_DISCOVERY_TYPE, {"rpFl": "0x367A2"}, port=49152)
    return AirPlayControlPlayer(
        provider=provider,
        player_id=player_id,
        raop_discovery_info=None,
        airplay_discovery_info=airplay_info,
        companion_discovery_info=companion_info,
        mrp_discovery_info=None,
        address="192.168.1.10",
        display_name="Test Apple Device",
        manufacturer="Apple",
        model="Apple TV 4K",
        initial_volume=25,
    )


# --------------------------------------------------------------------------------------
# Streaming (base player) pairing
# --------------------------------------------------------------------------------------


async def test_streaming_pin_pairing_persists_airplay_credentials() -> None:
    """The AirPlay 2 PIN happy path finishes with the credentials under the AirPlay key."""
    collected: dict[str, Any] = {}

    async def finish(_session: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"player_id": "test_player"}

    session, mass = _make_session(finish)
    player = _streaming_player()
    pairing = AsyncMock()
    pairing.finish_pairing = AsyncMock(return_value=FAKE_AP2_CREDS)

    with patch(_PAIRING_TARGET, return_value=pairing):
        task = asyncio.create_task(player.run_setup_flow(session))
        await _pump(session, task, lambda _step: {CONF_PAIRING_PIN: "1234"})

    assert collected == {CONF_AIRPLAY_CREDENTIALS: FAKE_AP2_CREDS}
    pairing.start_pairing_session.assert_awaited_once()
    pairing.start_pin_pairing.assert_awaited_once()
    pairing.finish_pairing.assert_awaited_once_with(pin="1234")
    pairing.close.assert_awaited()
    forms = [step for step in _published_steps(mass) if step.type == FlowStepType.FORM]
    assert forms[0].step_id == "pair_pin"
    # steps localize under the provider's own namespace
    assert forms[0].translation_owner == "provider.airplay"


async def test_streaming_pin_pairing_uses_raop_credentials_key() -> None:
    """A legacy RAOP device stores its pairing secret under the RAOP-specific key."""
    collected: dict[str, Any] = {}

    async def finish(_session: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"player_id": "test_player"}

    session, _mass = _make_session(finish)
    player = _streaming_player(raop=True)
    pairing = AsyncMock()
    pairing.finish_pairing = AsyncMock(return_value=FAKE_RAOP_CREDS)

    with patch(_PAIRING_TARGET, return_value=pairing):
        task = asyncio.create_task(player.run_setup_flow(session))
        await _pump(session, task, lambda _step: {CONF_PAIRING_PIN: "4321"})

    assert collected == {CONF_RAOP_CREDENTIALS: FAKE_RAOP_CREDS}


async def test_streaming_pin_pairing_retries_on_wrong_pin() -> None:
    """A rejected PIN re-renders the form (with an error) and a fresh pairing is started."""
    collected: dict[str, Any] = {}

    async def finish(_session: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"player_id": "test_player"}

    session, mass = _make_session(finish)
    player = _streaming_player()
    rejected = AsyncMock()
    rejected.finish_pairing = AsyncMock(side_effect=PlayerCommandFailed("wrong pin"))
    accepted = AsyncMock()
    accepted.finish_pairing = AsyncMock(return_value=FAKE_AP2_CREDS)
    attempts = [rejected, accepted]
    pins = iter(["0000", "1234"])

    # snapshot each step's errors at publish time: a successful submit later clears
    # them on the (same) step object, so the live object cannot be inspected afterwards
    published: list[tuple[str, dict[str, str]]] = []
    mass.signal_event.side_effect = lambda *_a, **kwargs: published.append(
        (kwargs["data"].step_id, dict(kwargs["data"].errors))
    )

    with patch(_PAIRING_TARGET, side_effect=lambda **_kwargs: attempts.pop(0)):
        task = asyncio.create_task(player.run_setup_flow(session))
        await _pump(session, task, lambda _step: {CONF_PAIRING_PIN: next(pins)})

    assert collected == {CONF_AIRPLAY_CREDENTIALS: FAKE_AP2_CREDS}
    # each attempt uses (and tears down) its own live pairing session
    rejected.close.assert_awaited()
    accepted.close.assert_awaited()
    pin_form_errors = [errors for step_id, errors in published if step_id == "pair_pin"]
    assert len(pin_form_errors) == 2
    # first attempt shows a clean form, the retry surfaces the failure
    assert pin_form_errors[0] == {}
    assert pin_form_errors[1].get("base")


async def test_streaming_already_paired_finishes_without_pairing() -> None:
    """Re-running the flow when streaming is already paired skips pairing and finishes."""
    finished_values: dict[str, Any] = {}

    async def finish(_session: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        finished_values.update({"values": values})
        return {"player_id": "test_player"}

    session, _mass = _make_session(finish)
    player = _streaming_player(setup_data={CONF_AIRPLAY_CREDENTIALS: FAKE_AP2_CREDS})

    with patch(_PAIRING_TARGET) as pairing_cls:
        task = asyncio.create_task(player.run_setup_flow(session))
        await _wait_for(lambda: session.finished)
        await task

    pairing_cls.assert_not_called()
    assert finished_values["values"] == {}


async def test_abort_mid_pairing_closes_session() -> None:
    """Cancelling the flow while awaiting the PIN tears the live pairing session down."""

    async def finish(_session: SetupSession, _values: dict[str, Any]) -> dict[str, str]:
        return {"player_id": "test_player"}

    session, _mass = _make_session(finish)
    player = _streaming_player()
    pairing = AsyncMock()

    with patch(_PAIRING_TARGET, return_value=pairing):
        task = asyncio.create_task(player.run_setup_flow(session))
        await _wait_for(
            lambda: session.current_step is not None and session.current_step.step_id == "pair_pin"
        )
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    pairing.close.assert_awaited()


async def test_pairing_start_failure_aborts_with_reason() -> None:
    """
    A failure starting the pairing session aborts cleanly, not as a raw crash.

    The engine turns an uncaught exception into a generic ``internal_error``; the flow
    instead raises ``AbortFlow("pairing_failed")`` so the user sees an actionable reason,
    and the half-started session is still torn down.
    """

    async def finish(_session: SetupSession, _values: dict[str, Any]) -> dict[str, str]:
        return {"player_id": "test_player"}

    session, _mass = _make_session(finish)
    player = _streaming_player()
    pairing = AsyncMock()
    # a device/binary/system failure surfaces from starting the session, not from finish
    pairing.start_pairing_session = AsyncMock(
        side_effect=RuntimeError("Unable to locate cliairplay binary")
    )

    with patch(_PAIRING_TARGET, return_value=pairing), pytest.raises(AbortFlow) as exc:
        await player.run_setup_flow(session)

    assert exc.value.reason == "pairing_failed"
    # the half-started session is still torn down
    pairing.close.assert_awaited()


# --------------------------------------------------------------------------------------
# Control player: streaming + optional Companion/MRP ("two codes")
# --------------------------------------------------------------------------------------


async def test_two_code_sequence_streaming_then_companion() -> None:
    """A controlled device pairs the streaming PIN, then the optional Companion PIN."""
    collected: dict[str, Any] = {}

    async def finish(_session: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"player_id": "apctl"}

    session, mass = _make_session(finish, player_id="apctl")
    player = _control_player()
    streaming = AsyncMock()
    streaming.finish_pairing = AsyncMock(return_value=FAKE_AP2_CREDS)
    companion = _pyatv_pairing("companion-creds")
    responses: dict[str, dict[str, Any]] = {
        "pair_pin": {CONF_PAIRING_PIN: "1234"},
        "companion_offer": {CONF_PAIR_NOW: True},
        "pair_companion": {CONF_COMPANION_PAIRING_PIN: "5678"},
        # MRP is offered for an Apple TV; decline it to keep this a two-code run
        "mrp_offer": {CONF_PAIR_NOW: False},
    }

    with (
        patch(_PAIRING_TARGET, return_value=streaming),
        patch(_PYATV_PAIR_TARGET, return_value=companion) as pyatv_pair,
    ):
        task = asyncio.create_task(player.run_setup_flow(session))
        await _pump(session, task, lambda step: responses[step.step_id])

    assert collected[CONF_AIRPLAY_CREDENTIALS] == FAKE_AP2_CREDS
    assert collected[CONF_COMPANION_CREDENTIALS] == "companion-creds"
    companion.pin.assert_called_once_with(5678)
    pyatv_pair.assert_called_once()
    step_ids = [step.step_id for step in _published_steps(mass) if step.type == FlowStepType.FORM]
    # streaming PIN comes before the optional control offers
    assert step_ids.index("pair_pin") < step_ids.index("companion_offer")
    assert "pair_companion" in step_ids


async def test_companion_offer_can_be_skipped() -> None:
    """Declining the optional Companion offer collects nothing and never pairs."""
    collected: dict[str, Any] = {}

    async def finish(_session: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"player_id": "apctl"}

    session, _mass = _make_session(finish, player_id="apctl")
    # streaming already paired, so the flow goes straight to the optional offers
    player = _control_player(setup_data={CONF_AIRPLAY_CREDENTIALS: FAKE_AP2_CREDS})
    responses: dict[str, dict[str, Any]] = {
        "companion_offer": {CONF_PAIR_NOW: False},
        "mrp_offer": {CONF_PAIR_NOW: False},
    }

    with (
        patch(_PAIRING_TARGET) as pairing_cls,
        patch(_PYATV_PAIR_TARGET) as pyatv_pair,
    ):
        task = asyncio.create_task(player.run_setup_flow(session))
        await _pump(session, task, lambda step: responses[step.step_id])

    assert collected == {}
    pairing_cls.assert_not_called()
    pyatv_pair.assert_not_called()


def _password_player(*, player_id: str = "test_player") -> AirPlayPlayer:
    """Create an AirPlay 2 player that announces password protection (flags bit 0x80)."""
    provider = MagicMock()
    provider.dacp_id = "0123456789ABCDEF"
    _stub_setup_data(provider, player_id, {})
    return AirPlayPlayer(
        provider=provider,
        player_id=player_id,
        display_name="Test HomePod",
        address="127.0.0.1",
        manufacturer="Apple",
        model="HomePod mini",
        raop_discovery_info=None,
        airplay_discovery_info=_service_info(
            AIRPLAY_DISCOVERY_TYPE, {"features": AP2_FEATURES, "flags": "0x80"}
        ),
    )


async def test_streaming_password_pairing_persists_the_device_password() -> None:
    """The entered password is stored as player config, not discarded with the form."""
    collected: dict[str, Any] = {}

    async def finish(_session: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"player_id": "test_player"}

    session, _mass = _make_session(finish)
    player = _password_player()
    pairing = AsyncMock()
    pairing.finish_pairing = AsyncMock(return_value=FAKE_AP2_CREDS)

    with patch(_PAIRING_TARGET, return_value=pairing):
        task = asyncio.create_task(player.run_setup_flow(session))
        await _pump(session, task, lambda _step: {CONF_PAIRING_PASSWORD: "hunter2"})

    # the pairing credentials still go to setup_data...
    assert collected == {CONF_AIRPLAY_CREDENTIALS: FAKE_AP2_CREDS}
    # ...and the password itself is persisted so every stream can present it
    player.mass.config.set_raw_player_config_value.assert_called_once_with(  # type: ignore[attr-defined]
        "test_player", CONF_PASSWORD, "hunter2"
    )


async def test_streaming_password_pairing_uses_a_password_form() -> None:
    """A password-protected device is asked for a password instead of a PIN."""

    async def finish(_session: SetupSession, _values: dict[str, Any]) -> dict[str, str]:
        return {"player_id": "test_player"}

    session, mass = _make_session(finish)
    player = _password_player()
    pairing = AsyncMock()
    pairing.finish_pairing = AsyncMock(return_value=FAKE_AP2_CREDS)

    with patch(_PAIRING_TARGET, return_value=pairing):
        task = asyncio.create_task(player.run_setup_flow(session))
        await _pump(session, task, lambda _step: {CONF_PAIRING_PASSWORD: "hunter2"})

    forms = [step for step in _published_steps(mass) if step.type == FlowStepType.FORM]
    assert [step.step_id for step in forms] == ["pair_password"]
    # the device shows no PIN in this flow, so no PIN pairing is started
    pairing.start_pin_pairing.assert_not_awaited()
