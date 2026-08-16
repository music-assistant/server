"""Generic LinkPlay player implementation, driven by the public pywiim API."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, NoReturn, cast

from async_upnp_client.exceptions import UpnpError
from async_upnp_client.profiles.dlna import DmrDevice
from music_assistant_models.enums import (
    IdentifierType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
)
from music_assistant_models.errors import PlayerCommandFailed, UnsupportedFeaturedException
from music_assistant_models.player import DeviceInfo
from pywiim import Player as PywiimPlayer
from pywiim import WiiMClient, WiiMError, WiiMGroupCompatibilityError
from pywiim.upnp.client import UpnpClient

from music_assistant.constants import EXTERNAL_SOURCES, create_sample_rates_config_entry
from music_assistant.models.player import Player, PlayerMedia

from .constants import PLAYER_ID_PREFIX
from .helpers import linkplay_slave_uuid_to_udn

if TYPE_CHECKING:
    from async_upnp_client.client import UpnpDevice, UpnpService, UpnpStateVariable
    from music_assistant_models.config_entries import ConfigEntry

    from .provider import WiimProvider

# pywiim reports play_state as one of these normalized strings.
PYWIIM_STATE_TO_MA: dict[str, PlaybackState] = {
    "play": PlaybackState.PLAYING,
    "pause": PlaybackState.PAUSED,
    "stop": PlaybackState.IDLE,
    "idle": PlaybackState.IDLE,
    "buffering": PlaybackState.PLAYING,
}

# The canonical pywiim source ids under which MA-initiated URL playback runs:
# ``network``/``custompushurl``/``http`` are the URL-streaming modes a pushed URL
# reports, and ``None`` covers transient handover reports. Any other source
# (spotify, bluetooth, airplay, physical inputs, ...) means an external app owns
# playback. Within these modes the live UPnP URI still discriminates MA vs external.
MA_OWNED_SOURCES = (None, "network", "custompushurl", "http")

# How long an unconfirmed MA stream keeps its optimistic ownership marker. Without
# UPnP eventing a stream can never be confirmed via the live URI, so the marker is
# released once a persistently idle device passes this handover window.
HANDOVER_CONFIRM_TIMEOUT = 30.0


class LinkPlayPlayer(Player):
    """
    Generic LinkPlay player in Music Assistant.

    Drives compatible LinkPlay speakers (e.g. Edifier) through the public pywiim
    HTTP API over the shared aiohttp session, while UPnP events from
    async-upnp-client provide low-latency refreshes on top of adaptive polling.
    """

    _attr_type = PlayerType.PLAYER

    def __init__(
        self,
        provider: WiimProvider,
        player_id: str,
        pywiim_player: PywiimPlayer,
        upnp_device: UpnpDevice,
        pywiim_upnp: UpnpClient,
        description_url: str,
        mac_address: str | None = None,
    ) -> None:
        """Initialize the Player."""
        super().__init__(provider, player_id)
        self._pywiim = pywiim_player
        self._client = pywiim_player.client
        self._pywiim_upnp: UpnpClient | None = pywiim_upnp
        self._upnp_device = upnp_device
        self._description_url = description_url
        self._dmr_device: DmrDevice | None = None
        self._eventing_active = False
        # Whether this cycle holds a trustworthy live UPnP URI: a live subscription or a
        # successful poll sets it, a failed poll clears it so a stale cached URI is not
        # read as a source takeover.
        self._live_uri_fresh = False
        self._mac_address = mac_address
        self._rebuild_lock = asyncio.Lock()
        # Ownership tracking for the URL MA last asked the device to play.
        self._ma_stream_uri: str | None = None
        self._ma_source_id: str | None = None
        self._ma_stream_confirmed = False
        self._ma_stream_since: float | None = None
        # Track identity for the currently reported now-playing: the URI drives the
        # elapsed-time reset (a new track without a position must not inherit the old
        # one), the full signature drives current-media rebuilds (stale fields cleared).
        self._last_track_uri: str | None = None
        self._last_now_playing_sig: str | None = None

        self._attr_name = pywiim_player.name or player_id
        self._attr_supported_features = {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.PAUSE,
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
            PlayerFeature.SET_MEMBERS,
        }
        self._attr_device_info = DeviceInfo(
            model=pywiim_player.model or upnp_device.model_name or "LinkPlay",
            manufacturer=upnp_device.manufacturer or "LinkPlay",
            software_version=pywiim_player.firmware,
        )
        self._attr_device_info.add_identifier(
            IdentifierType.UUID, player_id.removeprefix(PLAYER_ID_PREFIX).removeprefix("uuid:")
        )
        if pywiim_player.host:
            self._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, pywiim_player.host)
        if mac_address:
            self._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, mac_address)

    # --- Lifecycle ---

    async def setup(self) -> None:
        """Handle logic when the player is set up in the Player controller."""
        self._sync_seek_feature()
        self._attr_needs_poll = True
        self._attr_poll_interval = 5
        await self._connect_eventing()

    async def poll(self) -> None:
        """Poll the device for transport state, position and volume updates."""
        await self._refresh_state()
        self._attr_poll_interval = 5 if self._attr_playback_state == PlaybackState.PLAYING else 30

    @property
    def pywiim_player(self) -> PywiimPlayer:
        """Return the underlying pywiim Player (used by the provider's group finders)."""
        return self._pywiim

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        await super().on_unload()
        await self.cleanup_resources()

    async def cleanup_resources(self) -> None:
        """Release the eventing subscriptions and UPnP client this player owns."""
        await self._disconnect_eventing()
        await self._close_pywiim_upnp()

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return player-specific config entries."""
        return [
            create_sample_rates_config_entry(
                max_sample_rate=192000,
                safe_max_sample_rate=192000,
                max_bit_depth=24,
                safe_max_bit_depth=24,
            ),
        ]

    @property
    def can_group_with(self) -> set[str]:
        """Return the ids of the other generic LinkPlay players this player can group with."""
        return {
            player.player_id
            for player in self.provider.players
            if isinstance(player, LinkPlayPlayer)
            and player.available
            and player.player_id != self.player_id
        }

    async def async_handle_address_change(
        self, new_ip: str, upnp_device: UpnpDevice, description_url: str
    ) -> None:
        """
        Rebuild the HTTP and UPnP backend against a new device address.

        The replacement is fully validated before any live resource is touched,
        so a failed rebuild leaves the existing backend intact for a later retry.

        :param new_ip: The device's new IP address.
        :param upnp_device: The UPnP device freshly probed at the new location.
        :param description_url: The matched description.xml URL at the new location.
        """
        async with self._rebuild_lock:
            if new_ip == self._pywiim.host:
                return
            new_client = WiiMClient(new_ip, session=self.mass.http_session)
            try:
                new_pywiim_upnp = await UpnpClient.create(
                    new_ip, description_url, session=self.mass.http_session
                )
            except UpnpError as err:
                self.logger.warning(
                    "Failed to rebuild UPnP for %s at %s: %s", self.name, new_ip, err
                )
                return
            provider = cast("WiimProvider", self.provider)
            new_pywiim = PywiimPlayer(
                new_client,
                upnp_client=new_pywiim_upnp,
                player_finder=provider.pywiim_player_finder,
                all_players_finder=provider.pywiim_all_players_finder,
            )
            try:
                await new_pywiim.refresh(full=True)
            except WiiMError as err:
                self.logger.warning(
                    "Failed to reach %s at new address %s: %s", self.name, new_ip, err
                )
                await self._safe_close_upnp(new_pywiim_upnp)
                return

            # Adopt the validated replacement, then release the old resources.
            await self._disconnect_eventing()
            old_pywiim_upnp = self._pywiim_upnp
            self._client = new_client
            self._pywiim = new_pywiim
            self._pywiim_upnp = new_pywiim_upnp
            self._upnp_device = upnp_device
            self._description_url = description_url
            self._ma_stream_confirmed = False
            await self._safe_close_upnp(old_pywiim_upnp)
            self._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, new_ip)
            await self._connect_eventing()
            await self._refresh_state()

    # --- Player commands ---

    async def play_media(self, media: PlayerMedia) -> None:
        """Play media command."""
        stream_url = await self.mass.streams.resolve_stream_url(self.player_id, media)
        try:
            await self._pywiim.play_url(stream_url)
        except WiiMError as err:
            # The device never took the new stream, so any currently-playing stream and
            # its ownership marker are left untouched for the scheduled refresh to report.
            self._raise_command_error("play_media", err)
        # The device accepted the stream; publish the optimistic queue/ownership state.
        self.set_current_media(
            uri=stream_url,
            title=media.title,
            artist=media.artist,
            album=media.album,
            image_url=media.image_url,
            duration=media.duration,
            source_id=media.source_id,
            clear_all=True,
        )
        self._attr_active_source = self.player_id
        self._attr_elapsed_time = 0
        self._attr_elapsed_time_last_updated = time.time()
        self._ma_stream_uri = stream_url
        self._ma_source_id = media.source_id
        self._ma_stream_confirmed = False
        self._ma_stream_since = time.time()
        self._last_track_uri = stream_url
        self._last_now_playing_sig = None
        # play_url is fire-and-forget and does not move pywiim's cached play_state, so
        # publish an optimistic PLAYING and poll fast until the device confirms; without
        # this the queue's short resume wait would race the 30s idle poll interval.
        self._attr_playback_state = PlaybackState.PLAYING
        self._attr_poll_interval = 5
        self.update_state()

    async def play(self) -> None:
        """Play command."""
        try:
            await self._pywiim.resume()
        except WiiMError as err:
            self._raise_command_error("play", err)
        self._publish_optimistic_state(PlaybackState.PLAYING)

    async def pause(self) -> None:
        """Pause command."""
        try:
            await self._pywiim.pause()
        except WiiMError as err:
            self._raise_command_error("pause", err)
        self._publish_optimistic_state(PlaybackState.PAUSED)

    async def stop(self) -> None:
        """Stop command."""
        try:
            await self._pywiim.stop()
        except WiiMError as err:
            # Leave the (still-playing) stream's ownership/media state intact on failure.
            self._raise_command_error("stop", err)
        self._attr_active_source = None
        self._attr_current_media = None
        self._last_track_uri = None
        self._last_now_playing_sig = None
        self._ma_source_id = None
        self._release_ma_ownership()
        self._attr_playback_state = PlaybackState.IDLE
        self.update_state()

    async def seek(self, position: int) -> None:
        """Seek to position in seconds."""
        try:
            await self._pywiim.seek(position)
        except WiiMError as err:
            self._raise_command_error("seek", err)
        self._attr_elapsed_time = float(position)
        self._attr_elapsed_time_last_updated = time.time()
        self.update_state()

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command on the player."""
        try:
            await self._pywiim.set_volume(volume_level / 100)
        except WiiMError as err:
            self._raise_command_error("volume_set", err)
        self._attr_volume_level = volume_level
        self.update_state()

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME MUTE command on the player."""
        try:
            await self._pywiim.set_mute(muted)
        except WiiMError as err:
            self._raise_command_error("volume_mute", err)
        self._attr_volume_muted = muted
        self.update_state()

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS command on the player."""
        try:
            for member_id in player_ids_to_add or []:
                member = self._require_linkplay_member(member_id)
                await member._pywiim.join_group(self._pywiim)
                await self._verify_group_change(member, member_id, expect_slave=True)
            for member_id in player_ids_to_remove or []:
                member = self._require_linkplay_member(member_id)
                await member._pywiim.leave_group()
                await self._verify_group_change(member, member_id, expect_slave=False)
        except WiiMGroupCompatibilityError as err:
            raise PlayerCommandFailed(
                f"Cannot group {self.name}: incompatible devices ({err})"
            ) from err
        except WiiMError as err:
            raise PlayerCommandFailed(f"set_members failed on {self.name}: {err}") from err
        finally:
            self._schedule_refresh()

    # --- Private helpers ---

    def _require_linkplay_member(self, player_id: str) -> LinkPlayPlayer:
        """
        Return a same-backend member for a grouping command.

        MA-created grouping stays within the generic LinkPlay backend; a request
        that targets any other player is rejected as unsupported rather than cast.
        """
        member = self.mass.players.get_player(player_id)
        if not isinstance(member, LinkPlayPlayer):
            raise UnsupportedFeaturedException(
                f"Cannot group {player_id} with {self.name}: cross-backend grouping is unsupported"
            )
        return member

    async def _verify_group_change(
        self, member: LinkPlayPlayer, member_id: str, expect_slave: bool
    ) -> None:
        """
        Confirm a grouping operation actually took effect on THIS leader's group.

        pywiim's ``join_group``/``leave_group`` can return without changing state
        (a failed join verification, or a leave on an externally-formed group that
        pywiim has no internal link for), and a bare role check would also accept a
        member that stayed attached to a different master. So the leader's own slave
        list is re-read and a failure is surfaced instead of silently succeeding.
        """
        await self._pywiim.refresh(full=False)
        try:
            slaves = await self._client.get_slaves_info()
        except WiiMError as err:
            raise PlayerCommandFailed(
                f"Could not verify grouping of {member_id} on {self.name}: {err}"
            ) from err
        current_members = {
            resolved
            for slave in slaves
            if isinstance(slave, dict)
            and (resolved := self._resolve_member_player_id(slave.get("uuid"))) is not None
        }
        if (member.player_id in current_members) != expect_slave:
            verb = "join" if expect_slave else "leave"
            raise PlayerCommandFailed(f"{member_id} did not {verb} the group led by {self.name}")

    def _publish_optimistic_state(self, playback_state: PlaybackState) -> None:
        """
        Apply the expected playback state after an accepted command and poll fast.

        Devices without UPnP eventing only reveal the new state on the next poll, so
        this keeps MA's queue from racing the idle poll interval after a transport
        command; the following polls/events reconcile the real state.
        """
        self._attr_playback_state = playback_state
        self._attr_poll_interval = 5
        self.update_state()

    def _sync_seek_feature(self) -> None:
        """Add or remove the SEEK feature to match the current source's capabilities."""
        if self._pywiim.supports_seek:
            self._attr_supported_features.add(PlayerFeature.SEEK)
        else:
            self._attr_supported_features.discard(PlayerFeature.SEEK)

    def _schedule_refresh(self) -> None:
        """Schedule a state refresh, coalescing bursts into a single in-flight refresh."""
        self.mass.create_task(self._refresh_state(), task_id=f"linkplay_refresh_{self.player_id}")

    def _raise_command_error(self, action: str, err: WiiMError) -> NoReturn:
        """Schedule a state refresh and raise a typed command failure."""
        self._schedule_refresh()
        raise PlayerCommandFailed(f"{action} failed on {self.name}: {err}") from err

    async def _refresh_state(self) -> None:
        """Refresh cached state from the device and push it to MA."""
        try:
            await self._pywiim.refresh(full=False)
        except WiiMError as err:
            self.logger.debug("Failed to refresh %s: %s", self.name, err)
            # Mirror the unavailable-device cleanup so an offline speaker stops
            # publishing its previous media and active queue source.
            self._attr_available = False
            self._attr_current_media = None
            self._attr_active_source = None
            self.update_state()
            return
        # Without event subscriptions the live UPnP transport URI would never update,
        # so poll it here to keep source-takeover detection working on those devices.
        if self._eventing_active:
            # A live subscription keeps the DMR's transport URI current.
            self._live_uri_fresh = True
        elif self._dmr_device is not None:
            try:
                await self._dmr_device.async_update()
                self._live_uri_fresh = True
            except UpnpError as err:
                self.logger.debug("UPnP poll failed for %s: %r", self.name, err)
                # The cached URI is now stale; untrust it so a transient failure is not
                # mistaken for a takeover of a still-playing MA stream.
                self._live_uri_fresh = False
        await self._update_group_members()
        self._push_state()

    async def _update_group_members(self) -> None:
        """Resolve the leader's slave list into MA group member ids."""
        if not self._pywiim.is_master:
            # Followers and solo players never manage members themselves; MA derives
            # a follower's relationship from the leader that lists it.
            self._attr_group_members = []
            return
        try:
            slaves: list[Any] = await self._client.get_slaves_info()
        except WiiMError as err:
            self.logger.debug("Failed to fetch slave list for %s: %s", self.name, err)
            return
        member_ids: list[str] = []
        for slave in slaves:
            if not isinstance(slave, dict):
                continue
            if (resolved := self._resolve_member_player_id(slave.get("uuid"))) is not None:
                member_ids.append(resolved)
        self._attr_group_members = [self.player_id, *member_ids] if member_ids else []

    def _resolve_member_player_id(self, slave_uuid: str | None) -> str | None:
        """
        Resolve a slave's UUID (24-char HTTP or full UDN form) to a registered player id.

        Matches against players already registered by this provider (in either
        backend) and ignores members that do not resolve to a known player.

        :param slave_uuid: The slave's UUID from the leader's slave list.
        """
        if not slave_uuid or (udn := linkplay_slave_uuid_to_udn(slave_uuid)) is None:
            return None
        target_hex = udn.removeprefix("uuid:").replace("-", "").upper()
        for player in self.provider.players:
            player_id = player.player_id
            if not player_id.startswith(PLAYER_ID_PREFIX):
                continue
            udn_hex = player_id[len(PLAYER_ID_PREFIX) :].removeprefix("uuid:").replace("-", "")
            if udn_hex.upper() == target_hex:
                return player_id
        return None

    def _push_state(self) -> None:
        """Map pywiim's cached state onto MA player attributes and publish it."""
        self._attr_available = self._pywiim.available
        if name := self._pywiim.name:
            self._attr_name = name

        if not self._attr_available:
            self._attr_current_media = None
            self._attr_active_source = None
            self.update_state()
            return

        self._sync_seek_feature()
        volume = self._pywiim.volume_level
        self._attr_volume_level = round(volume * 100) if volume is not None else None
        # An unknown mute state must stay unknown rather than collapse to "unmuted".
        self._attr_volume_muted = self._pywiim.is_muted

        if self._pywiim.is_slave:
            # A follower's playback is derived by MA from its leader, so only its
            # own volume/mute is reported and it manages no members of its own.
            self._attr_group_members = []
            self.update_state()
            return

        self._attr_playback_state = PYWIIM_STATE_TO_MA.get(
            self._pywiim.play_state or "", PlaybackState.IDLE
        )
        self._reconcile_ma_ownership()

        if self._attr_playback_state == PlaybackState.IDLE:
            # A confirmed MA stream that reaches idle has ended, so ownership is over.
            # Without eventing a stream is never confirmed, so also release once the
            # device has stayed idle past the bounded handover window.
            if self._ma_stream_confirmed or self._handover_window_expired():
                self._release_ma_ownership()
            if self._ma_stream_uri is None:
                # Ownership released (or never held): nothing is playing for us. While a
                # handover is still in flight the marker is kept, so the optimistic media
                # set by play_media survives a transient idle report.
                self._attr_active_source = None
                self._attr_current_media = None
                self._last_track_uri = None
                self._last_now_playing_sig = None
            self.update_state()
            return

        is_ma_playback = self._ma_stream_uri is not None
        live_uri = self._live_track_uri()
        track_uri = (self._ma_stream_uri or "") if is_ma_playback else (live_uri or "")
        signature = self._now_playing_signature(track_uri)
        track_changed = track_uri != self._last_track_uri
        metadata_changed = signature != self._last_now_playing_sig

        # A fresh position wins; otherwise a *track* change (new URI) resets the anchor so
        # the previous track's position does not keep advancing. A now-playing metadata
        # change on the same URI (e.g. radio) leaves the continuous position untouched.
        if (position := self._pywiim.media_position) is not None:
            self._attr_elapsed_time = float(position)
            self._attr_elapsed_time_last_updated = time.time()
        elif track_changed:
            self._attr_elapsed_time = 0.0
            self._attr_elapsed_time_last_updated = time.time()

        if is_ma_playback:
            self._set_ma_media(metadata_changed)
        else:
            self._set_external_media(live_uri, metadata_changed)

        self._last_track_uri = track_uri
        self._last_now_playing_sig = signature
        self.update_state()

    def _now_playing_signature(self, track_uri: str) -> str:
        """Build a stable signature of the current now-playing for change detection."""
        return "|".join(
            [
                track_uri,
                self._pywiim.source or "",
                self._pywiim.media_title or "",
                self._pywiim.media_artist or "",
                self._pywiim.media_album or "",
                self._pywiim.media_image_url or "",
                str(self._pywiim.media_duration or ""),
            ]
        )

    def _reconcile_ma_ownership(self) -> None:
        """Release the MA-ownership marker once playback is no longer our stream."""
        if self._ma_stream_uri is None:
            return
        source = self._pywiim.source
        if source not in MA_OWNED_SOURCES:
            # A non-network source (spotify/bluetooth/airplay/physical input) took over.
            self._release_ma_ownership()
            return
        live_uri = self._live_track_uri()
        if not live_uri:
            # No live UPnP URI yet (handover in progress); keep the optimistic marker.
            return
        if live_uri == self._ma_stream_uri:
            self._ma_stream_confirmed = True
        elif self._ma_stream_confirmed or self._handover_window_expired():
            # Either our stream loaded earlier and a different URL now plays, or the
            # handover window elapsed without our stream ever appearing (an external
            # controller took over before/instead of it) — both are a takeover.
            self._release_ma_ownership()

    def _release_ma_ownership(self) -> None:
        """Clear the marker that says MA owns the current playback."""
        self._ma_stream_uri = None
        self._ma_stream_confirmed = False
        self._ma_stream_since = None

    def _handover_window_expired(self) -> bool:
        """Return whether an unconfirmed MA stream has outlived its handover window."""
        return (
            self._ma_stream_since is not None
            and (time.time() - self._ma_stream_since) > HANDOVER_CONFIRM_TIMEOUT
        )

    def _live_track_uri(self) -> str | None:
        """Return the device's live UPnP transport URI, if a fresh one is available."""
        if self._dmr_device is None or not self._live_uri_fresh:
            return None
        return self._dmr_device.current_track_uri or None

    def _set_ma_media(self, metadata_changed: bool) -> None:
        """Publish current media for MA-initiated playback."""
        self._attr_active_source = self.player_id
        if not (self._ma_stream_confirmed or self._handover_window_expired()):
            # During the handover window the device can still expose the previous track's
            # cached metadata, so the media play_media published is kept until our stream
            # is provably live (a matching live URI) or the window has elapsed.
            return
        has_metadata = bool(
            self._pywiim.media_title or self._pywiim.media_artist or self._pywiim.media_album
        )
        if not has_metadata:
            # Keep the optimistic metadata play_media set until the device reports its own.
            return
        self.set_current_media(
            uri=self._ma_stream_uri or "",
            title=self._pywiim.media_title,
            artist=self._pywiim.media_artist,
            album=self._pywiim.media_album,
            image_url=self._pywiim.media_image_url,
            duration=self._pywiim.media_duration,
            source_id=self._ma_source_id,
            clear_all=metadata_changed,
        )

    def _set_external_media(self, live_uri: str | None, metadata_changed: bool) -> None:
        """Publish current media for playback MA did not start (read-only)."""
        # This path only runs for playback MA does not own, so it must always read as
        # external. MA keeps surfacing the remembered MA queue unless the reported source
        # is a recognised external one, so URL/network modes fall back to the sentinel.
        source = self._pywiim.source
        if source and source.lower() in EXTERNAL_SOURCES:
            self._attr_active_source = source
        else:
            self._attr_active_source = "external"
        title = self._pywiim.media_title
        artist = self._pywiim.media_artist
        album = self._pywiim.media_album
        if not (title or artist or album or live_uri):
            self._attr_current_media = None
            return
        self.set_current_media(
            uri=live_uri or "",
            title=title,
            artist=artist,
            album=album,
            image_url=self._pywiim.media_image_url,
            duration=self._pywiim.media_duration,
            source_id=self._attr_active_source,
            clear_all=metadata_changed,
        )

    async def _connect_eventing(self) -> None:
        """Subscribe to UPnP events; fall back to polling the DMR when unavailable."""
        notify_server = cast("WiimProvider", self.provider).notify_server
        self._eventing_active = False
        self._dmr_device = DmrDevice(self._upnp_device, notify_server.event_handler)
        self._dmr_device.on_event = self._handle_upnp_event
        try:
            await self._dmr_device.async_subscribe_services(auto_resubscribe=True)
        except UpnpError as err:
            # Subscription failed or was rejected; the DMR is retained so adaptive
            # polling can still read the live transport URI via async_update().
            self.logger.debug("UPnP event subscription unavailable for %s: %r", self.name, err)
        else:
            self._eventing_active = True

    async def _disconnect_eventing(self) -> None:
        """Unsubscribe from UPnP events and drop the DMR device."""
        if self._dmr_device is None:
            return
        self._dmr_device.on_event = None
        device, self._dmr_device = self._dmr_device, None
        self._eventing_active = False
        try:
            await device.async_unsubscribe_services()
        except UpnpError as err:
            self.logger.debug("Error unsubscribing UPnP events for %s: %r", self.name, err)

    async def _close_pywiim_upnp(self) -> None:
        """Close and drop the injected pywiim UPnP client, if any."""
        if self._pywiim_upnp is None:
            return
        upnp, self._pywiim_upnp = self._pywiim_upnp, None
        await self._safe_close_upnp(upnp)

    async def _safe_close_upnp(self, upnp: UpnpClient | None) -> None:
        """Best-effort close of a pywiim UPnP client (never closes the shared session)."""
        if upnp is None:
            return
        try:
            await upnp.close()
        except (UpnpError, OSError) as err:
            self.logger.debug("Error closing pywiim UPnP client for %s: %r", self.name, err)

    def _handle_upnp_event(
        self, service: UpnpService, state_variables: Sequence[UpnpStateVariable[Any]]
    ) -> None:
        """Handle a UPnP event by scheduling a fresh state refresh."""
        del service
        if not state_variables:
            # async-upnp-client signals a failed auto-resubscribe with an empty variable
            # sequence; drop to polling so transport/source changes keep being observed.
            self._eventing_active = False
        self._schedule_refresh()
