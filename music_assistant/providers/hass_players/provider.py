"""
Home Assistant PlayerProvider for Music Assistant.

Allows using media_player entities in HA to be used as players in MA.
Requires the Home Assistant Plugin.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from music_assistant.mass import MusicAssistant
from music_assistant.models.player_provider import PlayerProvider

from .constants import CONF_PLAYERS
from .helpers import get_esphome_supported_audio_formats, get_hass_media_players
from .player import HomeAssistantPlayer

if TYPE_CHECKING:
    from hass_client.models import CompressedState, EntityStateEvent
    from hass_client.models import Device as HassDevice
    from hass_client.models import Entity as HassEntity
    from hass_client.models import State as HassState
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.providers.hass import HomeAssistantProvider


class HomeAssistantPlayerProvider(PlayerProvider):
    """Home Assistant PlayerProvider for Music Assistant."""

    hass_prov: HomeAssistantProvider
    on_unload_callbacks: list[Callable[[], None]] | None = None

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        hass_prov: HomeAssistantProvider,
    ) -> None:
        """Initialize MusicProvider."""
        super().__init__(mass, manifest, config)
        self.hass_prov = hass_prov

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await super().loaded_in_mass()
        player_ids = cast("list[str]", self.config.get_value(CONF_PLAYERS))
        # prefetch the device- and entity registry
        device_registry = {x["id"]: x for x in await self.hass_prov.hass.get_device_registry()}
        entity_registry = {
            x["entity_id"]: x for x in await self.hass_prov.hass.get_entity_registry()
        }
        # setup players from hass entities
        async for state in get_hass_media_players(self.hass_prov):
            if state["entity_id"] not in player_ids:
                continue
            await self._setup_player(state, entity_registry, device_registry)
        # register for entity state updates
        self.on_unload_callbacks = [
            await self.hass_prov.hass.subscribe_entities(self._on_entity_state_update, player_ids)
        ]

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        is_removed will be set to True when the provider is removed from the configuration.
        """
        if self.on_unload_callbacks:
            for callback in self.on_unload_callbacks:
                callback()

    async def _setup_player(
        self,
        state: HassState,
        entity_registry: dict[str, HassEntity],
        device_registry: dict[str, HassDevice],
    ) -> None:
        """Handle setup of a Player from an hass entity."""
        hass_device: HassDevice | None = None
        hass_domain: str | None = None
        # collect extra player data
        extra_player_data: dict[str, Any] = {}
        if entity_registry_entry := entity_registry.get(state["entity_id"]):
            hass_device = device_registry.get(entity_registry_entry["device_id"])
            hass_domain = entity_registry_entry["platform"]
            extra_player_data["entity_registry_id"] = entity_registry_entry["id"]
            extra_player_data["hass_domain"] = hass_domain
            extra_player_data["hass_device_id"] = hass_device["id"] if hass_device else None
            if hass_domain == "esphome":
                # if the player is an ESPHome player, we need to check if it is a V2 player
                # as the V2 player has different capabilities and needs different config entries
                # The new media player component publishes its supported sample rates but that info
                # is not exposed directly by HA, so we fetch it from the diagnostics.
                esphome_supported_audio_formats = await get_esphome_supported_audio_formats(
                    self.hass_prov, entity_registry_entry["config_entry_id"]
                )
                extra_player_data["esphome_supported_audio_formats"] = (
                    esphome_supported_audio_formats
                )
        # collect device info
        dev_info: dict[str, Any] = {}
        if hass_device:
            extra_player_data["hass_device_id"] = hass_device["id"]
            if model := hass_device.get("model"):
                dev_info["model"] = model
            if manufacturer := hass_device.get("manufacturer"):
                dev_info["manufacturer"] = manufacturer
            if model_id := hass_device.get("model_id"):
                dev_info["model_id"] = model_id
            if sw_version := hass_device.get("sw_version"):
                dev_info["software_version"] = sw_version
            if connections := hass_device.get("connections"):
                for key, value in connections:
                    if key == "mac":
                        dev_info["mac_address"] = value

        # create the player
        player = HomeAssistantPlayer(
            provider=self,
            hass=self.hass_prov.hass,
            player_id=state["entity_id"],
            hass_state=state,
            dev_info=dev_info,
            extra_player_data=extra_player_data,
            entity_registry=entity_registry,
        )
        await self.mass.players.register(player)

    def _on_entity_state_update(self, event: EntityStateEvent) -> None:
        """Handle Entity State event."""

        def update_player_from_state_msg(entity_id: str, state: CompressedState) -> None:
            """Handle updating MA player with updated info in a HA CompressedState."""
            player = cast("HomeAssistantPlayer | None", self.mass.players.get(entity_id))
            if player is None:
                # edge case - one of our subscribed entities was not available at startup
                # and now came available - we should still set it up
                player_ids = cast("list[str]", self.config.get_value(CONF_PLAYERS))
                if entity_id not in player_ids:
                    return  # should not happen, but guard just in case
                self.mass.create_task(self._late_add_player(entity_id))
                return
            player.update_from_compressed_state(state)

        if entity_additions := event.get("a"):
            for entity_id, state in entity_additions.items():
                update_player_from_state_msg(entity_id, state)
        if entity_changes := event.get("c"):
            for entity_id, state_diff in entity_changes.items():
                if "+" not in state_diff:
                    continue
                update_player_from_state_msg(entity_id, state_diff["+"])

    async def _late_add_player(self, entity_id: str) -> None:
        """Handle setup of Player from HA entity that became available after startup."""
        # prefetch the device- and entity registry
        device_registry = {x["id"]: x for x in await self.hass_prov.hass.get_device_registry()}
        entity_registry = {
            x["entity_id"]: x for x in await self.hass_prov.hass.get_entity_registry()
        }
        async for state in get_hass_media_players(self.hass_prov):
            if state["entity_id"] != entity_id:
                continue
            await self._setup_player(state, entity_registry, device_registry)
