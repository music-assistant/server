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
    IdentifierType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
)
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.media_items import AudioFormat

from music_assistant.helpers.util import get_primary_ip_address_from_zeroconf, is_valid_mac_address
from music_assistant.models.player import DeviceInfo, Player, PlayerMedia

from .constants import (
    AIRPLAY_AP2_SETUP_LEAD_MS,
    AIRPLAY_DISCOVERY_TYPE,
    AIRPLAY_FLOW_PCM_FORMAT,
    AIRPLAY_HIRES_SAMPLE_RATES,
    AIRPLAY_PCM_FORMAT,
    AIRPLAY_RAOP_SETUP_LEAD_MS,
    BASE_PLAYER_FEATURES,
    CONF_ACTION_FINISH_PAIRING,
    CONF_ACTION_RESET_PAIRING,
    CONF_ACTION_START_PAIRING,
    CONF_AIRPLAY_CREDENTIALS,
    CONF_AP2PASSWORD,
    CONF_ENCRYPTION,
    CONF_ENTRY_SYNC_ADJUST_AIRPLAY,
    CONF_FORCE_RAOP,
    CONF_HIRES_PLAYBACK,
    CONF_IGNORE_VOLUME,
    CONF_LEGACY_FORCE_RAOP,
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
    player_id_to_mac_address,
    supports_airplay2,
)
from .stream_session import AirPlayStreamSession

if TYPE_CHECKING:
    from zeroconf.asyncio import AsyncServiceInfo

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
        super().__init__(provider, player_id)
        self.address = address
        self.stream: AirPlayStream | None = None
        self.last_command_sent = 0.0
        self._lock = asyncio.Lock()
        self._active_pairing: AirPlayPairing | None = None
        self._transitioning = False  # Set during stream replacement to ignore stale DACP messages
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
        self._attr_enabled_by_default = True

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
        misbehaves). Legacy RAOP preferences are also preserved for Apple devices.
        Otherwise the cliairplay binary resolves the route itself from the mDNS TXT
        records (--protocol auto) and the ``protocol`` property above only reflects
        MA's own planning heuristic (timing, ports).
        """
        return StreamingProtocol.RAOP if self._force_raop_active else None

    @property
    def hires_playback_enabled(self) -> bool:
        """Return if 24-bit hi-res playback is enabled (and possible) for this player."""
        # 24-bit only works over the native AirPlay 2 flow, so the opt-in is
        # only effective for AirPlay 2 capable devices that are not forced to RAOP.
        return (
            bool(self.config.get_value(CONF_HIRES_PLAYBACK, False))
            and self.airplay_discovery_info is not None
            and self.protocol_override != StreamingProtocol.RAOP
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
                self.config.get_value(CONF_AIRPLAY_CREDENTIALS)
                or self.config.get_value(CONF_RAOP_CREDENTIALS)
            ):
                return True
        return False

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
        """Get the lead time in ms between starting the stream and the audible start."""
        # the binary owns all lead/buffer handling from the chosen start instant;
        # MA only budgets a fixed setup lead for spawn + connect + session setup
        # + receiver pre-fill. Native AirPlay 2 needs a larger budget than RAOP
        # (its pre-fill is paced), otherwise the start clips intermittently.
        if self.protocol == StreamingProtocol.RAOP:
            return AIRPLAY_RAOP_SETUP_LEAD_MS
        return AIRPLAY_AP2_SETUP_LEAD_MS

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        base_entries: list[ConfigEntry] = []
        require_authentication = self._requires_pin_pairing() or self._requires_password_pairing()

        # Handle pairing actions
        if action and require_authentication:
            await self._handle_pairing_action(action=action, values=values)

        # Add pairing config entries for Apple TV and macOS devices
        if require_authentication:
            base_entries = [*self._get_pairing_config_entries(values)]

        # Effective RAOP state reflects the force-RAOP toggle currently in the form
        # (falling back to stored config) so the RAOP device password and the hi-res
        # option show/hide consistently with it, not against stale stored state.
        if values is not None and CONF_FORCE_RAOP in values:
            force_raop = self._force_raop_available and bool(values[CONF_FORCE_RAOP])
        else:
            force_raop = self._force_raop_active
        is_raop = force_raop or not self._is_airplay2_capable

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
            ConfigEntry(
                key=CONF_HIRES_PLAYBACK,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                # 24-bit requires the native AirPlay 2 flow, so the option is only
                # offered for AirPlay 2 capable devices that are not forced to RAOP
                hidden=not self.airplay_discovery_info or is_raop,
                category="protocol_generic",
                advanced=True,
            ),
        ]

        return base_entries

    async def stop(self) -> None:
        """Send STOP command to player."""
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
        async with self._lock:
            if self.stream and self.stream.running:
                await self.stream.send_cli_command("ACTION=PLAY")

    async def pause(self) -> None:
        """Send PAUSE command to player."""
        if self.group_members or self.synced_to:
            # Each member of a sync group is an independent cliairplay process anchored
            # to a shared start instant; a broadcast pause/resume cannot keep the members
            # sample-aligned on resume. So grouped/synced playback is paused by stopping
            # the whole session and letting the queue controller resume it from the saved
            # position with a fresh shared anchor.
            self.logger.debug("Player is part of a sync group, using STOP instead of PAUSE")
            await self.stop()
            return

        async with self._lock:
            if not self.stream or not self.stream.running:
                return
            await self.stream.send_cli_command("ACTION=PAUSE")

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on given player."""
        async with self._lock:
            if self.synced_to:
                # this should not happen, but guard anyways
                raise RuntimeError("Player is synced")
            self._attr_current_media = media

            # Always stop any existing stream
            if self.stream and self.stream.running and self.stream.session:
                # Set transitioning flag to ignore stale DACP messages (like prevent-playback)
                self._transitioning = True
                await self.stream.session.stop()
                self.stream = None

            # select audio source
            sync_clients = self._get_sync_clients()
            session_pcm_format = self._get_session_pcm_format(sync_clients, media)
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
        if self.stream:
            # remove this player from the stream session if it is running
            if self.stream.running and self.stream.session:
                await self.stream.session.remove_client(self, reason="player unloaded")
            self.stream = None
        if self._active_pairing:
            await self._active_pairing.close()
            self._active_pairing = None

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
        so the toggle is never offered for them; only an explicitly migrated legacy
        preference can still force RAOP there. RAOP-only and AirPlay-2-only devices
        have nothing to force, so they are excluded as well.
        """
        return (
            self._is_airplay2_capable
            and self.raop_discovery_info is not None
            and not is_apple_device(self.device_info.manufacturer, self.device_info.model)
        )

    @property
    def _force_raop_active(self) -> bool:
        """Return whether RAOP is being forced through the escape-hatch toggle."""
        if self._force_raop_available and self.config.get_value(CONF_FORCE_RAOP, False):
            return True
        return (
            self.raop_discovery_info is not None
            and is_apple_device(self.device_info.manufacturer, self.device_info.model)
            and self.provider.mass.config.get_raw_player_config_value(
                self.player_id, CONF_LEGACY_FORCE_RAOP, False
            )
            is True
        )

    def _get_pairing_config_entries(
        self, values: dict[str, ConfigValueType] | None
    ) -> list[ConfigEntry]:
        """
        Return pairing config entries for Apple TV and macOS devices.

        Uses native pairing for both AirPlay 2 (HAP) and RAOP protocols.
        """
        self.logger.debug(f"_get_pairing_config_entries with values: {values}")
        entries: list[ConfigEntry] = []

        # Pairing flavor follows capability detection: an AirPlay 2 capable device
        # (always the case for a genuine HomePod / Apple TV 4+) pairs over HAP
        # ("AirPlay"), while a legacy RAOP-only device (including older Apple TVs)
        # uses the RAOP pairing flavor. Non-Apple AirPlay 2 receivers essentially
        # never require pairing, so the force-RAOP toggle never reaches this path.
        protocol = self.protocol
        protocol_name = "RAOP" if protocol == StreamingProtocol.RAOP else "AirPlay"
        protocol_key = (
            CONF_RAOP_CREDENTIALS
            if protocol == StreamingProtocol.RAOP
            else CONF_AIRPLAY_CREDENTIALS
        )
        has_creds_for_current_protocol = (
            values.get(protocol_key) if values else self.config.get_value(protocol_key)
        )
        self.logger.debug(
            f"Has credentials for {protocol_name}: {has_creds_for_current_protocol!s}"
        )

        if not has_creds_for_current_protocol:
            # If pairing was started, show PIN or password entry (depending on device configuration)
            if self._active_pairing and self._active_pairing.is_pairing:
                if self._requires_pin_pairing():
                    self.logger.debug(f"Device requires PIN pairing for {protocol_name}")
                    entries.append(
                        ConfigEntry(
                            key=CONF_PAIRING_PIN,
                            type=ConfigEntryType.STRING,
                            required=True,
                            category="protocol_generic",
                        )
                    )
                    entries.append(
                        ConfigEntry(
                            key=CONF_ACTION_FINISH_PAIRING,
                            type=ConfigEntryType.ACTION,
                            translation_key="finish_pairing_pin",
                            translation_params=[protocol_name],
                            action=CONF_ACTION_FINISH_PAIRING,
                            category="protocol_generic",
                        )
                    )
                elif self._requires_password_pairing():
                    self.logger.debug(f"Device requires password pairing for {protocol_name}")
                    entries.append(
                        ConfigEntry(
                            key=CONF_PAIRING_PASSWORD,
                            type=ConfigEntryType.SECURE_STRING,
                            required=True,
                            category="protocol_generic",
                        )
                    )
                    entries.append(
                        ConfigEntry(
                            key=CONF_ACTION_FINISH_PAIRING,
                            type=ConfigEntryType.ACTION,
                            translation_key="finish_pairing_password",
                            translation_params=[protocol_name],
                            action=CONF_ACTION_FINISH_PAIRING,
                            category="protocol_generic",
                        )
                    )
            else:
                # Show pairing instructions and start button
                self.logger.debug(
                    f"Device requires pairing for {protocol_name}, but no active pairing session"
                )
                entries.append(
                    ConfigEntry(
                        key="pairing_instructions",
                        type=ConfigEntryType.LABEL,
                        translation_params=[protocol_name],
                        category="protocol_generic",
                    )
                )
                entries.append(
                    ConfigEntry(
                        key=CONF_ACTION_START_PAIRING,
                        type=ConfigEntryType.ACTION,
                        translation_key="start_pairing",
                        translation_params=[protocol_name],
                        action=CONF_ACTION_START_PAIRING,
                        category="protocol_generic",
                    )
                )
        else:
            self.logger.debug(f"Device is already paired for {protocol_name}, showing reset option")
            # Show paired status
            entries.append(
                ConfigEntry(
                    key="pairing_status",
                    type=ConfigEntryType.LABEL,
                    translation_params=[protocol_name],
                    category="protocol_generic",
                )
            )
            # Add reset pairing button
            entries.append(
                ConfigEntry(
                    key=CONF_ACTION_RESET_PAIRING,
                    type=ConfigEntryType.ACTION,
                    translation_key="reset_pairing",
                    translation_params=[protocol_name],
                    action=CONF_ACTION_RESET_PAIRING,
                    category="protocol_generic",
                )
            )

        # Store credentials (hidden from UI)
        for protocol in (StreamingProtocol.RAOP, StreamingProtocol.AIRPLAY2):
            conf_key = self._get_credentials_key(protocol)
            entries.append(
                ConfigEntry(
                    key=conf_key,
                    type=ConfigEntryType.SECURE_STRING,
                    label=conf_key,
                    default_value=None,
                    value=values.get(conf_key) if values else None,
                    required=False,
                    hidden=True,
                    category="protocol_generic",
                )
            )
            if protocol is StreamingProtocol.AIRPLAY2:
                entries.append(
                    ConfigEntry(
                        key=CONF_AP2PASSWORD,
                        type=ConfigEntryType.SECURE_STRING,
                        label=CONF_AP2PASSWORD,
                        default_value=None,
                        value=values.get(CONF_PAIRING_PASSWORD) if values else None,
                        required=False,
                        hidden=True,
                        category="protocol_generic",
                    )
                )
        return entries

    async def _handle_pairing_action(
        self, action: str, values: dict[str, ConfigValueType] | None
    ) -> None:
        """
        Handle pairing actions.

        Uses native pairing for both AirPlay 2 (HAP) and RAOP protocols.
        Both produce credentials compatible with cliairplay.
        """
        self.logger.debug(f"_handle_pairing_action with action: {action} and values: {values}")
        # Pair with the flavor matching the resolved streaming protocol
        # (see _get_pairing_config_entries for the capability-based rationale).
        protocol = self.protocol
        protocol_name = "RAOP" if protocol == StreamingProtocol.RAOP else "AirPlay"

        if action == CONF_ACTION_START_PAIRING:
            await self._reset_pairing(values, protocol, protocol_name)
            await self._start_pairing(protocol, protocol_name)
        elif action == CONF_ACTION_FINISH_PAIRING:
            await self._finish_pairing(values, protocol, protocol_name)
        elif action == CONF_ACTION_RESET_PAIRING:
            await self._reset_pairing(values, protocol, protocol_name)

    async def _start_pairing(self, protocol: StreamingProtocol, protocol_name: str) -> None:
        """Begin a new pairing session for the given protocol."""
        self.logger.debug(f"_start_pairing for protocol: {protocol_name}")
        if self._active_pairing and self._active_pairing.is_pairing:
            self.logger.warning("Pairing process already in progress for %s", self.display_name)
            return

        self.logger.info("Starting %s pairing for %s", protocol_name, self.display_name)

        from .pairing import AirPlayPairing  # noqa: PLC0415

        # Determine port based on protocol
        # Note: For Apple devices, pairing always happens on the AirPlay port (7000)
        # even when streaming will use RAOP. The RAOP port (5000) is only for streaming.
        port: int | None = None
        if self.airplay_discovery_info:
            port = self.airplay_discovery_info.port or 7000
        elif self.raop_discovery_info:
            # Fallback for devices without AirPlay service
            port = self.raop_discovery_info.port or 5000
        # Get the DACP ID from the provider - must match what cliairplay uses
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

        self._active_pairing = AirPlayPairing(
            address=pairing_address,
            name=self.display_name,
            protocol=protocol,
            logger=self.logger,
            port=port,
            device_id=device_id,
        )
        await self._active_pairing.start_pairing_session()

        if self._requires_pin_pairing():
            await self._active_pairing.start_pin_pairing()

    async def _finish_pairing(
        self,
        values: dict[str, ConfigValueType] | None,
        protocol: StreamingProtocol,
        protocol_name: str,
    ) -> None:
        """
        Complete an in-progress pairing session.

        ``values`` may contain a PIN or a password supplied by the user when required.
        """
        self.logger.debug(f"_finish_pairing for protocol: {protocol_name} with values: {values}")
        if not values:
            return
        pin = None
        if self._requires_pin_pairing():
            pin = values.get(CONF_PAIRING_PIN)
            if not pin:
                self.logger.warning("No PIN provided for pairing")
                return
        elif self._requires_password_pairing():
            pin = values.get(CONF_PAIRING_PASSWORD)
            if not pin:
                self.logger.warning("No password configured for pairing")
                return

        if not self._active_pairing:
            self.logger.warning(f"No active pairing session for {self.display_name}")
            return
        if not pin:
            self.logger.warning("No authentication method provided (PIN or password)")
            return
        credentials = await self._active_pairing.finish_pairing(pin=str(pin))
        self._active_pairing = None

        # Store credentials with the protocol-specific key
        cred_key = self._get_credentials_key(protocol)
        values[cred_key] = credentials

        self.logger.info(f"Finished {protocol_name} pairing for {self.display_name}")

    async def _reset_pairing(
        self,
        values: dict[str, ConfigValueType] | None,
        protocol: StreamingProtocol,
        protocol_name: str,
    ) -> None:
        """Clear stored credentials for the given protocol."""
        cred_key = self._get_credentials_key(protocol)
        self.logger.info(f"Resetting {protocol_name} pairing for {self.display_name}")
        if values is not None:
            values[cred_key] = None
            values[CONF_AP2PASSWORD] = None
        self.config.update({cred_key: None, CONF_AP2PASSWORD: None})

    def _on_player_media_updated(self) -> None:
        """Handle callback when the current media of the player is updated."""
        if not self.stream or not self.stream.running:
            return
        metadata = self.state.current_media
        if not metadata:
            return
        progress = int(metadata.corrected_elapsed_time or 0)
        self.mass.create_task(self.stream.send_metadata(progress, metadata))

    def _get_session_pcm_format(
        self, sync_clients: list[AirPlayPlayer], media: PlayerMedia
    ) -> AudioFormat:
        """
        Select the session (flow) PCM format for a new stream session.

        :param sync_clients: All players that will take part in the session.
        :param media: The media that is about to be played.
        """
        # The session runs at 48 kHz only when every member supports it (hi-res
        # enabled); any 16-bit/44.1 member pins the session to the 44.1 base.
        common_rates = set.intersection(
            *({sample_rate for sample_rate, _ in c.supported_sample_rates} for c in sync_clients)
        )
        if 48000 not in common_rates:
            return AIRPLAY_FLOW_PCM_FORMAT
        # Only lift the session to 48 kHz for 48k-family content; 44.1k(-family)
        # content stays at 44.1 to avoid a pointless resample for the common case.
        content_rate = 0
        if (
            media.source_id
            and media.queue_item_id
            and (
                queue_item := self.mass.player_queues.get_item(media.source_id, media.queue_item_id)
            )
            and queue_item.streamdetails
        ):
            content_rate = queue_item.streamdetails.audio_format.sample_rate
        if content_rate and content_rate % 48000 == 0:
            return AudioFormat(
                content_type=AIRPLAY_FLOW_PCM_FORMAT.content_type,
                sample_rate=48000,
                bit_depth=AIRPLAY_FLOW_PCM_FORMAT.bit_depth,
            )
        return AIRPLAY_FLOW_PCM_FORMAT

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


class GenericAirPlayPlayer(AirPlayPlayer):
    """AirPlay protocol endpoint without independent device control."""

    _attr_type = PlayerType.PROTOCOL
