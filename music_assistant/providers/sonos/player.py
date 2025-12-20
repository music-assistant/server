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
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import (
    ConfigEntryType,
    EventType,
    MediaType,
    PlaybackState,
    PlayerFeature,
    RepeatMode,
)
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.player import PlayerMedia

from music_assistant.constants import (
    CONF_ENTRY_HTTP_PROFILE_DEFAULT_2,
    CONF_ENTRY_OUTPUT_CODEC,
    create_sample_rates_config_entry,
)
from music_assistant.helpers.tags import async_parse_tags
from music_assistant.helpers.upnp import get_xml_soap_set_next_url, get_xml_soap_set_url
from music_assistant.models.player import Player
from music_assistant.providers.sonos.const import (
    CONF_AIRPLAY_MODE,
    PLAYBACK_STATE_MAP,
    PLAYER_SOURCE_MAP,
    SOURCE_AIRPLAY,
    SOURCE_LINE_IN,
    SOURCE_RADIO,
    SOURCE_SPOTIFY,
    SOURCE_TV,
)

if TYPE_CHECKING:
    from aiosonos.api.models import DiscoveryInfo as SonosDiscoveryInfo
    from music_assistant_models.event import MassEvent

    from .provider import SonosPlayerProvider

SUPPORTED_FEATURES = {
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
        # Sonos speakers can optionally have airplay (most S2 speakers do)
        # and this airplay player can also be a player within MA.
        # We can do some smart stuff if we link them together where possible.
        # The player we can just guess from the sonos player id (mac address).
        self.airplay_player_id = f"ap{self.player_id[7:-5].lower()}"
        self.sonos_queue: SonosQueue = SonosQueue()

    @property
    def airplay_mode_enabled(self) -> bool:
        """Return if airplay mode is enabled for the player."""
        return self.mass.config.get_raw_player_config_value(
            self.player_id, CONF_AIRPLAY_MODE, False
        )

    @property
    def airplay_mode_active(self) -> bool:
        """Return if airplay mode is active for the player."""
        return (
            self.airplay_mode_enabled
            and self.client.player.is_coordinator
            and (airplay_player := self.get_linked_airplay_player(False))
            and airplay_player.playback_state in (PlaybackState.PLAYING, PlaybackState.PAUSED)
        )

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
        if SonosCapability.AUDIO_CLIP in self.discovery_info["device"]["capabilities"]:
            _supported_features.add(PlayerFeature.PLAY_ANNOUNCEMENT)
        if not self.client.player.has_fixed_volume:
            _supported_features.add(PlayerFeature.VOLUME_SET)
            _supported_features.add(PlayerFeature.VOLUME_MUTE)
        if not self.get_linked_airplay_player(False):
            _supported_features.add(PlayerFeature.NEXT_PREVIOUS)
        if not self.get_linked_airplay_player(True):
            _supported_features.add(PlayerFeature.ENQUEUE)
        self._attr_supported_features = _supported_features

        self._attr_name = (
            self.discovery_info["device"]["name"]
            or self.discovery_info["device"]["modelDisplayName"]
        )
        self._attr_device_info.model = self.discovery_info["device"]["modelDisplayName"]
        self._attr_device_info.manufacturer = self._provider.manifest.name
        self._attr_can_group_with = {self._provider.instance_id}

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
        # register callback for airplay player state changes
        self._on_unload_callbacks.append(
            self.mass.subscribe(
                self._on_airplay_player_event,
                (EventType.PLAYER_UPDATED, EventType.PLAYER_ADDED),
                self.airplay_player_id,
            )
        )

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the player."""
        base_entries = [
            *await super().get_config_entries(action=action, values=values),
            CONF_ENTRY_OUTPUT_CODEC,
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
        return [
            *base_entries,
            ConfigEntry(
                key="airplay_detected",
                type=ConfigEntryType.BOOLEAN,
                label="airplay_detected",
                hidden=True,
                required=False,
                default_value=self.get_linked_airplay_player(False) is not None,
            ),
            ConfigEntry(
                key=CONF_AIRPLAY_MODE,
                type=ConfigEntryType.BOOLEAN,
                label="Enable AirPlay mode",
                description="Almost all newer Sonos speakers have AirPlay support. "
                "If you have the AirPlay provider enabled in Music Assistant, "
                "your Sonos speaker will also be detected as a AirPlay speaker, meaning "
                "you can group them with other AirPlay speakers.\n\n"
                "By default, Music Assistant uses the Sonos protocol for playback but with this "
                "feature enabled, it will use the AirPlay protocol instead by redirecting "
                "the playback related commands to the linked AirPlay player in Music Assistant, "
                "allowing you to mix and match Sonos speakers with AirPlay speakers. \n\n"
                "NOTE: You need to have the AirPlay provider enabled as well as "
                "the AirPlay version of this player.",
                required=False,
                default_value=False,
                depends_on="airplay_detected",
                hidden=SonosCapability.AIRPLAY not in self.discovery_info["device"]["capabilities"],
            ),
        ]

    def get_linked_airplay_player(self, enabled_only: bool = True) -> Player | None:
        """Return the linked airplay player if available/enabled."""
        if enabled_only and not self.airplay_mode_enabled:
            return None
        if not (airplay_player := self.mass.players.get(self.airplay_player_id)):
            return None
        if not airplay_player.available:
            return None
        return airplay_player

    async def volume_set(self, volume_level: int) -> None:
        """
        Handle VOLUME_SET command on the player.

        Will only be called if the PlayerFeature.VOLUME_SET is supported.

        :param volume_level: volume level (0..100) to set on the player.
        """
        await self.client.player.set_volume(volume_level)
        # sync volume level with airplay player
        if airplay_player := self.get_linked_airplay_player(False):
            if airplay_player.playback_state not in (PlaybackState.PLAYING, PlaybackState.PAUSED):
                airplay_player._attr_volume_level = volume_level

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
            self.logger.debug("Ignore STOP command: Player is synced to another player.")
            return
        if airplay_player := self.get_linked_airplay_player(True):
            # linked airplay player is active, redirect the command
            self.logger.debug("Redirecting PLAY command to linked airplay player.")
            await airplay_player.play()
        else:
            await self.client.player.group.play()

    async def stop(self) -> None:
        """Handle STOP command on the player."""
        if self.client.player.is_passive:
            self.logger.debug("Ignore STOP command: Player is synced to another player.")
            return
        if (airplay_player := self.get_linked_airplay_player(True)) and self.airplay_mode_active:
            # linked airplay player is active, redirect the command
            self.logger.debug("Redirecting STOP command to linked airplay player.")
            await airplay_player.stop()
        else:
            await self.client.player.group.stop()
        self.update_state()

    async def pause(self) -> None:
        """
        Handle PAUSE command on the player.

        Will only be called if the player reports PlayerFeature.PAUSE is supported.
        """
        if self.client.player.is_passive:
            self.logger.debug("Ignore STOP command: Player is synced to another player.")
            return
        if (airplay_player := self.get_linked_airplay_player(True)) and self.airplay_mode_active:
            # linked airplay player is active, redirect the command
            self.logger.debug("Redirecting PAUSE command to linked airplay player.")
            await airplay_player.pause()
            return
        active_source = self._attr_active_source
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
        if airplay_player := self.get_linked_airplay_player(True):
            # airplay mode is enabled, redirect the command
            self.logger.debug("Redirecting PLAY_MEDIA command to linked airplay player.")
            await self._play_media_airplay(airplay_player, media)
            return
        if media.source_id:
            await self._set_sonos_queue_from_mass_queue(media.source_id)

        if (
            not self.flow_mode and media.source_id and media.queue_item_id
        ) or media.media_type == MediaType.PLUGIN_SOURCE:
            # Regular Queue item playback
            # create a sonos cloud queue and load it
            cloud_queue_url = f"{self.mass.streams.base_url}/sonos_queue/v2.3/"
            await self.client.player.group.play_cloud_queue(
                cloud_queue_url,
                item_id=media.queue_item_id,
            )
            return

        # play duration-less (long running) radio streams
        # enforce AAC here because Sonos really does not support FLAC streams without duration
        media.uri = media.uri.replace(".flac", ".aac").replace(".wav", ".aac")
        if media.source_id and media.queue_item_id:
            object_id = f"mass:{media.source_id}:{media.queue_item_id}"
        else:
            object_id = media.uri
        await self.client.player.group.play_stream_url(
            media.uri,
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
        if airplay_player := self.get_linked_airplay_player(False):
            # if airplay mode is enabled, we could possibly receive child player id's that are
            # not Sonos players, but AirPlay players. We redirect those.
            airplay_player_ids_to_add = {x for x in player_ids_to_add if x.startswith("ap")}
            airplay_player_ids_to_remove = {x for x in player_ids_to_remove if x.startswith("ap")}
            if airplay_player_ids_to_add or airplay_player_ids_to_remove:
                await self.mass.players.cmd_set_members(
                    airplay_player.player_id,
                    player_ids_to_add=list(airplay_player_ids_to_add),
                    player_ids_to_remove=list(airplay_player_ids_to_remove),
                )
        sonos_player_ids_to_add = {x for x in player_ids_to_add if not x.startswith("ap")}
        sonos_player_ids_to_remove = {x for x in player_ids_to_remove if not x.startswith("ap")}
        if sonos_player_ids_to_add or sonos_player_ids_to_remove:
            await self.client.player.group.modify_group_members(
                player_ids_to_add=list(sonos_player_ids_to_add),
                player_ids_to_remove=list(sonos_player_ids_to_remove),
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
        airplay_player = self.get_linked_airplay_player(False)
        if self.client.player.is_coordinator:
            # player is group coordinator
            active_group = self.client.player.group
            if len(self.client.player.group_members) > 1:
                self._attr_group_members = list(self.client.player.group_members)
            else:
                self._attr_group_members.clear()
            # append airplay child's to group childs
            if self.airplay_mode_enabled and airplay_player:
                airplay_childs = [
                    x for x in airplay_player._attr_group_members if x != airplay_player.player_id
                ]
                self._attr_group_members.extend(airplay_childs)
                airplay_prov = airplay_player.provider
                self._attr_can_group_with.update(
                    x.player_id
                    for x in airplay_prov.players
                    if x.player_id != airplay_player.player_id
                )
            else:
                self._attr_can_group_with = {self._provider.instance_id}
        else:
            # player is group child (synced to another player)
            group_parent: SonosPlayer = self.mass.players.get(
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
            # check if the MA airplay player is active
            if airplay_player and airplay_player.playback_state in (
                PlaybackState.PLAYING,
                PlaybackState.PAUSED,
            ):
                self._attr_playback_state = airplay_player.playback_state
                self._attr_active_source = airplay_player.active_source
                self._attr_elapsed_time = airplay_player.elapsed_time
                self._attr_elapsed_time_last_updated = airplay_player.elapsed_time_last_updated
                self._attr_current_media = airplay_player.current_media
                # return early as we dont need further info
                return
            else:
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
            if (object_id := container.get("id", {}).get("objectId")) and object_id.startswith(
                "mass:"
            ):
                self._attr_active_source = object_id.split(":")[1]
            else:
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
            if not retry_on_fail or not self.mass.players.get(self.player_id):
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

    def _on_airplay_player_event(self, event: MassEvent) -> None:
        """Handle incoming event from linked airplay player."""
        if not self.mass.config.get_raw_player_config_value(self.player_id, CONF_AIRPLAY_MODE):
            return
        if event.object_id != self.airplay_player_id:
            return
        self.update_attributes()
        self.update_state()

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

    async def _play_media_airplay(
        self,
        airplay_player: Player,
        media: PlayerMedia,
    ) -> None:
        """Handle PLAY MEDIA using the legacy upnp api."""
        player_id = self.player_id
        if (
            airplay_player.playback_state == PlaybackState.PLAYING
            and airplay_player.active_source == media.source_id
        ):
            # if the airplay player is already playing,
            # the stream will be reused so no need to do the whole grouping thing below
            await self.mass.players.play_media(airplay_player.player_id, media)
            return

        # Sonos has an annoying bug (for years already, and they dont seem to care),
        # where it looses its sync childs when airplay playback is (re)started.
        # Try to handle it here with this workaround.
        org_group_childs = {x for x in self.client.player.group.player_ids if x != player_id}
        if org_group_childs:
            # ungroup all childs first
            await self.client.player.group.modify_group_members(
                player_ids_to_add=[], player_ids_to_remove=list(org_group_childs)
            )
        # start playback on the airplay player
        await self.mass.players.play_media(airplay_player.player_id, media)
        # re-add the original group childs to the sonos player if needed
        if org_group_childs:
            # wait a bit to let the airplay playback start
            await asyncio.sleep(3)
            await self.client.player.group.modify_group_members(
                player_ids_to_add=list(org_group_childs),
                player_ids_to_remove=[],
            )

    async def _play_media_legacy(
        self,
        media: PlayerMedia,
    ) -> None:
        """Handle PLAY MEDIA using the legacy upnp api."""
        xml_data, soap_action = get_xml_soap_set_url(media)
        player_ip = self.device_info.ip_address
        async with self.mass.http_session_no_ssl.post(
            f"http://{player_ip}:1400/MediaRenderer/AVTransport/Control",
            headers={
                "SOAPACTION": soap_action,
                "Content-Type": "text/xml; charset=utf-8",
                "Connection": "close",
            },
            data=xml_data,
        ) as resp:
            if resp.status != 200:
                raise PlayerCommandFailed(
                    f"Failed to send command to Sonos player: {resp.status} {resp.reason}"
                )
            await self.play()

    async def _enqueue_next_legacy(
        self,
        media: PlayerMedia,
    ) -> None:
        """Handle enqueuing of the next (queue) item on the player using legacy upnp api."""
        xml_data, soap_action = get_xml_soap_set_next_url(media)
        player_ip = self.device_info.ip_address
        async with self.mass.http_session_no_ssl.post(
            f"http://{player_ip}:1400/MediaRenderer/AVTransport/Control",
            headers={
                "SOAPACTION": soap_action,
                "Content-Type": "text/xml; charset=utf-8",
                "Connection": "close",
            },
            data=xml_data,
        ) as resp:
            if resp.status != 200:
                raise PlayerCommandFailed(
                    f"Failed to send command to Sonos player: {resp.status} {resp.reason}"
                )

    async def _set_sonos_queue_from_mass_queue(self, queue_id: str) -> None:
        """Set the SonosQueue items from the given MA PlayerQueue."""
        items: list[PlayerMedia] = []
        queue = self.mass.player_queues.get(queue_id)
        if not queue:
            self.sonos_queue.items.clear()
            return
        current_index = queue.current_index or 0

        # Add a few items before the current index for context
        offset = max(0, current_index - 4)
        for idx in range(offset, current_index):
            if queue_item := self.mass.player_queues.get_item(queue_id, idx):
                if queue_item.available:
                    media = await self.mass.player_queues.player_media_from_queue_item(
                        queue_item, False
                    )
                    items.append(media)

        # Add the current item
        if current_item := self.mass.player_queues.get_item(queue_id, current_index):
            if current_item.available:
                media = await self.mass.player_queues.player_media_from_queue_item(
                    current_item, False
                )
                items.append(media)

        # Use get_next_item to fetch next items, which accounts for repeat mode
        last_index: int | str = current_index
        for _ in range(5):
            next_item = self.mass.player_queues.get_next_item(queue_id, last_index)
            if next_item is None:
                break
            media = await self.mass.player_queues.player_media_from_queue_item(next_item, False)
            items.append(media)
            last_index = next_item.queue_item_id

        self.sonos_queue.items = items
        self.logger.debug(
            "Set Sonos queue items from MA queue %s: %s",
            queue_id,
            [x.title for x in self.sonos_queue.items],
        )
