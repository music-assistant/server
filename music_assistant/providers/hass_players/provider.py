"""
Home Assistant PlayerProvider for Music Assistant.

Allows using media_player entities in HA to be used as players in MA.
Requires the Home Assistant Plugin.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, ProviderFeature

from music_assistant.mass import MusicAssistant
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.providers.hass import DOMAIN as HASS_DOMAIN

from .constants import (
    CONF_PLAYERS,
    DISABLED_REASON_NATIVE_DUPLICATE,
    DISABLED_REASON_NATIVE_INTEGRATION,
    NATIVE_SUPPORTED_HASS_INTEGRATIONS,
)
from .helpers import (
    get_esphome_supported_audio_formats,
    get_hass_media_players,
    get_media_player_entity_registry,
    native_player_macs,
    normalized_mac,
)
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
        supported_features = {ProviderFeature.REMOVE_PLAYER}
        super().__init__(mass, manifest, config, supported_features=supported_features)
        self.hass_prov = hass_prov

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        hass_prov = cast("HomeAssistantProvider|None", self.mass.get_provider(HASS_DOMAIN))
        player_entities: list[ConfigValueOption] = []
        if hass_prov and hass_prov.hass.connected:
            # entities that are already imported must stay selectable,
            # so editing the provider config never drops them
            selected_players = self._selected_players()
            device_registry = {x["id"]: x for x in await hass_prov.hass.get_device_registry()}
            native_macs = native_player_macs(self.mass)
            entity_registry = await get_media_player_entity_registry(hass_prov)
            async for state, entity_registry_entry in get_hass_media_players(
                hass_prov, entity_registry
            ):
                entity_id = state["entity_id"]
                name = f"{state['attributes']['friendly_name']} ({entity_id})"
                disabled_reason: str | None = None
                if entity_id not in selected_players:
                    disabled_reason = self._native_support_reason(
                        entity_registry_entry, device_registry, native_macs
                    )
                player_entities.append(
                    ConfigValueOption(
                        entity_id,
                        title=name,
                        disabled=disabled_reason is not None,
                        disabled_reason=disabled_reason,
                    )
                )
        return (
            ConfigEntry(
                key=CONF_PLAYERS,
                type=ConfigEntryType.STRING,
                multi_value=True,
                required=True,
                # the provider is added before any entity can be picked, so it starts out
                # with an empty selection and the players are chosen in its options
                default_value=[],
                options=player_entities,
            ),
        )

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await super().loaded_in_mass()
        player_ids = cast("list[str]", self.config.get_value(CONF_PLAYERS))
        # prefetch the device- and entity registry
        device_registry = {x["id"]: x for x in await self.hass_prov.hass.get_device_registry()}
        entity_registry = await get_media_player_entity_registry(self.hass_prov)
        # setup players from hass entities
        async for state, _ in get_hass_media_players(self.hass_prov, entity_registry):
            if state["entity_id"] not in player_ids:
                continue
            await self._setup_player(state, entity_registry, device_registry)
        # register for entity state updates
        self.on_unload_callbacks = [
            await self.hass_prov.hass.subscribe_entities(self._on_entity_state_update, player_ids)
        ]
        # cleanup any players that are no longer in the config
        for player_conf in await self.mass.config.get_player_configs(
            provider=self.instance_id, include_unavailable=True, include_disabled=True
        ):
            if player_conf.player_id not in player_ids:
                await self.mass.players.remove(player_conf.player_id)

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        is_removed will be set to True when the provider is removed from the configuration.
        """
        if self.on_unload_callbacks:
            for callback in self.on_unload_callbacks:
                callback()

    async def remove_player(self, player_id: str) -> None:
        """Remove a player."""
        player_ids = cast("list[str]", self.config.get_value(CONF_PLAYERS))
        if player_id in player_ids:
            player_ids.remove(player_id)
            self.mass.config.set_raw_provider_config_value(
                self.instance_id, CONF_PLAYERS, player_ids
            )
        await self.mass.players.unregister(player_id, True)

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
            player = cast("HomeAssistantPlayer | None", self.mass.players.get_player(entity_id))
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
        entity_registry = await get_media_player_entity_registry(self.hass_prov)
        async for state, _ in get_hass_media_players(self.hass_prov, entity_registry):
            if state["entity_id"] != entity_id:
                continue
            await self._setup_player(state, entity_registry, device_registry)

    def _selected_players(self) -> set[str]:
        """Return the entity ids currently selected as players (stored config)."""
        selected: set[str] = set()
        if isinstance(stored_players := self.get_config_value(CONF_PLAYERS), list):
            selected.update(cast("list[str]", stored_players))
        return selected

    @staticmethod
    def _native_support_reason(
        entity_registry_entry: HassEntity | None,
        device_registry: dict[str, HassDevice],
        native_macs: set[str],
    ) -> str | None:
        """Return why the entity can not be imported (natively supported), or None if it can."""
        if entity_registry_entry is None:
            return None
        if entity_registry_entry["platform"] in NATIVE_SUPPORTED_HASS_INTEGRATIONS:
            return DISABLED_REASON_NATIVE_INTEGRATION
        device = device_registry.get(entity_registry_entry["device_id"] or "")
        if device is not None and any(
            normalized_mac(connection[1]) in native_macs
            for connection in device.get("connections", [])
            if len(connection) == 2 and connection[0] == "mac"
        ):
            return DISABLED_REASON_NATIVE_DUPLICATE
        return None
