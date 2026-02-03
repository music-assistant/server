"""Samsung WAM player provider."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ProviderFeature
from pywam.device import SPEAKER_MODELS

from music_assistant.constants import CONF_ENTRY_MANUAL_DISCOVERY_IPS
from music_assistant.models.player_provider import PlayerProvider

from .features.discovery.handler import DiscoveryHandler
from .features.grouping.coordinator import GroupingCoordinator

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType

    from music_assistant.mass import MusicAssistant

    from .player import WamPlayer


class SamsungWamProvider(PlayerProvider):
    """Samsung WAM player provider."""

    supported_models: dict[str, dict[str, Any]]
    wam_players: dict[str, WamPlayer]
    groups: GroupingCoordinator
    discovery: DiscoveryHandler

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return {ProviderFeature.SYNC_PLAYERS}

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider and its feature handlers."""
        self.supported_models = SPEAKER_MODELS
        self.wam_players = {}

        # Wire up provider-level feature handlers
        self.groups = GroupingCoordinator(self)
        self.discovery = DiscoveryHandler(self)

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded and is ready to start."""
        await self.discovery.start()
        self.groups.start_sync_task()

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider.

        :param is_removed: True if the provider is being permanently removed.
        """
        await self.discovery.stop()
        self.groups.stop_sync_task()
        self.wam_players.clear()
        self.groups.states.clear()


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return all config entries for this provider.

    :param mass: The MusicAssistant instance.
    :param instance_id: The ID of the provider instance.
    :param action: Action trigger from config UI.
    :param values: The current configuration values.
    :return: A tuple of ConfigEntry objects.
    """
    # ruff: noqa: ARG001
    return (CONF_ENTRY_MANUAL_DISCOVERY_IPS,)
