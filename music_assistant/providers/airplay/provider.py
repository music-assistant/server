"""AirPlay Player provider for Music Assistant."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from contextlib import suppress
from dataclasses import replace
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import TYPE_CHECKING, Final, cast

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, PlaybackState, PlayerType
from music_assistant_models.errors import MediaNotFoundError
from zeroconf import NonUniqueNameException, ServiceStateChange
from zeroconf.asyncio import AsyncServiceInfo

from music_assistant.constants import (
    CONF_LOG_LEVEL,
    CONF_PLAYER_PROTOCOLS,
    CONF_PLAYERS,
    CONF_PROVIDERS,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.helpers.config_entries import (
    CONF_CONNECTED_PLAYERS,
    CONF_PUBLISH_NAME_TEMPLATE,
    resolve_publish_name,
)
from music_assistant.helpers.datetime import utc
from music_assistant.helpers.json import SerializableType
from music_assistant.helpers.process import AsyncProcess
from music_assistant.helpers.util import (
    get_ip_addresses,
    get_ip_pton,
    get_primary_ip_address_from_zeroconf,
    select_free_port,
)
from music_assistant.models.player_provider import PlayerProvider

from .constants import (
    AIRPLAY_DISCOVERY_TYPE,
    AIRPLAY_VOLUME_MUTE,
    CLI_PROBLEM_MARKERS,
    COMPANION_DISCOVERY_TYPE,
    CONF_COMPAT_PINS_REVIEWED,
    CONF_PASSWORD_INVALID,
    CONF_PASSWORD_MARKERS_REVIEWED,
    CONF_STORED_VOLUME,
    CONF_STREAMING_MODE,
    CONF_VERBOSE_PTP_LOGGING,
    DACP_DISCOVERY_TYPE,
    EXTERNAL_ARTWORK_PATH_PREFIX,
    FALLBACK_VOLUME,
    MRP_DISCOVERY_TYPE,
    PTP_DAEMON_WARN_BURST,
    PTP_DAEMON_WARN_WINDOW,
    RAOP_DISCOVERY_TYPE,
    STREAMING_MODE_AP2_COMPAT,
    STREAMING_MODE_AUTO,
    AirPlayRemoteCommand,
    StreamingProtocol,
)
from .control_player import AirPlayControlPlayer
from .dashboard import AirPlayDashboards
from .helpers import (
    SetKind,
    SetMembership,
    cli_clock_args,
    convert_airplay_volume,
    get_cli_binary,
    get_model_info,
    get_protocol_id,
    is_airplay_record_of,
    is_apple_device,
    parse_set_membership,
    probe_audio_formats,
)
from .player import AirPlayPlayer, GenericAirPlayPlayer
from .sendspin_bridge import SendspinBridgeManager
from .speaker import AirPlaySpeaker

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant_models.config_entries import ProviderConfig

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
# Grace period after broadcasting a goodbye for a stale DACP registration before
# re-registering the (name-stable) service, letting the cache flush the old record.
DACP_RECLAIM_DELAY: Final[float] = 1.0
# Opt-in for pyatv's own debug logging. Set to any non-empty value to trace the
# pyatv protocol traffic itself.
ENV_PYATV_DEBUG: Final[str] = "MASS_PYATV_DEBUG"


class AirPlayProvider(PlayerProvider):
    """Player provider for AirPlay based players."""

    reload_on_streams_network_change = True
    _dacp_server: asyncio.Server
    _dacp_info: AsyncServiceInfo
    _bridge_manager: SendspinBridgeManager
    dashboards: AirPlayDashboards
    _ptp_daemon: AsyncProcess | None = None
    _ptp_daemon_stdout_task: asyncio.Task[None] | None = None
    _ptp_daemon_started: float = 0.0
    _ptp_daemon_restarted: bool = False
    _ptp_daemon_stop_requested: bool = False
    # Set once the running daemon reports it has bound 319/320 and opened its
    # control channel; created/cleared per daemon start so a crash+restart
    # re-gates readiness. None until the daemon is first started.
    _ptp_daemon_ready: asyncio.Event | None = None
    # Rate-limit state for daemon lines promoted to WARNING (see
    # PTP_DAEMON_WARN_BURST).
    _ptp_daemon_warn_window_start: float | None = None
    _ptp_daemon_warns_in_window: int = 0
    _ptp_daemon_warns_suppressed: int = 0
    _unsub_timeline_rate: Callable[[], None] | None = None
    _ptp_daemon_restart_task: asyncio.Task[None] | None = None

    @property
    def bridge_manager(self) -> SendspinBridgeManager:
        """Return the Sendspin bridge manager."""
        return self._bridge_manager

    @property
    def ptp_daemon_running(self) -> bool:
        """
        Return if the shared PTP clock daemon process is alive.

        This reflects process liveness only (spawned, not closed, still
        running). It says nothing about whether streams can use it: a daemon
        that never bound its ports stays alive and reports True here while every
        group silently degrades to NTP. Use :attr:`ptp_daemon_ready` for the
        real state, or :meth:`wait_ptp_daemon_ready` to wait for it.
        """
        return (
            self._ptp_daemon is not None
            and not self._ptp_daemon.closed
            and self._ptp_daemon.returncode is None
        )

    @property
    def ptp_daemon_ready(self) -> bool:
        """
        Return whether the shared PTP clock daemon is serving streams right now.

        This is the condition a session actually gates on: the daemon has bound
        the privileged PTP ports (UDP 319/320), opened its control channel, and
        is still alive.
        """
        return (
            self.ptp_daemon_running
            and self._ptp_daemon_ready is not None
            and self._ptp_daemon_ready.is_set()
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
        # a speaker of a set controls its set
        if (owner := player.owner) is None:
            return
        player = owner
        player_id = self.bridge_manager.get_transport_command_target(player.protocol_id) or (
            player.player.player_id if player.player else None
        )
        if player_id is None:
            return
        # Some receivers echo play (or play/pause) as confirmation of a command from MA,
        # also while MA's own stream to them is still starting.
        stream = player.stream
        stream_starting = stream is not None and stream.running and stream.start_pending
        match command:
            case AirPlayRemoteCommand.PLAY:
                if player.playback_state != PlaybackState.PLAYING and not stream_starting:
                    self.mass.create_task(self.mass.players.cmd_play(player_id))
            case AirPlayRemoteCommand.PAUSE:
                if player.playback_state == PlaybackState.PLAYING:
                    self.mass.create_task(self.mass.players.cmd_pause(player_id))
            case AirPlayRemoteCommand.PLAY_PAUSE:
                if not stream_starting:
                    self.mass.create_task(self.mass.players.cmd_play_pause(player_id))
            case AirPlayRemoteCommand.NEXT:
                self.mass.create_task(self.mass.players.cmd_next_track(player_id))
            case AirPlayRemoteCommand.PREVIOUS:
                self.mass.create_task(self.mass.players.cmd_previous_track(player_id))

    def get_set_carrier(self, set_id: str) -> AirPlayPlayer | None:
        """
        Return the protocol that carries a home theater set, None while it is not there.

        :param set_id: Id of the set.
        """
        return next(
            (
                player
                for player in self.get_players()
                if (membership := player.set_membership)
                and membership.set_id == set_id
                and membership.position == 0
            ),
            None,
        )

    def get_set_speakers(self, set_id: str) -> list[AirPlaySpeaker]:
        """
        Return the speakers of a set that are on the network or still play for it, by position.

        :param set_id: Id of the set.
        """
        speakers = [
            (membership.position, speaker)
            for speaker in self._speakers.values()
            if (speaker.available or (speaker.stream is not None and speaker.stream.running))
            and (membership := speaker.set_membership)
            and membership.set_id == set_id
            and membership.position is not None
        ]
        return [speaker for _, speaker in sorted(speakers, key=lambda item: item[0])]

    def get_pair_position(self, protocol_id: str, pair_id: str) -> int | None:
        """
        Return the place of a HomePod in its stereo pair: 0 for the one that carries the pair.

        None while it is not decided which HomePod carries the pair.

        :param protocol_id: Protocol id of the HomePod.
        :param pair_id: Id of the pair.
        """
        if (carrier := self._pair_carriers.get(pair_id)) is None:
            return None
        return 0 if carrier == protocol_id else 1

    def has_stored_room(self, protocol_id: str) -> bool:
        """
        Return whether the stored player of a device is a multi-channel player of a set it carries.

        :param protocol_id: Protocol id of the device.
        """
        record = self.mass.config.get_protocol_record(protocol_id) or {}
        if not (player_id := record.get("player_id")):
            return False
        return bool(
            self.mass.config.get(f"{CONF_PLAYERS}/{player_id}/player_type")
            == PlayerType.MULTI_CHANNEL
        )

    async def release_set_speakers(self, set_id: str) -> None:
        """
        Give the speakers of a set back to discovery, as players of their own.

        :param set_id: Id of the set, whose player the user disabled.
        """
        released = [
            speaker
            for speaker in self._speakers.values()
            if (membership := speaker.set_membership) and membership.set_id == set_id
        ]
        # all out before the first await, so a record that arrives meanwhile finds none of them
        for speaker in released:
            del self._speakers[speaker.protocol_id]
        for speaker in released:
            if (
                speaker.available
                and speaker.airplay_discovery_info
                and not self.get_protocol(speaker.protocol_id)
            ):
                await self._setup_player(
                    speaker.protocol_id, speaker.display_name, speaker.airplay_discovery_info
                )

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        return (
            ConfigEntry(
                key=CONF_VERBOSE_PTP_LOGGING,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                required=False,
                advanced=True,
            ),
        )

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._set_pyatv_log_level()
        self._drop_unverified_password_markers()
        self._reset_auto_pinned_compat_modes()
        self._companion_info_by_address: dict[str, AsyncServiceInfo] = {}
        self._mrp_info_by_address: dict[str, AsyncServiceInfo] = {}
        # The speakers of the home theater sets of Apple TVs, by their own protocol id.
        # They are never registered: each plays for the protocol of its set.
        self._speakers: dict[str, AirPlaySpeaker] = {}
        # Protocol id of the device that carries each set seen, also of a disabled one
        self._set_carriers: dict[str, str] = {}
        # The last record of each HomePod of a stereo pair, by pair id, and which HomePod carries
        # each pair: decided once and kept, as the records tell no roles.
        self._pair_records: dict[str, dict[str, AsyncServiceInfo]] = {}
        self._pair_carriers: dict[str, str] = {}
        # Shared audible instants for in-flight announcements, keyed by
        # (group player id, render key) -> unix ms. The controller
        # forwards a group-entity announcement to every member concurrently;
        # each call arms its own member and reuses the instant the first call
        # planned, so all rooms render the clip in sync (managed by
        # announce.py, pruned as plans pass).
        self._announce_plans: dict[tuple[str, str], int] = {}

        # Initialize Sendspin bridge manager for protocol linking
        self._bridge_manager = SendspinBridgeManager(self)

        # Registers eligible Apple TVs as dashboard endpoints for the tvOS app
        self.dashboards = AirPlayDashboards(self)

        # register DACP zeroconf service
        dacp_port = await select_free_port(39831, 49831)
        # Use first 16 hex chars of server_id as a persistent DACP ID
        # This ensures the DACP ID remains the same across restarts, which is required
        # for AirPlay 2 (HAP) pair-verify to work with previously paired devices
        self.dacp_id = dacp_id = self.mass.server_id[:16].upper()
        self.logger.debug("Starting DACP ActiveRemote %s on port %s", dacp_id, dacp_port)
        self._dacp_server = await asyncio.start_server(self._handle_dacp_request, port=dacp_port)
        self._dacp_info = await self._create_dacp_service_info(dacp_port)
        await self._register_dacp_service()

        # Run one shared PTP clock daemon for the provider lifetime: all native
        # AirPlay 2 streams attach to it (--ptp-shared) so multi-room sync groups
        # lock to a single grandmaster while UDP 319/320 is bound only once.
        await self._start_ptp_daemon()
        # The daemon is restarted on a new rate of the timeline; a stream cannot
        # follow one, so the rate waits until none runs.
        self._unsub_timeline_rate = self.mass.timeline_clock.add_rate_listener(
            self._on_timeline_rate_changed, pinned=self._has_running_streams
        )

    async def update_config(self, config: ProviderConfig, changed_keys: set[str]) -> None:
        """Handle logic when the config is updated."""
        await super().update_config(config, changed_keys)
        # a log level(-only) change does not reload the provider,
        # so realign pyatv's logger here
        if f"values/{CONF_LOG_LEVEL}" in changed_keys:
            self._set_pyatv_log_level()

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
            elif state_change == ServiceStateChange.Removed:
                await self._handle_airplay_goodbye(name)
                return
            else:
                # If we are not in a 'Removed' state, we need info to be filled to update the player
                return
        elif "@" in info.name:
            raw_id, display_name = info.name.split(".")[0].split("@", 1)
        elif deviceid := info.decoded_properties.get("deviceid"):
            raw_id = deviceid
            display_name = info.name.split(".")[0]
        else:
            return
        protocol_id = get_protocol_id(raw_id)
        if pair_id := self._note_pair_record(protocol_id, info):
            await self._replay_pair_records(pair_id, protocol_id)
        if (membership := self._membership_of(protocol_id, info)) and membership.position == 0:
            self._set_carriers[membership.set_id] = protocol_id
            if not self.mass.players.wants_protocol(protocol_id):
                # the user disabled the player of this set: its speakers play on their own
                await self.release_set_speakers(membership.set_id)
        if (speaker := self._speakers.get(protocol_id)) and await self._update_speaker(
            speaker, display_name, state_change, info
        ):
            return
        # handle removed player
        if state_change == ServiceStateChange.Removed:
            if _player := self.get_protocol(protocol_id):
                # the player has become unavailable
                self.logger.debug("Player offline: %s", _player.display_name)
                # Remove the Sendspin bridge first
                await self._bridge_manager.remove_bridge(protocol_id)
                await self.mass.players.unregister(protocol_id)
            self.dashboards.unregister(protocol_id)
            return
        # handle update for existing device
        assert info is not None  # type guard
        player: AirPlayPlayer | None
        if player := cast("AirPlayPlayer | None", self.get_protocol(protocol_id)):
            if self._is_speaker_record(protocol_id, info):
                # a device that joined a set is a speaker of that set from now on
                await self._bridge_manager.remove_bridge(protocol_id)
                await self.mass.players.unregister(protocol_id)
                await self._setup_player(protocol_id, display_name, info)
                return
            # update the latest discovery info for existing player
            player.set_discovery_info(info, display_name)
            # only control players can ever be dashboard endpoints
            if isinstance(player, AirPlayControlPlayer):
                self.dashboards.reconcile(protocol_id)
            # the record can give the device a set, take it away or change it
            self._bridge_manager.update_endpoints(protocol_id)
            player.report_speakers()
            return
        await self._setup_player(protocol_id, display_name, info)

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        # Unregister all dashboard endpoints
        dashboards = getattr(self, "dashboards", None)
        if dashboards:
            await dashboards.unload()
        # Stop all Sendspin bridges
        bridge_manager = getattr(self, "_bridge_manager", None)
        if bridge_manager:
            await bridge_manager.close()
        if self._unsub_timeline_rate is not None:
            self._unsub_timeline_rate()
            self._unsub_timeline_rate = None
        # terminate the shared PTP clock daemon
        self._ptp_daemon_stop_requested = True
        if self._ptp_daemon_ready is not None:
            self._ptp_daemon_ready.clear()
        if (restart_task := self._ptp_daemon_restart_task) is not None:
            # it stops at the flag, and closes a daemon it spawned meanwhile
            await asyncio.wait([restart_task])
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
        streams_by_route: dict[str, int] = {}
        for player in [*self.get_players(), *self._speakers.values()]:
            if not (player.stream and player.stream.running):
                continue
            stream_type = "airplay2" if player.protocol == StreamingProtocol.AIRPLAY2 else "raop"
            streams_by_type[stream_type] = streams_by_type.get(stream_type, 0) + 1
            # The route the binary resolved names the timing source each stream
            # really got (PTP or NTP), which the daemon flags cannot: a daemon
            # can be alive and every stream still be running on NTP. A process
            # that has not reported its route yet is counted apart rather than
            # folded into either.
            route = player.stream.active_route or "unreported"
            streams_by_route[route] = streams_by_route.get(route, 0) + 1
        return {
            "dacp_server_running": self._dacp_server.is_serving(),
            "ptp_daemon_running": self.ptp_daemon_running,
            "ptp_daemon_ready": self.ptp_daemon_ready,
            "active_streams": sum(streams_by_type.values()),
            "streams_by_type": streams_by_type,
            "streams_by_route": streams_by_route,
        }

    def get_players(self) -> list[AirPlayPlayer]:
        """Return the AirPlay protocols of this instance that are attached or wait for a player."""
        return cast("list[AirPlayPlayer]", self.protocols)

    def get_player(self, protocol_id: str) -> AirPlayPlayer | None:
        """Return AirplayPlayer by its protocol id."""
        return cast("AirPlayPlayer | None", self.get_protocol(protocol_id))

    async def resolve_image(self, path: str) -> bytes:
        """
        Resolve artwork for the current external media on an Apple device.

        :param path: AirPlay artwork path produced for the image proxy.
        :return: Raw artwork bytes.
        :raises MediaNotFoundError: If the artwork is invalid, stale, or unavailable.
        """
        try:
            prefix, protocol_id, artwork_id = path.split("/", 2)
        except ValueError as err:
            raise MediaNotFoundError("Invalid AirPlay artwork path") from err
        player = self.get_player(protocol_id)
        if (
            prefix != EXTERNAL_ARTWORK_PATH_PREFIX
            or not artwork_id
            or not isinstance(player, AirPlayControlPlayer)
        ):
            raise MediaNotFoundError("AirPlay artwork is unavailable")
        return await player.async_get_external_artwork(artwork_id)

    def _set_pyatv_log_level(self) -> None:
        """Keep pyatv's (very chatty) logging quiet unless it is explicitly asked for."""
        # pyatv logs every protocol message, HTTP exchange and encrypted payload of
        # each control connection at debug level, which buries our own logging and
        # rotates the log file within minutes. Its debug output is therefore held
        # back even on verbose sessions, which are meant to surface our own deep
        # diagnostics (the cliairplay [STATUS] and PTP traces) rather than pyatv's.
        if os.environ.get(ENV_PYATV_DEBUG):
            logging.getLogger("pyatv").setLevel(logging.DEBUG)
        else:
            logging.getLogger("pyatv").setLevel(max(self.logger.level + 10, logging.INFO))

    async def _setup_player(  # noqa: PLR0915
        self, protocol_id: str, display_name: str, discovery_info: AsyncServiceInfo
    ) -> None:
        """Handle setup of a new player that is discovered using mdns."""
        # return early if the user disabled the player or switched this output off; a
        # speaker of a set is no player of its own, also while its old player is disabled
        if not self._is_speaker_record(
            protocol_id, discovery_info
        ) and not self.mass.players.wants_protocol(protocol_id):
            self.logger.debug("Ignoring %s in discovery as it is disabled.", display_name)
            return
        # Filter out this server's own AirPlay Receiver (shairport-sync) instances
        # before anything else: they must never register as AirPlay players.
        if await self._is_own_airplay_receiver(display_name, discovery_info):
            self.logger.debug(
                "Ignoring %s in discovery: it is an AirPlay Receiver instance of this server",
                display_name,
            )
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
            # the name can belong to another device: mDNS renames the instance of a device
            # that advertises a name already taken, but not the name in its RAOP record
            if airplay_discovery_info is None or not is_airplay_record_of(
                airplay_discovery_info, protocol_id
            ):
                airplay_discovery_info = await self._find_cached_service(
                    AIRPLAY_DISCOVERY_TYPE, lambda info: is_airplay_record_of(info, protocol_id)
                )
        else:
            # AirPlay service discovered - try to also find the RAOP service
            self.logger.debug("Discovered AirPlay service for %s", display_name)
            airplay_discovery_info = discovery_info
            raop_discovery_info = await self.mass.discovery.async_find_mdns_service(
                RAOP_DISCOVERY_TYPE, display_name, timeout=10.0
            )

        # a lookup that found no record is no goodbye of the records of the device
        if airplay_discovery_info and (
            pair_id := self._note_pair_record(protocol_id, airplay_discovery_info)
        ):
            await self._replay_pair_records(pair_id, protocol_id)
        if (
            membership := self._membership_of(protocol_id, airplay_discovery_info)
        ) and membership.position is None:
            # a HomePod of a stereo pair waits until it is decided which one carries the pair
            self.logger.debug(
                "Ignoring %s until the other HomePod of its pair is seen", display_name
            )
            return
        if airplay_discovery_info:
            model_discovery_info = airplay_discovery_info
        elif raop_discovery_info:
            model_discovery_info = raop_discovery_info
        else:
            return  # should not happen, but guard just in case
        manufacturer, model = get_model_info(model_discovery_info)
        # shairport-sync's nqptp on this host holds the PTP ports MA's own PTP engine needs
        is_local_shairport = model == "ShairportSync" and await self._is_advertised_by_this_host(
            model_discovery_info
        )

        prefer_ipv6 = ":" in str(self.mass.streams.publish_ip)
        address = get_primary_ip_address_from_zeroconf(discovery_info, prefer_ipv6=prefer_ipv6)
        if not address:
            return  # should not happen, but guard just in case

        # if we reach this point, all preflights are ok and we can create the player
        self.logger.debug("Discovered AirPlay device %s on %s", display_name, address)

        if self._is_speaker_record(protocol_id, airplay_discovery_info):
            self._add_speaker(
                AirPlaySpeaker(
                    provider=self,
                    protocol_id=protocol_id,
                    raop_discovery_info=raop_discovery_info,
                    airplay_discovery_info=airplay_discovery_info,
                    address=address,
                    display_name=display_name,
                    manufacturer=manufacturer,
                    model=model,
                )
            )
            return

        # Get stored volume from playerconfig
        volume = int(
            self.mass.config.get_protocol_value(protocol_id, CONF_STORED_VOLUME, FALLBACK_VOLUME)
        )

        # Final check before registration to handle race conditions
        # (multiple MDNS events processed in parallel for same device)
        if self.get_protocol(protocol_id):
            self.logger.debug(
                "Player %s already attached during setup, skipping register", protocol_id
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
                protocol_id=protocol_id,
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
                protocol_id=protocol_id,
                raop_discovery_info=raop_discovery_info,
                airplay_discovery_info=airplay_discovery_info,
                address=address,
                display_name=display_name,
                manufacturer=manufacturer,
                model=model,
                initial_volume=volume,
                is_local_shairport=is_local_shairport,
            )
        await self.mass.players.register(player)
        if self.get_protocol(protocol_id) is not player:
            # not attached: the user disabled the player (e.g. a Mac, disabled by default)
            return

        # A receiver only publishes its audio formats (and so whether it can do
        # 24-bit) in its /info response, never in its mDNS records, so ask it
        # directly. Off the discovery path: mdns callbacks are serialized per
        # provider, so an unreachable device must not hold up the next player.
        if airplay_discovery_info and airplay_discovery_info.port:
            self.mass.create_task(
                self._learn_audio_formats(player, address, airplay_discovery_info.port)
            )

        # Set up Sendspin bridge for protocol linking (if Sendspin provider is available)
        await self._bridge_manager.evaluate_bridge(player)

        # Track control players (Apple TVs) for dashboard eligibility
        if isinstance(player, AirPlayControlPlayer):
            self.dashboards.setup_player(player)

    def _is_speaker_record(self, protocol_id: str, discovery_info: AsyncServiceInfo | None) -> bool:
        """
        Return whether a record names its device a speaker of a set that plays as one player.

        The speakers of a set whose own player the user disabled stay players of their own.

        :param protocol_id: Protocol id of the device.
        :param discovery_info: The mdns record of the device.
        """
        membership = self._membership_of(protocol_id, discovery_info)
        if membership is None or membership.position in (None, 0):
            return False
        carrier_id = self._set_carriers.get(membership.set_id)
        return carrier_id is None or self.mass.players.wants_protocol(carrier_id)

    async def _update_speaker(
        self,
        speaker: AirPlaySpeaker,
        display_name: str,
        state_change: ServiceStateChange,
        info: AsyncServiceInfo | None,
    ) -> bool:
        """
        Apply an mdns record of a known speaker; False when it is no speaker anymore.

        :param speaker: The speaker the record is for.
        :param display_name: The advertised device name.
        :param state_change: What happened to the record.
        :param info: The record, None for a removed one.
        """
        if state_change == ServiceStateChange.Removed or info is None:
            # Only a fresh _airplay._tcp record says again that it is a speaker of its set.
            # A stream it still plays is left alone: one that ends stops its set with it.
            speaker.set_available(False)
        elif info.type != AIRPLAY_DISCOVERY_TYPE:
            speaker.set_discovery_info(info, display_name)
        elif not self._is_speaker_record(speaker.protocol_id, info):
            # it left its set: normal discovery finds it as a player of its own
            del self._speakers[speaker.protocol_id]
            await self._leave_session(speaker, "speaker left its set")
            self._on_set_speakers_changed(speaker)
            return False
        else:
            returned = not speaker.available
            speaker.set_discovery_info(info, display_name)
            speaker.set_available(True)
            if returned:
                self._rejoin_set_session(speaker)
        self._on_set_speakers_changed(speaker)
        return True

    def _add_speaker(self, speaker: AirPlaySpeaker) -> None:
        """
        Keep a newly found speaker of a set, which plays for its set from now on.

        :param speaker: The speaker to keep.
        """
        self._speakers[speaker.protocol_id] = speaker
        self.logger.debug("%s is a speaker of a home theater set", speaker.display_name)
        self._on_set_speakers_changed(speaker)
        self._rejoin_set_session(speaker)

    def _find_by_airplay_name(self, name: str) -> AirPlayPlayer | None:
        """
        Return the protocol or speaker whose _airplay._tcp record has the given service name.

        :param name: The full service name of the record.
        """
        return next(
            (
                player
                for player in [*self.get_players(), *self._speakers.values()]
                if player.airplay_discovery_info and player.airplay_discovery_info.name == name
            ),
            None,
        )

    def _membership_of(
        self, protocol_id: str, discovery_info: AsyncServiceInfo | None
    ) -> SetMembership | None:
        """
        Return the place of a device in a set; for a stereo pair, the place decided for it.

        :param protocol_id: Protocol id of the device.
        :param discovery_info: The mdns record of the device.
        """
        membership = parse_set_membership(discovery_info)
        if membership is not None and membership.kind == SetKind.STEREO_PAIR:
            return replace(
                membership, position=self.get_pair_position(protocol_id, membership.set_id)
            )
        return membership

    def _note_pair_record(
        self, protocol_id: str, discovery_info: AsyncServiceInfo | None
    ) -> str | None:
        """
        Keep what a record of a HomePod tells about its stereo pair; decide who carries it.

        The HomePod whose player is the room of the pair keeps carrying it, and so does the only
        one of the two with a stored player: the player of the other is removed once the pair
        formed. Without either, the HomePod with the lowest protocol id carries it, once both
        records are in. Return the id of the pair when that was decided just now.

        :param protocol_id: Protocol id of the device.
        :param discovery_info: The mdns record of the device.
        """
        if discovery_info is None:
            # a device that said goodbye: its records are no evidence of a pair any more
            for records in self._pair_records.values():
                records.pop(protocol_id, None)
            return None
        if discovery_info.type != AIRPLAY_DISCOVERY_TYPE:
            return None
        membership = parse_set_membership(discovery_info)
        pair_id = (
            membership.set_id
            if membership is not None and membership.kind == SetKind.STEREO_PAIR
            else None
        )
        for other_id, records in self._pair_records.items():
            if other_id != pair_id and records.pop(protocol_id, None) is not None:
                # a HomePod that left its pair carries it no more
                if self._pair_carriers.get(other_id) == protocol_id:
                    del self._pair_carriers[other_id]
        if pair_id is None:
            return None
        records = self._pair_records.setdefault(pair_id, {})
        records[protocol_id] = discovery_info
        if pair_id in self._pair_carriers:
            return None
        players = [device_id for device_id in records if self._has_stored_player(device_id)]
        if rooms := sorted(device_id for device_id in records if self.has_stored_room(device_id)):
            carrier = rooms[0]
        elif len(records) == 2 and len(players) == 1:
            # a room formed while the server ran is stored as the player it was until it attaches
            carrier = players[0]
        elif len(records) == 2:
            # the records tell no roles; the lowest id makes the same choice at every start
            carrier = min(records)
        else:
            return None
        self._pair_carriers[pair_id] = carrier
        return pair_id

    def _has_stored_player(self, protocol_id: str) -> bool:
        """
        Return whether a device has a stored player.

        :param protocol_id: Protocol id of the device.
        """
        record = self.mass.config.get_protocol_record(protocol_id) or {}
        return bool(
            (player_id := record.get("player_id"))
            and self.mass.config.get(f"{CONF_PLAYERS}/{player_id}")
        )

    async def _handle_airplay_goodbye(self, name: str) -> None:
        """
        Handle the goodbye of an _airplay._tcp record, which comes without the record.

        The record that names the set of a device is gone; its other records keep the device on
        the network, but they name no set.

        :param name: The service name of the record.
        """
        for records in self._pair_records.values():
            for device_id, info in list(records.items()):
                if info.name == name:
                    del records[device_id]
        if (gone := self._find_by_airplay_name(name)) is None:
            return
        if isinstance(gone, AirPlaySpeaker):
            await self._update_speaker(gone, gone.display_name, ServiceStateChange.Removed, None)
        else:
            gone.set_airplay_record_gone()

    async def _replay_pair_records(self, pair_id: str, protocol_id: str) -> None:
        """
        Apply the last records of the other HomePods of a pair once who carries it was decided.

        :param pair_id: Id of the pair.
        :param protocol_id: Protocol id of the HomePod whose record is being applied.
        """
        for other_id, info in list(self._pair_records.get(pair_id, {}).items()):
            if other_id != protocol_id:
                await self.on_mdns_service_state_change(info.name, ServiceStateChange.Updated, info)

    def _rejoin_set_session(self, speaker: AirPlaySpeaker) -> None:
        """
        Add a speaker that is back or new to the session its set plays in without it.

        :param speaker: The speaker that is back or new.
        """
        if (
            (airplay_set := speaker.owner)
            and (session := airplay_set.session)
            and speaker not in session.sync_clients
            and any(endpoint in session.sync_clients for endpoint in airplay_set.endpoints)
        ):
            self.mass.create_task(session.add_client(speaker, returning=True))

    def _on_set_speakers_changed(self, speaker: AirPlaySpeaker) -> None:
        """
        Update the set of a speaker that came, went or changed.

        :param speaker: The speaker that changed.
        """
        if (airplay_set := speaker.owner) is None:
            return
        # whether the set needs a password depends on its speakers
        airplay_set.update_state()
        airplay_set.report_speakers()
        if isinstance(airplay_set, AirPlayControlPlayer):
            airplay_set.on_speakers_changed()
        self._bridge_manager.update_endpoints(airplay_set.protocol_id)

    @staticmethod
    async def _leave_session(speaker: AirPlaySpeaker, reason: str) -> None:
        """
        Take a speaker out of the stream session it plays in, as a detached player leaves its.

        :param speaker: The speaker that goes.
        :param reason: Short reason for the teardown logs.
        """
        if session := speaker.session:
            await session.remove_client(speaker, reason=reason)
        speaker.stream = None

    async def _learn_audio_formats(self, player: AirPlayPlayer, host: str, port: int) -> None:
        """Read the audio formats a receiver advertises, so 24-bit can be auto-enabled."""
        if formats := await probe_audio_formats(self.mass, host, port):
            player.advertised_audio_formats = formats

    async def _is_own_airplay_receiver(
        self, display_name: str, discovery_info: AsyncServiceInfo
    ) -> bool:
        """
        Return whether a discovered service is an AirPlay Receiver instance of this server.

        :param display_name: The advertised device name (mdns name without any id prefix).
        :param discovery_info: The mdns service info that triggered the discovery.
        """
        from music_assistant.providers.airplay_receiver import (  # noqa: PLC0415
            airplay_receiver_ports,
        )

        # Collect the advertised names and ports of all configured AirPlay Receiver
        # instances from raw config storage: this works regardless of provider load
        # order at boot, when the receiver instances may not be running yet.
        receiver_names: set[str] = set()
        receiver_ports: set[int] = set()
        for instance_id, raw_conf in self.mass.config.get(CONF_PROVIDERS, {}).items():
            if not isinstance(raw_conf, dict) or raw_conf.get("domain") != "airplay_receiver":
                continue
            if not raw_conf.get("enabled", True):
                continue
            values = raw_conf.get("values")
            values = values if isinstance(values, dict) else {}
            player_ids = [str(player_id) for player_id in values.get(CONF_CONNECTED_PLAYERS) or []]
            receiver_ports.update(airplay_receiver_ports(str(instance_id), player_ids).values())
            template = values.get(CONF_PUBLISH_NAME_TEMPLATE)
            for player_id in player_ids:
                # best effort: a registered player's live name, else its stored config
                # name; when neither resolves the port match stays the strong signal
                if player := self.mass.players.get_player(player_id):
                    player_name: str | None = player.name
                else:
                    stored_name = self.mass.config.get_player_value(
                        player_id, "name"
                    ) or self.mass.config.get_player_value(player_id, "default_name")
                    player_name = str(stored_name) if stored_name else None
                if player_name:
                    receiver_names.add(resolve_publish_name(template, player_name))
        # running instances are authoritative for the actual daemon ports
        for prov in self.mass.get_provider_instances("airplay_receiver"):
            if ports := getattr(prov, "airplay_ports", None):
                receiver_ports.update(ports)
        if not receiver_names and not receiver_ports:
            return False

        if not await self._is_advertised_by_this_host(discovery_info):
            return False

        # Match by advertised name (robust even when the TXT record did not resolve).
        if display_name in receiver_names:
            return True
        # Match by port, gated on the shairport-sync model so user-run AirPlay
        # receivers on this machine (e.g. the macOS built-in receiver, which also
        # listens on port 7000) remain usable as players when their name differs.
        _, model = get_model_info(discovery_info)
        return model == "ShairportSync" and discovery_info.port in receiver_ports

    async def _is_advertised_by_this_host(self, discovery_info: AsyncServiceInfo) -> bool:
        """Return whether an mdns advertisement originates from this host."""
        # shairport-sync's embedded mDNS responder announces address records for
        # every host interface and which one resolves (first) is undefined, so match
        # the full advertised address set against all of this host's addresses
        # instead of a single picked address.
        advertised_ips: set[IPv4Address | IPv6Address] = set()
        for value in discovery_info.parsed_addresses():
            with suppress(ValueError):
                advertised_ips.add(ip_address(value))
        if not advertised_ips:
            return False
        if any(advertised_ip.is_loopback for advertised_ip in advertised_ips):
            return True
        host_ips: set[IPv4Address | IPv6Address] = set()
        for value in await get_ip_addresses(include_ipv6=True):
            with suppress(ValueError):
                host_ips.add(ip_address(value))
        return not advertised_ips.isdisjoint(host_ips)

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
        return await self._find_cached_service(
            service_type,
            lambda info: not player_addresses.isdisjoint(info.parsed_addresses()),
        )

    async def _find_cached_service(
        self,
        service_type: str,
        matches: Callable[[AsyncServiceInfo], bool],
    ) -> AsyncServiceInfo | None:
        """Find a cached mDNS service of the given type that the predicate accepts."""
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
            if matches(discovery_info):
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

    async def _create_dacp_service_info(self, port: int) -> AsyncServiceInfo:
        """
        Build the DACP ActiveRemote mDNS service, reachable on the publish IP only.

        :param port: The port the DACP server listens on.
        """
        return AsyncServiceInfo(
            DACP_DISCOVERY_TYPE,
            name=f"iTunes_Ctrl_{self.dacp_id}.{DACP_DISCOVERY_TYPE}",
            addresses=[await get_ip_pton(self.mass.streams.publish_ip)],
            port=port,
            properties={
                "txtvers": "1",
                "Ver": "63B5E5C0C201542E",
                "DbId": "63B5E5C0C201542E",
                "OSsi": "0x1F5",
            },
            # A host name of our own: other mDNS responders on this machine also answer
            # for the machine's host name, with addresses a receiver may not reach,
            # which holds up its DACP requests. The trailing dot makes zeroconf answer
            # address queries for this name.
            server=f"ma-dacp-{self.dacp_id.lower()}.local.",
        )

    async def _register_dacp_service(self) -> None:
        """
        Register the DACP ActiveRemote mDNS service, reclaiming a stale name if needed.

        The service name is derived from the (persistent) server id, so it is identical
        across every reload and restart. A previous registration that was not cleanly
        torn down - a reload racing a slow initial load, or a leftover from a prior crash -
        keeps the same name in the zeroconf cache and makes a plain register raise
        NonUniqueNameException. In that case we broadcast a goodbye for the name to flush
        the stale record, then register again.
        """
        aiozc = self.mass.discovery.aiozc
        try:
            await aiozc.async_register_service(self._dacp_info)
            return
        except NonUniqueNameException:
            self.logger.debug(
                "DACP service %s already advertised - reclaiming stale registration",
                self._dacp_info.name,
            )
        await aiozc.async_unregister_service(self._dacp_info)
        await asyncio.sleep(DACP_RECLAIM_DELAY)
        await aiozc.async_register_service(self._dacp_info)

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
        # Advertise the same grandmaster identity as the per-stream sessions
        # (which derive their PTP clock id from --dacp), so receivers see one
        # consistent clock whether the daemon or an in-process engine serves it.
        args = [cli_binary, "--ptp-daemon", "--dacp", self.dacp_id]
        if_arg = await self.mass.streams.get_source_ip()
        if if_arg:
            args += ["--if", if_arg]
        # Read after the last await before the spawn. A new rate while the daemon spawns
        # finds no daemon to restart, so the timeline is checked again once it runs.
        clock_args = cli_clock_args(self.mass.timeline_clock)
        args += clock_args
        # The daemon runs quiet by default: its per-packet PTP tracing
        # (Announce/Sync/Delay_Req, ~10 lines/s) needs BOTH verbose logging and
        # the dedicated opt-in, so ordinary verbose sessions are not flooded
        # with timing chatter that only matters for clock-sync debugging.
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL) and self.config.get_value(
            CONF_VERBOSE_PTP_LOGGING
        ):
            args += ["--debug", "10"]
        # The binding the daemon ends up with is the first thing needed when triaging
        # timing issues from a user's log, and it is invisible otherwise: --if is left
        # out entirely for a default bind ip.
        self.logger.debug("Starting shared PTP clock daemon: if=%s", if_arg or "<all interfaces>")
        daemon = AsyncProcess(args, stdout=True, stderr=True, name="cliairplay-ptp-daemon")
        # (Re)gate readiness for this daemon instance: not ready until a reader
        # sees the "daemon up" line (a restart clears any previous readiness).
        if self._ptp_daemon_ready is None:
            self._ptp_daemon_ready = asyncio.Event()
        else:
            self._ptp_daemon_ready.clear()
        self._reset_ptp_daemon_warn_budget()
        await daemon.start()
        if self._ptp_daemon_stop_requested:
            # the provider was unloaded while the daemon spawned
            await daemon.close()
            return
        self._ptp_daemon = daemon
        self._ptp_daemon_started = time.monotonic()
        daemon.attach_stderr_reader(self.mass.create_task(self._ptp_daemon_stderr_reader(daemon)))
        self._ptp_daemon_stdout_task = self.mass.create_task(self._ptp_daemon_stdout_reader(daemon))
        self.mass.create_task(self._ptp_daemon_monitor(daemon))
        if cli_clock_args(self.mass.timeline_clock) != clock_args:
            await self._restart_ptp_daemon()

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
        """Log a PTP daemon output line and detect its readiness signal."""
        # Routine daemon output is verbose-only so it never floods a user's log
        # (the per-packet timing trace only runs at verbose in the first place).
        lowered = line.lower()
        if any(marker in lowered for marker in CLI_PROBLEM_MARKERS):
            self._warn_ptp_daemon_line(line)
        else:
            self.logger.log(VERBOSE_LOG_LEVEL, "PTP daemon: %s", line)
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

    def _reset_ptp_daemon_warn_budget(self) -> None:
        """
        Give a newly spawned daemon a full warning budget of its own.

        The budget is per daemon, not per provider: a daemon that spent it
        before crashing would otherwise have its replacement's startup failure -
        the one line worth reading - suppressed for the rest of the window.
        Whatever the old daemon had held back is reported on the way out rather
        than dropped.
        """
        if self._ptp_daemon_warns_suppressed:
            self.logger.warning(
                "PTP daemon: %d further problem line(s) from the previous daemon were "
                "suppressed; enable verbose logging to see them all",
                self._ptp_daemon_warns_suppressed,
            )
        self._ptp_daemon_warn_window_start = None
        self._ptp_daemon_warns_in_window = 0
        self._ptp_daemon_warns_suppressed = 0

    def _warn_ptp_daemon_line(self, line: str) -> None:
        """
        Warn about a daemon line that reads like a problem, at a bounded rate.

        The markers are broad by design, and "error" is ordinary vocabulary in
        clock telemetry (offset error, path delay error), so one matching line
        in the daemon's per-packet trace would fill a user's log with warnings.
        A burst still gets through - which is what a real one-shot daemon
        failure looks like - and the rest of the window is counted and reported
        once instead. Suppressed lines still reach the verbose log.

        :param line: The daemon output line that matched a problem marker.
        """
        now = time.monotonic()
        window_start = self._ptp_daemon_warn_window_start
        # None means no window is open yet, which is not the same as one that
        # started at monotonic zero: time.monotonic() counts from boot on Linux,
        # so a server started in the first minute of uptime would otherwise get
        # a first window shorter than the rest.
        if window_start is None or now - window_start >= PTP_DAEMON_WARN_WINDOW:
            if self._ptp_daemon_warns_suppressed:
                self.logger.warning(
                    "PTP daemon: %d further problem line(s) were suppressed over the last "
                    "%.0fs; enable verbose logging to see them all",
                    self._ptp_daemon_warns_suppressed,
                    PTP_DAEMON_WARN_WINDOW,
                )
            self._ptp_daemon_warn_window_start = now
            self._ptp_daemon_warns_in_window = 0
            self._ptp_daemon_warns_suppressed = 0
        if self._ptp_daemon_warns_in_window < PTP_DAEMON_WARN_BURST:
            self._ptp_daemon_warns_in_window += 1
            self.logger.warning("PTP daemon: %s", line)
            return
        self._ptp_daemon_warns_suppressed += 1
        self.logger.log(VERBOSE_LOG_LEVEL, "PTP daemon: %s", line)

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

    def _on_timeline_rate_changed(self) -> None:
        """Put the shared PTP clock daemon on the new rate of the timeline."""
        self._ptp_daemon_restart_task = self.mass.create_task(self._restart_ptp_daemon())

    async def _restart_ptp_daemon(self) -> None:
        """Restart a running shared PTP clock daemon, so it runs on the current timeline."""
        if (daemon := self._ptp_daemon) is None or self._ptp_daemon_stop_requested:
            return
        # Detached first, so its monitor takes this exit for a wanted one, and with
        # readiness dropped, so no stream attaches to it on its way out.
        self._ptp_daemon = None
        if self._ptp_daemon_ready is not None:
            self._ptp_daemon_ready.clear()
        if (stdout_task := self._ptp_daemon_stdout_task) and not stdout_task.done():
            stdout_task.cancel()
            with suppress(asyncio.CancelledError):
                await stdout_task
        await daemon.close()
        if not self._ptp_daemon_stop_requested:
            await self._start_ptp_daemon()

    def _has_running_streams(self) -> bool:
        """Return whether a cliairplay stream process runs."""
        return any(
            player.stream is not None and player.stream.running
            for player in [*self.get_players(), *self._speakers.values()]
        )

    async def _handle_dacp_request(  # noqa: C901, PLR0912, PLR0915
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
                    for x in [*self.get_players(), *self._speakers.values()]
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
            # a speaker of a set acts on its set
            owner = player.owner if player else None
            if not player or not owner or (parent_player := owner.player) is None:
                return

            # commands from the device address the player it renders for
            player_id = parent_player.player_id
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
                active_queue = self.mass.players.get_active_queue(parent_player)
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
                if owner.playback_state == PlaybackState.PLAYING:
                    self.mass.call_later(
                        1.0,
                        self.mass.players.cmd_pause,
                        player_id,
                        task_id=f"debounced_pause_{player_id}",
                    )
            elif "dmcp.device-volume=" in path and not player.ignore_volume_reports:
                # This is a bit annoying as this can be either the device confirming a new volume
                # we've sent or the device requesting a new volume itself.
                # The player hands a new level back exactly as reported, so it cannot start
                # a pingpong of volume changes
                airplay_volume = float(path.split("dmcp.device-volume=", 1)[-1])
                if airplay_volume <= AIRPLAY_VOLUME_MUTE:
                    player._attr_volume_muted = True
                    if player.stream and player.stream.running:
                        self.mass.create_task(player.stream.send_cli_command("VOLUME=0"))
                    player.update_state()
                    self.bridge_manager.sync_volume(player.protocol_id)
                else:
                    if player.volume_muted:
                        player._attr_volume_muted = False
                        if player.stream and player.stream.running:
                            self.mass.create_task(
                                player.stream.send_cli_command(f"VOLUME={player.volume_level or 0}")
                            )
                    volume = convert_airplay_volume(airplay_volume)
                    player.update_volume_from_device(volume, airplay_volume)
            elif "dmcp.volume=" in path:
                # volume change request from device (e.g. volume buttons)
                volume = int(path.split("dmcp.volume=", 1)[-1])
                player.update_volume_from_device(volume)
            elif "device-prevent-playback=1" in path:
                # device switched to another source (or is powered off)
                # Cancel any pending debounced pause since prevent-playback takes precedence
                self.mass.cancel_timer(f"debounced_pause_{player_id}")
                # Ignore during stream transition (stale message from old CLI process)
                if owner._transitioning or not player.stream:
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
                    if session := player.stream.session:
                        self.logger.debug(
                            "Prevent playback command detected for player %s",
                            player.name,
                        )
                        if not player.renders_audio:
                            # the device of a set only shows what its speakers play: they
                            # play on until another sender takes them
                            self.mass.create_task(
                                session.remove_silent_member(player, rejoin=False)
                            )
                        elif owner.synced_to or parent_player.active_group:
                            self.mass.create_task(
                                self.mass.players.cmd_ungroup(parent_player.player_id)
                            )
                        else:
                            # a set stops on all of its speakers
                            for endpoint in owner.session_endpoints:
                                if endpoint.stream:
                                    self.mass.create_task(endpoint.stream.stop())
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

    def _drop_unverified_password_markers(self) -> None:
        """
        Clear the stored "password rejected" verdicts left by earlier releases, once.

        Those releases marked a player whenever the binary reported an auth-shaped
        rejection, without separating a password challenge from a device that
        refused the handshake outright. The refusals put players that have no
        password at all into a setup flow only a password could leave, so the
        verdicts are dropped and left to be earned again on the next connect.
        """
        if self.mass.config.get_raw_provider_config_value(
            self.instance_id, CONF_PASSWORD_MARKERS_REVIEWED, False
        ):
            return
        # walks the stored protocol configs, which also hold the devices that
        # were not discovered yet
        for protocol_id, raw_conf in self.mass.config.get(CONF_PLAYER_PROTOCOLS, {}).items():
            if not isinstance(raw_conf, dict) or raw_conf.get("provider") != self.instance_id:
                continue
            if not self.mass.config.get_protocol_value(protocol_id, CONF_PASSWORD_INVALID, False):
                continue
            self.logger.info("Clearing the unverified password marker on %s", protocol_id)
            self.mass.config.set_protocol_value(protocol_id, CONF_PASSWORD_INVALID, False)
        # last, so a failure part-way through leaves the review to be retried on
        # the next load instead of stranding the players it never reached
        self.mass.config.set_raw_provider_config_value(
            self.instance_id, CONF_PASSWORD_MARKERS_REVIEWED, True
        )

    def _reset_auto_pinned_compat_modes(self) -> None:
        """
        Reset the compatibility-mode pins left by earlier releases, once.

        Those releases switched a player's streaming mode to compatibility mode
        themselves when its native control channel failed, which a network
        dropout of the device also triggers. The pin outlived the dropout and
        put the player on a lane many devices reject outright, so the
        machine-written values are returned to Automatic; a user who wants
        compatibility mode can simply pin it again.
        """
        if self.mass.config.get_raw_provider_config_value(
            self.instance_id, CONF_COMPAT_PINS_REVIEWED, False
        ):
            return
        # walks the stored protocol configs, which also hold the devices that
        # were not discovered yet
        for protocol_id, raw_conf in self.mass.config.get(CONF_PLAYER_PROTOCOLS, {}).items():
            if not isinstance(raw_conf, dict) or raw_conf.get("provider") != self.instance_id:
                continue
            if (
                self.mass.config.get_protocol_value(protocol_id, CONF_STREAMING_MODE)
                != STREAMING_MODE_AP2_COMPAT
            ):
                continue
            self.logger.info("Resetting the streaming mode of %s back to Automatic", protocol_id)
            self.mass.config.set_protocol_value(
                protocol_id, CONF_STREAMING_MODE, STREAMING_MODE_AUTO
            )
        # last, so a failure part-way through leaves the reset to be retried on
        # the next load instead of stranding the players it never reached
        self.mass.config.set_raw_provider_config_value(
            self.instance_id, CONF_COMPAT_PINS_REVIEWED, True
        )
