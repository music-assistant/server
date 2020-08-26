"""Representation of a Cast device on the network."""
from typing import List, Optional

import pychromecast
from pychromecast.socket_client import (
    CONNECTION_STATUS_CONNECTED,
    CONNECTION_STATUS_DISCONNECTED,
)

from music_assistant.models.player import DeviceInfo, Player, PlayerState
from music_assistant.models.player_queue import QueueItem
from music_assistant.utils import LOGGER

from .const import PROV_ID, PROVIDER_CONFIG_ENTRIES
from .models import CastStatusListener, ChromecastInfo





class ChromecastPlayer():
    """Representation of a Cast device on the network.
       This class is the holder of the pychromecast.Chromecast object and its
       socket client. It therefore handles all reconnects and audio group changing
       "elected leader" itself.
    """

    def __init__(self, mass, cast_info: ChromecastInfo):
        """Initialize the cast device."""
        self.mass = mass
        self._player = Player(
            player_id=cast_info.uuid,
            id=PROV_ID)
        self._cast_info = cast_info
        self.services = cast_info.services
        self._chromecast: Optional[pychromecast.Chromecast] = None
        self.cast_status = None
        self.media_status = None
        self.media_status_received = None
        self.mz_media_status = {}
        self.mz_media_status_received = {}
        self.mz_mgr = None
        self._available = False
        self._status_listener: Optional[CastStatusListener] = None

    def get_state(self) -> PlayerState:
        """Return the state of the player."""
        if self.media_status is None:
            return None
        if self.media_status.player_is_playing:
            return PlayerState.Playing
        if self.media_status.player_is_paused:
            return PlayerState.Paused
        if self.media_status.player_is_idle:
            return PlayerState.Stopped
        if self._chromecast is not None and self._chromecast.is_idle:
            return PlayerState.Off            
        return PlayerState.Stopped

    def get_current_time(self) -> int:
        """Return position of current playing media in seconds."""
        if self.media_status is None or not (
            self.media_status.player_is_playing
            or self.media_status.player_is_paused
            or self.media_status.player_is_idle
        ):
            return None
        return self.media_status.current_time

    async def async_set_cast_info(self, cast_info: ChromecastInfo):
        """Set the cast information and set up the chromecast object."""
        self._cast_info = cast_info
        self._player.name = cast_info.friendly_name
        self._player.available = False
        self._player.is_group_player = cast_info.is_audio_group
        self._player.device_info = DeviceInfo(
            model=cast_info.model_name, 
            address=f"{cast_info.host}:{cast_info.port}",
            manufacturer=cast_info.manufacturer)

        if self._chromecast is not None:
            return
        LOGGER.debug(
            "[%s] Connecting to cast device by service %s", self._cast_info.friendly_name, self.services)
        chromecast = await self.mass.async_add_executor_job(
            pychromecast.get_chromecast_from_service,
            (
                self.services,
                cast_info.uuid,
                cast_info.model_name,
                cast_info.friendly_name,
                None,
                None,
            ),
            self.mass.zeroconf,
        )
        self._chromecast = chromecast
        self.mz_mgr = self.mass.player_manager.providers[PROV_ID].mz_mgr

        self._status_listener = CastStatusListener(self, chromecast, self.mz_mgr)
        self._available = False
        self.cast_status = chromecast.status
        self.media_status = chromecast.media_controller.status
        self._chromecast.start()

    async def _async_disconnect(self):
        """Disconnect Chromecast object if it is set."""
        # if self.__cc_report_progress_task:
        #     self.__cc_report_progress_task.cancel()
        if self._chromecast is None:
            return
        LOGGER.warning(
            "[%s] Disconnecting from chromecast socket", self._cast_info.friendly_name)
        self._available = False
        await self.mass.async_add_executor_job(self._chromecast.disconnect)
        self._invalidate()

    def _invalidate(self):
        """Invalidate some attributes."""
        self._chromecast = None
        self.cast_status = None
        self.media_status = None
        self.media_status_received = None
        self.mz_media_status = {}
        self.mz_media_status_received = {}
        self.mz_mgr = None
        if self._status_listener is not None:
            self._status_listener.invalidate()
            self._status_listener = None

    # ========== Callbacks ==========

    def new_cast_status(self, cast_status):
        """Handle updates of the cast status."""
        LOGGER.info("received cast status for %s", self.name)
        self.cast_status = cast_status
        self._player.muted = cast_status.volume_muted
        self._player.volume_level = cast_status.volume_level * 100
        self._player.name = self._chromecast.name
        # TODO: handle update to player manager

    def new_media_status(self, media_status):
        """Handle updates of the media status."""
        LOGGER.info("received media_status for %s", self.name)
        self.media_status = media_status
        self._player.state = self.get_state()
        self._player.current_uri = media_status.content_id if media_status else None
        self._player.elapsed_time = self.get_current_time()
        self._player.powered = self._player.state != PlayerState.Off
        # TODO: handle update to player manager


    def new_connection_status(self, connection_status):
        """Handle updates of connection status."""
        LOGGER.info("received connection_status for %s", self.name)
        if connection_status.status == CONNECTION_STATUS_DISCONNECTED:
            self._available = False
            self._invalidate()
            self.mass.async_create_task(self.update())
            return

        new_available = connection_status.status == CONNECTION_STATUS_CONNECTED
        if new_available != self._available:
            # Connection status callbacks happen often when disconnected.
            # Only update state when availability changed to put less pressure
            # on state machine.
            LOGGER.debug(
                "[%s] Cast device availability changed: %s",
                self._cast_info.friendly_name,
                connection_status.status,
            )
            self._available = new_available
            self.mass.async_create_task(self.update())

    def multizone_new_media_status(self, group_uuid, media_status):
        """Handle updates of audio group media status."""
        LOGGER.info("received multizone_new_media_status for %s", self.name)
        self.mz_media_status[group_uuid] = media_status
        self.new_media_status(media_status)
        # self.mz_media_status_received[group_uuid] = dt_util.utcnow()
        # self.mass.async_create_task(self.update())

    @property
    def media_controller(self):
        """
        Return media controller.
        First try from our own cast, then groups which our cast is a member in.
        """
        media_status = self.media_status
        media_controller = self._chromecast.media_controller

        if media_status is None or media_status.player_state == "UNKNOWN":
            groups = self.mz_media_status
            for k, val in groups.items():
                if val and val.player_state != "UNKNOWN":
                    media_controller = self.mz_mgr.get_multizone_mediacontroller(k)
                    break

        return media_controller

    # ========== Service Calls ==========

    def stop(self):
        """Send stop command to player."""
        self.media_controller.stop()

    def play(self):
        """Send play command to player."""
        self.media_controller.play()

    def pause(self):
        """Send pause command to player."""
        self.media_controller.pause()

    def next(self):
        """Send next track command to player."""
        self.media_controller.queue_next()

    def previous(self):
        """Send previous track command to player."""
        self.media_controller.queue_prev()

    def power_on(self):
        """Send power ON command to player."""
        self.powered = True

    def power_off(self):
        """Send power OFF command to player."""
        self.powered = False

    def volume_set(self, volume_level):
        """Send new volume level command to player."""
        self._chromecast.set_volume(volume_level)
        # self.volume_level = volume_level

    def volume_mute(self, is_muted=False):
        """Send mute command to player."""
        self._chromecast.set_volume_muted(is_muted)

    def play_uri(self, uri: str):
        """Play single uri on player."""
        if self.queue.use_queue_stream:
            # create CC queue so that skip and previous will work
            queue_item = QueueItem()
            queue_item.name = "Music Assistant"
            queue_item.uri = uri
            return self.queue_load([queue_item, queue_item])
        else:
            self._chromecast.play_media(uri, "audio/flac")

    def queue_load(self, queue_items: List[QueueItem]):
        """load (overwrite) queue with new items"""
        cc_queue_items = self.__create_queue_items(queue_items[:50])
        queuedata = {
            "type": "QUEUE_LOAD",
            "repeatMode": "REPEAT_ALL" if self.queue.repeat_enabled else "REPEAT_OFF",
            "shuffle": False,  # handled by our queue controller
            "queueType": "PLAYLIST",
            "startIndex": 0,  # Item index to play after this request or keep same item if undefined
            "items": cc_queue_items,  # only load 50 tracks at once or the socket will crash
        }
        self.__send_player_queue(queuedata)
        if len(queue_items) > 50:
            self.queue_append(queue_items[51:])

    def queue_append(self, queue_items: List[QueueItem]):
        """
            append new items at the end of the queue
        """
        cc_queue_items = self.__create_queue_items(queue_items)
        for chunk in chunks(cc_queue_items, 50):
            queuedata = {"type": "QUEUE_INSERT", "insertBefore": None, "items": chunk}
            self.__send_player_queue(queuedata)

    def __create_queue_items(self, tracks):
        """create list of CC queue items from tracks"""
        queue_items = []
        for track in tracks:
            queue_item = self.__create_queue_item(track)
            queue_items.append(queue_item)
        return queue_items

    def __create_queue_item(self, track):
        """create CC queue item from track info"""
        return {
            "opt_itemId": track.queue_item_id,
            "autoplay": True,
            "preloadTime": 10,
            "playbackDuration": int(track.duration),
            "startTime": 0,
            "activeTrackIds": [],
            "media": {
                "contentId": track.uri,
                "customData": {
                    "provider": track.provider,
                    "uri": track.uri,
                    "item_id": track.queue_item_id,
                },
                "contentType": "audio/flac",
                "streamType": "LIVE" if self.queue.use_queue_stream else "BUFFERED",
                "metadata": {
                    "title": track.name,
                    "artist": track.artists[0].name if track.artists else "",
                },
                "duration": int(track.duration),
            },
        }

    def __send_player_queue(self, queuedata):
        """Send new data to the CC queue"""
        media_controller = self.media_controller
        receiver_ctrl = media_controller._socket_client.receiver_controller

        def send_queue():
            """Plays media after chromecast has switched to requested app."""
            queuedata["mediaSessionId"] = media_controller.status.media_session_id
            media_controller.send_message(queuedata, inc_session_id=False)

        if not media_controller.status.media_session_id:
            receiver_ctrl.launch_app(
                media_controller.app_id, callback_function=send_queue
            )
        else:
            send_queue()
