"""AirPlay Player implementations."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import time
from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.constants import PLAYER_CONTROL_NATIVE
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    CrossfadeMode,
    IdentifierType,
    MediaType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
)
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.media_items import AudioFormat

from music_assistant.controllers.streams.audio import overlay_active
from music_assistant.helpers.util import get_primary_ip_address_from_zeroconf, is_valid_mac_address
from music_assistant.models.player import DeviceInfo, Player, PlayerMedia
from music_assistant.models.setup_flow import AbortFlow

from .constants import (
    AIRPLAY_AP2_SETUP_LEAD_MS,
    AIRPLAY_DISCOVERY_TYPE,
    AIRPLAY_HIRES_AUDIO_FORMATS,
    AIRPLAY_HIRES_SAMPLE_RATES,
    AIRPLAY_PCM_FORMAT,
    AIRPLAY_RAOP_SETUP_LEAD_MS,
    AIRPLAY_REJOIN_ATTEMPT_DELAYS,
    BASE_PLAYER_FEATURES,
    CONF_AIRPLAY_CREDENTIALS,
    CONF_ENCRYPTION,
    CONF_ENTRY_SYNC_ADJUST_AIRPLAY,
    CONF_FORCE_RAOP,
    CONF_IGNORE_VOLUME,
    CONF_PAIRING_PASSWORD,
    CONF_PAIRING_PIN,
    CONF_PASSWORD,
    CONF_RAOP_CREDENTIALS,
    CONF_STORED_VOLUME,
    FALLBACK_VOLUME,
    LEGACY_PAIRING_BIT,
    PASSWORD_BIT,
    PIN_REQUIRED,
    RAOP_DISCOVERY_TYPE,
    StreamingProtocol,
)
from .helpers import (
    is_apple_device,
    is_macos_device,
    player_id_to_mac_address,
    supports_airplay2,
)
from .stream_session import AirPlayStreamSession

if TYPE_CHECKING:
    from zeroconf.asyncio import AsyncServiceInfo

    from music_assistant.models.setup_flow import SetupSession

    from .pairing import AirPlayPairing
    from .provider import AirPlayProvider
    from .stream import AirPlayStream

# Docker bridge subnet, sometimes wrongly advertised via mDNS by containerized devices.
_DOCKER_SUBNET = ipaddress.ip_network("172.16.0.0/12")


class AirPlayPlayer(Player):
    """Base implementation shared by all AirPlay players."""

    def __init__(
        self,
        provider: AirPlayProvider,
        player_id: str,
        raop_discovery_info: AsyncServiceInfo | None,
        airplay_discovery_info: AsyncServiceInfo | None,
        address: str,
        display_name: str,
        manufacturer: str,
        model: str,
        initial_volume: int = FALLBACK_VOLUME,
    ) -> None:
        """Initialize AirPlayPlayer."""
        self.raop_discovery_info = raop_discovery_info
        self.airplay_discovery_info = airplay_discovery_info
        # Audio formats the receiver advertises, learned from its /info response;
        # zero until that lands (or when the device publishes no format tables).
        self.advertised_audio_formats = 0
        self._attr_enabled_by_default = not is_macos_device(manufacturer, model)
        super().__init__(provider, player_id)
        self.address = address
        self.stream: AirPlayStream | None = None
        self.last_command_sent = 0.0
        self._lock = asyncio.Lock()
        self._transitioning = False  # Set during stream replacement to ignore stale DACP messages
        self._rejoin_task: asyncio.Task[None] | None = None
        # Set (static) player attributes
        self._attr_name = display_name
        self._attr_available = True
        mac_address = player_id_to_mac_address(player_id)
        self._attr_device_info = DeviceInfo(
            model=model,
            manufacturer=manufacturer,
        )
        # Only add MAC address if it's valid (not 00:00:00:00:00:00)
        if is_valid_mac_address(mac_address):
            self._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, mac_address)
        self._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, address)
        self._attr_device_info.add_identifier(IdentifierType.AIRPLAY_ID, player_id)
        self._attr_volume_level = initial_volume
        self._attr_can_group_with = {provider.instance_id}

    @property
    def protocol(self) -> StreamingProtocol:
        """Get the streaming protocol to use/prefer for this player."""
        # AirPlay 2 whenever the device can speak it and RAOP is not being forced;
        # RAOP for legacy receivers (or when the force-RAOP escape hatch is set).
        if self._is_airplay2_capable and not self._force_raop_active:
            return StreamingProtocol.AIRPLAY2
        return StreamingProtocol.RAOP

    @property
    def protocol_override(self) -> StreamingProtocol | None:
        """
        Return the user-forced streaming protocol, or None for automatic selection.

        The only override a user can set is the "force RAOP" escape hatch (offered
        for AirPlay-2-capable non-Apple receivers whose AirPlay 2 implementation
        misbehaves). Otherwise the cliairplay binary resolves the route itself from
        the mDNS TXT records (--protocol auto) and the ``protocol`` property above
        only reflects MA's own planning heuristic (timing, ports).
        """
        return StreamingProtocol.RAOP if self._force_raop_active else None

    @property
    def hires_playback_enabled(self) -> bool:
        """Return if 24-bit hi-res playback is possible for this player."""
        # 24-bit only works over the AirPlay 2 flow, so a device that streams RAOP
        # (a legacy receiver, or the force-RAOP escape hatch) stays on the 16-bit
        # base whatever it advertises.
        return (
            bool(self.advertised_audio_formats & AIRPLAY_HIRES_AUDIO_FORMATS)
            and self.protocol == StreamingProtocol.AIRPLAY2
        )

    @property
    def supported_sample_rates(self) -> list[tuple[int, int]]:
        """Return the (sample_rate, bit_depth) pairs this player natively supports."""
        if self.hires_playback_enabled:
            return AIRPLAY_HIRES_SAMPLE_RATES
        return [(AIRPLAY_PCM_FORMAT.sample_rate, AIRPLAY_PCM_FORMAT.bit_depth)]

    @property
    def needs_setup(self) -> bool:
        """Return if the player needs setup."""
        if self._requires_pin_pairing() or (
            self._requires_password_pairing() and self.protocol == StreamingProtocol.AIRPLAY2
        ):
            # Credentials for either protocol keep the player usable: the binary
            # picks the best route for the credentials it has. The pairing section
            # in the player config still offers pairing for the active protocol
            # (e.g. to upgrade a legacy RAOP pairing to AirPlay 2).
            if not (
                self.get_setup_value(CONF_AIRPLAY_CREDENTIALS)
                or self.get_setup_value(CONF_RAOP_CREDENTIALS)
            ):
                return True
        return False

    @property
    def setup_reason(self) -> str | None:
        """Return why the player needs setup, or None when it is ready to use."""
        return "pairing_required" if self.needs_setup else None

    @property
    def requires_flow_mode(self) -> bool:
        """Return if the player requires flow mode."""
        return True

    @property
    def supported_features(self) -> set[PlayerFeature]:
        """Return the supported features of this player."""
        # PAUSE is always advertised, including while synced. This keeps the AirPlay
        # player itself as the pause control target so pause() can decide what to do:
        # a true pause for a single player, or a full session stop for a sync group
        # (see pause()). If PAUSE were dropped while grouped, the players controller
        # could fall through to a linked native player's pause (e.g. a Sonos acting as
        # an AirPlay receiver), which only pauses the sync leader while the other
        # members keep playing.
        return {*BASE_PLAYER_FEATURES, PlayerFeature.PAUSE}

    @property
    def can_group_with(self) -> set[str]:
        """
        Return player IDs this player can group with.

        RAOP and AP2 players can group with other RAOP and/or AP2 players.
        """
        prov = cast("AirPlayProvider", self.provider)
        return {
            p.player_id for p in prov.get_players() if p.available and p.player_id != self.player_id
        }

    @property
    def wait_start(self) -> int:
        """Get the setup lead required by an externally timed audio source."""
        if self.protocol == StreamingProtocol.RAOP:
            return AIRPLAY_RAOP_SETUP_LEAD_MS
        return AIRPLAY_AP2_SETUP_LEAD_MS

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        # Pairing/credentials are no longer config entries: they are collected by the
        # interactive setup flow (run_setup_flow) and stored in the player's setup_data.
        base_entries: list[ConfigEntry] = []

        # Effective RAOP state from the current (stored) force-RAOP setting, so the
        # RAOP-only entries show/hide consistently with it.
        is_raop = self._force_raop_active or not self._is_airplay2_capable

        # "Force RAOP" escape hatch: only for AirPlay-2-capable non-Apple receivers
        # (see _force_raop_available). Framed as a per-device workaround for a
        # misbehaving AirPlay 2 implementation, not a general protocol choice.
        if self._force_raop_available:
            base_entries.append(
                ConfigEntry(
                    key=CONF_FORCE_RAOP,
                    type=ConfigEntryType.BOOLEAN,
                    default_value=False,
                    category="protocol_generic",
                    advanced=True,
                )
            )

        # Regular AirPlay config entries
        base_entries += [
            CONF_ENTRY_SYNC_ADJUST_AIRPLAY,
            ConfigEntry(
                key=CONF_ENCRYPTION,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                hidden=not is_raop,
                category="protocol_generic",
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_PASSWORD,
                type=ConfigEntryType.SECURE_STRING,
                default_value=None,
                required=False,
                # the device password is only consumed by the RAOP flow
                hidden=not is_raop,
                category="protocol_generic",
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_IGNORE_VOLUME,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                category="protocol_generic",
                advanced=True,
            ),
        ]

        return base_entries

    async def run_setup_flow(self, session: SetupSession) -> None:
        """
        Run the interactive setup flow for this AirPlay player (streaming pairing).

        :param session: The setup flow session used to interact with the user.
        """
        collected: dict[str, ConfigValueType] = {}
        await self._run_streaming_pairing(session, collected)
        await session.finish(collected)

    async def stop(self) -> None:
        """Send STOP command to player."""
        # an explicit stop (including power-off routed as stop) is user intent:
        # drop any pending automatic re-join
        self.cancel_group_rejoin()
        async with self._lock:
            if self.stream and self.stream.session:
                # forward stop to the entire stream session
                await self.stream.session.stop()
            elif cast("AirPlayProvider", self.provider).bridge_manager.stop_streaming(
                self.player_id
            ):
                # Sendspin bridge active: trigger full bridge cleanup
                # which stops streaming, kills the CLI, and cancels writer tasks
                pass
            elif self.stream and self.stream.running:
                # Fallback: stop protocol directly
                await self.stream.stop(force=True)
                self.stream = None
            self._attr_current_media = None
            self.update_state()

    async def play(self) -> None:
        """Send PLAY (unpause) command to player."""
        if self.group_members or self.synced_to:
            # Grouped pause parks the whole session (standby); unpausing one
            # member cannot restart the group in sync. Resume via the queue
            # instead: play_media flushes and re-anchors every parked member at
            # one shared instant. The queue can belong to a linked native parent
            # (for example Sonos), so resolve it instead of using the AirPlay ID.
            active_queue = self.mass.players.get_active_queue(self)
            if active_queue is None:
                raise PlayerCommandFailed(
                    f"Cannot resume grouped AirPlay player {self.display_name} without an active queue"
                )
            await self.mass.player_queues.resume(active_queue.queue_id, fade_in=False)
            return
        async with self._lock:
            if self.stream and self.stream.running:
                if await self.stream.send_cli_command("ACTION=PLAY"):
                    # Resuming re-anchors playout; the binary zeroes its own
                    # re-anchor total on resume, so drop the tracked shift to
                    # keep the server and binary baselines aligned.
                    self.stream.reset_reanchor_shift()

    async def pause(self) -> None:
        """Send PAUSE command to player."""
        if self.group_members or self.synced_to:
            # A broadcast pause cannot keep independent member processes
            # sample-aligned on resume. Instead the session is parked: every
            # member stalls but keeps its connection (and remote control), and
            # the queue's resume flushes and re-anchors over the live
            # connections — the same coordinated warm restart as seek/next.
            if (
                self.stream
                and self.stream.running
                and self.stream.session
                and await self.stream.session.standby()
            ):
                return
            # Some member no longer has a live connection: full stop and let
            # the queue controller resume from the saved position.
            self.logger.debug("Sync group cannot be parked, using STOP instead of PAUSE")
            await self.stop()
            return

        async with self._lock:
            if not self.stream or not self.stream.running:
                return
            await self.stream.send_cli_command("ACTION=PAUSE")

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on given player."""
        # the player is being (re)purposed on purpose: drop any pending
        # automatic re-join left over from an unexpected stream loss
        self.cancel_group_rejoin()
        async with self._lock:
            if self.synced_to:
                # this should not happen, but guard anyways
                raise RuntimeError("Player is synced")
            self._attr_current_media = media

            sync_clients = self._get_sync_clients()
            session_pcm_format = await self._get_session_pcm_format(sync_clients, media)

            # Warm path: a live, compatible session absorbs the new media via a
            # flush-refill in place (seek/next never pays the reconnect cost).
            if (
                self.stream
                and self.stream.running
                and self.stream.session
                and self.stream.session.can_replace(sync_clients, session_pcm_format)
            ):
                self._transitioning = True
                audio_source = self.mass.streams.get_stream(
                    media, session_pcm_format, self.player_id, use_flow_stream_buffering=True
                )
                if await self.stream.session.replace(audio_source, media):
                    self._transitioning = False
                    # A seek changes no media identity, so the identity-driven
                    # metadata callback stays silent and receivers would show
                    # a stale Now Playing position; nudge every member once
                    # the queue position has settled.
                    for member in self.stream.session.sync_clients:
                        self.mass.call_later(
                            1,
                            member._on_player_media_updated,
                            task_id=f"player_media_updated_{member.player_id}",
                        )
                    return
                # warm replacement failed; fall through to a cold restart

            # Cold path: stop any existing stream and set up from scratch
            if self.stream and self.stream.running and self.stream.session:
                # Set transitioning flag to ignore stale DACP messages (like prevent-playback)
                self._transitioning = True
                await self.stream.session.stop()
                self.stream = None

            # select audio source
            audio_source = self.mass.streams.get_stream(
                media, session_pcm_format, self.player_id, use_flow_stream_buffering=True
            )

            # setup StreamSession for player (and its sync childs if any)
            provider = cast("AirPlayProvider", self.provider)
            stream_session = AirPlayStreamSession(
                provider,
                sync_clients,
                session_pcm_format,
                media,
            )
            await stream_session.start(audio_source)
            self._attr_elapsed_time = time.time() - stream_session.start_time
            self._attr_elapsed_time_last_updated = time.time()
            self._transitioning = False

    async def volume_set(self, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        if self.stream and self.stream.running and self.volume_muted is not True:
            await self.stream.send_cli_command(f"VOLUME={volume_level}")
        self._attr_volume_level = volume_level
        self.update_state()
        # store last state in playerconfig
        self.mass.config.set_raw_player_config_value(
            self.player_id, CONF_STORED_VOLUME, volume_level
        )

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME_MUTE command on the player."""
        self._attr_volume_muted = muted
        if self.stream and self.stream.running:
            volume = 0 if muted else (self.volume_level or 0)
            await self.stream.send_cli_command(f"VOLUME={volume}")
        self.update_state()

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS command on the player."""
        async with self._lock:
            if self.synced_to:
                # this should not happen, but guard anyways
                raise RuntimeError("Player is synced, cannot set members")
            if not player_ids_to_add and not player_ids_to_remove:
                # nothing to do
                return

            stream_session = (
                self.stream.session
                if self.stream and self.stream.running and self.stream.session
                else None
            )
            # handle removals first
            if player_ids_to_remove:
                if self.player_id in player_ids_to_remove:
                    if stream_session and len(stream_session.sync_clients) > 1:
                        # Other clients remain: remove only this leader client,
                        # session continues for remaining players (dynamic leader switch)
                        await stream_session.remove_client(self, reason="leader removed from group")
                    elif stream_session:
                        # Last client, stop the whole session
                        await stream_session.stop()
                    self._attr_group_members = []
                    self.update_state()
                    return

                for child_player in self._get_sync_clients():
                    if child_player.player_id in player_ids_to_remove:
                        # update group_members first to prevent race conditions
                        # where a concurrent play_media could re-include this player
                        if child_player.player_id in self._attr_group_members:
                            self._attr_group_members.remove(child_player.player_id)
                        if stream_session:
                            await stream_session.remove_client(
                                child_player, reason="child removed from group"
                            )
                        elif child_player.stream and child_player.stream.running:
                            # leader's stream is no longer running but child still has
                            # an active stream - stop it directly
                            await child_player.stream.stop(force=True)

                # If group leader is left alone after removals, clear the group_members list
                if (
                    self._attr_group_members
                    and len(self._attr_group_members) == 1
                    and self.player_id in self._attr_group_members
                ):
                    self._attr_group_members = []

            # handle additions
            for player_id in player_ids_to_add or []:
                if player_id == self.player_id or player_id in self.group_members:
                    # nothing to do: player is already part of the group
                    continue
                child_player_to_add: AirPlayPlayer | None = cast(
                    "AirPlayPlayer | None", self.mass.players.get_player(player_id)
                )
                if not child_player_to_add:
                    # should not happen, but guard against it
                    continue

                # ensure the child does not have an existing stream session active
                if child_player_to_add := cast(
                    "AirPlayPlayer | None", self.mass.players.get_player(player_id)
                ):
                    if (
                        child_player_to_add.playback_state == PlaybackState.PAUSED
                        and child_player_to_add.stream
                    ):
                        # Stop the paused stream to avoid a deadlock situation
                        await child_player_to_add.stream.stop()
                    if (
                        child_player_to_add.stream
                        and child_player_to_add.stream.running
                        and child_player_to_add.stream.session
                        and child_player_to_add.stream.session != stream_session
                    ):
                        await child_player_to_add.stream.session.remove_client(
                            child_player_to_add, reason="moving to different session"
                        )

                # add new child to the existing stream (RAOP or AirPlay2) session (if any)
                self._attr_group_members.append(player_id)
                if stream_session and child_player_to_add is not None:
                    # Skip add_client if the player is already streaming in this session
                    # (e.g. after a dynamic leader switch where the stream continues)
                    if child_player_to_add not in stream_session.sync_clients:
                        await stream_session.add_client(child_player_to_add)

            # Ensure group leader includes itself in group_members when it has members
            # This is required for the synced_to property to work correctly
            if self._attr_group_members and self.player_id not in self._attr_group_members:
                self._attr_group_members.insert(0, self.player_id)

            # always update the state after modifying group members
            self.update_state()

    def update_volume_from_device(self, volume: int) -> None:
        """Update volume from device feedback."""
        ignore_volume_report = (
            self.config.get_value(CONF_IGNORE_VOLUME)
            or self.device_info.manufacturer.lower() == "apple"
        )

        if ignore_volume_report:
            return

        cur_volume = self.volume_level or 0
        if abs(cur_volume - volume) > 1 or (time.time() - self.last_command_sent) > 3:
            self.mass.create_task(self.volume_set(volume))
        else:
            self._attr_volume_level = volume
            self.mass.config.set_raw_player_config_value(self.player_id, CONF_STORED_VOLUME, volume)
            self.update_state()

    def set_discovery_info(self, discovery_info: AsyncServiceInfo, display_name: str) -> None:
        """Set/update the discovery info for the player."""
        self._attr_name = display_name
        if discovery_info.type == AIRPLAY_DISCOVERY_TYPE:
            self.airplay_discovery_info = discovery_info
        elif discovery_info.type == RAOP_DISCOVERY_TYPE:
            self.raop_discovery_info = discovery_info
        else:  # guard
            return
        cur_address = self.address
        prefer_ipv6 = ":" in str(self.mass.streams.publish_ip)
        new_address = get_primary_ip_address_from_zeroconf(discovery_info, prefer_ipv6=prefer_ipv6)
        if new_address is None:
            # should always be set, but guard against None
            return
        if cur_address != new_address:
            # Ignore mDNS updates that replace a routable address with a Docker bridge one.
            try:
                if (
                    cur_address
                    and ipaddress.ip_address(new_address) in _DOCKER_SUBNET
                    and ipaddress.ip_address(cur_address) not in _DOCKER_SUBNET
                ):
                    self.logger.warning(
                        "Ignoring mDNS update from %s to Docker address %s",
                        cur_address,
                        new_address,
                    )
                    self.update_state()
                    return
            except ValueError:
                pass
            self.logger.debug("Address updated from %s to %s", cur_address, new_address)
            self._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, new_address)
            self.address = new_address
        self.update_state()

    def set_state_from_stream(
        self,
        state: PlaybackState | None = None,
        elapsed_time: float | None = None,
        stream: AirPlayStream | None = None,
    ) -> None:
        """
        Set the playback state from stream (RAOP or AirPlay2).

        :param state: New playback state (or None to keep current).
        :param elapsed_time: New elapsed time (or None to keep current).
        :param stream: The stream instance sending this update (for validation).
        """
        # Ignore state updates from old/stale streams
        if stream is not None and stream != self.stream:
            return
        if state is not None:
            self._attr_playback_state = state
        if elapsed_time is not None:
            self._attr_elapsed_time = elapsed_time
            self._attr_elapsed_time_last_updated = time.time()
        self.update_state()

    def get_stream_pcm_format(self, session_pcm_format: AudioFormat) -> AudioFormat:
        """
        Return the PCM format to feed this player's cliairplay process.

        :param session_pcm_format: The PCM format of the (shared) stream session.
        """
        if not self.hires_playback_enabled:
            return AIRPLAY_PCM_FORMAT
        # 24-bit: the binary expects raw s32le input on stdin (--bitdepth 24)
        # and truncates to 24-bit ALAC internally.
        supported_rates = {sample_rate for sample_rate, _ in self.supported_sample_rates}
        sample_rate = (
            session_pcm_format.sample_rate
            if session_pcm_format.sample_rate in supported_rates
            else AIRPLAY_PCM_FORMAT.sample_rate
        )
        return AudioFormat(
            content_type=ContentType.PCM_S32LE,
            sample_rate=sample_rate,
            bit_depth=24,
        )

    def sync_volume_level(self) -> None:
        """
        Sync volume from parent player if needed.

        AirPlay players only report their volume level when we are actually streaming to them
        and we remember the last used/reported volume level in the player config by default
        but if we have a parent player, that may know better about the current volume level,
        so we try to sync from that parent player if possible
        """
        if (
            self.protocol_parent_id
            and (parent_player := self.mass.players.get_player(self.protocol_parent_id))
            and parent_player.state.volume_level is not None
        ):
            if self._has_native_protocol_parent:
                # Native parent volume is on the receiver/amplifier scale.
                # Keep the AirPlay child volume learned from DACP feedback instead.
                return
            if self._attr_volume_level == parent_player.state.volume_level:
                return
            self._attr_volume_level = parent_player.state.volume_level
            self.mass.config.set_raw_player_config_value(
                self.player_id, CONF_STORED_VOLUME, self._attr_volume_level
            )
            self.update_state()

    async def on_config_updated(self) -> None:
        """Handle logic when the player config is updated."""
        await super().on_config_updated()
        prov = cast("AirPlayProvider", self.provider)
        await prov.bridge_manager.evaluate_bridge(self)

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        await super().on_unload()
        self.cancel_group_rejoin()
        if self.stream:
            # remove this player from the stream session if it is running
            if self.stream.running and self.stream.session:
                await self.stream.session.remove_client(self, reason="player unloaded")
            self.stream = None

    def schedule_group_rejoin(self, candidate_ids: list[str]) -> None:
        """
        Schedule a bounded automatic re-join of this player to its still-active group.

        Used when this player's stream process died unexpectedly while it was part
        of a playing sync group (e.g. the device rode out a network blackout): the
        player is re-added to the group's live session through the regular
        late-join path after a short backoff. Any user action on the player (or it
        joining a session by other means) cancels the re-join; when the group is
        no longer playing, its membership was changed meanwhile or the device is
        offline, the re-join is abandoned and the player simply stays idle.

        :param candidate_ids: Player ids that led or shared the group at the
            moment the stream was lost, used to resolve the re-join target (the
            leadership may transfer while the backoff runs).
        """
        self.cancel_group_rejoin()
        self.logger.info(
            "Scheduling automatic re-join of %s to its group after unexpected stream loss",
            self.display_name,
        )
        self._rejoin_task = self.mass.create_task(self._group_rejoin_attempts(candidate_ids))

    def cancel_group_rejoin(self) -> None:
        """Cancel any pending automatic group re-join attempts for this player."""
        rejoin_task = self._rejoin_task
        self._rejoin_task = None
        # never self-cancel: the re-join attempt itself flows through the same
        # session (re)start paths that call this to clear stale schedules
        if rejoin_task and not rejoin_task.done() and rejoin_task is not asyncio.current_task():
            rejoin_task.cancel()

    @property
    def _has_native_protocol_parent(self) -> bool:
        """Return True if this AirPlay protocol player is linked to a native parent."""
        if not self.protocol_parent_id:
            return False
        parent_player = self.mass.players.get_player(self.protocol_parent_id)
        return bool(parent_player and parent_player.volume_control == PLAYER_CONTROL_NATIVE)

    def _get_flags(self) -> int:
        # Flags are either present via "sf" or "flags". Taken from pyatv.protocols.airplay.utils.
        # We combine flags from both RAOP and AirPlay discovery services because
        # LEGACY_PAIRING_BIT (0x200) is typically only in the RAOP service sf field
        # (e.g. Apple TV HD), while PIN_REQUIRED (0x8) may only appear in the AirPlay
        # service sf/flags field. Using only one source misses the pairing requirement.
        flags = 0
        for discovery_info in filter(None, [self.raop_discovery_info, self.airplay_discovery_info]):
            raw = (
                discovery_info.properties.get(b"sf")
                or discovery_info.properties.get(b"flags")
                or b"0x0"
            )
            with contextlib.suppress(ValueError, TypeError):
                flags |= int(raw, 16)
        return flags

    def _requires_pin_pairing(self) -> bool:
        """
        Check if this device requires pairing.

        Adapted from pyatv.protocols.airplay.utils.get_pairing_requirement.
        """
        return bool(self._get_flags() & (LEGACY_PAIRING_BIT | PIN_REQUIRED))

    def _requires_password_pairing(self) -> bool:
        """
        Check if this device requires password authentication.

        Password can be used for pairing instead of interactive PIN entry.
        """
        return bool(self._get_flags() & PASSWORD_BIT)

    def _get_credentials_key(self, protocol: StreamingProtocol) -> str:
        """Get the config key for credentials for given protocol."""
        if protocol == StreamingProtocol.RAOP:
            return CONF_RAOP_CREDENTIALS
        return CONF_AIRPLAY_CREDENTIALS

    @property
    def _advertised_features(self) -> str | None:
        """Return the AirPlay features bitmask the device advertises via mDNS."""
        # Prefer the _airplay service's ``features``, falling back to the _raop
        # service's ``ft`` when the former is absent (some devices only populate one).
        features: str | None = None
        if self.airplay_discovery_info:
            features = self.airplay_discovery_info.decoded_properties.get(
                "features"
            ) or self.airplay_discovery_info.decoded_properties.get("ft")
        if not features and self.raop_discovery_info:
            features = self.raop_discovery_info.decoded_properties.get("ft")
        return features

    @property
    def _is_airplay2_capable(self) -> bool:
        """
        Return whether this device can stream over AirPlay 2.

        Mirrors the feature-bit test the cliairplay binary uses for its own route
        selection: a device is AirPlay 2 capable when it exposes the _airplay
        service and either advertises the AirPlay 2 feature bits or offers no RAOP
        fallback at all (i.e. it is a pure AirPlay 2 receiver).
        """
        if not self.airplay_discovery_info:
            return False
        return supports_airplay2(self._advertised_features) or not self.raop_discovery_info

    @property
    def _force_raop_available(self) -> bool:
        """
        Return whether the "force RAOP" escape hatch applies to this device.

        Offered only for AirPlay-2-capable non-Apple receivers that also advertise
        a RAOP service to fall back to. Genuine Apple devices are always AirPlay 2,
        while RAOP-only and AirPlay-2-only devices have nothing to force.
        """
        return (
            self._is_airplay2_capable
            and self.raop_discovery_info is not None
            and not is_apple_device(self.device_info.manufacturer, self.device_info.model)
        )

    @property
    def _force_raop_active(self) -> bool:
        """Return whether RAOP is being forced through the escape-hatch toggle."""
        return self._force_raop_available and bool(self.config.get_value(CONF_FORCE_RAOP, False))

    async def _run_streaming_pairing(
        self, session: SetupSession, collected: dict[str, ConfigValueType]
    ) -> None:
        """
        Pair the streaming protocol (RAOP or AirPlay 2), unless already paired.

        Credentials for either protocol keep the player usable, so this no-ops when
        any are already stored (e.g. when the flow is re-launched from the player
        settings). The obtained credentials are added to ``collected`` under the
        protocol-specific key.

        :param session: The setup flow session used to interact with the user.
        :param collected: The values collected so far; updated in place.
        """
        if self.get_setup_value(CONF_AIRPLAY_CREDENTIALS) or self.get_setup_value(
            CONF_RAOP_CREDENTIALS
        ):
            return
        pin_pairing = self._requires_pin_pairing()
        # a password only replaces PIN pairing on the native AirPlay 2 flow
        password_pairing = (
            self._requires_password_pairing() and self.protocol == StreamingProtocol.AIRPLAY2
        )
        if not (pin_pairing or password_pairing):
            return

        protocol = self.protocol
        cred_key = self._get_credentials_key(protocol)
        if pin_pairing:
            step_id, field_key, field_type = "pair_pin", CONF_PAIRING_PIN, ConfigEntryType.STRING
        else:
            step_id, field_key, field_type = (
                "pair_password",
                CONF_PAIRING_PASSWORD,
                ConfigEntryType.SECURE_STRING,
            )

        errors: dict[str, str] | None = None
        while True:
            # Each attempt uses a fresh session: finish_pairing() closes the live
            # subprocess/session on completion, so a rejected PIN needs a new one
            # (and the device re-shows its PIN).
            pairing = await self._prepare_streaming_pairing(protocol, pin_pairing=pin_pairing)
            try:
                values = await session.form(
                    [
                        ConfigEntry(
                            key=field_key,
                            type=field_type,
                            required=True,
                            category="protocol_generic",
                        )
                    ],
                    step_id=step_id,
                    errors=errors,
                )
                credentials = await pairing.finish_pairing(pin=str(values[field_key]))
            except PlayerCommandFailed as err:
                errors = {"base": err.translation_key or str(err)}
                continue
            finally:
                # tears down the subprocess on retry, success and abort (cancellation)
                await pairing.close()
            collected[cred_key] = credentials
            return

    async def _prepare_streaming_pairing(
        self, protocol: StreamingProtocol, *, pin_pairing: bool
    ) -> AirPlayPairing:
        """
        Build and start a streaming pairing session (the device shows its PIN).

        A failure here cannot be recovered by re-prompting the user, so it aborts the
        flow; a partially started session is torn down first.

        :param protocol: The streaming protocol to pair (RAOP or AirPlay 2).
        :param pin_pairing: Whether the device shows a PIN the user must enter.
        """
        pairing: AirPlayPairing | None = None
        started = False
        try:
            pairing = self._build_streaming_pairing(protocol)
            await pairing.start_pairing_session()
            if pin_pairing:
                await pairing.start_pin_pairing()
            started = True
        except Exception as err:
            # a failure starting the session (device unreachable, binary/system
            # issue, ...) cannot be fixed by re-prompting, so abort with a clear
            # reason instead of letting it surface as a generic internal error
            self.logger.warning("Could not start AirPlay pairing session: %s", err)
            raise AbortFlow("pairing_failed") from err
        finally:
            if not started and pairing is not None:
                await pairing.close()
        assert pairing is not None  # reached only when started, i.e. a live session
        return pairing

    def _build_streaming_pairing(self, protocol: StreamingProtocol) -> AirPlayPairing:
        """
        Build an AirPlayPairing for the given streaming protocol.

        :param protocol: The streaming protocol to pair (RAOP or AirPlay 2).
        """
        from .pairing import AirPlayPairing  # noqa: PLC0415

        # For Apple devices pairing always happens on the AirPlay port (7000) even
        # when streaming will use RAOP; the RAOP port (5000) is only for streaming.
        port: int | None = None
        if self.airplay_discovery_info:
            port = self.airplay_discovery_info.port or 7000
        elif self.raop_discovery_info:
            port = self.raop_discovery_info.port or 5000
        provider = cast("AirPlayProvider", self.provider)
        device_id = provider.dacp_id
        pairing_address = self.address
        if protocol == StreamingProtocol.AIRPLAY2 and not isinstance(
            ipaddress.ip_address(pairing_address), ipaddress.IPv4Address
        ):
            if self.airplay_discovery_info:
                discovered_address = get_primary_ip_address_from_zeroconf(
                    self.airplay_discovery_info
                )
                if discovered_address and isinstance(
                    ipaddress.ip_address(discovered_address), ipaddress.IPv4Address
                ):
                    pairing_address = discovered_address
            if not isinstance(ipaddress.ip_address(pairing_address), ipaddress.IPv4Address):
                raise PlayerCommandFailed("AirPlay pairing requires an IPv4 device address")
        return AirPlayPairing(
            address=pairing_address,
            name=self.display_name,
            protocol=protocol,
            logger=self.logger,
            port=port,
            device_id=device_id,
        )

    def _on_player_media_updated(self) -> None:
        """Handle callback when the current media of the player is updated."""
        if not self.stream or not self.stream.running:
            return
        metadata = self.state.current_media
        if not metadata:
            return
        progress = int(metadata.corrected_elapsed_time or 0)
        self.mass.create_task(self.stream.send_metadata(progress, metadata))

    async def _get_session_pcm_format(
        self, sync_clients: list[AirPlayPlayer], media: PlayerMedia
    ) -> AudioFormat:
        """
        Select the shared PCM format for a new stream session.

        :param sync_clients: All players that will take part in the session.
        :param media: The media that is about to be played.
        """
        queue = self.mass.player_queues.get(media.source_id) if media.source_id else None
        queue_item = (
            self.mass.player_queues.get_item(media.source_id, media.queue_item_id)
            if media.source_id and media.queue_item_id
            else None
        )
        streamdetails = queue_item.streamdetails if queue_item else None
        crossfade_enabled = bool(
            queue
            and media.media_type == MediaType.TRACK
            and self.mass.streams.get_crossfade_mode(queue) != CrossfadeMode.DISABLED
        )
        return await self.mass.streams.audio.select_flow_pcm_format(
            self,
            start_streamdetails=streamdetails,
            crossfade_enabled=crossfade_enabled,
            overlay_active=bool(queue and overlay_active(queue)),
            fallback_sample_rate=AIRPLAY_PCM_FORMAT.sample_rate,
            output_players=sync_clients,
        )

    def _get_sync_clients(self) -> list[AirPlayPlayer]:
        """Get all sync clients for a player."""
        sync_clients: list[AirPlayPlayer] = []
        # we need to return the player itself too
        group_child_ids = {self.player_id}
        group_child_ids.update(self.group_members)
        for child_id in group_child_ids:
            if client := cast("AirPlayPlayer | None", self.mass.players.get_player(child_id)):
                sync_clients.append(client)
        return sync_clients

    async def _group_rejoin_attempts(self, candidate_ids: list[str]) -> None:
        """Re-join this player to its group's live session after a bounded backoff."""
        max_attempts = len(AIRPLAY_REJOIN_ATTEMPT_DELAYS)
        for attempt, delay in enumerate(AIRPLAY_REJOIN_ATTEMPT_DELAYS, start=1):
            await asyncio.sleep(delay)
            if (
                self.group_members
                or (self.stream and self.stream.running)
                or self.playback_state != PlaybackState.IDLE
                # synced into a group outside the original one = deliberate regroup.
                # Still pointing at an original candidate is fine: a static group
                # keeps the sync membership while only the session lost this player.
                or (self.synced_to and self.synced_to not in candidate_ids)
            ):
                # the player was grouped or repurposed by other means meanwhile
                self.logger.debug(
                    "Automatic group re-join for %s cancelled: player is active again",
                    self.display_name,
                )
                return
            if not self.available:
                # the device is offline: an attempt cannot succeed and the user
                # may well have switched it off on purpose
                self.logger.debug(
                    "Automatic group re-join for %s cancelled: player is unavailable",
                    self.display_name,
                )
                return
            target = self._resolve_rejoin_target(candidate_ids)
            if target is None:
                # the group may be between sessions (e.g. a track change); keep
                # trying until the attempts run out
                self.logger.debug(
                    "Automatic group re-join attempt %d/%d for %s: no playing group found",
                    attempt,
                    max_attempts,
                    self.display_name,
                )
                continue
            # When the sync membership survived the stream loss (a static group,
            # where membership is configuration), only the running session needs
            # healing; a group command would no-op on the existing membership.
            heal_session = (
                target.stream.session
                if self.player_id in target.group_members and target.stream is not None
                else None
            )
            try:
                if heal_session is not None:
                    await heal_session.add_client(self)
                else:
                    await self.mass.players.cmd_group(self.player_id, target.player_id)
            except Exception as err:
                self.logger.warning(
                    "Automatic re-join of %s to group of %s failed (attempt %d/%d): %s",
                    self.display_name,
                    target.display_name,
                    attempt,
                    max_attempts,
                    err,
                )
                continue
            # A failed late-join is swallowed inside the grouping path (the player
            # then holds group membership without a live stream), so verify the
            # session actually carries this player before declaring success.
            if (
                self.stream
                and self.stream.running
                and self.stream.session
                and self in self.stream.session.sync_clients
            ):
                self.logger.info(
                    "Automatically re-joined %s to the group of %s after stream loss",
                    self.display_name,
                    target.display_name,
                )
                return
            self.logger.warning(
                "Automatic re-join of %s did not produce a running stream (attempt %d/%d)",
                self.display_name,
                attempt,
                max_attempts,
            )
            if heal_session is None:
                # undo the group membership this attempt created so a retry (or
                # a manual regroup) starts from a clean join
                await self.mass.players.cmd_ungroup(self.player_id)
        self.logger.warning(
            "Giving up on automatic group re-join for %s after %d attempt(s); "
            "the player stays idle",
            self.display_name,
            max_attempts,
        )

    def _resolve_rejoin_target(self, candidate_ids: list[str]) -> AirPlayPlayer | None:
        """Resolve which player now carries the group's actively playing session."""
        for candidate_id in candidate_ids:
            candidate = self.mass.players.get_player(candidate_id)
            if candidate is None or candidate is self:
                continue
            if not isinstance(candidate, AirPlayPlayer):
                continue
            if candidate.synced_to:
                # the candidate was absorbed into another group since the loss
                # (user intent): never follow the old group's players elsewhere.
                # A leadership transfer inside the original group is still found:
                # the promoted member is itself one of the candidates.
                continue
            if not candidate.available:
                continue
            # only a PLAYING session can absorb a late joiner: a parked (paused)
            # session has no live timeline to anchor against
            if candidate.playback_state != PlaybackState.PLAYING:
                continue
            if not (candidate.stream and candidate.stream.running and candidate.stream.session):
                continue
            return candidate
        return None


class GenericAirPlayPlayer(AirPlayPlayer):
    """AirPlay protocol endpoint without independent device control."""

    _attr_type = PlayerType.PROTOCOL
