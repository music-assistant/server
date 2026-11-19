"""Tests for what a saved player config means to the Sendspin output of the player."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import EventType, IdentifierType

from music_assistant.constants import (
    CONF_OUTPUT_CHANNELS,
    CONF_PLAYER_PROTOCOLS,
    CONF_PROTOCOL_KEY_SPLITTER,
    CONF_PROTOCOL_OFF_BY_DEFAULT,
    CONF_UNDERLYING_PLAYER_ID,
)
from music_assistant.providers.sendspin import provider as provider_module
from music_assistant.providers.sendspin.provider import SendspinProvider

if TYPE_CHECKING:
    from tests.scenarios.harness import FakeProtocol, Scenario

KITCHEN = {
    IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A0",
    IdentifierType.IP_ADDRESS: "192.168.1.20",
}
OUTPUT_ID = "ap_kitchen"
SWITCH = f"{OUTPUT_ID}{CONF_PROTOCOL_KEY_SPLITTER}enabled"
BRIDGE_OUTPUT_ID = "spb_5478c9e60da0"
BRIDGE_SWITCH = f"{BRIDGE_OUTPUT_ID}{CONF_PROTOCOL_KEY_SPLITTER}enabled"


async def _player_with_an_output(scenario: Scenario) -> tuple[str, FakeProtocol]:
    """
    Set up a player with a protocol of its own and an output of another provider.

    :param scenario: The running server.
    :return: The id of the player and the protocol of its output.
    """
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    native = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    output = airplay.protocol(OUTPUT_ID, "Kitchen", role="audio", identifiers=KITCHEN)
    await scenario.register(native)
    await scenario.register(output)
    await scenario.idle()
    return scenario.single_player(native, output).player_id, output


async def _save(scenario: Scenario, player_id: str, values: dict[str, Any]) -> None:
    """
    Save values of the config of a player the way a user does.

    A save that switches an output on waits for that output to attach, on the clock.

    :param scenario: The running server.
    :param player_id: Id of the player.
    :param values: The values to save.
    """
    save = await scenario.command(scenario.mass.config.save_player_config(player_id, values))
    await scenario.advance(10)
    await save


def _is_off_by_default(scenario: Scenario) -> bool:
    """Return whether the output is stored as left switched off by its provider."""
    record = scenario.mass.config.get_protocol_record(OUTPUT_ID) or {}
    return CONF_PROTOCOL_OFF_BY_DEFAULT in record["values"]


# ---------------------------------------------------------------- the event of a saved config


@pytest.mark.parametrize("away", [False, True], ids=["output attached", "output away"])
async def test_a_save_that_only_switches_an_output_is_signalled(
    scenario: Scenario, away: bool
) -> None:
    """Switching one output of a player tells the listeners its config was saved."""
    player_id, output = await _player_with_an_output(scenario)
    if away:
        await scenario.unregister(output)
    scenario.clear_events()

    await _save(scenario, player_id, {SWITCH: False})

    assert len(scenario.events(EventType.PLAYER_CONFIG_UPDATED, object_id=player_id)) == 1
    assert scenario.mass.config.is_protocol_switched_on(OUTPUT_ID) is False
    scenario.clear_events()

    await _save(scenario, player_id, {SWITCH: True})

    assert len(scenario.events(EventType.PLAYER_CONFIG_UPDATED, object_id=player_id)) == 1


async def test_a_save_that_changes_no_output_is_not_signalled(scenario: Scenario) -> None:
    """A save that leaves the player and its outputs as they are tells nobody."""
    player_id, _output = await _player_with_an_output(scenario)
    scenario.clear_events()

    await _save(scenario, player_id, {SWITCH: True})

    assert scenario.events(EventType.PLAYER_CONFIG_UPDATED, object_id=player_id) == []


# ---------------------------------------------------------------- a save of the whole form


def _store_bridge_output(scenario: Scenario, player_id: str, *, enabled: bool) -> None:
    """
    Store the Sendspin output a bridge of the AirPlay output of the player registered.

    :param scenario: The running server.
    :param player_id: Id of the player.
    :param enabled: Whether the Sendspin output is switched on.
    """
    scenario.mass.config.set(
        f"{CONF_PLAYER_PROTOCOLS}/{BRIDGE_OUTPUT_ID}",
        {
            "protocol_id": BRIDGE_OUTPUT_ID,
            "provider": "sendspin",
            "player_id": player_id,
            "enabled": enabled,
            "values": {CONF_UNDERLYING_PLAYER_ID: OUTPUT_ID},
        },
    )


async def test_a_form_that_switches_the_carrier_off_sends_the_output_switch_as_it_was(
    scenario: Scenario,
) -> None:
    """The form sends every switch: one it leaves as it was asks for nothing."""
    player_id, _output = await _player_with_an_output(scenario)
    _store_bridge_output(scenario, player_id, enabled=True)

    await _save(scenario, player_id, {SWITCH: False, BRIDGE_SWITCH: True})

    assert scenario.mass.config.is_protocol_switched_on(OUTPUT_ID) is False
    record = scenario.mass.config.get_protocol_record(BRIDGE_OUTPUT_ID) or {}
    assert record["enabled"] is True


async def test_switching_an_output_on_while_its_carrier_goes_off_is_refused(
    scenario: Scenario,
) -> None:
    """An output that is off can not be switched on in the save that switches its carrier off."""
    player_id, _output = await _player_with_an_output(scenario)
    _store_bridge_output(scenario, player_id, enabled=False)

    with pytest.raises(RuntimeError, match="underlying player"):
        await _save(scenario, player_id, {SWITCH: False, BRIDGE_SWITCH: True})

    assert scenario.mass.config.is_protocol_switched_on(OUTPUT_ID) is True


# ---------------------------------------------------------------- whose off it is


async def _left_off_by_its_provider(scenario: Scenario) -> str:
    """
    Set up a player whose output its provider left switched off, and return the player's id.

    :param scenario: The running server.
    """
    player_id, _output = await _player_with_an_output(scenario)
    await scenario.mass.config.save_protocol_config(OUTPUT_ID, {"enabled": False})
    scenario.mass.config.set_protocol_value(OUTPUT_ID, CONF_PROTOCOL_OFF_BY_DEFAULT, True)
    await scenario.settle()
    assert _is_off_by_default(scenario)
    return player_id


async def test_an_output_its_user_switches_is_no_default_of_its_provider(
    scenario: Scenario,
) -> None:
    """Once the user switched an output on, switching it off again is the off of the user."""
    player_id = await _left_off_by_its_provider(scenario)

    await _save(scenario, player_id, {SWITCH: True})

    assert scenario.mass.config.is_protocol_switched_on(OUTPUT_ID) is True
    assert not _is_off_by_default(scenario), "the output still reads as off by default"

    await _save(scenario, player_id, {SWITCH: False})

    assert scenario.mass.config.is_protocol_switched_on(OUTPUT_ID) is False
    assert not _is_off_by_default(scenario), "the off of the user reads as a default"


@pytest.mark.parametrize(
    "values",
    [
        pytest.param({"enabled": False}, id="switched off again"),
        pytest.param({CONF_OUTPUT_CHANNELS: "left"}, id="another setting"),
        pytest.param({}, id="nothing"),
    ],
)
async def test_a_save_that_does_not_switch_the_output_leaves_its_default_alone(
    scenario: Scenario, values: dict[str, Any]
) -> None:
    """An output that stays off is still off by the default of its provider."""
    await _left_off_by_its_provider(scenario)

    await scenario.mass.config.save_protocol_config(OUTPUT_ID, values)

    assert scenario.mass.config.is_protocol_switched_on(OUTPUT_ID) is False
    assert _is_off_by_default(scenario)


# ---------------------------------------------------------------- the listener


def _provider() -> Any:
    """Return a stand-in of the Sendspin provider, which runs."""
    provider = MagicMock()
    provider._unloading = False
    provider.mass.closing = False
    return provider


async def test_a_saved_player_config_evaluates_the_bridges_of_that_player() -> None:
    """The Sendspin provider lets the bridges of a player follow a saved config."""
    provider = _provider()

    with patch.object(provider_module, "evaluate_bridges_of_player", AsyncMock()) as evaluate:
        await SendspinProvider._on_player_config_updated(provider, MagicMock(object_id="pl01"))

    evaluate.assert_awaited_once_with(provider.mass, "pl01")


@pytest.mark.parametrize("case", ["unloading", "closing", "no player"])
async def test_a_saved_config_is_ignored_while_the_provider_goes(case: str) -> None:
    """Nothing is evaluated for a provider that unloads, a server that stops or no player."""
    provider = _provider()
    provider._unloading = case == "unloading"
    provider.mass.closing = case == "closing"
    event = MagicMock(object_id=None if case == "no player" else "pl01")

    with patch.object(provider_module, "evaluate_bridges_of_player", AsyncMock()) as evaluate:
        await SendspinProvider._on_player_config_updated(provider, event)

    evaluate.assert_not_awaited()


async def test_the_provider_listens_for_saved_player_configs(tmp_path: Path) -> None:
    """The listener is subscribed when the provider is set up, and goes when it unloads."""
    provider = _provider()
    provider.mass.storage_path = str(tmp_path)

    with (
        patch.object(provider_module, "get_or_create_server_identity", MagicMock()),
        patch("aiosendspin.noise.trust_store.FileServerPairingStore.open", AsyncMock()),
        patch.object(provider_module, "_evict_stale_pairings", AsyncMock(return_value=(0, 0))),
        patch.object(provider_module, "SendspinServer", MagicMock()),
        patch.object(provider_module, "is_port_in_use", AsyncMock(return_value=False)),
    ):
        await SendspinProvider.handle_async_init(provider)

    subscribed = {call.args[1]: call.args[0] for call in provider.mass.subscribe.call_args_list}
    assert subscribed[EventType.PLAYER_CONFIG_UPDATED] == provider._on_player_config_updated
    assert provider.mass.subscribe.return_value in provider.unregister_cbs
