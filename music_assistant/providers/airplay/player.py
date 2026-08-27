"""AirPlay Player implementations."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import time
from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
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

from . import announce
from .constants import (
    AIRPLAY_DISCOVERY_TYPE,
    AIRPLAY_HIRES_AUDIO_FORMATS,
    AIRPLAY_HIRES_SAMPLE_RATES,
    AIRPLAY_PCM_FORMAT,
    AIRPLAY_REJOIN_ATTEMPT_DELAYS,
    AIRPLAY_VOLUME_ECHO_GRACE_S,
    BASE_PLAYER_FEATURES,
    CONF_AIRPLAY_CREDENTIALS,
    CONF_BUFFER_DEPTH,
    CONF_ENABLE_HIRES,
    CONF_ENCRYPTION,
    CONF_ENTRY_SYNC_ADJUST_AIRPLAY,
    CONF_IGNORE_VOLUME,
    CONF_PAIR_NOW,
    CONF_PAIRING_PASSWORD,
    CONF_PAIRING_PIN,
    CONF_PASSWORD,
    CONF_PASSWORD_INVALID,
    CONF_RAOP_CREDENTIALS,
    CONF_STORED_VOLUME,
    CONF_STREAMING_MODE,
    FALLBACK_VOLUME,
    LEGACY_PAIRING_BIT,
    PAIRING_PIN_FORMAT,
    PASSWORD_BIT,
    PIN_REQUIRED,
    RAOP_DISCOVERY_TYPE,
    STREAMING_MODE_AP2_COMPAT,
    STREAMING_MODE_AP2_NTP,
    STREAMING_MODE_AP2_PTP,
    STREAMING_MODE_AUTO,
    STREAMING_MODE_RAOP,
    StreamingProtocol,
)
from .helpers import (
    default_buffer_depth,
    default_hires_enabled,
    get_decoded_property,
    is_apple_device,
    is_macos_device,
    parse_airplay_features,
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
        # Serializes the two paths that can put a cliairplay process on this
        # receiver (the native stream session and the Sendspin bridge), from the
        # moment either decides to displace what is published until it publishes
        # its own stream. Two processes on one receiver reset each other's RTSP
        # channel and both sessions die. Always taken INSIDE self._lock, never
        # around it: an explicit stop holds self._lock while it tears a stream
        # down, and the reverse order would deadlock against it.
        self.stream_spawn_lock = asyncio.Lock()
        self.last_command_sent = 0.0
        self._volume_reports_ignored_until = 0.0
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
        # RAOP for legacy receivers (or when the RAOP streaming mode is set).
        if self._is_airplay2_capable and self.streaming_mode != STREAMING_MODE_RAOP:
            return StreamingProtocol.AIRPLAY2
        return StreamingProtocol.RAOP

    @property
    def streaming_mode(self) -> str:
        """
        Return the effective per-player streaming mode.

        Automatic unless the (advanced) streaming-mode setting pins a lane the
        device actually offers; a stored value the device no longer advertises
        falls back to Automatic rather than forcing an impossible route.
        """
        value = str(self.config.get_value(CONF_STREAMING_MODE, STREAMING_MODE_AUTO))
        offered = {option.value for option in self.streaming_mode_options}
        return value if value in offered else STREAMING_MODE_AUTO

    @property
    def streaming_mode_options(self) -> list[ConfigValueOption]:
        """
        Return the streaming-mode options this device can actually offer.

        Every option is an escape from the automatic AirPlay 2 route, gated on
        the device's own advertisements: the AirPlay 2 lanes need AirPlay 2
        capability (PTP timing additionally needs the SupportsPTP bit), and
        legacy RAOP needs an advertised _raop service to fall back to. A
        RAOP-only device has no alternative lane and keeps Automatic only,
        which hides the entry entirely. Apple receivers get every lane except
        NTP timing — they render silence on an NTP-timed realtime stream
        (hardware-measured). Of their lanes, the compatibility flow and
        legacy RAOP are the escapes for networks where the PTP ports are
        blocked; pinning PTP is an explicit choice of the normal lane.
        """
        options = [ConfigValueOption(STREAMING_MODE_AUTO, "Automatic (recommended)")]
        if not self._is_airplay2_capable:
            return options
        apple = is_apple_device(self.device_info.manufacturer, self.device_info.model)
        features = parse_airplay_features(self._advertised_features)
        if (features >> 41) & 1:
            options.append(ConfigValueOption(STREAMING_MODE_AP2_PTP, "AirPlay 2 - PTP timing"))
        if not apple:
            options.append(ConfigValueOption(STREAMING_MODE_AP2_NTP, "AirPlay 2 - NTP timing"))
        options.append(
            ConfigValueOption(STREAMING_MODE_AP2_COMPAT, "AirPlay 2 - compatibility mode")
        )
        if self.raop_discovery_info is not None:
            options.append(ConfigValueOption(STREAMING_MODE_RAOP, "AirPlay 1 (RAOP)"))
        return options

    @property
    def protocol_override(self) -> StreamingProtocol | None:
        """
        Return the user-forced streaming protocol, or None for automatic selection.

        Only the RAOP streaming mode forces the protocol outright; the AirPlay 2
        modes stay on the AirPlay 2 protocol and pin the flow/timing through the
        binary's --protocol/--timing arguments instead. Otherwise the cliairplay
        binary resolves the route itself from the mDNS TXT records (--protocol
        auto) and the ``protocol`` property above only reflects MA's own planning
        heuristic (timing, ports).
        """
        if self.streaming_mode == STREAMING_MODE_RAOP:
            return StreamingProtocol.RAOP
        return None

    @property
    def hires_playback_enabled(self) -> bool:
        """Return if 24-bit hi-res playback is possible and enabled for this player."""
        # 24-bit only works over the AirPlay 2 flow, so a device that streams RAOP
        # (a legacy receiver, or the force-RAOP escape hatch) stays on the 16-bit
        # base whatever it advertises.
        return (
            bool(self.advertised_audio_formats & AIRPLAY_HIRES_AUDIO_FORMATS)
            and self.protocol == StreamingProtocol.AIRPLAY2
            # the compat lane is 16-bit only, so hi-res stands down while the pin is active
            and self.streaming_mode != STREAMING_MODE_AP2_COMPAT
            and bool(self.config.get_value(CONF_ENABLE_HIRES, self._hires_default_enabled))
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
        # A stored password satisfies password protection on its own (the binary
        # authenticates with it directly; stored credentials are only its
        # fallback), so the password side is fully covered by the check above.
        if self.needs_password_setup:
            return True
        if self._requires_pin_pairing():
            # Credentials for either protocol keep the player usable: the binary
            # picks the best route for the credentials it has. Re-running the setup
            # flow from the player settings offers replacing a stored pairing.
            if not (
                self.get_setup_value(CONF_AIRPLAY_CREDENTIALS)
                or self.get_setup_value(CONF_RAOP_CREDENTIALS)
            ):
                return True
        return False

    @property
    def setup_reason(self) -> str | None:
        """Return why the player needs setup, or None when it is ready to use."""
        if not self.needs_setup:
            return None
        return "password_required" if self.needs_password_setup else "pairing_required"

    @property
    def password_required(self) -> bool:
        """Return if the device announces that it is password protected."""
        # Two announcement forms, verified against live devices (including Apple
        # TVs, which raise the password bit only while a password is actually
        # set): receivers publish the password bit in sf/flags and/or the classic
        # pw boolean. Enforcement can also exist WITHOUT any announcement (stale
        # TXT after the password was enabled); that case is caught at connect
        # time via password_invalid.
        if self._get_flags() & PASSWORD_BIT:
            return True
        if raop_info := self.raop_discovery_info:
            return (raop_info.decoded_properties.get("pw") or "").lower() == "true"
        return False

    @property
    def password_invalid(self) -> bool:
        """Return if the device rejected the stored password on its last connect."""
        return bool(
            self.mass.config.get_raw_player_config_value(
                self.player_id, CONF_PASSWORD_INVALID, False
            )
        )

    @property
    def needs_password_setup(self) -> bool:
        """Return if the device password still has to be entered through the setup flow."""
        # The password is only ever entered through the setup flow, so both a
        # device that announces password protection without one stored and a
        # password the device rejected must send the user back into that flow.
        if self.password_invalid:
            return True
        return self.password_required and not self.config.get_value(CONF_PASSWORD)

    def set_password_invalid(self, invalid: bool) -> None:
        """
        Persist (or clear) the marker that the device rejected the stored password.

        :param invalid: True when the device rejected the password, False once a
            connect succeeded or a new password was stored.
        """
        if self.password_invalid == invalid:
            # keeps a successful connect from writing the config on every stream
            return
        self.mass.config.set_raw_player_config_value(self.player_id, CONF_PASSWORD_INVALID, invalid)
        # needs_setup/setup_reason are part of the player's own state inputs, so a
        # plain update publishes the (dis)appeared setup action to the clients.
        self.update_state()

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
        features = {*BASE_PLAYER_FEATURES, PlayerFeature.PAUSE}
        # An announcement is mixed into the audio the player is already rendering, so
        # the feature is only offered while there is live playback to mix into. Without
        # it the players controller plays the announcement its own way, which leaves
        # the device to whatever else may be streaming to it.
        if not self.has_live_audio:
            features.discard(PlayerFeature.PLAY_ANNOUNCEMENT)
        return features

    @property
    def has_live_audio(self) -> bool:
        """Return True if the player is rendering audio an announcement can mix into."""
        if self.playback_state != PlaybackState.PLAYING:
            return False
        return self.stream is not None and self.stream.running and self.stream.connected

    @property
    def applies_announcement_volume(self) -> bool:
        """Return True: the announcement volume is applied around the mixed clip."""
        return True

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
    def native_grouping_requires_own_stream(self) -> bool:
        """Return True: members are attached to this player's own stream session."""
        return True

    @property
    def live_session_members(self) -> list[str]:
        """Return the id's of the players the running stream session feeds."""
        # group membership is bookkeeping that outlives the session: a member can be
        # dropped from the session (write failures) or never make it in (a refused
        # late join) while still being listed as part of the group, and without a
        # session there is nobody to render with at all
        if self.stream and self.stream.running and self.stream.session:
            return [x.player_id for x in self.stream.session.sync_clients]
        return []

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        # Pairing/credentials are no longer config entries: they are collected by the
        # interactive setup flow (run_setup_flow) and stored in the player's setup_data.
        base_entries: list[ConfigEntry] = []

        # Effective RAOP state from the current (stored) streaming mode, so the
        # RAOP-only entries show/hide consistently with it.
        is_raop = self.protocol == StreamingProtocol.RAOP

        # Streaming-mode escape hatch: a per-device pin of the protocol/timing
        # lane for receivers whose automatic route misbehaves. Only offered
        # when the device actually has a lane to choose (Apple receivers are
        # always native AirPlay 2 with PTP and get no entry).
        mode_options = self.streaming_mode_options
        if len(mode_options) > 1:
            base_entries.append(
                ConfigEntry(
                    key=CONF_STREAMING_MODE,
                    type=ConfigEntryType.STRING,
                    options=mode_options,
                    default_value=STREAMING_MODE_AUTO,
                    category="protocol_generic",
                    advanced=True,
                )
            )

        # 24-bit toggle, shown only when the device advertises 24-bit support
        # (per-device default: see default_hires_enabled). Hidden rather than
        # omitted when it does not: the formats are probed async after
        # registration, and an entry absent from the registration-time config
        # parse would drop the user's stored value until the next config save.
        base_entries.append(
            ConfigEntry(
                key=CONF_ENABLE_HIRES,
                type=ConfigEntryType.BOOLEAN,
                default_value=self._hires_default_enabled,
                hidden=not self.advertised_audio_formats & AIRPLAY_HIRES_AUDIO_FORMATS,
                category="protocol_generic",
                requires_reload=True,
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
                # Storage (and encryption) vehicle only: the device password is
                # entered through the setup flow, which is also what a wrong
                # password sends the user back to. A hidden entry keeps its stored
                # value across config saves (the frontend never submits it).
                hidden=True,
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
            # Receiver-queue depth presets. The range reaches past the standard
            # 2 s receiver buffer because that figure is only what the binary
            # assumes for a device that reports no window of its own, and the
            # deepest starving devices ask for more than the assumption. The
            # default comes from the device-family table, and Automatic resolves
            # through that same table at stream time, so selecting it never
            # downgrades an affected device.
            ConfigEntry(
                key=CONF_BUFFER_DEPTH,
                type=ConfigEntryType.INTEGER,
                options=[
                    ConfigValueOption(0),
                    ConfigValueOption(500),
                    ConfigValueOption(750),
                    ConfigValueOption(1000),
                    ConfigValueOption(1500),
                    ConfigValueOption(1750),
                    ConfigValueOption(2000),
                    ConfigValueOption(2500),
                    ConfigValueOption(3000),
                ],
                default_value=default_buffer_depth(
                    self.device_info.manufacturer or "",
                    self.device_info.model or "",
                    get_decoded_property(self.airplay_discovery_info, "fv")
                    if self.airplay_discovery_info
                    else None,
                ),
                category="protocol_generic",
                advanced=True,
                requires_reload=True,
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
                # Sendspin bridge active: it tears the transport down straight
                # away and takes the player out of the Sendspin session
                pass
            elif self.stream and self.stream.running:
                # Fallback: stop protocol directly
                await self.stream.stop(force=True)
                self.stream = None
            self._attr_current_media = None
            self.update_state()

    async def play(self) -> None:
        """Handle PLAY (unpause) command on the player."""
        session = self.stream.session if self.stream and self.stream.running else None
        if self.group_members or self.synced_to or (session and session.parked):
            # Grouped pause parks the whole session (standby); unpausing one
            # member cannot restart the group in sync, and a parked member is
            # held with nothing being fed until a re-anchor - which ACTION=PLAY
            # does not carry, so it would report playback over silence. The park
            # outlives the group, so a player left alone by an ungroup is keyed
            # on the park itself, not on its membership. Resume via the queue
            # instead: play_media flushes and re-anchors every parked member at
            # one shared instant. The queue can belong to a linked native parent
            # (for example Sonos), so resolve it instead of using the AirPlay ID.
            active_queue = self.mass.players.get_active_queue(self)
            if active_queue is None:
                raise PlayerCommandFailed(
                    f"Cannot resume AirPlay player {self.display_name} without an active queue"
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
                    media, session_pcm_format, self.player_id
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
                            member.on_player_media_updated,
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
            audio_source = self.mass.streams.get_stream(media, session_pcm_format, self.player_id)

            # setup StreamSession for player (and its sync childs if any)
            provider = cast("AirPlayProvider", self.provider)
            stream_session = AirPlayStreamSession(
                provider,
                sync_clients,
                session_pcm_format,
                media,
            )
            await stream_session.start(audio_source)
            self._transitioning = False

    async def play_announcement(
        self, announcement: PlayerMedia, volume_level: int | None = None
    ) -> None:
        """
        Play an announcement natively, mixed over the audio the player is rendering.

        :param announcement: Details of the announcement that needs to be played.
        :param volume_level: Optional volume level for the announcement.
        """
        # The lock windows live inside the orchestration: the dispatch decision and
        # the arming hold self._lock like play_media does, while the multi-second
        # clip waits run outside it (see announce.py).
        await announce.play_announcement(self, announcement, volume_level)

    async def volume_set(self, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        # Record before sending: the connect-time volume push reads this attribute,
        # so a send that suspends first would let that push send the stale level.
        self._attr_volume_level = volume_level
        if self.stream and self.stream.running and self.volume_muted is not True:
            await self.stream.send_cli_command(f"VOLUME={volume_level}")
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
                    # Callers only ask for this leader alone or for the whole group at once.
                    # A partial self+subset removal would need the other requested members
                    # released here as well, instead of returning right after the leader.
                    remaining_members = [
                        member_id
                        for member_id in self._attr_group_members
                        if member_id != self.player_id and member_id not in player_ids_to_remove
                    ]
                    if stream_session and remaining_members:
                        # Members stay behind: remove only this leader client,
                        # the session continues for the remaining players
                        await stream_session.remove_client(self, reason="leader removed from group")
                    elif stream_session:
                        # The whole group is being removed, tear the session down
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
                elif self.active_output_protocol not in (None, "native"):
                    # Members can only be attached to this player's own stream session, which
                    # does not exist while it renders through one of its output protocols.
                    self.logger.warning(
                        "%s joined the group of %s while that player renders through another "
                        "output protocol: there is no stream session to join, so it stays silent",
                        child_player_to_add.display_name if child_player_to_add else player_id,
                        self.display_name,
                    )

            # Ensure group leader includes itself in group_members when it has members
            # This is required for the synced_to property to work correctly
            if self._attr_group_members and self.player_id not in self._attr_group_members:
                self._attr_group_members.insert(0, self.player_id)

            # always update the state after modifying group members
            self.update_state()

    @property
    def ignore_volume_reports(self) -> bool:
        """Return True if the device's own volume reports must not be acted on."""
        if self._volume_reports_ignored_until > time.time():
            # a level we sent ourselves is still echoing back
            return True
        return bool(
            self.config.get_value(CONF_IGNORE_VOLUME)
            or self.device_info.manufacturer.lower() == "apple"
        )

    def suppress_volume_reports(self, seconds: float = AIRPLAY_VOLUME_ECHO_GRACE_S) -> None:
        """
        Ignore the device's own volume reports for the given time.

        :param seconds: How long from now the reports are ignored; a window that is
            already open is only ever extended.
        """
        self._volume_reports_ignored_until = max(
            self._volume_reports_ignored_until, time.time() + seconds
        )

    def update_volume_from_device(self, volume: int) -> None:
        """Update volume from device feedback."""
        if self.ignore_volume_reports:
            return

        cur_volume = self.volume_level or 0
        if abs(cur_volume - volume) > 1 or (time.time() - self.last_command_sent) > 3:
            self.mass.create_task(self._adopt_device_volume(volume))
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
        # The stream reclaims the device: an external (Companion-observed)
        # source snapshot can leak in during a brief stream-restart window and
        # would otherwise stick, freezing the UI on a stale "external source"
        # view while we stream. While MA streams, the stream is the sole
        # authority on this player's state.
        active_source = getattr(self, "_attr_active_source", None)
        if active_source is not None and active_source in getattr(self, "_external_source_ids", ()):
            media = getattr(self, "_attr_current_media", None)
            if media is not None and media.source_id == active_source:
                self._attr_current_media = None
            self._attr_active_source = None
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

    @property
    def owns_volume(self) -> bool:
        """
        Return True if this output is the resolved owner of its own volume.

        AirPlay volume is the receiver's own volume: setting it writes through to the
        device and persists there after the session ends. It may therefore only be set
        when no other control owns the volume of this output.
        """
        if not (parent_id := self.protocol_parent_id):
            # a standalone AirPlay player has no other interface to defer to
            return True
        if not (parent_player := self.mass.players.get_player(parent_id)):
            return True
        return self._control_routes_to_self(parent_player.volume_control_for_output(self.player_id))

    def release_foreign_mute_latch(self) -> None:
        """Clear our mute latch when another control owns the mute of this output."""
        if not self._attr_volume_muted:
            # nothing latched, so nothing that could silence this stream
            return
        if not (parent_id := self.protocol_parent_id):
            return
        if not (parent_player := self.mass.players.get_player(parent_id)):
            return
        if self._control_routes_to_self(parent_player.mute_control_for_output(self.player_id)):
            # our own mute, applied through the parent
            return
        # The mute belongs to a control that does not own this output (a sibling interface,
        # the receiver itself, or nothing at all). Our mute is a latch that only an explicit
        # unmute clears, so leaving it set would report a mute we do not own and turn the
        # next volume command into a silent one.
        self._attr_volume_muted = False
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

    def on_player_media_updated(self) -> None:
        """Handle callback when the current media of the player is updated."""
        if not self.stream or not self.stream.running:
            return
        metadata = self.state.current_media
        if not metadata:
            return
        progress = int(metadata.corrected_elapsed_time or 0)
        self.mass.create_task(self.stream.send_metadata(progress, metadata))

    async def _adopt_device_volume(self, volume: int) -> None:
        """
        Take over a level the device set itself.

        :param volume: The level the device reported.
        """
        ignored_until = self._volume_reports_ignored_until
        await self.volume_set(volume)
        # Writing the level back is a volume command like any other and opens the echo
        # window, but this one only hands the device its own level: leaving the window
        # open would swallow the rest of a volume the user is still turning up. A longer
        # window opened while this was in flight (an announcement) still stands.
        if self._volume_reports_ignored_until <= time.time() + AIRPLAY_VOLUME_ECHO_GRACE_S:
            self._volume_reports_ignored_until = ignored_until

    def _control_routes_to_self(self, control: str) -> bool:
        """Return True if the given (resolved) control routes to this player."""
        if control == self.player_id:
            return True
        # bridge players riding on this player (e.g. Sendspin-over-AirPlay) forward to us
        if control_player := self.mass.players.get_player(control):
            return control_player.underlying_player_id == self.player_id
        return False

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

    async def _run_streaming_pairing(
        self, session: SetupSession, collected: dict[str, ConfigValueType]
    ) -> None:
        """
        Pair the streaming protocol (RAOP or AirPlay 2) and collect the device password.

        The two are evaluated independently: a device that is already paired can
        still be missing its password (or have had it rejected), which is exactly
        the state a receiver ends up in when it gains password protection after
        it was set up.

        :param session: The setup flow session used to interact with the user.
        :param collected: The values collected so far; updated in place.
        """
        password_collected = await self._run_protocol_pairing(session, collected)
        if not password_collected and self.needs_password_setup:
            await self._ask_device_password(session)

    async def _run_protocol_pairing(
        self, session: SetupSession, collected: dict[str, ConfigValueType]
    ) -> bool:
        """
        Pair the streaming protocol (RAOP or AirPlay 2), unless already paired.

        When the device requires pairing this runs it, re-offering it as a skippable
        step when credentials are already stored (so a re-launched flow can replace a
        stale pairing). When the device requires no pairing, any leftover credentials
        are cleared: they would keep forcing the pair-verify route, which some
        receivers (e.g. HomePods after their password was removed) accept while
        refusing to actually output audio. The obtained credentials are added to
        ``collected`` under the protocol-specific key.

        :param session: The setup flow session used to interact with the user.
        :param collected: The values collected so far; updated in place.
        :return: Whether the device password was collected as part of the pairing.
        """
        pin_pairing = self._requires_pin_pairing()
        # a password only replaces PIN pairing on the native AirPlay 2 flow
        password_pairing = self.password_required and self.protocol == StreamingProtocol.AIRPLAY2
        if not (pin_pairing or password_pairing):
            for cred_key in (CONF_AIRPLAY_CREDENTIALS, CONF_RAOP_CREDENTIALS):
                if self.get_setup_value(cred_key) is not None:
                    collected[cred_key] = None
            return False
        already_paired = bool(
            self.get_setup_value(CONF_AIRPLAY_CREDENTIALS)
            or self.get_setup_value(CONF_RAOP_CREDENTIALS)
        )
        if already_paired and not await self._offer_optional_pairing(
            session, "streaming_repair_offer"
        ):
            return False

        protocol = self.protocol
        cred_key = self._get_credentials_key(protocol)
        if pin_pairing:
            step_id, field_key, field_type, field_format = (
                "pair_pin",
                CONF_PAIRING_PIN,
                ConfigEntryType.PAIRING_CODE,
                PAIRING_PIN_FORMAT,
            )
        else:
            step_id, field_key, field_type, field_format = (
                "pair_password",
                CONF_PAIRING_PASSWORD,
                ConfigEntryType.SECURE_STRING,
                None,
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
                            format=field_format,
                        )
                    ],
                    step_id=step_id,
                    errors=errors,
                )
                entered_value = str(values[field_key])
                credentials = await pairing.finish_pairing(pin=entered_value)
            except PlayerCommandFailed as err:
                # leave a default-level trace: the flow swallows the error into
                # the re-served form, which support logs otherwise never show
                self.logger.warning("Pairing with %s failed: %s", self.display_name, err)
                errors = {"base": err.translation_key or str(err)}
                continue
            finally:
                # tears down the subprocess on retry, success and abort (cancellation)
                await pairing.close()
            collected[cred_key] = credentials
            if password_pairing:
                # The device password authenticates every later stream too (the
                # binary's transient leg), so keep it next to the credentials
                # instead of discarding it with the setup form.
                self._store_device_password(entered_value)
            return password_pairing

    async def _ask_device_password(self, session: SetupSession) -> None:
        """
        Ask for the device password and store it, without attempting any pairing.

        Covers the devices that have no pairing to do: a legacy RAOP receiver, and
        an already paired device whose password is missing or was rejected. There
        is no live session to validate the entry against, so a wrong password only
        surfaces on the next connect - which marks the player as needing setup again.

        :param session: The setup flow session used to interact with the user.
        """
        values = await session.form(
            [
                ConfigEntry(
                    key=CONF_PAIRING_PASSWORD,
                    type=ConfigEntryType.SECURE_STRING,
                    required=True,
                    category="protocol_generic",
                )
            ],
            step_id="pair_password",
        )
        self._store_device_password(str(values[CONF_PAIRING_PASSWORD]))

    async def _offer_optional_pairing(self, session: SetupSession, step_id: str) -> bool:
        """
        Ask whether to run the offered (optional) pairing now.

        :param session: The setup flow session used to interact with the user.
        :param step_id: The (i18n) step id describing the offered pairing.
        """
        values = await session.form(
            [
                ConfigEntry(
                    key=CONF_PAIR_NOW,
                    type=ConfigEntryType.BOOLEAN,
                    default_value=False,
                    category="protocol_generic",
                )
            ],
            step_id=step_id,
        )
        return bool(values[CONF_PAIR_NOW])

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

    def _store_device_password(self, password: str) -> None:
        """
        Persist a device password so every later stream can authenticate with it.

        :param password: The plaintext password entered by the user.
        """
        self.mass.config.set_raw_player_config_value(
            self.player_id, CONF_PASSWORD, self.mass.config.encrypt_string(password)
        )
        # a freshly entered password deserves a clean slate: the reject marker
        # would otherwise keep the player in "needs setup" until the next connect
        self.set_password_invalid(False)

    @property
    def _hires_default_enabled(self) -> bool:
        """Return the per-device default for the 24-bit toggle."""
        return default_hires_enabled(
            self.device_info.manufacturer or "", self.device_info.model or ""
        )


class GenericAirPlayPlayer(AirPlayPlayer):
    """AirPlay protocol endpoint without independent device control."""

    _attr_type = PlayerType.PROTOCOL
