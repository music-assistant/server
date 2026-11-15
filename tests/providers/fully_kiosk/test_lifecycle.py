"""Tests for the configured devices of the Fully Kiosk provider on a running server."""

from __future__ import annotations

import gc
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from music_assistant.constants import CONF_USE_SSL
from music_assistant.controllers.players.protocol_linking import PLAYER_ID_PREFIX
from music_assistant.providers.fully_kiosk.provider import CONF_MANUAL_IPS

from .conftest import KITCHEN, KITCHEN_HOST, OFFICE, OFFICE_HOST, load_provider

if TYPE_CHECKING:
    from music_assistant.models.player import Player
    from music_assistant.providers.fully_kiosk.provider import FullyKioskProvider
    from tests.scenarios.harness import Scenario


def _player(provider: FullyKioskProvider, protocol_id: str) -> Player:
    """Return the player a device is attached to."""
    protocol = provider.get_protocol(protocol_id)
    assert protocol is not None
    assert protocol.player is not None
    return protocol.player


def _hosts(scenario: Scenario, provider: FullyKioskProvider) -> Any:
    """Return the hosts the provider is set up with, as stored."""
    return scenario.mass.config.get_raw_provider_config_value(provider.instance_id, CONF_MANUAL_IPS)


async def test_every_configured_device_gets_a_player_of_its_own(
    provider: FullyKioskProvider,
) -> None:
    """A configured device first seen now is attached to a minted player."""
    kitchen = _player(provider, KITCHEN)

    assert kitchen.player_id.startswith(PLAYER_ID_PREFIX)
    assert kitchen.player_id != _player(provider, OFFICE).player_id


async def test_discovery_leaves_an_attached_device_alone(
    scenario: Scenario, provider: FullyKioskProvider
) -> None:
    """Discovering again keeps the attached object, with a disabled device to offer."""
    protocol = provider.get_protocol(KITCHEN)
    await scenario.disable(_player(provider, OFFICE).player_id)
    attach = scenario.mass.players.register

    with patch.object(scenario.mass.players, "register", wraps=attach) as attached:
        await provider.discover_players()
        await scenario.settle()

    assert [call.args[0].protocol_id for call in attached.call_args_list] == [OFFICE]
    assert provider.get_protocol(KITCHEN) is protocol
    assert provider.get_protocol(OFFICE) is None


async def test_a_removed_device_is_forgotten(
    scenario: Scenario, provider: FullyKioskProvider
) -> None:
    """A removed player leaves the configured hosts and does not come back."""
    player_id = _player(provider, KITCHEN).player_id

    await scenario.remove(player_id)
    await scenario.idle()

    assert _hosts(scenario, provider) == [f"{OFFICE_HOST}:2323"]
    assert provider.get_protocol(KITCHEN) is None
    assert scenario.player(player_id) is None


async def test_a_removed_disabled_device_is_forgotten(
    scenario: Scenario, provider: FullyKioskProvider
) -> None:
    """The removal of a player that is disabled also reaches its device."""
    player_id = _player(provider, OFFICE).player_id
    await scenario.disable(player_id)
    gc.collect()

    await scenario.remove(player_id)
    await scenario.idle()

    assert _hosts(scenario, provider) == [KITCHEN_HOST]
    assert scenario.mass.config.get_protocol_record(OFFICE) is None


async def test_a_removed_device_that_never_attached_is_forgotten(
    scenario: Scenario, provider: FullyKioskProvider
) -> None:
    """A device disabled before the server started is found by the removal of its player."""
    player_id = _player(provider, OFFICE).player_id
    await scenario.disable(player_id)
    await scenario.restart()
    provider = await load_provider(scenario, provider.instance_id)
    assert provider.get_protocol(OFFICE) is None
    gc.collect()

    await scenario.remove(player_id)
    await scenario.idle()

    assert _hosts(scenario, provider) == [KITCHEN_HOST]
    assert scenario.mass.config.get_protocol_record(OFFICE) is None


async def test_a_re_enabled_device_comes_back_on_its_player(
    scenario: Scenario, provider: FullyKioskProvider
) -> None:
    """Enabling a disabled player attaches its device to it again."""
    player_id = _player(provider, KITCHEN).player_id
    await scenario.disable(player_id)

    await scenario.enable(player_id)
    await scenario.idle()

    assert _player(provider, KITCHEN).player_id == player_id


async def test_a_host_taken_out_of_the_config_is_removed(
    scenario: Scenario, provider: FullyKioskProvider
) -> None:
    """At the next load a device that is no longer configured loses its player."""
    player_id = _player(provider, KITCHEN).player_id
    scenario.mass.config.set_raw_provider_config_value(
        provider.instance_id, CONF_MANUAL_IPS, [f"{OFFICE_HOST}:2323"]
    )

    # a config change reloads the provider
    await scenario.mass.unload_provider(provider.instance_id)
    await load_provider(scenario, provider.instance_id)

    assert scenario.player(player_id) is None
    assert scenario.mass.config.get_protocol_record(KITCHEN) is None


async def test_a_setting_of_a_new_player_is_its_device_s(
    scenario: Scenario, provider: FullyKioskProvider
) -> None:
    """The SSL setting of a player with a minted id is stored for its device and read back."""
    player_id = _player(provider, KITCHEN).player_id

    await scenario.configure_player(player_id, **{CONF_USE_SSL: True})

    protocol = provider.get_protocol(KITCHEN)
    assert protocol is not None
    assert protocol.get_config_value(CONF_USE_SSL) is True
    assert CONF_USE_SSL not in (scenario.mass.config.get(f"players/{player_id}/values") or {})
