"""LIFX Lights Sync provider for Music Assistant."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from typing import TYPE_CHECKING, cast

from lifx_entertainment import (
    Grouping,
    LifxDevice,
    LifxLanAPI,
    LifxTransport,
    build_areas,
    discover_devices,
)
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import SetupFailedError

from music_assistant.models.plugin import PluginProvider

from .bridge import LifxEntertainmentBridgeManager
from .constants import (
    COLOR_MODES,
    CONF_BRIGHTNESS,
    CONF_BROADCAST,
    CONF_COLOR_MODE,
    CONF_DEVICES,
    CONF_EXTRA_HOSTS,
    CONF_GROUPING,
    CONF_LIGHT_LATENCY_MS,
    CONF_POWER_ON,
    CONF_TRANSITION_MS,
    DEFAULT_BRIGHTNESS,
    DEFAULT_BROADCAST,
    DEFAULT_COLOR_MODE,
    DEFAULT_GROUPING,
    DEFAULT_LIGHT_LATENCY_MS,
    DEFAULT_POWER_ON,
    DEFAULT_TRANSITION_MS,
    DISCOVERY_TIMEOUT_S,
    GROUPINGS,
    REDISCOVERY_INTERVAL_S,
)
from .settings import (
    get_brightness,
    get_color_mode,
    get_light_latency_ms,
    get_power_on,
    get_transition_ms,
)
from .setup_flow import parse_devices, parse_hosts

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.sendspin.provider import SendspinProvider

LOGGER = logging.getLogger(__name__)

# Settings that can be applied to running bridges without a provider reload.
_LIVE_SETTINGS = (
    CONF_BRIGHTNESS,
    CONF_COLOR_MODE,
    CONF_LIGHT_LATENCY_MS,
    CONF_TRANSITION_MS,
    CONF_POWER_ON,
)


class LifxEntertainmentProvider(PluginProvider):
    """Provider that syncs LIFX lights to music via Sendspin."""

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature],
    ) -> None:
        """Initialize the provider."""
        super().__init__(mass, manifest, config, supported_features)
        self._transport: LifxTransport | None = None
        self._api: LifxLanAPI | None = None
        self._devices: dict[str, LifxDevice] = {}
        self._bridge_manager: LifxEntertainmentBridgeManager | None = None
        self._rediscovery_task: asyncio.Task[None] | None = None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the playback settings; lights are picked in the setup flow."""
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
                key=CONF_LIGHT_LATENCY_MS,
                type=ConfigEntryType.INTEGER,
                default_value=DEFAULT_LIGHT_LATENCY_MS,
                range=(0, 3000),
                immediate_apply=True,
                category="settings",
            ),
            ConfigEntry(
                key=CONF_TRANSITION_MS,
                type=ConfigEntryType.INTEGER,
                default_value=DEFAULT_TRANSITION_MS,
                range=(0, 1000),
                immediate_apply=True,
                category="settings",
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_POWER_ON,
                type=ConfigEntryType.BOOLEAN,
                default_value=DEFAULT_POWER_ON,
                category="settings",
            ),
        )

    @property
    def lifx_api(self) -> LifxLanAPI | None:
        """Return the LIFX LAN API client."""
        return self._api

    async def handle_async_init(self) -> None:
        """Open the LAN transport, refresh the devices and set up a bridge per area."""
        # Reset a color mode that no longer exists, so the dropdown shows a valid option.
        stored_mode = self.config.get_value(CONF_COLOR_MODE)
        if stored_mode is not None and str(stored_mode) not in COLOR_MODES:
            self._update_config_value(CONF_COLOR_MODE, DEFAULT_COLOR_MODE)

        stored = parse_devices(self.get_setup_value(CONF_DEVICES))
        if not stored:
            raise SetupFailedError("No LIFX lights configured")
        self._devices = {d.serial: d for d in stored}
        sendspin_provider = cast("SendspinProvider | None", self.mass.get_provider("sendspin"))
        if sendspin_provider is None:
            raise SetupFailedError("The Sendspin provider is not available")

        try:
            self._transport = LifxTransport()
            await self._transport.open()
            self._api = LifxLanAPI(self._transport)
            await self._rediscover()
            self._bridge_manager = LifxEntertainmentBridgeManager(self)
            grouping = str(self.get_setup_value(CONF_GROUPING) or DEFAULT_GROUPING)
            if grouping not in GROUPINGS:
                grouping = DEFAULT_GROUPING
            areas = build_areas(list(self._devices.values()), Grouping(grouping))
            self.logger.info(
                "Found %d LIFX light player(s): %s",
                len(areas),
                ", ".join(f"{a.name} ({len(a.devices)} lights)" for a in areas),
            )
            await self._bridge_manager.setup_bridges(sendspin_provider, areas)
        except OSError as err:
            # unload() is not called for a provider whose setup failed: clean up here.
            await self.unload()
            raise SetupFailedError(f"Failed to open the LIFX LAN connection: {err}") from err
        except BaseException:
            await self.unload()
            raise
        self._rediscovery_task = self.mass.create_task(self._rediscovery_loop())

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        if self._rediscovery_task and not self._rediscovery_task.done():
            self._rediscovery_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._rediscovery_task
        self._rediscovery_task = None
        if self._bridge_manager:
            await self._bridge_manager.stop_all()
            self._bridge_manager = None
        if self._transport:
            self._transport.close()
            self._transport = None
        self._api = None

    async def update_config(self, config: ProviderConfig, changed_keys: set[str]) -> None:
        """Apply playback settings in place; reload for anything else."""
        settings_keys = {f"values/{key}" for key in _LIVE_SETTINGS}
        if changed_keys and changed_keys <= settings_keys and self._bridge_manager:
            self._bridge_manager.update_settings(
                color_mode=get_color_mode(config),
                brightness=get_brightness(config),
                light_latency_ms=get_light_latency_ms(config),
                transition_ms=get_transition_ms(config),
                power_on=get_power_on(config),
            )
            self.config = config
            return

        await super().update_config(config, changed_keys)

    async def _rediscovery_loop(self) -> None:
        """Periodically re-discover the lights so IP changes (DHCP) are followed."""
        while True:
            await asyncio.sleep(REDISCOVERY_INTERVAL_S)
            try:
                await self._rediscover()
            except Exception as err:  # noqa: BLE001 - the background rediscovery must keep running
                self.logger.debug("LIFX rediscovery failed: %s", err)

    async def _rediscover(self) -> None:
        """Update the configured devices in place, so running sessions follow IP changes."""
        if self._transport is None:
            return
        broadcast = str(self.get_setup_value(CONF_BROADCAST) or DEFAULT_BROADCAST)
        hosts = parse_hosts(self.get_setup_value(CONF_EXTRA_HOSTS))
        # Probe the last known addresses too, in case broadcasts don't reach this host.
        hosts += [d.host for d in self._devices.values() if d.host not in hosts]
        found = await discover_devices(
            self._transport, timeout=DISCOVERY_TIMEOUT_S, broadcast=broadcast, hosts=hosts
        )
        changed = False
        for device in found:
            current = self._devices.get(device.serial)
            if current is None:
                continue
            if current.host != device.host or current.port != device.port:
                self.logger.info(
                    "LIFX light '%s' moved from %s to %s", current.label, current.host, device.host
                )
                current.host, current.port = device.host, device.port
                changed = True
            if device.label:
                current.label = device.label
            # Zones define the bridges' channels, so only refresh them before the bridges
            # exist. A failed probe reads as "no zones", so only trust a positive one.
            if (
                self._bridge_manager is None
                and device.is_multizone
                and (
                    (current.zone_support, current.zone_count)
                    != (device.zone_support, device.zone_count)
                )
            ):
                current.zone_support, current.zone_count = device.zone_support, device.zone_count
                changed = True
        missing = set(self._devices) - {d.serial for d in found}
        if missing:
            self.logger.debug(
                "LIFX light(s) not answering discovery: %s",
                ", ".join(self._devices[s].label or s for s in missing),
            )
        if changed:
            self._update_setup_data(
                CONF_DEVICES, json.dumps([d.to_dict() for d in self._devices.values()])
            )
