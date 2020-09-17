"""Representation of a Cast device on the network."""
import logging
import uuid
from datetime import datetime
from typing import List, Optional

import pychromecast
from music_assistant.models.player import DeviceInfo, PlayerFeature, PlayerState
from music_assistant.models.player_queue import QueueItem
from music_assistant.utils import compare_strings
from pychromecast.controllers.multizone import MultizoneController
from pychromecast.socket_client import (
    CONNECTION_STATUS_CONNECTED,
    CONNECTION_STATUS_DISCONNECTED,
)

from .const import PLAYER_CONFIG_ENTRIES, PROV_ID
from .models import CastStatusListener, ChromecastInfo

LOGGER = logging.getLogger(PROV_ID)
PLAYER_FEATURES = [PlayerFeature.QUEUE]


class ChromecastPlayer:
    """Representation of a Cast device on the network.

    This class is the holder of the pychromecast.Chromecast object and
    handles all reconnects and audio group changing
    "elected leader" itself.
    """

    def __init__(self, mass, cast_info: ChromecastInfo):
        """Initialize the cast device."""
        self.mass = mass
        self.features = PLAYER_FEATURES
        self.config_entries = PLAYER_CONFIG_ENTRIES
        self.provider_id = PROV_ID
        self._cast_info = cast_info
        self.services = cast_info.services
        self._chromecast: Optional[pychromecast.Chromecast] = None
        self.cast_status = None
        self.media_status = None
        self.media_status_received = None
        self.mz_mgr = None
        self.mz_manager = None
        self._available = False
        self._powered = False
        self._status_listener: Optional[CastStatusListener] = None
        self._is_speaker_group = False
        self.last_updated = datetime.utcnow()

    @property
    def player_id(self):
        """Return id of this player."""
        return self._cast_info.uuid

    @property
    def name(self):
        """Return name of this player."""
        return (
            self._chromecast.name if self._chromecast else self._cast_info.friendly_name
        )

    @property
    def powered(self):
        """Return power state of this player."""
        return self._powered

    @property
    def should_poll(self):
        """Return bool if this player needs to be polled for state changes."""
        if not self._chromecast or not self._chromecast.media_controller:
            return False
        return self._chromecast.media_controller.status.player_is_playing

    @property
    def state(self) -> PlayerState:
        """Return the state of the player."""
        if self.media_status is None:
            return PlayerState.Stopped
        if self.media_status.player_is_playing:
            return PlayerState.Playing
        if self.media_status.player_is_paused:
            return PlayerState.Paused
        if self.media_status.player_is_idle:
            return PlayerState.Stopped
        return PlayerState.Stopped

    @property
    def elapsed_time(self) -> int:
        """Return position of current playing media in seconds."""
        if self.media_status is None or not (
            self.media_status.player_is_playing
            or self.media_status.player_is_paused
            or self.media_status.player_is_idle
        ):
            return 0

        if self.media_status.player_is_playing:
            return self._chromecast.media_controller.status.adjusted_current_time
        # Not playing, return last reported seek time
        return self.media_status.current_time

    @property
    def available(self):
        """Return availablity state of this player."""
        return self._available

    @property
    def current_uri(self):
        """Return current_uri of this player."""
        return self.media_status.content_id if self.media_status else None

    @property
    def volume_level(self):
        """Return volume_level of this player."""
        return int(self.cast_status.volume_level * 100 if self.cast_status else 0)

    @property
    def muted(self):
        """Return mute state of this player."""
        return self.cast_status.volume_muted if self.cast_status else False

    @property
    def is_group_player(self):
        """Return if this player is a group player."""
        return self._cast_info.is_audio_group and not self._is_speaker_group

    @property
    def group_childs(self):
        """Return group_childs."""
        if (
            self._cast_info.is_audio_group
            and self._chromecast
            and not self._is_speaker_group
        ):
            return [
                str(uuid.UUID(item)) for item in self._chromecast.mz_controller.members
            ]
        return []

    @property
    def device_info(self):
        """Return deviceinfo."""
        return DeviceInfo(
            model=self._cast_info.model_name,
            address=f"{self._cast_info.host}:{self._cast_info.port}",
            manufacturer=self._cast_info.manufacturer,
        )

    async def set_cast_info(self, cast_info: ChromecastInfo):
        """Set the cast information and set up the chromecast object."""
        self._cast_info = cast_info
        if self._chromecast is not None:
            return
        LOGGER.debug(
            "[%s] Connecting to cast device by service %s",
            self._cast_info.friendly_name,
            self.services,
        )
        chromecast = pychromecast.get_chromecast_from_service(
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
        self.mz_mgr = self.mass.get_provider(PROV_ID).mz_mgr

        self._status_listener = CastStatusListener(self, chromecast, self.mz_mgr)
        self._available = False
        self.cast_status = chromecast.status
        self.media_status = chromecast.media_controller.status
        mz_controller = MultizoneController(chromecast.uuid)
        # mz.register_listener(
        #     MZListener(mz, self.__handle_group_members_update, self.mass.loop)
        # )
        chromecast.register_handler(mz_controller)
        chromecast.mz_controller = mz_controller
        self._chromecast.start()

    def disconnect(self):
        """Disconnect Chromecast object if it is set."""
        if self._chromecast is None:
            return
        LOGGER.warning(
            "[%s] Disconnecting from chromecast socket", self._cast_info.friendly_name
        )
        self._available = False
        self._chromecast.disconnect()
        self._invalidate()

    def _invalidate(self):
        """Invalidate some attributes."""
        self._chromecast = None
        self.cast_status = None
        self.media_status = None
        self.media_status_received = None
        self.mz_mgr = None
        if self._status_listener is not None:
            self._status_listener.invalidate()
            self._status_listener = None

    # ========== Callbacks ==========

    def new_cast_status(self, cast_status):
        """Handle updates of the cast status."""
        LOGGER.debug("received cast status for %s", self.name)
        self.cast_status = cast_status
        self._is_speaker_group = (
            self._cast_info.is_audio_group
            and self._chromecast.mz_controller
            and self._chromecast.mz_controller.members
            and compare_strings(
                self._chromecast.mz_controller.members[0], self.player_id
            )
        )
        self.mass.add_job(self.mass.player_manager.async_update_player(self))

    def new_media_status(self, media_status):
        """Handle updates of the media status."""
        LOGGER.debug("received media_status for %s", self.name)
        self.media_status = media_status
        self.mass.add_job(self.mass.player_manager.async_update_player(self))
        if media_status.player_is_playing:
            self._powered = True

    def new_connection_status(self, connection_status):
        """Handle updates of connection status."""
        LOGGER.debug("received connection_status for %s", self._cast_info.friendly_name)
        if connection_status.status == CONNECTION_STATUS_DISCONNECTED:
            self._available = False
            self._invalidate()
            self.mass.add_job(self.mass.player_manager.async_update_player(self))
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
            self.mass.add_job(self.mass.player_manager.async_update_player(self))
            if self._cast_info.is_audio_group and new_available:
                self._chromecast.mz_controller.update_members()

    # ========== Service Calls ==========

    def stop(self):
        """Send stop command to player."""
        if not self._chromecast.socket_client.is_connected:
            LOGGER.warning("Ignore player command: Socket client is not connected.")
            return
        self._chromecast.media_controller.stop()

    def play(self):
        """Send play command to player."""
        if not self._chromecast.socket_client.is_connected:
            LOGGER.warning("Ignore player command: Socket client is not connected.")
            return
        self._chromecast.media_controller.play()

    def pause(self):
        """Send pause command to player."""
        if not self._chromecast.socket_client.is_connected:
            LOGGER.warning("Ignore player command: Socket client is not connected.")
            return
        self._chromecast.media_controller.pause()

    def next(self):
        """Send next track command to player."""
        if not self._chromecast.socket_client.is_connected:
            LOGGER.warning("Ignore player command: Socket client is not connected.")
            return
        self._chromecast.media_controller.queue_next()

    def previous(self):
        """Send previous track command to player."""
        if not self._chromecast.socket_client.is_connected:
            LOGGER.warning("Ignore player command: Socket client is not connected.")
            return
        self._chromecast.media_controller.queue_prev()

    def power_on(self):
        """Send power ON command to player."""
        if not self._chromecast.socket_client.is_connected:
            LOGGER.warning("Ignore player command: Socket client is not connected.")
            return
        self._powered = True
        self._chromecast.set_volume_muted(False)

    def power_off(self):
        """Send power OFF command to player."""
        if not self._chromecast.socket_client.is_connected:
            LOGGER.warning("Ignore player command: Socket client is not connected.")
            return
        if self.media_status and (
            self.media_status.player_is_playing or self.media_status.player_is_paused
        ):
            self._chromecast.media_controller.stop()
            self._chromecast.quit_app()
        self._powered = False
        # chromecast has no real poweroff so we send mute instead
        self._chromecast.set_volume_muted(True)

    def volume_set(self, volume_level):
        """Send new volume level command to player."""
        if not self._chromecast.socket_client.is_connected:
            LOGGER.warning("Ignore player command: Socket client is not connected.")
            return
        self._chromecast.set_volume(volume_level)
        # self.volume_level = volume_level

    def volume_mute(self, is_muted=False):
        """Send mute command to player."""
        if not self._chromecast.socket_client.is_connected:
            LOGGER.warning("Ignore player command: Socket client is not connected.")
            return
        self._chromecast.set_volume_muted(is_muted)

    def play_uri(self, uri: str):
        """Play single uri on player."""
        if not self._chromecast.socket_client.is_connected:
            LOGGER.warning("Ignore player command: Socket client is not connected.")
            return
        player_queue = self.mass.player_manager.get_player_queue(self.player_id)
        if player_queue.use_queue_stream:
            # create CC queue so that skip and previous will work
            queue_item = QueueItem()
            queue_item.name = "Music Assistant"
            queue_item.uri = uri
            return self.queue_load([queue_item, queue_item])
        self._chromecast.play_media(uri, "audio/flac")

    def queue_load(self, queue_items: List[QueueItem]):
        """Load (overwrite) queue with new items."""
        if not self._chromecast.socket_client.is_connected:
            LOGGER.warning("Ignore player command: Socket client is not connected.")
            return
        player_queue = self.mass.player_manager.get_player_queue(self.player_id)
        cc_queue_items = self.__create_queue_items(queue_items[:50])
        repeat_enabled = player_queue.use_queue_stream or player_queue.repeat_enabled
        queuedata = {
            "type": "QUEUE_LOAD",
            "repeatMode": "REPEAT_ALL" if repeat_enabled else "REPEAT_OFF",
            "shuffle": False,  # handled by our queue controller
            "queueType": "PLAYLIST",
            "startIndex": 0,  # Item index to play after this request or keep same item if undefined
            "items": cc_queue_items,  # only load 50 tracks at once or the socket will crash
        }
        self.__send_player_queue(queuedata)
        if len(queue_items) > 50:
            self.queue_append(queue_items[51:])

    def queue_append(self, queue_items: List[QueueItem]):
        """Append new items at the end of the queue."""
        if not self._chromecast.socket_client.is_connected:
            LOGGER.warning("Ignore player command: Socket client is not connected.")
            return
        cc_queue_items = self.__create_queue_items(queue_items)
        for chunk in chunks(cc_queue_items, 50):
            queuedata = {"type": "QUEUE_INSERT", "insertBefore": None, "items": chunk}
            self.__send_player_queue(queuedata)

    def __create_queue_items(self, tracks):
        """Create list of CC queue items from tracks."""
        queue_items = []
        for track in tracks:
            queue_item = self.__create_queue_item(track)
            queue_items.append(queue_item)
        return queue_items

    def __create_queue_item(self, track):
        """Create CC queue item from track info."""
        player_queue = self.mass.player_manager.get_player_queue(self.player_id)
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
                "streamType": "LIVE" if player_queue.use_queue_stream else "BUFFERED",
                "metadata": {
                    "title": track.name,
                    "artist": track.artists[0].name if track.artists else "",
                },
                "duration": int(track.duration),
            },
        }

    def __send_player_queue(self, queuedata):
        """Send new data to the CC queue."""
        media_controller = self._chromecast.media_controller
        # pylint: disable=protected-access
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


def chunks(_list, chunk_size):
    """Yield successive n-sized chunks from list."""
    for i in range(0, len(_list), chunk_size):
        yield _list[i : i + chunk_size]
