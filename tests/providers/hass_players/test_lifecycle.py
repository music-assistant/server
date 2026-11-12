"""Tests for the entities of the Home Assistant MediaPlayers provider on a running server."""

from __future__ import annotations

import asyncio
import gc
import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_OUTPUT_CODEC
from music_assistant.controllers.players.protocol_linking import PLAYER_ID_PREFIX
from music_assistant.providers.hass_players.constants import CONF_PLAYERS
from music_assistant.providers.hass_players.provider import HomeAssistantPlayerProvider

if TYPE_CHECKING:
    from music_assistant.models.player import Player
    from music_assistant.providers.hass import HomeAssistantProvider
    from music_assistant.providers.hass_players.player import HomeAssistantPlayer
    from tests.scenarios.harness import Scenario

BEDROOM = "media_player.bedroom"
KITCHEN = "media_player.kitchen"
PLAY_MEDIA = 512


def _hass_prov() -> HomeAssistantProvider:
    """Return a Home Assistant provider whose instance holds the bedroom and kitchen players."""
    entities = {
        entity_id: {"entity_id": entity_id, "platform": "kodi", "device_id": None, "id": entity_id}
        for entity_id in (BEDROOM, KITCHEN)
    }
    states = [
        {
            "entity_id": entity_id,
            "state": "idle",
            "attributes": {"friendly_name": entity_id, "supported_features": PLAY_MEDIA},
        }
        for entity_id in entities
    ]
    return cast(
        "HomeAssistantProvider",
        SimpleNamespace(
            instance_id="hass",
            hass=SimpleNamespace(
                subscribe_entities=AsyncMock(return_value=MagicMock()),
                call_service=AsyncMock(),
            ),
            logger=logging.getLogger("test.hass"),
            get_device_registry=AsyncMock(return_value={}),
            get_entity_registry=AsyncMock(return_value=entities),
            get_entity_registry_entries=AsyncMock(return_value=entities),
            get_states=AsyncMock(return_value=states),
        ),
    )


def _first_event() -> dict[str, Any]:
    """Return the first event of an entity subscription, which lists every entity as added."""
    attributes = {"supported_features": PLAY_MEDIA}
    return {
        "a": {
            entity_id: {"s": "idle", "a": {"friendly_name": entity_id, **attributes}}
            for entity_id in (BEDROOM, KITCHEN)
        }
    }


def _register(
    scenario: Scenario, instance_id: str, hass_prov: HomeAssistantProvider
) -> HomeAssistantPlayerProvider:
    """Put a real provider in place of the fake one of its instance, before it is loaded."""
    manifest = scenario.mass.get_provider_manifest("hass_players")
    entry = ConfigEntry(key=CONF_PLAYERS, type=ConfigEntryType.STRING, multi_value=True)
    raw = scenario.mass.config.get(f"providers/{instance_id}")
    config = cast("ProviderConfig", ProviderConfig.parse([entry], raw))
    provider = HomeAssistantPlayerProvider(scenario.mass, manifest, config, hass_prov)
    provider.available = True
    provider.initialized.set()
    scenario.mass._providers[instance_id] = provider
    return provider


async def _load(scenario: Scenario, instance_id: str) -> HomeAssistantPlayerProvider:
    """Load a real provider in place of the fake one of its instance, as the server does."""
    provider = _register(scenario, instance_id, _hass_prov())
    await provider.loaded_in_mass()
    await scenario.mass.run_provider_discovery(instance_id)
    await scenario.settle()
    return provider


@pytest.fixture
async def provider(scenario: Scenario) -> HomeAssistantPlayerProvider:
    """
    Return a real provider set up with the bedroom and kitchen entities.

    :param scenario: The running server.
    """
    fake = await scenario.add_provider("hass_players")
    scenario.mass.config.set_raw_provider_config_value(
        fake.instance_id, CONF_PLAYERS, [BEDROOM, KITCHEN]
    )
    return await _load(scenario, fake.instance_id)


def _player(provider: HomeAssistantPlayerProvider, entity_id: str) -> Player:
    """Return the player an entity is attached to."""
    protocol = provider.get_protocol(entity_id)
    assert protocol is not None
    assert protocol.player is not None
    return protocol.player


def _selected(scenario: Scenario, provider: HomeAssistantPlayerProvider) -> Any:
    """Return the entities the provider is set up with, as stored."""
    return scenario.mass.config.get_raw_provider_config_value(provider.instance_id, CONF_PLAYERS)


async def test_every_configured_entity_gets_a_player_of_its_own(
    provider: HomeAssistantPlayerProvider,
) -> None:
    """A configured entity first seen now is attached to a minted player."""
    bedroom = _player(provider, BEDROOM)

    assert bedroom.player_id.startswith(PLAYER_ID_PREFIX)
    assert bedroom.player_id != _player(provider, KITCHEN).player_id


async def test_discovery_leaves_an_attached_entity_alone(
    scenario: Scenario, provider: HomeAssistantPlayerProvider
) -> None:
    """Discovering again neither builds nor attaches anything for an attached entity."""
    protocol = provider.get_protocol(BEDROOM)

    await provider.discover_players()
    await scenario.settle()

    assert provider.get_protocol(BEDROOM) is protocol
    provider.hass_prov.get_states.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.parametrize("first", ["discovery", "event"])
async def test_an_entity_is_set_up_once_by_discovery_and_its_first_state_event(
    scenario: Scenario, first: str
) -> None:
    """
    An entity gets one player object, whichever of discovery and the first state event starts first.

    The other one starts while the first still reads the states from Home Assistant.
    """
    fake = await scenario.add_provider("hass_players")
    scenario.mass.config.set_raw_provider_config_value(
        fake.instance_id, CONF_PLAYERS, [BEDROOM, KITCHEN]
    )
    hass_prov = _hass_prov()
    states = hass_prov.get_states.return_value  # type: ignore[attr-defined]
    states_read = asyncio.Event()

    async def _get_states(**_kwargs: Any) -> list[dict[str, Any]]:
        await states_read.wait()
        return cast("list[dict[str, Any]]", states)

    hass_prov.get_states = AsyncMock(side_effect=_get_states)  # type: ignore[method-assign]
    provider = _register(scenario, fake.instance_id, hass_prov)
    await provider.loaded_in_mass()
    on_event = hass_prov.hass.subscribe_entities.await_args.args[0]
    attach = AsyncMock(wraps=scenario.mass.players.attach)

    with patch.object(scenario.mass.players, "attach", attach):
        if first == "event":
            on_event(_first_event())
            await scenario.settle()
        discovery = asyncio.create_task(scenario.mass.run_provider_discovery(fake.instance_id))
        await scenario.settle()
        if first == "discovery":
            on_event(_first_event())
            await scenario.settle()
        states_read.set()
        await discovery
        await scenario.settle()

    assert sorted(call.args[0].protocol_id for call in attach.await_args_list) == [BEDROOM, KITCHEN]
    hass_prov.get_states.assert_awaited_once()
    hass_prov.get_device_registry.assert_awaited_once()  # type: ignore[attr-defined]


async def test_discovery_offers_only_the_entities_without_an_attached_player(
    scenario: Scenario, provider: HomeAssistantPlayerProvider
) -> None:
    """With a disabled entity to offer, the attached one keeps its object."""
    protocol = provider.get_protocol(BEDROOM)
    await scenario.disable(_player(provider, KITCHEN).player_id)

    await provider.discover_players()
    await scenario.settle()

    assert provider.get_protocol(BEDROOM) is protocol
    assert provider.get_protocol(KITCHEN) is None


async def test_a_removed_entity_is_forgotten(
    scenario: Scenario, provider: HomeAssistantPlayerProvider
) -> None:
    """A removed player leaves the configured entities and does not come back."""
    player_id = _player(provider, BEDROOM).player_id

    await scenario.remove(player_id)
    await scenario.idle()

    assert _selected(scenario, provider) == [KITCHEN]
    assert provider.get_protocol(BEDROOM) is None
    assert [player.name for player in scenario.visible_players()] == [KITCHEN]


async def test_a_removed_disabled_entity_is_forgotten(
    scenario: Scenario, provider: HomeAssistantPlayerProvider
) -> None:
    """The removal of a player that is disabled also reaches its entity."""
    player_id = _player(provider, BEDROOM).player_id
    await scenario.disable(player_id)
    gc.collect()

    await scenario.remove(player_id)
    await scenario.idle()

    assert _selected(scenario, provider) == [KITCHEN]
    assert provider.get_protocol(BEDROOM) is None
    assert scenario.mass.config.get_protocol_record(BEDROOM) is None


async def test_a_removed_entity_that_never_attached_is_forgotten(
    scenario: Scenario, provider: HomeAssistantPlayerProvider
) -> None:
    """An entity disabled before the server started is found by the removal of its player."""
    player_id = _player(provider, BEDROOM).player_id
    await scenario.disable(player_id)
    await scenario.restart()
    provider = await _load(scenario, provider.instance_id)
    assert provider.get_protocol(BEDROOM) is None
    gc.collect()

    await scenario.remove(player_id)
    await scenario.idle()

    assert _selected(scenario, provider) == [KITCHEN]
    assert scenario.mass.config.get_protocol_record(BEDROOM) is None


async def test_a_re_enabled_entity_comes_back_on_its_player(
    scenario: Scenario, provider: HomeAssistantPlayerProvider
) -> None:
    """Enabling a disabled player attaches its entity to it again."""
    player_id = _player(provider, BEDROOM).player_id
    await scenario.disable(player_id)

    await scenario.enable(player_id)
    await scenario.idle()

    assert _player(provider, BEDROOM).player_id == player_id


async def test_an_entity_taken_out_of_the_config_is_removed(
    scenario: Scenario, provider: HomeAssistantPlayerProvider
) -> None:
    """At the next load an entity that is no longer configured loses its player."""
    player_id = _player(provider, BEDROOM).player_id
    scenario.mass.config.set_raw_provider_config_value(
        provider.instance_id, CONF_PLAYERS, [KITCHEN]
    )

    # a config change reloads the provider
    await scenario.mass.unload_provider(provider.instance_id)
    await _load(scenario, provider.instance_id)

    assert scenario.player(player_id) is None
    assert scenario.mass.config.get_protocol_record(BEDROOM) is None
    assert [player.name for player in scenario.visible_players()] == [KITCHEN]


async def test_a_setting_of_a_new_player_is_its_entity_s(
    scenario: Scenario, provider: HomeAssistantPlayerProvider
) -> None:
    """The codec of a player with a minted id is stored for its entity and read back."""
    player_id = _player(provider, BEDROOM).player_id

    await scenario.configure_player(player_id, **{CONF_OUTPUT_CODEC: "flac"})

    protocol = cast("HomeAssistantPlayer", provider.get_protocol(BEDROOM))
    assert protocol.get_config_value(CONF_OUTPUT_CODEC) == "flac"
    assert CONF_OUTPUT_CODEC not in (scenario.mass.config.get(f"players/{player_id}/values") or {})
