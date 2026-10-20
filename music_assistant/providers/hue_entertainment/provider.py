"""
Hue Lights Sync provider for Music Assistant.

Discovers entertainment areas on a paired Hue bridge and creates
virtual Sendspin players for each. When music plays to a virtual player,
the lights in that entertainment area react to the music.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from hue_entertainment import HueEntertainmentAPI
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType
from zeroconf import ServiceStateChange

from music_assistant.models.plugin import PluginProvider

from .bridge import HueEntertainmentBridgeManager
from .constants import (
    COLOR_MODES,
    CONF_BRIDGE_HOST,
    CONF_BRIDGE_ID,
    CONF_BRIGHTNESS,
    CONF_COLOR_MODE,
    CONF_HUE_LATENCY_MS,
    CONF_USERNAME,
    DEFAULT_BRIGHTNESS,
    DEFAULT_COLOR_MODE,
    DEFAULT_HUE_LATENCY_MS,
)
from .settings import get_brightness, get_color_mode, get_hue_latency_ms

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest
    from zeroconf.asyncio import AsyncServiceInfo

    from music_assistant.mass import MusicAssistant

LOGGER = logging.getLogger(__name__)


class HueEntertainmentProvider(PluginProvider):
    """Provider that syncs Hue lights to music via Sendspin."""

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature],
    ) -> None:
        """Initialize the provider."""
        super().__init__(mass, manifest, config, supported_features)
        self._hue_api: HueEntertainmentAPI | None = None
        self._bridge_manager: HueEntertainmentBridgeManager | None = None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """
        Return the (options) config entries for the Hue Entertainment provider.

        Bridge pairing runs in the interactive setup flow (see ``setup_flow.py``); only the
        playback/visualization settings are configured here.
        """
        return (
            ConfigEntry(
                key=CONF_BRIGHTNESS,
                type=ConfigEntryType.INTEGER,
                default_value=DEFAULT_BRIGHTNESS,
                range=(0, 100),
                category="settings",
            ),
            ConfigEntry(
                key=CONF_COLOR_MODE,
                type=ConfigEntryType.STRING,
                default_value=DEFAULT_COLOR_MODE,
                options=[ConfigValueOption(mode, title=mode.capitalize()) for mode in COLOR_MODES],
                category="settings",
            ),
            ConfigEntry(
                key=CONF_HUE_LATENCY_MS,
                type=ConfigEntryType.INTEGER,
                default_value=DEFAULT_HUE_LATENCY_MS,
                range=(0, 3000),
                immediate_apply=True,
                category="settings",
            ),
        )

    @property
    def hue_api(self) -> HueEntertainmentAPI | None:
        """Return the Hue API client."""
        return self._hue_api

    async def loaded_in_mass(self) -> None:
        """Initialize Hue bridge connection and set up entertainment area bridges."""
        # Migrate orphaned color_mode values from older versions to the default
        # so the settings dropdown shows a valid option.
        stored_mode = self.config.get_value(CONF_COLOR_MODE)
        if stored_mode is not None and str(stored_mode) not in COLOR_MODES:
            self._update_config_value(CONF_COLOR_MODE, DEFAULT_COLOR_MODE)

        host = self.get_setup_value(CONF_BRIDGE_HOST)
        username = self.get_setup_value(CONF_USERNAME)

        if not host or not username:
            self.logger.warning("Hue bridge not configured, provider inactive")
            self.available = False
            return

        self._hue_api = HueEntertainmentAPI(str(host), str(username))
        self._bridge_manager = HueEntertainmentBridgeManager(self)

        # Fetch entertainment areas and set up bridges
        try:
            areas = await self._hue_api.get_entertainment_areas()
            if not areas:
                self.logger.warning("No entertainment areas found on Hue bridge at %s", host)
            else:
                self.logger.info(
                    "Found %d entertainment area(s) on Hue bridge: %s",
                    len(areas),
                    ", ".join(a.name for a in areas),
                )
            await self._bridge_manager.setup_bridges(areas)
            self.available = True
        except Exception as err:
            self.logger.error("Failed to initialize Hue bridge at %s: %s", host, err)
            self.available = False

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        if self._bridge_manager:
            await self._bridge_manager.stop_all()
            self._bridge_manager = None
        if self._hue_api:
            await self._hue_api.close()
            self._hue_api = None

    async def on_mdns_service_state_change(
        self, name: str, state_change: ServiceStateChange, info: AsyncServiceInfo | None
    ) -> None:
        """
        Handle mDNS service discovery for Hue bridges.

        Updates the bridge IP address if it changes (e.g. DHCP renewal).
        """
        if info is None:
            return

        # Extract the bridge ID from the mDNS service name
        raw_bridge_id = info.properties.get(b"bridgeid", b"")
        bridge_id = raw_bridge_id.decode("utf-8", errors="ignore") if raw_bridge_id else ""
        if not bridge_id:
            return

        configured_bridge_id = self.get_setup_value(CONF_BRIDGE_ID) or ""

        if state_change == ServiceStateChange.Removed:
            if bridge_id == configured_bridge_id:
                self.logger.info("Hue bridge %s removed from network", bridge_id)
                self.available = False
            return

        # Extract IP address from service info
        addresses = info.parsed_addresses()
        if not addresses:
            return
        new_host = addresses[0]

        if state_change == ServiceStateChange.Added:
            # If we don't have a bridge ID configured yet, and no host is set,
            # this is likely the initial discovery during setup
            if not configured_bridge_id:
                self.logger.debug(
                    "Discovered Hue bridge %s at %s (not yet configured)", bridge_id, new_host
                )
                return

        if bridge_id != configured_bridge_id:
            return

        # Update the host if it changed
        current_host = self.get_setup_value(CONF_BRIDGE_HOST) or ""
        if new_host != current_host:
            self.logger.info(
                "Hue bridge %s IP changed from %s to %s",
                bridge_id,
                current_host,
                new_host,
            )
            if self._hue_api:
                self._hue_api.host = new_host
            # Persist the new IP
            self._update_setup_data(CONF_BRIDGE_HOST, new_host)

        if not self.available:
            self.available = True
            # Re-initialize bridges if we were previously unavailable
            if self._hue_api and self._bridge_manager:
                try:
                    areas = await self._hue_api.get_entertainment_areas()
                    await self._bridge_manager.setup_bridges(areas)
                except Exception as err:
                    self.logger.warning("Failed to reinitialize bridges: %s", err)

    async def update_config(self, config: ProviderConfig, changed_keys: set[str]) -> None:
        """
        Handle config changes.

        Settings like brightness/color_mode can be updated
        without a full provider reload.
        """
        # changed_keys arrive namespaced as 'values/<key>'; only skip the reload when
        # every changed key can be applied in place (anything else, including the
        # log level, still needs the base implementation).
        settings_keys = {
            f"values/{key}" for key in (CONF_BRIGHTNESS, CONF_COLOR_MODE, CONF_HUE_LATENCY_MS)
        }
        if changed_keys and changed_keys <= settings_keys and self._bridge_manager:
            self._bridge_manager.update_settings(
                color_mode=get_color_mode(config),
                brightness=get_brightness(config),
                hue_latency_ms=get_hue_latency_ms(config),
            )
            self.config = config
            return

        await super().update_config(config, changed_keys)
