"""Tests for the fake Sendspin devices' pairing profiles."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from aiosendspin.models.types import PairMethod
from aiosendspin.noise.trust_store import FileClientPairingStore

from music_assistant.providers._demo_sendspin_clients.constants import STATIC_PIN
from music_assistant.providers._demo_sendspin_clients.device import (
    FakeSendspinDevice,
    _scenario_identity,
)
from music_assistant.providers._demo_sendspin_clients.scenarios import (
    SCENARIOS,
    SCENARIOS_BY_ID,
    PinChannel,
)

if TYPE_CHECKING:
    from pathlib import Path

    from music_assistant.providers._demo_sendspin_clients.scenarios import Scenario


def test_scenario_ids_are_unique() -> None:
    """Every scenario has its own id, so devices never share an identity."""
    assert len(SCENARIOS_BY_ID) == len(SCENARIOS)


def test_identity_is_stable_per_scenario() -> None:
    """The same scenario always yields the same client id, across restarts."""
    assert _scenario_identity("open").peer_id == _scenario_identity("open").peer_id
    assert _scenario_identity("open").peer_id != _scenario_identity("locked").peer_id


def test_matrix_covers_every_pairing_method() -> None:
    """Each pairing method, and a device offering none, is represented."""
    assert any(scenario.pairing_psk for scenario in SCENARIOS)
    assert any(scenario.static_pin for scenario in SCENARIOS)
    assert any(scenario.dynamic_pin for scenario in SCENARIOS)
    assert any(scenario.unpaired_access for scenario in SCENARIOS)
    assert any(
        not scenario.offers_pairing and not scenario.unpaired_access for scenario in SCENARIOS
    )
    assert any(scenario.source_role for scenario in SCENARIOS)
    assert any(scenario.pin_channel.has_speaker for scenario in SCENARIOS)


def test_token_scenarios_offer_no_pin_method() -> None:
    """The setup flow only offers token pairing when no PIN method is offered."""
    for scenario in SCENARIOS:
        if scenario.pairing_psk and not (scenario.static_pin or scenario.dynamic_pin):
            assert scenario.secret_locations, f"{scenario.scenario_id} needs a secret location"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.scenario_id)
async def test_device_advertises_its_scenario(scenario: Scenario, tmp_path: Path) -> None:
    """A started device implements and enables exactly the methods its scenario declares."""
    device = FakeSendspinDevice(scenario, tmp_path, "ws://127.0.0.1:1/sendspin")
    try:
        await device.start()
        client = device._client
        assert client is not None

        implemented = client.implemented_pair_methods
        assert (PairMethod.STATIC_PIN in implemented) is scenario.static_pin
        assert (PairMethod.DYNAMIC_PIN in implemented) is scenario.dynamic_pin
        assert client.secret_locations == scenario.secret_locations
        assert ("display" in client.pin_out_channels) is scenario.pin_channel.has_display
        assert ("speaker" in client.pin_out_channels) is scenario.pin_channel.has_speaker

        store = await FileClientPairingStore.open(tmp_path / f"{scenario.scenario_id}.json")
        config = await store.get_pairing_config()
        assert config.pairing_psk_enabled is scenario.pairing_psk
        assert config.static_pin_enabled is scenario.static_pin
        assert config.dynamic_pin_enabled is scenario.dynamic_pin
        assert config.unpaired_access_enabled is scenario.unpaired_access
        assert config.dynamic_pin_min_length == scenario.min_pin_length
        assert (await store.static_pin() == STATIC_PIN) is scenario.static_pin
        assert (device.pairing_token is not None) is scenario.pairing_psk
    finally:
        await device.stop()


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.scenario_id)
def test_gesture_gating_matches_the_spec(scenario: Scenario) -> None:
    """A device needs its button pressed for a static PIN, or a dynamic PIN under six digits."""
    expected = scenario.static_pin or (scenario.dynamic_pin and scenario.min_pin_length < 6)
    assert scenario.gesture_gated is expected


def test_pin_channel_flags() -> None:
    """BOTH covers each single channel, NONE covers neither."""
    assert PinChannel.BOTH.has_display
    assert PinChannel.BOTH.has_speaker
    assert not PinChannel.NONE.has_display
    assert not PinChannel.NONE.has_speaker


async def test_stop_during_start_leaves_nothing_running(tmp_path: Path) -> None:
    """
    A stop landing mid-start must not leave a reconnect loop behind.

    An escaped loop keeps dialling under an identity the next load reuses, and the two
    connections then displace each other on every attempt.
    """
    device = FakeSendspinDevice(SCENARIOS[0], tmp_path, "ws://127.0.0.1:1/sendspin")
    starting = asyncio.create_task(device.start())
    await asyncio.sleep(0)
    await device.stop()
    await starting
    assert device._task is None
    assert device._client is None
