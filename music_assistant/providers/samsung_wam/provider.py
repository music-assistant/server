"""Samsung WAM player provider."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse

from music_assistant_models.enums import ProviderFeature
from pywam.device import SPEAKER_MODELS

from music_assistant.constants import CONF_ENTRY_MANUAL_DISCOVERY_IPS
from music_assistant.models.player_provider import PlayerProvider

from .features.discovery.handler import DiscoveryHandler
from .features.grouping.coordinator import GroupingCoordinator

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry

    from .player import WamPlayer


class SamsungWamProvider(PlayerProvider):
    """Samsung WAM player provider."""

    supported_models: dict[str, dict[str, Any]]
    groups: GroupingCoordinator
    discovery: DiscoveryHandler

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return {ProviderFeature.SYNC_PLAYERS}

    def get_players(self) -> list[WamPlayer]:
        """Return all registered WAM players."""
        return cast("list[WamPlayer]", self.players)

    def get_player(self, player_id: str) -> WamPlayer | None:
        """
        Return a WAM player by ID.

        :param player_id: The player ID to look up.
        :return: The matching WamPlayer, or None if not found.
        """
        return cast("WamPlayer | None", self.mass.players.get_player(player_id))

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return all config entries for this provider."""
        return (CONF_ENTRY_MANUAL_DISCOVERY_IPS,)

    async def handle_async_init(self) -> None:
        """Initialise the provider."""
        self.supported_models = SPEAKER_MODELS
        self.groups = GroupingCoordinator(self)
        self.discovery = DiscoveryHandler(self)

    async def loaded_in_mass(self) -> None:
        """Start discovery and initial group sync."""
        await self.discovery.start()
        self.groups.start_sync_task()

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle close/cleanup of the provider.

        :param is_removed: True if the provider is being permanently removed.
        """
        await self.discovery.stop()
        self.groups.stop_sync_task()
        for player in list(self.get_players()):
            self.logger.debug("Unloading player %s", player.log_name)
            await self.mass.players.unregister(player.player_id, is_removed)
        self.groups.states.clear()

    async def on_upnp_service_discovered(
        self, search_target: str, discovery_info: Mapping[str, Any]
    ) -> None:
        """
        Handle a UPnP/SSDP presence notification.

        :param search_target: The SSDP service type that was matched.
        :param discovery_info: The raw SSDP response headers.
        """
        location = discovery_info.get("location", "")
        ip_address = urlparse(location).hostname
        if not ip_address:
            return

        usn = discovery_info.get("usn", "")
        udn = usn.split("::")[0].removeprefix("uuid:")
        if not udn:
            return

        await self.discovery.on_upnp_discovered(udn, ip_address)
