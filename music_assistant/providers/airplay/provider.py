"""AirPlay Player provider for Music Assistant."""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import time
from contextlib import suppress
from ipaddress import ip_address
from typing import Final, cast

from music_assistant_models.enums import PlaybackState
from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceInfo

from music_assistant.constants import CONF_PLAYERS, CONF_SYNC_ADJUST, VERBOSE_LOG_LEVEL
from music_assistant.helpers.datetime import utc
from music_assistant.helpers.json import SerializableType
from music_assistant.helpers.process import AsyncProcess
from music_assistant.helpers.util import (
    get_ip_pton,
    get_primary_ip_address_from_zeroconf,
    select_free_port,
)
from music_assistant.models.player_provider import PlayerProvider

from .constants import (
    AIRPLAY_DISCOVERY_TYPE,
    AIRPLAY_VOLUME_MUTE,
    COMPANION_DISCOVERY_TYPE,
    CONF_FORCE_RAOP,
    CONF_IGNORE_VOLUME,
    CONF_LEGACY_AIRPLAY_PROTOCOL,
    CONF_LEGACY_FORCE_RAOP,
    CONF_PROTOCOL_MIGRATION_MARKER,
    CONF_STORED_VOLUME,
    CONF_SYNC_ADJUST_RESET_MARKER,
    DACP_DISCOVERY_TYPE,
    FALLBACK_VOLUME,
    MRP_DISCOVERY_TYPE,
    RAOP_DISCOVERY_TYPE,
    AirPlayRemoteCommand,
    StreamingProtocol,
)
from .control_player import AirPlayControlPlayer
from .helpers import (
    convert_airplay_volume,
    get_cli_binary,
    get_model_info,
    is_apple_device,
)
from .player import AirPlayPlayer, GenericAirPlayPlayer
from .sendspin_bridge import SendspinBridgeManager

# Marker the `cliairplay --ptp-daemon` process prints once it has bound the
# privileged PTP ports (UDP 319/320) and opened its control channel. Until this
# line is seen the daemon is spawned but not yet able to serve shared-clock
# streams, so a group start before it would race the daemon's readiness.
PTP_DAEMON_READY_MARKER: Final[str] = "[PTP] daemon up"
# Bounded wait for the daemon to report readiness before a stream session
# decides its timing source. Once ready the check returns immediately; only a
# session that starts while the daemon is still coming up (or never binds) pays
# any of this, and only up to the moment readiness is signalled.
PTP_DAEMON_READY_TIMEOUT: Final[float] = 3.0


class AirPlayProvider(PlayerProvider):
    """Player provider for AirPlay based players."""

    _dacp_server: asyncio.Server
    _dacp_info: AsyncServiceInfo
    _bridge_manager: SendspinBridgeManager
    _ptp_daemon: AsyncProcess | None = None
    _ptp_daemon_stdout_task: asyncio.Task[None] | None = None
    _ptp_daemon_started: float = 0.0
    _ptp_daemon_restarted: bool = False
    _ptp_daemon_stop_requested: bool = False
    # Set once the running daemon reports it has bound 319/320 and opened its
    # control channel; created/cleared per daemon start so a crash+restart
    # re-gates readiness. None until the daemon is first started.
    _ptp_daemon_ready: asyncio.Event | None = None

    @property
    def bridge_manager(self) -> SendspinBridgeManager:
        """Return the Sendspin bridge manager."""
        return self._bridge_manager

    @property
    def ptp_daemon_running(self) -> bool:
        """
        Return if the shared PTP clock daemon process is alive.

        This reflects process liveness only (spawned, not closed, still
        running) for status/diagnostics. The streaming timing decision must use
        :meth:`wait_ptp_daemon_ready`, which also waits for the daemon to have
        actually bound its ports and opened its control channel.
        """
        return (
            self._ptp_daemon is not None
            and not self._ptp_daemon.closed
            and self._ptp_daemon.returncode is None
        )

    async def wait_ptp_daemon_ready(self, timeout: float = PTP_DAEMON_READY_TIMEOUT) -> bool:
        """
        Wait until the shared PTP clock daemon is ready to serve streams.

        Readiness means the daemon has bound the privileged PTP ports (UDP
        319/320) and opened its control channel - not merely that the process is
        alive. A stream session gates its group-wide timing decision on this so
        it never attaches members to a clock that is not yet serving.

        :param timeout: Maximum seconds to wait for the readiness signal.
        :return: True if the daemon has signalled readiness, False otherwise
            (never started, failed to bind, or not ready within the timeout).
        """
        event = self._ptp_daemon_ready
        daemon = self._ptp_daemon
        if event is None or daemon is None or daemon.closed or daemon.returncode is not None:
            return False
        if event.is_set():
            return True

        ready_task = asyncio.create_task(event.wait())
        exit_task = asyncio.create_task(daemon.wait())
        try:
            done, _ = await asyncio.wait(
                (ready_task, exit_task),
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            return (
                ready_task in done
                and event.is_set()
                and self._ptp_daemon is daemon
                and not daemon.closed
                and daemon.returncode is None
            )
        finally:
            ready_task.cancel()
            exit_task.cancel()
            await asyncio.gather(ready_task, exit_task, return_exceptions=True)

    def handle_remote_command(self, player: AirPlayPlayer, command: AirPlayRemoteCommand) -> None:
        """Dispatch a transport command received from an AirPlay receiver."""
        player_id = player.player_id
        match command:
            case AirPlayRemoteCommand.PLAY:
                # Some receivers echo play as confirmation of a command from MA.
                if player.playback_state != PlaybackState.PLAYING:
                    self.mass.create_task(self.mass.players.cmd_play(player_id))
            case AirPlayRemoteCommand.PAUSE:
                if player.playback_state == PlaybackState.PLAYING:
                    self.mass.create_task(self.mass.players.cmd_pause(player_id))
            case AirPlayRemoteCommand.PLAY_PAUSE:
                self.mass.create_task(self.mass.players.cmd_play_pause(player_id))
            case AirPlayRemoteCommand.NEXT:
                self.mass.create_task(self.mass.players.cmd_next_track(player_id))
            case AirPlayRemoteCommand.PREVIOUS:
                self.mass.create_task(self.mass.players.cmd_previous_track(player_id))

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._companion_info_by_address: dict[str, AsyncServiceInfo] = {}
        self._mrp_info_by_address: dict[str, AsyncServiceInfo] = {}

        # Initialize Sendspin bridge manager for protocol linking
        self._bridge_manager = SendspinBridgeManager(self)

        # register DACP zeroconf service
        dacp_port = await select_free_port(39831, 49831)
        # Use first 16 hex chars of server_id as a persistent DACP ID
        # This ensures the DACP ID remains the same across restarts, which is required
        # for AirPlay 2 (HAP) pair-verify to work with previously paired devices
        self.dacp_id = dacp_id = self.mass.server_id[:16].upper()
        self.logger.debug("Starting DACP ActiveRemote %s on port %s", dacp_id, dacp_port)
        self._dacp_server = await asyncio.start_server(self._handle_dacp_request, port=dacp_port)
        server_id = f"iTunes_Ctrl_{dacp_id}.{DACP_DISCOVERY_TYPE}"
        self._dacp_info = AsyncServiceInfo(
            DACP_DISCOVERY_TYPE,
            name=server_id,
            addresses=[await get_ip_pton(str(self.mass.streams.publish_ip))],
            port=dacp_port,
            properties={
                "txtvers": "1",
                "Ver": "63B5E5C0C201542E",
                "DbId": "63B5E5C0C201542E",
                "OSsi": "0x1F5",
            },
            server=f"{socket.gethostname()}.local",
        )
        await self.mass.discovery.aiozc.async_register_service(self._dacp_info)

        self._migrate_protocol_preferences()
        self._migrate_sync_adjust()

        # Run one shared PTP clock daemon for the provider lifetime: all native
        # AirPlay 2 streams attach to it (--ptp-shared) so multi-room sync groups
        # lock to a single grandmaster while UDP 319/320 is bound only once.
        await self._start_ptp_daemon()

    async def on_mdns_service_state_change(
        self, name: str, state_change: ServiceStateChange, info: AsyncServiceInfo | None
    ) -> None:
        """Handle MDNS service state callback."""
        if (info and info.type == COMPANION_DISCOVERY_TYPE) or name.endswith(
            COMPANION_DISCOVERY_TYPE
        ):
            await self._handle_companion_service_state_change(name, state_change, info)
            return
        if (info and info.type == MRP_DISCOVERY_TYPE) or name.endswith(MRP_DISCOVERY_TYPE):
            await self._handle_mrp_service_state_change(name, state_change, info)
            return
        if not info:
            if state_change == ServiceStateChange.Removed and "@" in name:
                # Service name is enough to mark the player as unavailable on 'Removed' notification
                raw_id, display_name = name.split(".", maxsplit=1)[0].split("@", 1)
            else:
                # If we are not in a 'Removed' state, we need info to be filled to update the player
                return
        elif "@" in info.name:
            raw_id, display_name = info.name.split(".")[0].split("@", 1)
        elif deviceid := info.decoded_properties.get("deviceid"):
            raw_id = deviceid.replace(":", "")
            display_name = info.name.split(".")[0]
        else:
            return
        player_id = f"ap{raw_id.lower()}"
        # handle removed player
        if state_change == ServiceStateChange.Removed:
            if _player := self.mass.players.get_player(player_id):
                # the player has become unavailable
                self.logger.debug("Player offline: %s", _player.display_name)
                # Remove the Sendspin bridge first
                await self._bridge_manager.remove_bridge(player_id)
                await self.mass.players.unregister(player_id)
            return
        # handle update for existing device
        assert info is not None  # type guard
        player: AirPlayPlayer | None
        if player := cast("AirPlayPlayer | None", self.mass.players.get_player(player_id)):
            # update the latest discovery info for existing player
            player.set_discovery_info(info, display_name)
            return
        await self._setup_player(player_id, display_name, info)

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        # Stop all Sendspin bridges
        bridge_manager = getattr(self, "_bridge_manager", None)
        if bridge_manager:
            await bridge_manager.close()
        # terminate the shared PTP clock daemon
        self._ptp_daemon_stop_requested = True
        if self._ptp_daemon_ready is not None:
            self._ptp_daemon_ready.clear()
        ptp_stdout_task = self._ptp_daemon_stdout_task
        if ptp_stdout_task and not ptp_stdout_task.done():
            ptp_stdout_task.cancel()
            with suppress(asyncio.CancelledError):
                await ptp_stdout_task
        if self._ptp_daemon and not self._ptp_daemon.closed:
            await self._ptp_daemon.close()
        self._ptp_daemon = None
        # shutdown DACP server
        if self._dacp_server:
            self._dacp_server.close()
        # shutdown DACP zeroconf service
        if self._dacp_info:
            await self.mass.discovery.aiozc.async_unregister_service(self._dacp_info)

    async def get_diagnostics(self) -> dict[str, SerializableType]:
        """Return diagnostics info for this provider to include in diagnostics reports."""
        streams_by_type: dict[str, int] = {}
        for player in self.get_players():
            if not (player.stream and player.stream.running):
                continue
            stream_type = "airplay2" if player.protocol == StreamingProtocol.AIRPLAY2 else "raop"
            streams_by_type[stream_type] = streams_by_type.get(stream_type, 0) + 1
        return {
            "dacp_server_running": self._dacp_server.is_serving(),
            "ptp_daemon_running": self.ptp_daemon_running,
            "active_streams": sum(streams_by_type.values()),
            "streams_by_type": streams_by_type,
        }

    def get_players(self) -> list[AirPlayPlayer]:
        """Return all airplay players belonging to this instance."""
        return cast("list[AirPlayPlayer]", self.players)

    def get_player(self, player_id: str) -> AirPlayPlayer | None:
        """Return AirplayPlayer by id."""
        return cast("AirPlayPlayer | None", self.mass.players.get_player(player_id))

    async def _setup_player(
        self, player_id: str, display_name: str, discovery_info: AsyncServiceInfo
    ) -> None:
        """Handle setup of a new player that is discovered using mdns."""
        # return early if player is disabled in config
        if not self.mass.config.get_raw_player_config_value(player_id, "enabled", True):
            self.logger.debug("Ignoring %s in discovery as it is disabled.", display_name)
            return
        raop_discovery_info: AsyncServiceInfo | None = None
        airplay_discovery_info: AsyncServiceInfo | None = None
        if discovery_info.type == RAOP_DISCOVERY_TYPE:
            # RAOP service discovered - try to also find the AirPlay service
            raop_discovery_info = discovery_info
            self.logger.debug("Discovered RAOP service for %s", display_name)
            airplay_discovery_info = await self.mass.discovery.async_find_mdns_service(
                AIRPLAY_DISCOVERY_TYPE, display_name, timeout=10.0
            )
        else:
            # AirPlay service discovered - try to also find the RAOP service
            self.logger.debug("Discovered AirPlay service for %s", display_name)
            airplay_discovery_info = discovery_info
            raop_discovery_info = await self.mass.discovery.async_find_mdns_service(
                RAOP_DISCOVERY_TYPE, display_name, timeout=10.0
            )

        if airplay_discovery_info:
            model_discovery_info = airplay_discovery_info
        elif raop_discovery_info:
            model_discovery_info = raop_discovery_info
        else:
            return  # should not happen, but guard just in case
        manufacturer, model = get_model_info(model_discovery_info)

        prefer_ipv6 = ":" in str(self.mass.streams.publish_ip)
        address = get_primary_ip_address_from_zeroconf(discovery_info, prefer_ipv6=prefer_ipv6)
        if not address:
            return  # should not happen, but guard just in case

        # Filter out shairport-sync instances running on THIS Music Assistant server
        # These are managed by the AirPlay Receiver provider, not the AirPlay provider
        # We check both model name AND that it's a local address to avoid filtering
        # shairport-sync instances running on other machines
        if model == "ShairportSync":
            # Check if this is a local address (loopback or matches our server's IP)
            if ip_address(address).is_loopback or address == self.mass.streams.publish_ip:
                # Only filter if the port matches one of MA's own AirPlay Receiver instances.
                # This allows user-configured shairport-sync instances on the same machine
                # to be used as AirPlay players (e.g., multiple audio outputs via shairport-sync).
                receiver_ports = {
                    port
                    for prov in self.mass.get_provider_instances("airplay_receiver")
                    if (port := getattr(prov, "airplay_port", None)) is not None
                }
                if discovery_info.port in receiver_ports:
                    return

        # if we reach this point, all preflights are ok and we can create the player
        self.logger.debug("Discovered AirPlay device %s on %s", display_name, address)

        # Get stored volume from playerconfig
        volume = int(
            self.mass.config.get_raw_player_config_value(
                player_id, CONF_STORED_VOLUME, FALLBACK_VOLUME
            )
        )

        # Final check before registration to handle race conditions
        # (multiple MDNS events processed in parallel for same device)
        if self.mass.players.get_player(player_id):
            self.logger.debug(
                "Player %s already registered during setup, skipping registration", player_id
            )
            return

        self.logger.debug(
            "Setting up player %s: manufacturer=%s, model=%s",
            display_name,
            manufacturer,
            model,
        )

        player_addresses = self._get_discovery_addresses(
            airplay_discovery_info,
            raop_discovery_info,
        )
        player_addresses.add(address)
        # Apple TVs and HomePods are standalone players with a native control
        # plane (Companion/MRP); all other receivers are protocol endpoints.
        # The model is decided from the device's own identity only: it defines
        # the player id exposed to API consumers (e.g. Home Assistant), so it
        # must not vary with the discovery timing of the separate Companion/MRP
        # mDNS records. Which control features are offered on a control player
        # is decided from the advertised capabilities instead.
        enhanced_control = is_apple_device(manufacturer, model)
        companion_info: AsyncServiceInfo | None = None
        mrp_info: AsyncServiceInfo | None = None
        if enhanced_control:
            companion_info, mrp_info = await asyncio.gather(
                self._get_related_discovery_info(
                    COMPANION_DISCOVERY_TYPE,
                    self._companion_info_by_address,
                    player_addresses,
                    display_name,
                ),
                self._get_related_discovery_info(
                    MRP_DISCOVERY_TYPE,
                    self._mrp_info_by_address,
                    player_addresses,
                    display_name,
                ),
            )

        player: AirPlayPlayer
        if enhanced_control:
            player = AirPlayControlPlayer(
                provider=self,
                player_id=player_id,
                raop_discovery_info=raop_discovery_info,
                airplay_discovery_info=airplay_discovery_info,
                companion_discovery_info=companion_info,
                mrp_discovery_info=mrp_info,
                address=address,
                display_name=display_name,
                manufacturer=manufacturer,
                model=model,
                initial_volume=volume,
            )
        else:
            player = GenericAirPlayPlayer(
                provider=self,
                player_id=player_id,
                raop_discovery_info=raop_discovery_info,
                airplay_discovery_info=airplay_discovery_info,
                address=address,
                display_name=display_name,
                manufacturer=manufacturer,
                model=model,
                initial_volume=volume,
            )
        await self.mass.players.register(player)

        # Set up Sendspin bridge for protocol linking (if Sendspin provider is available)
        await self._bridge_manager.evaluate_bridge(player)

    async def _handle_companion_service_state_change(
        self,
        name: str,
        state_change: ServiceStateChange,
        info: AsyncServiceInfo | None,
    ) -> None:
        """Associate a Companion service with its controlled AirPlay player."""
        if state_change == ServiceStateChange.Removed:
            # Keep the last endpoint while the device sleeps. Some devices can
            # withdraw services from mDNS before Companion reports the asleep
            # state, but the existing endpoint is still needed to wake them.
            return
        if info is None:
            return

        addresses = self._cache_control_service(self._companion_info_by_address, info)
        player = self._find_airplay_player(addresses)
        if isinstance(player, AirPlayControlPlayer):
            await player.set_companion_discovery_info(info)

    async def _handle_mrp_service_state_change(
        self,
        name: str,
        state_change: ServiceStateChange,
        info: AsyncServiceInfo | None,
    ) -> None:
        """Associate an MRP service with its controlled AirPlay player."""
        if state_change == ServiceStateChange.Removed or info is None:
            return
        addresses = self._cache_control_service(self._mrp_info_by_address, info)
        player = self._find_airplay_player(addresses)
        if isinstance(player, AirPlayControlPlayer):
            await player.set_mrp_discovery_info(info)

    async def _get_related_discovery_info(
        self,
        service_type: str,
        info_by_address: dict[str, AsyncServiceInfo],
        player_addresses: set[str],
        display_name: str,
    ) -> AsyncServiceInfo | None:
        """Return a related control service from cache or mDNS discovery."""
        for address in player_addresses:
            if discovery_info := info_by_address.get(address):
                return discovery_info
        if discovery_info := await self._find_cached_discovery_info(
            service_type,
            player_addresses,
        ):
            self._cache_control_service(info_by_address, discovery_info)
            return discovery_info
        discovery_info = await self.mass.discovery.async_find_mdns_service(
            service_type,
            display_name,
        )
        if discovery_info is None:
            discovery_info = await self._find_cached_discovery_info(
                service_type,
                player_addresses,
            )
            if discovery_info is None:
                return None
        if player_addresses.isdisjoint(discovery_info.parsed_addresses()):
            return None
        self._cache_control_service(info_by_address, discovery_info)
        return discovery_info

    async def _find_cached_discovery_info(
        self,
        service_type: str,
        player_addresses: set[str],
    ) -> AsyncServiceInfo | None:
        """Find a cached mDNS service sharing an address with the AirPlay endpoint."""
        service_type_lower = service_type.lower()
        for mdns_name in set(self.mass.discovery.aiozc.zeroconf.cache.cache):
            if service_type_lower not in mdns_name or mdns_name == service_type_lower:
                continue
            discovery_info = AsyncServiceInfo(service_type, mdns_name)
            if not await discovery_info.async_request(
                self.mass.discovery.aiozc.zeroconf,
                3000,
            ):
                continue
            if not player_addresses.isdisjoint(discovery_info.parsed_addresses()):
                return discovery_info
        return None

    def _find_airplay_player(self, addresses: set[str]) -> AirPlayPlayer | None:
        """Return the AirPlay player matching any advertised address."""
        return next(
            (
                candidate
                for candidate in self.get_players()
                if not addresses.isdisjoint(
                    {
                        candidate.address,
                        *self._get_discovery_addresses(
                            candidate.airplay_discovery_info,
                            candidate.raop_discovery_info,
                            candidate.mrp_discovery_info
                            if isinstance(candidate, AirPlayControlPlayer)
                            else None,
                        ),
                    }
                )
            ),
            None,
        )

    @staticmethod
    def _cache_control_service(
        info_by_address: dict[str, AsyncServiceInfo],
        info: AsyncServiceInfo,
    ) -> set[str]:
        """Cache a control service by address and return its current addresses."""
        addresses = set(info.parsed_addresses())
        # Drop addresses this service no longer advertises (e.g. after a DHCP
        # change), so a stale entry can never classify a future device that
        # gets the old address assigned.
        for stale_address in [
            address
            for address, cached in info_by_address.items()
            if cached.name == info.name and address not in addresses
        ]:
            del info_by_address[stale_address]
        for address in addresses:
            info_by_address[address] = info
        return addresses

    @staticmethod
    def _get_discovery_addresses(
        *discovery_infos: AsyncServiceInfo | None,
    ) -> set[str]:
        """Return all IP addresses advertised by the given mDNS services."""
        return {
            address
            for discovery_info in discovery_infos
            if discovery_info is not None
            for address in discovery_info.parsed_addresses()
        }

    def _migrate_sync_adjust(self) -> None:
        """One-time reset of persisted sync_adjust values on this provider's players."""
        # The unified cliairplay binary uses a different timing model than the old
        # implementation, so offsets calibrated against it would now break sync
        # instead of fixing it. Reset them once; a provider-level marker prevents
        # wiping adjustments the user makes after the migration.
        if self.mass.config.get_raw_provider_config_value(
            self.instance_id, CONF_SYNC_ADJUST_RESET_MARKER, False
        ):
            return
        for raw_conf in list(self.mass.config.get(CONF_PLAYERS, {}).values()):
            if not isinstance(raw_conf, dict) or raw_conf.get("provider") != self.instance_id:
                continue
            if not (player_id := raw_conf.get("player_id")):
                continue
            stored = self.mass.config.get_raw_player_config_value(player_id, CONF_SYNC_ADJUST, 0)
            if not stored:
                continue
            self.logger.warning(
                "Resetting sync_adjust of %sms for player %s: the unified AirPlay engine "
                "uses a different timing model, so corrections calibrated against the old "
                "implementation no longer apply",
                stored,
                player_id,
            )
            self.mass.config.set_raw_player_config_value(player_id, CONF_SYNC_ADJUST, 0)
        self.mass.config.set_raw_provider_config_value(
            self.instance_id, CONF_SYNC_ADJUST_RESET_MARKER, True
        )

    def _migrate_protocol_preferences(self) -> None:
        """Preserve explicit RAOP selections from the legacy protocol setting."""
        if self.mass.config.get_raw_provider_config_value(
            self.instance_id, CONF_PROTOCOL_MIGRATION_MARKER, False
        ):
            return
        for raw_conf in list(self.mass.config.get(CONF_PLAYERS, {}).values()):
            if not isinstance(raw_conf, dict) or raw_conf.get("provider") != self.instance_id:
                continue
            if not (player_id := raw_conf.get("player_id")):
                continue
            legacy_protocol = self.mass.config.get_raw_player_config_value(
                player_id, CONF_LEGACY_AIRPLAY_PROTOCOL, 0
            )
            force_raop = self.mass.config.get_raw_player_config_value(
                player_id, CONF_FORCE_RAOP, None
            )
            if legacy_protocol == StreamingProtocol.RAOP and force_raop is None:
                self.mass.config.set_raw_player_config_value(player_id, CONF_FORCE_RAOP, True)
                self.mass.config.set_raw_player_config_value(
                    player_id, CONF_LEGACY_FORCE_RAOP, True
                )
        self.mass.config.set_raw_provider_config_value(
            self.instance_id, CONF_PROTOCOL_MIGRATION_MARKER, True
        )

    async def _start_ptp_daemon(self) -> None:
        """Spawn the shared PTP clock daemon (cliairplay --ptp-daemon)."""
        try:
            cli_binary = await get_cli_binary()
        except RuntimeError as err:
            self.logger.warning(
                "cliairplay binary unavailable (%s) - "
                "PTP timing is degraded, AirPlay 2 streams fall back to NTP",
                err,
            )
            return
        args = [cli_binary, "--ptp-daemon"]
        bind_ip = str(self.mass.streams.bind_ip)
        if bind_ip not in ("0.0.0.0", "::", ""):
            args += ["--if", bind_ip]
        daemon = AsyncProcess(args, stdout=True, stderr=True, name="cliairplay-ptp-daemon")
        # (Re)gate readiness for this daemon instance: not ready until a reader
        # sees the "daemon up" line (a restart clears any previous readiness).
        if self._ptp_daemon_ready is None:
            self._ptp_daemon_ready = asyncio.Event()
        else:
            self._ptp_daemon_ready.clear()
        await daemon.start()
        self._ptp_daemon = daemon
        self._ptp_daemon_started = time.monotonic()
        daemon.attach_stderr_reader(self.mass.create_task(self._ptp_daemon_stderr_reader(daemon)))
        self._ptp_daemon_stdout_task = self.mass.create_task(self._ptp_daemon_stdout_reader(daemon))
        self.mass.create_task(self._ptp_daemon_monitor(daemon))

    async def _ptp_daemon_stderr_reader(self, daemon: AsyncProcess) -> None:
        """Forward PTP daemon stderr output to the debug log."""
        async for line in daemon.iter_stderr():
            self._handle_ptp_daemon_line(line)

    async def _ptp_daemon_stdout_reader(self, daemon: AsyncProcess) -> None:
        """Drain (and debug-log) the PTP daemon stdout pipe."""
        buffer = b""
        while chunk := await daemon.read(1024):
            buffer += chunk
            while b"\n" in buffer:
                raw_line, buffer = buffer.split(b"\n", 1)
                if line := raw_line.decode("utf-8", errors="ignore").strip():
                    self._handle_ptp_daemon_line(line)

    def _handle_ptp_daemon_line(self, line: str) -> None:
        """Debug-log a PTP daemon output line and detect its readiness signal."""
        self.logger.debug("PTP daemon: %s", line)
        # The readiness marker is matched on either pipe: the daemon's diagnostic
        # output is not contractually stdout-vs-stderr, so both readers feed this
        # handler and setting the event is idempotent.
        if (
            (event := self._ptp_daemon_ready) is not None
            and not event.is_set()
            and PTP_DAEMON_READY_MARKER in line
        ):
            self.logger.debug("Shared PTP clock daemon reported ready")
            event.set()

    async def _ptp_daemon_monitor(self, daemon: AsyncProcess) -> None:
        """Watch the PTP daemon process and restart it once if it crashes."""
        returncode = await daemon.wait()
        if self._ptp_daemon_stop_requested or self._ptp_daemon is not daemon:
            return
        self._ptp_daemon = None
        # Crash detected: drop readiness immediately so a session starting during
        # the restart window degrades consistently instead of attaching to a dead
        # clock (a restart re-sets it once the new daemon reports ready).
        if self._ptp_daemon_ready is not None:
            self._ptp_daemon_ready.clear()
        runtime = time.monotonic() - self._ptp_daemon_started
        if runtime < 5:
            # immediate exit: UDP 319/320 already taken or missing privileges
            # (root or CAP_NET_BIND_SERVICE) - streams fall back to their
            # in-process timing engine, so playback keeps working.
            self.logger.warning(
                "PTP clock daemon could not start (exit code %s) - PTP timing is degraded. "
                "Multi-room sync of native AirPlay 2 players may drift; ensure UDP ports "
                "319/320 are free and run the server as root or grant cliairplay "
                "CAP_NET_BIND_SERVICE.",
                returncode,
            )
            return
        if not self._ptp_daemon_restarted:
            self._ptp_daemon_restarted = True
            self.logger.warning(
                "PTP clock daemon stopped unexpectedly (exit code %s) - restarting", returncode
            )
            await self._start_ptp_daemon()
            return
        self.logger.warning(
            "PTP clock daemon stopped again (exit code %s) - giving up. "
            "PTP timing is degraded until the provider is reloaded.",
            returncode,
        )

    async def _handle_dacp_request(  # noqa: PLR0915
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle new connection on the socket."""
        try:
            raw_request = b""
            while recv := await reader.read(1024):
                raw_request += recv
                if len(recv) < 1024:
                    break
            if not raw_request:
                # Some device (Phorus PS10) seems to send empty request
                # Maybe as a ack message? we have nothing to do here with empty request
                # so we return early.
                return

            request = raw_request.decode("UTF-8")
            if "\r\n\r\n" in request:
                headers_raw, body = request.split("\r\n\r\n", 1)
            else:
                headers_raw = request
                body = ""
            headers_split = headers_raw.split("\r\n")
            headers = {}
            for line in headers_split[1:]:
                if ":" not in line:
                    continue
                x, y = line.split(":", 1)
                headers[x.strip()] = y.strip()
            active_remote = headers.get("Active-Remote")
            _, path, _ = headers_split[0].split(" ")
            # lookup airplay player by active remote id
            player: AirPlayPlayer | None = next(
                (
                    x
                    for x in self.get_players()
                    if x.stream and x.stream.active_remote_id == active_remote
                ),
                None,
            )
            self.logger.debug(
                "DACP request for %s (%s): %s -- %s",
                player.name if player else "UNKNOWN PLAYER",
                active_remote,
                path,
                body,
            )
            # machine-parseable capture of raw DACP traffic for building replay test fixtures
            if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
                capture = {
                    "player": player.name if player else None,
                    "active_remote": active_remote,
                    "method": headers_split[0].split(" ", 1)[0],
                    "path": path,
                    "headers": headers,
                    "body": body,
                    "raw_b64": base64.b64encode(raw_request).decode("ascii"),
                }
                self.logger.log(VERBOSE_LOG_LEVEL, "AIRPLAY_DACP_CAPTURE %s", json.dumps(capture))
            if not player:
                return
            if player.protocol_parent_id and (
                parent := self.mass.players.get_player(player.protocol_parent_id)
            ):
                parent_player = parent
            else:
                parent_player = player

            player_id = player.player_id
            ignore_volume_report = (
                self.mass.config.get_raw_player_config_value(player_id, CONF_IGNORE_VOLUME, False)
                or player.device_info.manufacturer.lower() == "apple"
            )
            if path == "/ctrl-int/1/nextitem":
                self.handle_remote_command(player, AirPlayRemoteCommand.NEXT)
            elif path == "/ctrl-int/1/previtem":
                self.handle_remote_command(player, AirPlayRemoteCommand.PREVIOUS)
            elif path == "/ctrl-int/1/play":
                self.handle_remote_command(player, AirPlayRemoteCommand.PLAY)
            elif path == "/ctrl-int/1/playpause":
                self.handle_remote_command(player, AirPlayRemoteCommand.PLAY_PAUSE)
            elif path == "/ctrl-int/1/stop":
                self.mass.create_task(self.mass.players.cmd_stop(player_id))
            elif path == "/ctrl-int/1/volumeup":
                self.mass.create_task(self.mass.players.cmd_volume_up(player_id))
            elif path == "/ctrl-int/1/volumedown":
                self.mass.create_task(self.mass.players.cmd_volume_down(player_id))
            elif path == "/ctrl-int/1/shuffle_songs":
                active_queue = self.mass.players.get_active_queue(player)
                if not active_queue:
                    return
                await self.mass.player_queues.set_shuffle(
                    active_queue.queue_id, not active_queue.shuffle_enabled
                )
            elif path == "/ctrl-int/1/pause":
                self.handle_remote_command(player, AirPlayRemoteCommand.PAUSE)
            elif path == "/ctrl-int/1/discrete-pause":
                # Some devices send discrete-pause right before device-prevent-playback=1
                # when switching to another source. We debounce the pause to avoid
                # unnecessary pause commands that would interfere with source switching
                # so we only process the pause command if we don't receive a
                # prevent-playback=1 within a short time window.
                if player.state.playback_state == PlaybackState.PLAYING:
                    self.mass.call_later(
                        1.0,
                        self.mass.players.cmd_pause,
                        player_id,
                        task_id=f"debounced_pause_{player_id}",
                    )
            elif "dmcp.device-volume=" in path and not ignore_volume_report:
                # This is a bit annoying as this can be either the device confirming a new volume
                # we've sent or the device requesting a new volume itself.
                # In case of a small rounding difference, we ignore this,
                # to prevent an endless pingpong of volume changes
                airplay_volume = float(path.split("dmcp.device-volume=", 1)[-1])
                if airplay_volume <= AIRPLAY_VOLUME_MUTE:
                    player._attr_volume_muted = True
                    if player.stream and player.stream.running:
                        self.mass.create_task(player.stream.send_cli_command("VOLUME=0"))
                    player.update_state()
                else:
                    if player.volume_muted:
                        player._attr_volume_muted = False
                        if player.stream and player.stream.running:
                            self.mass.create_task(
                                player.stream.send_cli_command(f"VOLUME={player.volume_level or 0}")
                            )
                    volume = convert_airplay_volume(airplay_volume)
                    player.update_volume_from_device(volume)
            elif "dmcp.volume=" in path:
                # volume change request from device (e.g. volume buttons)
                volume = int(path.split("dmcp.volume=", 1)[-1])
                player.update_volume_from_device(volume)
            elif "device-prevent-playback=1" in path:
                # device switched to another source (or is powered off)
                # Cancel any pending debounced pause since prevent-playback takes precedence
                self.mass.cancel_timer(f"debounced_pause_{player_id}")
                # Ignore during stream transition (stale message from old CLI process)
                if player._transitioning or not player.stream:
                    self.logger.debug("Ignoring prevent-playback during stream transition")
                elif player.stream.prevent_playback:
                    # Already handling a prevent-playback for this stream
                    # (duplicate message while ungroup/stop is still in progress)
                    self.logger.debug("Ignoring duplicate prevent-playback for %s", player.name)
                elif not player.stream.connected:
                    # Some devices (e.g. Denon AVR-X2700H) emit a transient
                    # prevent-playback=1/=0 pair during RAOP session setup.
                    # A real "device switched off / source switched" event only happens
                    # once the stream is actually established, so ignore these.
                    self.logger.debug(
                        "Ignoring prevent-playback for %s - stream not yet established",
                        player.name,
                    )
                else:
                    player.stream.prevent_playback = True
                    if player.stream.session:
                        self.logger.debug(
                            "Prevent playback command detected for player %s",
                            player.name,
                        )
                        if player.synced_to or parent_player.state.active_group:
                            self.mass.create_task(
                                self.mass.players.cmd_ungroup(parent_player.player_id)
                            )
                        else:
                            self.mass.create_task(player.stream.stop())
            elif "device-prevent-playback=0" in path:
                # device reports that its ready for playback again
                # use a debounced reset to avoid race conditions where a quick
                # prevent-playback=0 between duplicate prevent-playback=1 messages
                # would reset the flag and allow the second message to act
                if (stream := player.stream) and stream.prevent_playback:
                    self.mass.call_later(
                        5,
                        setattr,
                        stream,
                        "prevent_playback",
                        False,
                        task_id=f"reset_prevent_playback_{player_id}",
                    )

            # send response
            date_str = utc().strftime("%a, %-d %b %Y %H:%M:%S")
            response = (
                f"HTTP/1.0 204 No Content\r\nDate: {date_str} "
                "GMT\r\nDAAP-Server: iTunes/7.6.2 (Windows; N;)\r\nContent-Type: "
                "application/x-dmap-tagged\r\nContent-Length: 0\r\n"
                "Connection: close\r\n\r\n"
            )
            writer.write(response.encode())
            await writer.drain()
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
