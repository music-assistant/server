"""WLED Audio Sync provider for Music Assistant."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.plugin import PluginProvider

from .bridge import WledBridgeManager
from .constants import (
    CONF_GAIN_DB,
    CONF_LATENCY_MS,
    CONF_PORT,
    DEFAULT_GAIN_DB,
    DEFAULT_LATENCY_MS,
    DEFAULT_PORT,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant

LOGGER = logging.getLogger(__name__)


class WledProvider(PluginProvider):
    """
    Provider that drives WLED's Audio Sync UDP protocol from MA playback.

    Each instance represents one sync zone, identified by a UDP port: any
    number of physical WLED devices join the zone by setting their own
    audioSyncPort (WLED's Usermods -> Audio Reactive -> Sync Settings) to
    match. Grouping the resulting virtual player with a real speaker player
    is what makes that zone's lights react to that speaker's audio.
    """

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature],
    ) -> None:
        """Initialize the provider."""
        super().__init__(mass, manifest, config, supported_features)
        self._bridge_manager: WledBridgeManager | None = None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the config entries for the WLED Audio Sync provider."""
        return (
            ConfigEntry(
                key=CONF_PORT,
                type=ConfigEntryType.INTEGER,
                default_value=DEFAULT_PORT,
                range=(1024, 65535),
                category="settings",
            ),
            ConfigEntry(
                key=CONF_LATENCY_MS,
                type=ConfigEntryType.INTEGER,
                default_value=DEFAULT_LATENCY_MS,
                range=(0, 3000),
                immediate_apply=True,
                category="settings",
            ),
            ConfigEntry(
                key=CONF_GAIN_DB,
                type=ConfigEntryType.FLOAT,
                default_value=DEFAULT_GAIN_DB,
                range=(-20, 40),
                immediate_apply=True,
                category="settings",
            ),
        )

    async def loaded_in_mass(self) -> None:
        """Start the sync-zone bridge for this instance's configured port."""
        port = int(float(str(self.config.get_value(CONF_PORT) or DEFAULT_PORT)))
        gain_db = float(str(self.config.get_value(CONF_GAIN_DB) or DEFAULT_GAIN_DB))
        self._bridge_manager = WledBridgeManager(self)
        await self._bridge_manager.start(port, gain_db=gain_db)
        self.available = True

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        if self._bridge_manager:
            await self._bridge_manager.stop()
            self._bridge_manager = None

    async def update_config(self, config: ProviderConfig, changed_keys: set[str]) -> None:
        """Handle config changes."""
        immediate_keys = {CONF_LATENCY_MS, CONF_GAIN_DB}
        if changed_keys and changed_keys <= immediate_keys and self._bridge_manager:
            self._bridge_manager.update_settings(
                latency_ms=int(float(str(config.get_value(CONF_LATENCY_MS) or DEFAULT_LATENCY_MS))),
                gain_db=float(str(config.get_value(CONF_GAIN_DB) or DEFAULT_GAIN_DB)),
            )
            self.config = config
            return

        # A changed port requires re-registering the Sendspin client, so fall
        # back to a full reload.
        await super().update_config(config, changed_keys)
