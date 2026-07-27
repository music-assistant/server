"""HEOS Player Provider implementation."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import SetupFailedError
from music_assistant_models.player import PlayerSource
from pyheos import Heos, HeosError, HeosOptions, MediaItem, PlayerUpdateResult, const
from zeroconf import ServiceStateChange

from music_assistant.constants import CONF_ENABLED, CONF_IP_ADDRESS, VERBOSE_LOG_LEVEL
from music_assistant.helpers.util import get_primary_ip_address_from_zeroconf
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.providers.heos.constants import HEOS_PASSIVE_SOURCES

from .constants import (
    CONF_TIMEOUT,
    CONNECT_INITIAL_RETRY_DELAY,
    CONNECT_MAX_ATTEMPTS,
    CONNECT_RETRY_BACKOFF_FACTOR,
    DEFAULT_TIMEOUT,
)
from .player import HeosPlayer

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest
    from zeroconf.asyncio import AsyncServiceInfo

    from music_assistant.mass import MusicAssistant


class HeosPlayerProvider(PlayerProvider):
    """Player provided for Denon HEOS."""

    _heos: Heos | None = None
    _heos_queue: Heos | None = None
    _player_discovery_running: bool = False
    _controller_discovery_running: bool = False

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature] | None = None,
    ) -> None:
        """Initialize the HEOS player provider."""
        super().__init__(mass, manifest, config, supported_features)
        self._music_source_list: list[PlayerSource] = []
        self._input_source_list: list[MediaItem] = []

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        return (
            ConfigEntry(
                key=CONF_IP_ADDRESS,
                type=ConfigEntryType.STRING,
                required=False,
                advanced=True,
                requires_reload=True,
            ),
            ConfigEntry(
                key=CONF_TIMEOUT,
                type=ConfigEntryType.INTEGER,
                default_value=DEFAULT_TIMEOUT,
                required=False,
                range=(10, 60),
                requires_reload=True,
                advanced=True,
            ),
        )

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            logging.getLogger("pyheos").setLevel(logging.DEBUG)
        else:
            logging.getLogger("pyheos").setLevel(self.logger.level + 10)

        if ip_address := self.config.get_value(CONF_IP_ADDRESS):
            # Manual IP path
            ip_address = cast("str", ip_address)
            try:
                await self._setup_controllers(ip_address)
            except SetupFailedError:
                self.logger.error(
                    "Failed to set up HEOS controller at configured IP %s", ip_address
                )
                await self._disconnect_controllers()
                raise

            # Explicitly discover players now
            await self.discover_players()

    async def _setup_controllers(self, controller_ip: str, connect_preferred: bool = False) -> None:
        """Set up the HEOS controller."""
        self.logger.debug("Attempting HEOS controller setup on IP %s", controller_ip)

        self._heos = Heos(
            HeosOptions(
                controller_ip,
                timeout=cast("int", self.config.get_value(CONF_TIMEOUT)),
                auto_reconnect=True,
                auto_failover=True,
            )
        )
        await self._connect_controller(controller_ip)

        try:
            self.logger.debug("HEOS controller connected, checking preferred setup")
            system_info = await self._heos.get_system_info()
            preferred_ips: list[str] | None = [
                host.ip_address for host in system_info.preferred_hosts if host.ip_address
            ]

            if preferred_ips and controller_ip not in preferred_ips:
                if connect_preferred:
                    self.logger.debug(
                        "Attempting to connect to preferred Host %s", preferred_ips[0]
                    )
                    await self._heos.disconnect()
                    # Set up controller with preferred host instead
                    return await self._setup_controllers(preferred_ips[0], connect_preferred=False)

                # Just log a warning, it still works but might be less reliable
                self.logger.warning("Configured IP %s is not a preferred HEOS host", controller_ip)
        except HeosError as err:
            self.logger.error("Failed to verify HEOS controller health: %s", err)
            raise SetupFailedError("Failed to verify HEOS controller health") from err

        try:
            self._heos.add_on_controller_event(self._handle_controller_event)
            await self._populate_sources()
        except HeosError as err:
            self.logger.error("Unexpected error setting up HEOS controller: %s", err)
            raise SetupFailedError("Unexpected error setting up HEOS controller") from err

        # Set up up dedicated queue controller, queue commands can be slow and we don't want them to interfere with event processing on the main controller connection
        try:
            self._heos_queue = Heos(
                HeosOptions(
                    controller_ip,
                    timeout=cast("int", self.config.get_value(CONF_TIMEOUT)),
                    auto_reconnect=True,
                    auto_failover=True,
                    events=False,
                )
            )
            await self._heos_queue.connect()
        except HeosError as err:
            self.logger.error("Failed to set up HEOS queue controller: %s", err)
            raise SetupFailedError("Failed to set up HEOS queue controller") from err

    async def _connect_controller(self, controller_ip: str) -> None:
        """Connect to the HEOS controller with a few retries for early mDNS announcements."""
        assert self._heos is not None

        for attempt in range(1, CONNECT_MAX_ATTEMPTS + 1):
            try:
                await self._heos.connect()
            except HeosError as err:
                if attempt == CONNECT_MAX_ATTEMPTS:
                    self.logger.error(
                        "Failed to connect to HEOS controller at %s after %d attempts: %s",
                        controller_ip,
                        CONNECT_MAX_ATTEMPTS,
                        err,
                    )
                    raise SetupFailedError("Failed to connect to HEOS controller") from err

                retry_delay = CONNECT_INITIAL_RETRY_DELAY * (
                    CONNECT_RETRY_BACKOFF_FACTOR ** (attempt - 1)
                )
                self.logger.debug(
                    "HEOS controller connection attempt %d/%d failed for %s: %s. Retrying in %.1fs",
                    attempt,
                    CONNECT_MAX_ATTEMPTS,
                    controller_ip,
                    err,
                    retry_delay,
                )
                await self._heos.disconnect()
                await asyncio.sleep(retry_delay)
            else:
                if attempt > 1:
                    self.logger.debug(
                        "Connected to HEOS controller at %s on attempt %d/%d",
                        controller_ip,
                        attempt,
                        CONNECT_MAX_ATTEMPTS,
                    )
                return

    async def _handle_controller_event(
        self, event: str, result: PlayerUpdateResult | None = None
    ) -> None:
        self.logger.debug("Controller event received: %s", event)

        if event == const.EVENT_GROUPS_CHANGED:
            for player in self.mass.players.all_players(provider_filter=self.instance_id):
                assert isinstance(player, HeosPlayer)  # for type checking
                await player.build_group_list()

        if event == const.EVENT_PLAYERS_CHANGED:
            if result is None:
                return

            await self.discover_players()

    async def _populate_sources(self) -> None:
        """Build source list based on data from controller."""
        if not self._heos:
            return
        self._input_source_list = list(await self._heos.get_input_sources())

        music_sources = await self._heos.get_music_sources()
        for source_id, source in music_sources.items():
            self._music_source_list.append(
                PlayerSource(
                    id=str(source_id),
                    name=source.name,
                    passive=source_id in HEOS_PASSIVE_SOURCES or not source.available,
                    can_play_pause=True,  # All sources support play/pause
                    can_next_previous=source_id == 1024,  # TODO: properly check
                )
            )

    @property
    def music_source_list(self) -> list[PlayerSource]:
        """Get mapped music source list from controller info."""
        return self._music_source_list

    @property
    def input_source_list(self) -> list[MediaItem]:
        """Get input list from controller info. This represents all inputs across all players."""
        return self._input_source_list

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        await self._disconnect_controllers()

        for player in self.players:
            self.logger.debug("Unloading player %s", player.name)
            await self.mass.players.unregister(player.player_id)

    async def _disconnect_controllers(self) -> None:
        """Disconnect HEOS controller connections."""
        if self._heos:
            self._heos.dispatcher.disconnect_all()  # Remove all event connections
            with suppress(Exception):
                await self._heos.disconnect()
            self._heos = None

        if self._heos_queue:
            self._heos_queue.dispatcher.disconnect_all()  # Remove all event connections
            with suppress(Exception):
                await self._heos_queue.disconnect()
            self._heos_queue = None

    async def discover_players(self) -> None:
        """Discover players for this provider."""
        if self._controller_discovery_running or self._player_discovery_running or not self._heos:
            return  # discovery already running or not set up

        try:
            self._player_discovery_running = True
            self.logger.debug("Discovering HEOS players")
            devices = await self._heos.get_players()
            for device in devices.values():
                player_id = str(device.player_id)
                if player := cast("HeosPlayer", self.mass.players.get_player(player_id)):
                    self.logger.debug(
                        "Updating existing HEOS player: %s (%s)", device.name, player_id
                    )
                    # Update properties such as name or availability
                    player.set_device_info()
                    player.update_state()
                    continue

                player_enabled = self.mass.config.get_raw_player_config_value(
                    player_id, CONF_ENABLED, default=True
                )
                if not player_enabled:
                    self.logger.debug("Skipping disabled player: %s (%s)", device.name, player_id)
                    continue
                self.logger.info("Discovered new HEOS player: %s (%s)", device.name, player_id)

                heos_player = HeosPlayer(self, device)
                await heos_player.setup()
        finally:
            self._player_discovery_running = False

    async def on_mdns_service_state_change(
        self, name: str, state_change: ServiceStateChange, info: AsyncServiceInfo | None
    ) -> None:
        """Discovery via mdns."""
        if state_change == ServiceStateChange.Removed:
            return

        if not info:
            return

        if self._heos or self._controller_discovery_running:
            self.logger.debug("Ignoring mDNS configuration because we're already set up")
            # We're already set up or in the process of setting up
            return

        device_ip = get_primary_ip_address_from_zeroconf(info)
        if not device_ip:
            self.logger.debug("Ignoring incomplete mdns discovery for HEOS player: %s", name)
            return

        self.logger.debug("Discovered HEOS device %s on %s", name, device_ip)

        self._controller_discovery_running = True
        try:
            await self._setup_controllers(device_ip, True)
        except SetupFailedError:
            self.logger.error(
                "Failed to set up HEOS controller at %s discovered via mDNS", device_ip
            )
            await self._disconnect_controllers()
        finally:
            self._controller_discovery_running = False

        await self.discover_players()
