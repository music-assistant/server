"""
Sonos Player provider for Music Assistant for speakers running the S2 firmware.

Based on the aiosonos library, which leverages the new websockets API of the Sonos S2 firmware.
https://github.com/music-assistant/aiosonos

SonosPlayer: Holds the details of the (discovered) Sonosplayer.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from aiohttp import ClientConnectorError
from aiosonos.api.models import ContainerType, MusicService, SonosCapability
from aiosonos.client import SonosLocalApiClient
from aiosonos.const import EventType as SonosEventType
from aiosonos.const import SonosEvent
from aiosonos.exceptions import ConnectionFailed, FailedCommand
from music_assistant_models.enums import (
    IdentifierType,
    MediaType,
    PlaybackState,
    PlayerFeature,
    RepeatMode,
)
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.player import OutputProtocol, PlayerMedia

from music_assistant.constants import (
    CONF_ENTRY_HTTP_PROFILE_DEFAULT_2,
    VERBOSE_LOG_LEVEL,
    create_sample_rates_config_entry,
)
from music_assistant.helpers.tags import async_parse_tags
from music_assistant.helpers.util import is_valid_mac_address
from music_assistant.models.player import Player
from music_assistant.providers.sonos.const import (
    PLAYBACK_STATE_MAP,
    PLAYER_SOURCE_MAP,
    SOURCE_AIRPLAY,
    SOURCE_LINE_IN,
    SOURCE_RADIO,
    SOURCE_SPOTIFY,
    SOURCE_TV,
    UNSUPPORTED_MODELS_NATIVE_ANNOUNCEMENTS,
)

if TYPE_CHECKING:
    from aiosonos.api.models import DiscoveryInfo as SonosDiscoveryInfo
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType

    from .provider import SonosPlayerProvider

SUPPORTED_FEATURES = {
    PlayerFeature.PLAY_MEDIA,
    PlayerFeature.PAUSE,
    PlayerFeature.SEEK,
    PlayerFeature.SELECT_SOURCE,
    PlayerFeature.SET_MEMBERS,
    PlayerFeature.GAPLESS_PLAYBACK,
}


@dataclass
class SonosQueue:
    """Simple representation of a Sonos (cloud) Queue."""

    items: list[PlayerMedia] = field(default_factory=list)
    last_updated: float = time.time()


class SonosPlayer(Player):
    """Holds the details of the (discovered) Sonosplayer."""

    def __init__(
        self,
        prov: SonosPlayerProvider,
        player_id: str,
        discovery_info: SonosDiscoveryInfo,
    ) -> None:
        """Initialize the SonosPlayer."""
        super().__init__(prov, player_id)
        self.discovery_info = discovery_info
        self.connected: bool = False
        self._listen_task: asyncio.Task | None = None
        self.sonos_queue: SonosQueue = SonosQueue()

    @property
    def synced_to(self) -> str | None:
        """
        Return the id of the player this player is synced to (sync leader).

        If this player is not synced to another player (or is the sync leader itself),
        this should return None.
        If it is part of a (permanent) group, this should also return None.
        """
        if self.client.player.is_coordinator:
            return None
        if self.client.player.group:
            return self.client.player.group.coordinator_id
        return None

    async def setup(self) -> None:
        """Handle setup of the player."""
        # connect the player first so we can fail early
        self.client = SonosLocalApiClient(
            self.device_info.ip_address, self.mass.http_session_no_ssl
        )
        await self._connect(False)

        # collect supported features
        _supported_features = SUPPORTED_FEATURES.copy()
        if (
            SonosCapability.AUDIO_CLIP in self.discovery_info["device"]["capabilities"]
            and self.discovery_info["device"]["modelDisplayName"]
            not in UNSUPPORTED_MODELS_NATIVE_ANNOUNCEMENTS
        ):
            _supported_features.add(PlayerFeature.PLAY_ANNOUNCEMENT)
        if not self.client.player.has_fixed_volume:
            _supported_features.add(PlayerFeature.VOLUME_SET)
            _supported_features.add(PlayerFeature.VOLUME_MUTE)
        _supported_features.add(PlayerFeature.NEXT_PREVIOUS)
        _supported_features.add(PlayerFeature.ENQUEUE)
        self._attr_supported_features = _supported_features

        self._attr_name = (
            self.discovery_info["device"]["name"]
            or self.discovery_info["device"]["modelDisplayName"]
        )
        self._attr_device_info.model = self.discovery_info["device"]["modelDisplayName"]
        self._attr_device_info.manufacturer = self._provider.manifest.name
        self._attr_can_group_with = {self._provider.instance_id}

        # Add identifiers for matching with other protocols (like AirPlay, DLNA)
        # The player_id is the Sonos UUID (e.g., RINCON_xxxxxxxxxxxx)
        self._attr_device_info.add_identifier(IdentifierType.UUID, self.player_id)
        # Extract MAC address from Sonos player_id (RINCON_XXXXXXXXXXXX01400)
        # The middle part contains the MAC address (last 6 bytes in hex)
        mac_address = self._extract_mac_from_player_id()
        # Only add MAC address if it's valid (not 00:00:00:00:00:00)
        if mac_address and is_valid_mac_address(mac_address):
            self._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, mac_address)

        if SonosCapability.LINE_IN in self.discovery_info["device"]["capabilities"]:
            self._attr_source_list.append(PLAYER_SOURCE_MAP[SOURCE_LINE_IN])
        if SonosCapability.HT_PLAYBACK in self.discovery_info["device"]["capabilities"]:
            self._attr_source_list.append(PLAYER_SOURCE_MAP[SOURCE_TV])
        if SonosCapability.AIRPLAY in self.discovery_info["device"]["capabilities"]:
            self._attr_source_list.append(PLAYER_SOURCE_MAP[SOURCE_AIRPLAY])

        self.update_attributes()
        await self.mass.players.register_or_update(self)

        # register callback for state changed
        self._on_unload_callbacks.append(
            self.client.subscribe(
                self.on_player_event,
                (
                    SonosEventType.GROUP_UPDATED,
                    SonosEventType.PLAYER_UPDATED,
                ),
            )
        )

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the player."""
        return [
            CONF_ENTRY_HTTP_PROFILE_DEFAULT_2,
            create_sample_rates_config_entry(
                # set safe max bit depth to 16 bits because the older Sonos players
                # do not support 24 bit playback (e.g. Play:1)
                max_sample_rate=48000,
                max_bit_depth=24,
                safe_max_bit_depth=16,
                hidden=False,
            ),
        ]

    async def volume_set(self, volume_level: int) -> None:
        """
        Handle VOLUME_SET command on the player.

        Will only be called if the PlayerFeature.VOLUME_SET is supported.

        :param volume_level: volume level (0..100) to set on the player.
        """
        await self.client.player.set_volume(volume_level)

    async def volume_mute(self, muted: bool) -> None:
        """
        Handle VOLUME MUTE command on the player.

        Will only be called if the PlayerFeature.VOLUME_MUTE is supported.

        :param muted: bool if player should be muted.
        """
        await self.client.player.set_volume(muted=muted)

    async def play(self) -> None:
        """Handle PLAY command on the player."""
        if self.client.player.is_passive:
            self.logger.debug("Ignore PLAY command: Player is synced to another player.")
            return
        await self.client.player.group.play()

    async def stop(self) -> None:
        """Handle STOP command on the player."""
        if self.client.player.is_passive:
            self.logger.debug("Ignore STOP command: Player is synced to another player.")
            return
        await self.client.player.group.stop()
        self.update_state()

    async def pause(self) -> None:
        """
        Handle PAUSE command on the player.

        Will only be called if the player reports PlayerFeature.PAUSE is supported.
        """
        if self.client.player.is_passive:
            self.logger.debug("Ignore PAUSE command: Player is synced to another player.")
            return
        active_source = self.state.active_source
        if self.mass.player_queues.get(active_source):
            # Sonos seems to be bugged when playing our queue tracks and we send pause,
            # it can't resume the current track and simply aborts/skips it
            # so we stop the player instead.
            # https://github.com/music-assistant/support/issues/3758
            # TODO: revisit this later once we implemented support for range requests
            # as I have the feeling the pause issue is related to seek support (=range requests)
            await self.stop()
            return
        if not self.client.player.group.playback_actions.can_pause:
            await self.stop()
            return
        await self.client.player.group.pause()

    async def next_track(self) -> None:
        """
        Handle NEXT_TRACK command on the player.

        Will only be called if the player reports PlayerFeature.NEXT_PREVIOUS
        is supported and the player is not currently playing a MA queue.
        """
        await self.client.player.group.skip_to_next_track()

    async def previous_track(self) -> None:
        """
        Handle PREVIOUS_TRACK command on the player.

        Will only be called if the player reports PlayerFeature.NEXT_PREVIOUS
        is supported and the player is not currently playing a MA queue.
        """
        await self.client.player.group.skip_to_previous_track()

    async def seek(self, position: int) -> None:
        """
        Handle SEEK command on the player.

        Seek to a specific position in the current track.
        Will only be called if the player reports PlayerFeature.SEEK is
        supported and the player is NOT currently playing a MA queue.

        :param position: The position to seek to, in seconds.
        """
        # sonos expects milliseconds
        await self.client.player.group.seek(position * 1000)

    async def play_media(
        self,
        media: PlayerMedia,
    ) -> None:
        """
        Handle PLAY MEDIA command on given player.

        This is called by the Player controller to start playing Media on the player,
        which can be a MA queue item/stream or a native source.
        The provider's own implementation should work out how to handle this request.

        :param media: Details of the item that needs to be played on the player.
        """
        if self.client.player.is_passive:
            # this should be already handled by the player manager, but just in case...
            msg = (
                f"Player {self.display_name} can not "
                "accept play_media command, it is synced to another player."
            )
            raise PlayerCommandFailed(msg)
        # for now always reset the active session
        self.client.player.group.active_session_id = None
        if media.source_id:
            await self._set_sonos_queue_from_mass_queue(media.source_id)

        if media.media_type == MediaType.ANNOUNCEMENT:
            # We cannot use play_stream_url for announcements because Sonos treats those
            # as duration less radio streams and will retry/loop them.
            if not media.duration and media.custom_data:
                announcement_url = media.custom_data.get("announcement_url", media.uri)
                media_info = await async_parse_tags(announcement_url, require_duration=True)
                media.duration = media_info.duration
            media.queue_item_id = "announcement"
            self.sonos_queue.items = [media]
            self.sonos_queue.last_updated = time.time()
            cloud_queue_url = f"{self.mass.streams.base_url}/sonos_queue/{self.player_id}/v2.3/"
            await self.client.player.group.play_cloud_queue(
                cloud_queue_url,
                item_id=media.queue_item_id,
            )
            return

        if (
            not self.flow_mode and media.source_id and media.queue_item_id
        ) or media.media_type == MediaType.PLUGIN_SOURCE:
            # Regular Queue item playback
            # create a sonos cloud queue and load it
            cloud_queue_url = f"{self.mass.streams.base_url}/sonos_queue/{self.player_id}/v2.3/"
            await self.client.player.group.play_cloud_queue(
                cloud_queue_url,
                item_id=media.queue_item_id,
            )
            return

        # play duration-less (long running) radio streams
        # enforce AAC here because Sonos really does not support FLAC streams without duration
        stream_url = await self.provider.mass.streams.resolve_stream_url(self.player_id, media)
        stream_url = stream_url.replace(".flac", ".aac").replace(".wav", ".aac")
        if media.source_id and media.queue_item_id:
            object_id = f"mass:{media.source_id}:{media.queue_item_id}"
        else:
            object_id = stream_url
        await self.client.player.group.play_stream_url(
            stream_url,
            {
                "name": media.title,
                "type": "track",
                "imageUrl": media.image_url,
                "id": {
                    "objectId": object_id,
                },
                "service": {"name": "Music Assistant", "id": "mass"},
            },
        )

    async def select_source(self, source: str) -> None:
        """
        Handle SELECT SOURCE command on the player.

        Will only be called if the PlayerFeature.SELECT_SOURCE is supported.

        :param source: The source(id) to select, as defined in the source_list.
        """
        if source == SOURCE_LINE_IN:
            await self.client.player.group.load_line_in(play_on_completion=True)
        elif source == SOURCE_TV:
            await self.client.player.load_home_theater_playback()
        else:
            # unsupported source - try to clear the queue/player
            await self.stop()

    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        """
        Handle enqueuing of the next (queue) item on the player.

        Called when player reports it started buffering a queue item
        and when the queue items updated.

        A PlayerProvider implementation is in itself responsible for handling this
        so that the queue items keep playing until its empty or the player stopped.

        Will only be called if the player reports PlayerFeature.ENQUEUE is
        supported and the player is currently playing a MA queue.

        This will NOT be called if the end of the queue is reached (and repeat disabled).
        This will NOT be called if the player is using flow mode to playback the queue.

         :param media: Details of the item that needs to be enqueued on the player.
        """
        if media.source_id:
            await self._set_sonos_queue_from_mass_queue(media.source_id)
        if session_id := self.client.player.group.active_session_id:
            await self.client.api.playback_session.refresh_cloud_queue(session_id)

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """
        Handle SET_MEMBERS command on the player.

        Group or ungroup the given child player(s) to/from this player.
        Will only be called if the PlayerFeature.SET_MEMBERS is supported.

        :param player_ids_to_add: List of player_id's to add to the group.
        :param player_ids_to_remove: List of player_id's to remove from the group.
        """
        player_ids_to_add = player_ids_to_add or []
        player_ids_to_remove = player_ids_to_remove or []
        if player_ids_to_add or player_ids_to_remove:
            await self.client.player.group.modify_group_members(
                player_ids_to_add=player_ids_to_add,
                player_ids_to_remove=player_ids_to_remove,
            )

    async def ungroup(self) -> None:
        """
        Handle UNGROUP command on the player.

        Remove the player from any (sync)groups it currently is grouped to.
        If this player is the sync leader (or group player),
        all child's will be ungrouped and the group dissolved.

        Will only be called if the PlayerFeature.SET_MEMBERS is supported.
        """
        await self.client.player.leave_group()

    async def play_announcement(
        self, announcement: PlayerMedia, volume_level: int | None = None
    ) -> None:
        """
        Handle (native) playback of an announcement on the player.

        Will only be called if the PlayerFeature.PLAY_ANNOUNCEMENT is supported.

        :param announcement: Details of the announcement that needs to be played on the player.
        :param volume_level: The volume level to play the announcement at (0..100).
            If not set, the player should use the current volume level.
        """
        self.logger.debug(
            "Playing announcement %s on %s",
            announcement.uri,
            self.display_name,
        )
        await self.client.player.play_audio_clip(
            announcement.uri, volume_level, name="Announcement"
        )
        # Wait until the announcement is finished playing
        # This is helpful for people who want to play announcements in a sequence
        # yeah we can also setup a subscription on the sonos player for this, but this is easier
        media_info = await async_parse_tags(announcement.uri, require_duration=True)
        duration = media_info.duration or 10
        await asyncio.sleep(duration)

    def on_player_event(self, event: SonosEvent | None) -> None:
        """Handle incoming event from player."""
        try:
            self.update_attributes()
        except Exception as err:
            self.logger.exception("Failed to update player attributes: %s", err)
            return
        try:
            self.update_state()
        except Exception as err:
            self.logger.exception("Failed to update player state: %s", err)

    def update_attributes(self) -> None:  # noqa: PLR0915
        """Update the player attributes."""
        self._attr_available = self.connected
        if not self.connected:
            return
        if self.client.player.has_fixed_volume:
            self._attr_volume_level = 100
        else:
            self._attr_volume_level = self.client.player.volume_level or 0
        self._attr_volume_muted = self.client.player.volume_muted

        group_parent = None
        if self.client.player.is_coordinator:
            # player is group coordinator - always report native group members
            active_group = self.client.player.group
            if len(self.client.player.group_members) > 1:
                self._attr_group_members = list(self.client.player.group_members)
            else:
                self._attr_group_members.clear()
            self._attr_can_group_with = {self._provider.instance_id}
        else:
            # player is group child (synced to another player)
            group_parent: SonosPlayer = self.mass.players.get_player(
                self.client.player.group.coordinator_id
            )
            if not group_parent or not group_parent.client or not group_parent.client.player:
                # handle race condition where the group parent is not yet discovered
                return
            active_group = group_parent.client.player.group
            self._attr_group_members.clear()

        # map playback state
        self._attr_playback_state = PLAYBACK_STATE_MAP[active_group.playback_state]
        self._attr_elapsed_time = active_group.position

        # figure out the active source based on the container
        container_type = active_group.container_type
        active_service = active_group.active_service
        container = active_group.playback_metadata.get("container")
        if (
            not active_service
            and container
            and container.get("service", {}).get("id") == MusicService.MUSIC_ASSISTANT
        ):
            active_service = MusicService.MUSIC_ASSISTANT
        if container_type == ContainerType.LINEIN:
            self._attr_active_source = SOURCE_LINE_IN
        elif container_type in (ContainerType.HOME_THEATER_HDMI, ContainerType.HOME_THEATER_SPDIF):
            self._attr_active_source = SOURCE_TV
        elif container_type == ContainerType.AIRPLAY:
            self._attr_active_source = SOURCE_AIRPLAY
        elif (
            container_type == ContainerType.STATION
            and active_service != MusicService.MUSIC_ASSISTANT
        ):
            self._attr_active_source = SOURCE_RADIO
            # add radio to source list if not yet there
            if SOURCE_RADIO not in [x.id for x in self._attr_source_list]:
                self._attr_source_list.append(PLAYER_SOURCE_MAP[SOURCE_RADIO])
        elif active_service == MusicService.SPOTIFY:
            self._attr_active_source = SOURCE_SPOTIFY
            # add spotify to source list if not yet there
            if SOURCE_SPOTIFY not in [x.id for x in self._attr_source_list]:
                self._attr_source_list.append(PLAYER_SOURCE_MAP[SOURCE_SPOTIFY])
        elif active_service == MusicService.MUSIC_ASSISTANT:
            # setting active source to None is fine
            self._attr_active_source = None
        # its playing some service we did not yet map
        elif container and container.get("service", {}).get("name"):
            self._attr_active_source = container["service"]["name"]
        elif container and container.get("name"):
            self._attr_active_source = container["name"]
        elif active_service:
            self._attr_active_source = active_service
        elif container_type:
            self._attr_active_source = container_type
        else:
            # the player has nothing loaded at all (empty queue and no service active)
            self._attr_active_source = None

        # special case: Sonos reports PAUSED state when MA stopped playback
        if (
            active_service == MusicService.MUSIC_ASSISTANT
            and self._attr_playback_state == PlaybackState.PAUSED
        ):
            self._attr_playback_state = PlaybackState.IDLE

        # parse current media
        self._attr_elapsed_time = self.client.player.group.position
        self._attr_elapsed_time_last_updated = time.time()
        current_media = None
        if (current_item := active_group.playback_metadata.get("currentItem")) and (
            (track := current_item.get("track")) and track.get("name")
        ):
            track_images = track.get("images", [])
            track_image_url = track_images[0].get("url") if track_images else None
            track_duration_millis = track.get("durationMillis")
            current_media = PlayerMedia(
                uri=track.get("id", {}).get("objectId") or track.get("mediaUrl"),
                media_type=MediaType.TRACK,
                title=track["name"],
                artist=track.get("artist", {}).get("name"),
                album=track.get("album", {}).get("name"),
                duration=track_duration_millis / 1000 if track_duration_millis else None,
                image_url=track_image_url,
            )
            if active_service == MusicService.MUSIC_ASSISTANT:
                current_media.source_id = self._attr_active_source
                current_media.queue_item_id = current_item["id"]
        # radio stream info
        if container and container.get("name") and active_group.playback_metadata.get("streamInfo"):
            images = container.get("images", [])
            image_url = images[0].get("url") if images else None
            current_media = PlayerMedia(
                uri=container.get("id", {}).get("objectId"),
                media_type=MediaType.RADIO,
                title=active_group.playback_metadata["streamInfo"],
                album=container["name"],
                image_url=image_url,
            )
        # generic info from container (also when MA is playing!)
        if container and container.get("name") and container.get("id"):
            if not current_media:
                current_media = PlayerMedia(
                    uri=container["id"]["objectId"], media_type=MediaType.UNKNOWN
                )
            if not current_media.image_url:
                images = container.get("images", [])
                current_media.image_url = images[0].get("url") if images else None
            if not current_media.title:
                current_media.title = container["name"]
            if not current_media.uri:
                current_media.uri = container["id"]["objectId"]

        self._attr_current_media = current_media

    async def on_protocol_playback(
        self,
        output_protocol: OutputProtocol,
    ) -> None:
        """Handle callback when playback starts on a protocol output."""
        # Only handle AirPlay protocol
        if output_protocol.protocol_domain != "airplay":
            return

        # Only if this player is a coordinator with group members
        if not self.client.player.is_coordinator:
            return

        current_members = list(self.client.player.group_members)
        if len(current_members) <= 1:
            # No group members to worry about
            return

        # Workaround for Sonos AirPlay ungrouping bug: when AirPlay playback starts
        # on a Sonos speaker that has native group members, Sonos dissolves the group.
        # We capture the group state here and restore it after a delay.

        self.logger.debug(
            "AirPlay playback starting on %s with native group members %s - "
            "scheduling restoration to work around Sonos ungrouping bug",
            self.name,
            current_members,
        )
        members_to_restore = [m for m in current_members if m != self.player_id]

        async def _restore_airplay_group() -> None:
            try:
                self.logger.info(
                    "Restoring AirPlay group for %s with members %s",
                    self.name,
                    members_to_restore,
                )
                # we call set_members on the PlayerController here so it
                # can try to regroup via the preferred protocol (which may be AirPlay),
                await self.set_members(player_ids_to_add=members_to_restore)
            except Exception as err:
                self.logger.warning("Failed to restore AirPlay group: %s", err)

        # Schedule restoration after 4 seconds to let AirPlay settle
        self.mass.call_later(
            4,
            _restore_airplay_group,
            task_id=f"restore_airplay_group_{self.player_id}",
        )

    def update_elapsed_time(self, elapsed_time: float | None = None) -> None:
        """Update the elapsed time of the current media."""
        if elapsed_time is not None:
            self._attr_elapsed_time = elapsed_time
        last_updated = time.time()
        self._attr_elapsed_time_last_updated = last_updated
        self.update_state()

    async def _connect(self, retry_on_fail: int = 0) -> None:
        """Connect to the Sonos player."""
        if self.mass.closing:
            return
        if self._listen_task and not self._listen_task.done():
            self.logger.debug("Already connected to Sonos player: %s", self.player_id)
            return
        try:
            await self.client.connect()
        except (ConnectionFailed, ClientConnectorError) as err:
            self.logger.warning("Failed to connect to Sonos player: %s", err)
            if not retry_on_fail or not self.mass.players.get_player(self.player_id):
                raise
            self._attr_available = False
            self.update_state()
            self.reconnect(min(retry_on_fail + 30, 3600))
            return
        self.connected = True
        self.logger.debug("Connected to player API")
        init_ready = asyncio.Event()

        async def _listener() -> None:
            try:
                await self.client.start_listening(init_ready)
            except Exception as err:
                if not isinstance(err, ConnectionFailed | asyncio.CancelledError):
                    self.logger.exception("Error in Sonos player listener: %s", err)
            finally:
                self.logger.info("Disconnected from player API")
                if self.connected and not self.mass.closing:
                    # we didn't explicitly disconnect, try to reconnect
                    # this should simply try to reconnect once and if that fails
                    # we rely on mdns to pick it up again later
                    await self._disconnect()
                    self._attr_available = False
                    self.update_state()
                    self.reconnect(5)

        self._listen_task = self.mass.create_task(_listener())
        await init_ready.wait()

    def reconnect(self, delay: float = 1) -> None:
        """Reconnect the player."""
        if self.mass.closing:
            return
        # use a task_id to prevent multiple reconnects
        task_id = f"sonos_reconnect_{self.player_id}"
        self.mass.call_later(delay, self._connect, delay, task_id=task_id)

    async def _disconnect(self) -> None:
        """Disconnect the client and cleanup."""
        self.connected = False
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
        if self.client:
            await self.client.disconnect()
        self.logger.debug("Disconnected from player API")

    async def sync_play_modes(self, queue_id: str) -> None:
        """Sync the play modes between MA and Sonos."""
        queue = self.mass.player_queues.get(queue_id)
        if not queue or queue.state not in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            return
        repeat_single_enabled = queue.repeat_mode == RepeatMode.ONE
        repeat_all_enabled = queue.repeat_mode == RepeatMode.ALL
        play_modes = self.client.player.group.play_modes
        if (
            play_modes.repeat != repeat_all_enabled
            or play_modes.repeat_one != repeat_single_enabled
        ):
            try:
                await self.client.player.group.set_play_modes(
                    repeat=repeat_all_enabled,
                    repeat_one=repeat_single_enabled,
                )
            except FailedCommand as err:
                if "groupCoordinatorChanged" not in str(err):
                    # this may happen at race conditions
                    raise

    async def _set_sonos_queue_from_mass_queue(self, queue_id: str) -> None:
        """Set the SonosQueue items from the given MA PlayerQueue."""
        items: list[PlayerMedia] = []
        queue = self.mass.player_queues.get(queue_id)
        if not queue:
            self.sonos_queue.items.clear()
            return
        current_index = queue.current_index or 0
        current_index = (
            queue.index_in_buffer if queue.index_in_buffer is not None else current_index
        )

        # Add a few items before the current index for context
        offset = max(0, current_index - 4)
        for idx in range(offset, current_index):
            if queue_item := self.mass.player_queues.get_item(queue_id, idx):
                if queue_item.available:
                    media = await self.mass.player_queues.player_media_from_queue_item(queue_item)
                    media.uri = await self.provider.mass.streams.resolve_stream_url(
                        self.player_id, media
                    )
                    items.append(media)

        # Add the current item
        if current_item := self.mass.player_queues.get_item(queue_id, current_index):
            if current_item.available:
                media = await self.mass.player_queues.player_media_from_queue_item(current_item)
                media.uri = await self.provider.mass.streams.resolve_stream_url(
                    self.player_id, media
                )
                items.append(media)

        # Use get_next_item to fetch next items, which accounts for repeat mode
        last_index: int | str = current_index
        for _ in range(5):
            next_item = self.mass.player_queues.get_next_item(queue_id, last_index)
            if next_item is None:
                break
            media = await self.mass.player_queues.player_media_from_queue_item(next_item)
            media.uri = await self.provider.mass.streams.resolve_stream_url(self.player_id, media)
            items.append(media)
            last_index = next_item.queue_item_id

        self.sonos_queue.items = items
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Set Sonos queue items from MA queue %s on player %s: %s",
            queue_id,
            self.player_id,
            [x.title for x in self.sonos_queue.items],
        )

    def _extract_mac_from_player_id(self) -> str | None:
        """Extract MAC address from Sonos player_id.

        Sonos player_ids follow the format RINCON_XXXXXXXXXXXX01400 where
        the middle 12 hex characters represent the MAC address.

        :return: MAC address string in XX:XX:XX:XX:XX:XX format, or None if not extractable.
        """
        # Remove RINCON_ prefix if present
        player_id = self.player_id
        player_id = player_id.removeprefix("RINCON_")  # Remove "RINCON_"

        # Remove the 01400 suffix (or similar) - should be last 5 chars
        if len(player_id) >= 17:  # 12 hex chars for MAC + 5 chars suffix
            mac_hex = player_id[:12]
        else:
            return None

        # Validate it looks like a MAC (all hex characters)
        try:
            int(mac_hex, 16)
        except ValueError:
            return None

        # Format as XX:XX:XX:XX:XX:XX
        return ":".join(mac_hex[i : i + 2].upper() for i in range(0, 12, 2))
