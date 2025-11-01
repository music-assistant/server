"""Chromecast Player implementation."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from music_assistant_models.config_entries import ConfigEntry

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType
from music_assistant_models.enums import MediaType, PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.errors import PlayerUnavailableError
from music_assistant_models.player import PlayerSource
from pychromecast import IDLE_APP_ID
from pychromecast.controllers.media import STREAM_TYPE_BUFFERED, STREAM_TYPE_LIVE
from pychromecast.controllers.multizone import MultizoneController
from pychromecast.socket_client import CONNECTION_STATUS_CONNECTED, CONNECTION_STATUS_DISCONNECTED

from music_assistant.constants import (
    ATTR_ANNOUNCEMENT_IN_PROGRESS,
    MASS_LOGO_ONLINE,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.models.player import DeviceInfo, Player, PlayerMedia

from .constants import (
    APP_MEDIA_RECEIVER,
    CAST_PLAYER_CONFIG_ENTRIES,
    CONF_ENTRY_SAMPLE_RATES_CAST,
    CONF_ENTRY_SAMPLE_RATES_CAST_GROUP,
    CONF_USE_MASS_APP,
    MASS_APP_ID,
)
from .helpers import CastStatusListener, ChromecastInfo

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry
    from pychromecast import Chromecast
    from pychromecast.controllers.media import MediaStatus
    from pychromecast.controllers.receiver import CastStatus
    from pychromecast.socket_client import ConnectionStatus

    from .provider import ChromecastProvider


class ChromecastPlayer(Player):
    """Chromecast Player."""

    active_cast_group: str | None = None

    def __init__(
        self,
        provider: ChromecastProvider,
        player_id: str,
        cast_info: ChromecastInfo,
        chromecast: Chromecast,
    ) -> None:
        """Init."""
        super().__init__(provider, player_id)
        if cast_info.is_audio_group and cast_info.is_multichannel_group:
            player_type = PlayerType.STEREO_PAIR
        elif cast_info.is_audio_group:
            player_type = PlayerType.GROUP
        else:
            player_type = PlayerType.PLAYER
        self.cc = chromecast
        self.status_listener: CastStatusListener | None
        self.cast_info = cast_info
        self.mz_controller: MultizoneController | None = None
        self.last_poll = 0.0
        self.flow_meta_checksum: str | None = None
        # set static variables
        self._attr_supported_features = {
            PlayerFeature.POWER,
            PlayerFeature.VOLUME_SET,
            PlayerFeature.PAUSE,
            PlayerFeature.NEXT_PREVIOUS,
            PlayerFeature.ENQUEUE,
            PlayerFeature.SEEK,
        }
        self._attr_name = self.cast_info.friendly_name
        self._attr_available = False
        self._attr_powered = False
        self._attr_needs_poll = True
        self._attr_type = player_type
        # Disable TV's by default
        # (can be enabled manually by the user)
        enabled_by_default = True
        for exclude in ("tv", "/12", "PUS", "OLED"):
            if exclude.lower() in cast_info.friendly_name.lower():
                enabled_by_default = False
        self._attr_enabled_by_default = enabled_by_default

        self._attr_device_info = DeviceInfo(
            model=self.cast_info.model_name,
            ip_address=f"{self.cast_info.host}:{self.cast_info.port}",
            manufacturer=self.cast_info.manufacturer or "",
        )
        assert provider.mz_mgr is not None  # for type checking
        status_listener = CastStatusListener(self, provider.mz_mgr)
        self.status_listener = status_listener
        if player_type == PlayerType.GROUP:
            mz_controller = MultizoneController(cast_info.uuid)
            self.cc.register_handler(mz_controller)
            self.mz_controller = mz_controller
        self.cc.start()

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        base_entries = await super().get_config_entries(action=action, values=values)
        if self.type == PlayerType.GROUP:
            return [
                *base_entries,
                *CAST_PLAYER_CONFIG_ENTRIES,
                CONF_ENTRY_SAMPLE_RATES_CAST_GROUP,
            ]

        return [*base_entries, *CAST_PLAYER_CONFIG_ENTRIES, CONF_ENTRY_SAMPLE_RATES_CAST]

    async def stop(self) -> None:
        """Send STOP command to given player."""
        await asyncio.to_thread(self.cc.media_controller.stop)

    async def play(self) -> None:
        """Send PLAY command to given player."""
        await asyncio.to_thread(self.cc.media_controller.play)

    async def pause(self) -> None:
        """Send PAUSE command to given player."""
        await asyncio.to_thread(self.cc.media_controller.pause)

    async def next_track(self) -> None:
        """Handle NEXT TRACK command for given player."""
        await asyncio.to_thread(self.cc.media_controller.queue_next)

    async def previous_track(self) -> None:
        """Handle PREVIOUS TRACK command for given player."""
        await asyncio.to_thread(self.cc.media_controller.queue_prev)

    async def seek(self, position: int) -> None:
        """Handle SEEK command on the player."""
        await asyncio.to_thread(self.cc.media_controller.seek, position)

    async def power(self, powered: bool) -> None:
        """Send POWER command to given player."""
        if powered:
            await self._launch_app()
            self._attr_active_source = self.player_id
        else:
            self._attr_active_source = None
            await asyncio.to_thread(self.cc.quit_app)
        # optimistically update the state
        self.mass.loop.call_soon_threadsafe(self.update_state)

    async def volume_set(self, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        await asyncio.to_thread(self.cc.set_volume, volume_level / 100)

    async def volume_mute(self, muted: bool) -> None:
        """Send VOLUME MUTE command to given player."""
        await asyncio.to_thread(self.cc.set_volume_muted, muted)

    async def play_media(
        self,
        media: PlayerMedia,
    ) -> None:
        """Handle PLAY MEDIA on given player."""
        queuedata = {
            "type": "LOAD",
            "media": self._create_cc_media_item(media),
        }
        # make sure that our media controller app is launched
        await self._launch_app()
        # send queue info to the CC
        media_controller = self.cc.media_controller
        await asyncio.to_thread(media_controller.send_message, data=queuedata, inc_session_id=True)
        if media.media_type in (MediaType.RADIO, MediaType.FLOW_STREAM):
            # in flow/radio mode we want to update the metadata more frequently
            # so we can show the current track info
            self._attr_poll_interval = 2

    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        """Handle enqueuing of the next item on the player."""
        next_item_id = None
        status = self.cc.media_controller.status
        # lookup position of current track in cast queue
        cast_current_item_id = getattr(status, "current_item_id", 0)
        cast_queue_items = getattr(status, "items", [])
        cur_item_found = False
        for item in cast_queue_items:
            if item["itemId"] == cast_current_item_id:
                cur_item_found = True
                continue
            if not cur_item_found:
                continue
            next_item_id = item["itemId"]
            # check if the next queue item isn't already queued
            if item.get("media", {}).get("customData", {}).get("uri") == media.uri:
                return
        queuedata = {
            "type": "QUEUE_INSERT",
            "insertBefore": next_item_id,
            "items": [
                {
                    "autoplay": True,
                    "startTime": 0,
                    "preloadTime": 0,
                    "media": self._create_cc_media_item(media),
                }
            ],
        }
        media_controller = self.cc.media_controller
        queuedata["mediaSessionId"] = media_controller.status.media_session_id
        await asyncio.to_thread(media_controller.send_message, data=queuedata, inc_session_id=True)

    async def poll(self) -> None:
        """Poll player for state updates."""
        # only update status of media controller if player is on
        if not self.powered:
            return
        if not self.cc.media_controller.is_active:
            return
        try:
            now = time.time()
            if (now - self.last_poll) >= 60:
                self.last_poll = now
                await asyncio.to_thread(self.cc.media_controller.update_status)
            await self.update_flow_metadata()
        except ConnectionResetError as err:
            raise PlayerUnavailableError from err

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        await super().on_unload()
        self.logger.debug("Disconnecting from chromecast socket %s", self.display_name)
        await self.mass.loop.run_in_executor(None, self.cc.disconnect, 10)
        self.mz_controller = None
        if self.status_listener is not None:
            self.status_listener.invalidate()
        self.status_listener = None

    async def update_flow_metadata(self) -> None:
        """Update the metadata of a cast player running the flow (or radio) stream."""
        if not self.powered:
            self._attr_poll_interval = 300
            return
        if not self.cc.media_controller.status.player_is_playing:
            return
        if self.active_cast_group:
            return
        if self.playback_state != PlaybackState.PLAYING:
            return
        if self.extra_attributes.get(ATTR_ANNOUNCEMENT_IN_PROGRESS):
            return
        if not (current_media := self.current_media):
            return
        if not (
            "/flow/" in self._attr_current_media.uri
            or self.current_media.media_type
            in (
                MediaType.RADIO,
                MediaType.PLUGIN_SOURCE,
            )
        ):
            # only update metadata for streams without known duration
            return
        self._attr_poll_interval = 2
        media_controller = self.cc.media_controller
        # update metadata of current item chromecast
        title = current_media.title or "Music Assistant"
        artist = current_media.artist or ""
        album = current_media.album or ""
        image_url = current_media.image_url or MASS_LOGO_ONLINE
        flow_meta_checksum = f"{current_media.uri}-{album}-{artist}-{title}-{image_url}"
        if self.flow_meta_checksum != flow_meta_checksum:
            # only update if something changed
            self.flow_meta_checksum = flow_meta_checksum
            queuedata = {
                "type": "PLAY",
                "mediaSessionId": media_controller.status.media_session_id,
                "customData": {
                    "metadata": {
                        "metadataType": 3,
                        "albumName": album,
                        "songName": title,
                        "artist": artist,
                        "title": title,
                        "images": [{"url": image_url}],
                    }
                },
            }
            await asyncio.to_thread(
                media_controller.send_message, data=queuedata, inc_session_id=True
            )

        if len(getattr(media_controller.status, "items", [])) < 2:
            # In flow mode, all queue tracks are sent to the player as continuous stream.
            # add a special 'command' item to the queue
            # this allows for on-player next buttons/commands to still work
            cmd_next_url = self.mass.streams.get_command_url(self.player_id, "next")
            msg = {
                "type": "QUEUE_INSERT",
                "mediaSessionId": media_controller.status.media_session_id,
                "items": [
                    {
                        "media": {
                            "contentId": cmd_next_url,
                            "customData": {
                                "uri": cmd_next_url,
                                "queue_item_id": cmd_next_url,
                            },
                            "contentType": "audio/flac",
                            "streamType": STREAM_TYPE_LIVE,
                            "metadata": {},
                        },
                        "autoplay": True,
                        "startTime": 0,
                        "preloadTime": 0,
                    }
                ],
            }
            await asyncio.to_thread(media_controller.send_message, data=msg, inc_session_id=True)

    async def _launch_app(self) -> None:
        """Launch the default Media Receiver App on a Chromecast."""
        event = asyncio.Event()

        if self.config.get_value(CONF_USE_MASS_APP, True):
            app_id = MASS_APP_ID
        else:
            app_id = APP_MEDIA_RECEIVER

        if self.cc.app_id == app_id:
            return  # already active

        def launched_callback(success: bool, response: dict[str, Any] | None) -> None:  # noqa: ARG001
            self.mass.loop.call_soon_threadsafe(event.set)

        def launch() -> None:
            # Quit the previous app before starting splash screen or media player
            if self.cc.app_id is not None:
                self.cc.quit_app()
            self.logger.debug("Launching App %s.", app_id)
            self.cc.socket_client.receiver_controller.launch_app(
                app_id,
                force_launch=True,
                callback_function=launched_callback,
            )

        await self.mass.loop.run_in_executor(None, launch)
        await event.wait()

    ### Callbacks from Chromecast Statuslistener

    def on_new_cast_status(self, status: CastStatus) -> None:
        """Handle updated CastStatus."""
        if status is None:
            return  # guard
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Received cast status for %s - app_id: %s - volume: %s",
            self.display_name,
            status.app_id,
            status.volume_level,
        )
        # handle stereo pairs
        if self.cast_info.is_multichannel_group:
            self._attr_type = PlayerType.STEREO_PAIR
            self.group_members.clear()
        # handle cast groups
        if self.cast_info.is_audio_group and not self.cast_info.is_multichannel_group:
            assert self.mz_controller is not None  # for type checking
            self._attr_type = PlayerType.GROUP
            self._attr_group_members = [str(UUID(x)) for x in self.mz_controller.members]
            self._attr_supported_features = {
                PlayerFeature.POWER,
                PlayerFeature.VOLUME_SET,
                PlayerFeature.PAUSE,
                PlayerFeature.ENQUEUE,
            }

        # update player status
        self._attr_name = self.cast_info.friendly_name
        self._attr_volume_level = int(status.volume_level * 100)
        self._attr_volume_muted = status.volume_muted
        new_powered = self.cc.app_id is not None and self.cc.app_id != IDLE_APP_ID
        self._attr_powered = new_powered
        if self._attr_powered and not new_powered and self._attr_type == PlayerType.GROUP:
            # group is being powered off, update group childs
            for child_id in self.group_members:
                if child := self.mass.players.get(child_id):
                    self.mass.loop.call_soon_threadsafe(child.update_state)
        self.mass.loop.call_soon_threadsafe(self.update_state)

    def on_new_media_status(self, status: MediaStatus) -> None:  # noqa: PLR0915
        """Handle updated MediaStatus."""
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Received media status for %s update: %s",
            self.display_name,
            status.player_state,
        )
        # handle player playing from a group
        group_player: ChromecastPlayer | None = None
        if self.active_cast_group is not None:
            if not (group_player := self.mass.players.get(self.active_cast_group)):
                return
            if not isinstance(group_player, ChromecastPlayer):
                return
            status = group_player.cc.media_controller.status

        # player state
        self._attr_elapsed_time_last_updated = time.time()
        if status.player_is_playing:
            self._attr_playback_state = PlaybackState.PLAYING
            self.set_current_media(uri=status.content_id or "", clear_all=True)
        elif status.player_is_paused:
            self._attr_playback_state = PlaybackState.PAUSED
            self._attr_current_media = None
            self._attr_active_source = None
        else:
            self._attr_playback_state = PlaybackState.IDLE
            self._attr_current_media = None
            self._attr_active_source = None

        # elapsed time
        self._attr_elapsed_time_last_updated = time.time()
        self._attr_elapsed_time = status.adjusted_current_time
        if status.player_is_playing:
            self._attr_elapsed_time = status.adjusted_current_time
        else:
            self._attr_elapsed_time = status.current_time

        # active source
        if group_player:
            self._attr_active_source = group_player.active_source or group_player.player_id
        elif self.cc.app_id in (MASS_APP_ID, APP_MEDIA_RECEIVER):
            self._attr_active_source = self.player_id
        else:
            app_name = self.cc.app_display_name or "Unknown App"
            app_id = app_name.lower().replace(" ", "_")
            self._attr_active_source = app_id
            has_controls = app_name in ("Spotify", "Qobuz", "YouTube Music", "Deezer", "Tidal")
            if not any(source.id == app_id for source in self._attr_source_list):
                self._attr_source_list.append(
                    PlayerSource(
                        id=app_id,
                        name=app_name,
                        passive=True,
                        can_play_pause=has_controls,
                        can_seek=has_controls,
                        can_next_previous=has_controls,
                    )
                )

        if status.content_id and not status.player_is_idle:
            self.set_current_media(
                uri=status.content_id,
                title=status.title,
                artist=status.artist,
                album=status.album_name,
                image_url=status.images[0].url if status.images else None,
                duration=int(status.duration) if status.duration is not None else None,
                media_type=MediaType.TRACK,
            )
        else:
            self._attr_current_media = None

        # weird workaround which is needed for multichannel group childs
        # (e.g. a stereo pair within a cast group)
        # where it does not receive updates from the group,
        # so we need to update the group child(s) manually
        if self.type == PlayerType.GROUP and self.powered:
            for child_id in self.group_members:
                if child := self.mass.players.get(child_id):
                    assert isinstance(child, ChromecastPlayer)  # for type checking
                    if not child.cast_info.is_multichannel_group:
                        continue
                    child._attr_playback_state = self.playback_state
                    child._attr_current_media = self.current_media
                    child._attr_elapsed_time = self.elapsed_time
                    child._attr_elapsed_time_last_updated = self.elapsed_time_last_updated
                    child._attr_active_source = self.active_source
                    self.mass.loop.call_soon_threadsafe(child.update_state)
        self.mass.loop.call_soon_threadsafe(self.update_state)

    def on_new_connection_status(self, status: ConnectionStatus) -> None:
        """Handle updated ConnectionStatus."""
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Received connection status update for %s - status: %s",
            self.display_name,
            status.status,
        )

        if status.status == CONNECTION_STATUS_DISCONNECTED:
            self._attr_available = False
            self.mass.loop.call_soon_threadsafe(self.update_state)
            return

        new_available = status.status == CONNECTION_STATUS_CONNECTED
        if new_available != self.available:
            self.logger.debug(
                "[%s] Cast device availability changed: %s",
                self.cast_info.friendly_name,
                status.status,
            )
            self._attr_available = new_available
            self._attr_device_info = DeviceInfo(
                model=self.cast_info.model_name,
                ip_address=f"{self.cast_info.host}:{self.cast_info.port}",
                manufacturer=self.cast_info.manufacturer or "",
            )
            self.mass.loop.call_soon_threadsafe(self.update_state)

            if new_available and self.type == PlayerType.PLAYER:
                # Poll current group status
                provider = cast("ChromecastProvider", self.provider)
                mz_mgr = provider.mz_mgr
                assert mz_mgr is not None  # for type checking
                for group_uuid in mz_mgr.get_multizone_memberships(self.cast_info.uuid):
                    group_media_controller = mz_mgr.get_multizone_mediacontroller(UUID(group_uuid))
                    if not group_media_controller:
                        continue

    def _create_cc_media_item(self, media: PlayerMedia) -> dict[str, Any]:
        """Create CC media item from MA PlayerMedia."""
        if media.media_type == MediaType.TRACK:
            stream_type = STREAM_TYPE_BUFFERED
        else:
            stream_type = STREAM_TYPE_LIVE
        metadata = {
            "metadataType": 3,
            "albumName": media.album or "",
            "songName": media.title or "",
            "artist": media.artist or "",
            "title": media.title or "",
            "images": [{"url": media.image_url}] if media.image_url else None,
        }
        return {
            "contentId": media.uri,
            "customData": {
                "uri": media.uri,
                "queue_item_id": media.uri,
            },
            "contentType": "audio/flac",
            "streamType": stream_type,
            "metadata": metadata,
            "duration": media.duration,
        }
