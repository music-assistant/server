"""Representation of a Cast device on the network."""
import asyncio
import logging
import uuid
from typing import List, Optional

import pychromecast
from music_assistant.helpers.compare import compare_strings
from music_assistant.helpers.typing import MusicAssistant
from music_assistant.helpers.util import create_task, yield_chunks
from music_assistant.models.config_entry import ConfigEntry
from music_assistant.models.player import DeviceInfo, Player, PlayerFeature, PlayerState
from music_assistant.models.player_queue import QueueItem
from pychromecast.controllers.multizone import MultizoneController, MultizoneManager
from pychromecast.socket_client import (
    CONNECTION_STATUS_CONNECTED,
    CONNECTION_STATUS_DISCONNECTED,
)

from .const import PLAYER_CONFIG_ENTRIES, PROV_ID
from .helpers import CastStatusListener, ChromecastInfo

LOGGER = logging.getLogger(PROV_ID)
PLAYER_FEATURES = [PlayerFeature.QUEUE]


class ChromecastPlayer(Player):
    """Representation of a Cast device on the network.

    This class is the holder of the pychromecast.Chromecast object and
    handles all reconnects and audio group changing
    "elected leader" itself.
    """

    def __init__(self, mass: MusicAssistant, cast_info: ChromecastInfo) -> None:
        """Initialize the cast device."""
        super().__init__()
        self.mass = mass
        self._cast_info = cast_info
        self._player_id = cast_info.uuid

        self.services = cast_info.services
        self._chromecast: Optional[pychromecast.Chromecast] = None
        self.cast_status = None
        self.media_status = None
        self.media_status_received = None
        self.mz_mgr: Optional[MultizoneManager] = None
        self._available = False
        self._status_listener: Optional[CastStatusListener] = None
        self._is_speaker_group = False

    @property
    def player_id(self) -> str:
        """Return player id of this player."""
        return self._player_id

    @property
    def provider_id(self) -> str:
        """Return provider id of this player."""
        return PROV_ID

    @property
    def name(self) -> str:
        """Return name of this player."""
        return self._cast_info.friendly_name

    @property
    def powered(self) -> bool:
        """Return power state of this player."""
        if not self._chromecast or not self.cast_status:
            return False
        if self.is_group_player:
            return (
                self._chromecast.media_controller.is_active
                and self.cast_status.app_id == pychromecast.APP_MEDIA_RECEIVER
            )
            # return self._chromecast is not None and self._chromecast.is_idle
            # return (
            #     self._chromecast.media_controller.app_id
            #     == pychromecast.config.APP_MEDIA_RECEIVER
            # )

        # Chromecast does not support power so we (ab)use mute instead
        if self._chromecast.media_controller.is_active:
            return (
                self.cast_status.app_id
                in ["705D30C6", self._chromecast.media_controller.app_id]
                and not self.cast_status.volume_muted
            )
        return not self.cast_status.volume_muted

    @property
    def should_poll(self) -> bool:
        """Return bool if this player needs to be polled for state changes."""
        return self.media_status and self.media_status.player_is_playing

    @property
    def state(self) -> PlayerState:
        """Return the state of the player."""
        if self.media_status is None:
            return PlayerState.IDLE
        if self.media_status.player_is_playing:
            return PlayerState.PLAYING
        if self.media_status.player_is_paused:
            return PlayerState.PAUSED
        if self.media_status.player_is_idle:
            return PlayerState.IDLE
        return PlayerState.IDLE

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
            # Add time since last update
            return self.media_status.adjusted_current_time
        # Not playing, return last reported seek time
        return self.media_status.current_time

    @property
    def available(self) -> bool:
        """Return availablity state of this player."""
        return self._available

    @property
    def current_uri(self) -> str:
        """Return current_uri of this player."""
        return self.media_status.content_id if self.media_status else None

    @property
    def volume_level(self) -> int:
        """Return volume_level of this player."""
        return self.cast_status.volume_level * 100 if self.cast_status else 0

    @property
    def muted(self) -> bool:
        """Return mute state of this player."""
        return self.cast_status.volume_muted if self.cast_status else False

    @property
    def is_group_player(self) -> bool:
        """Return if this player is a group player."""
        return self._cast_info.is_audio_group and not self._is_speaker_group

    @property
    def group_childs(self) -> List[str]:
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
    def device_info(self) -> DeviceInfo:
        """Return deviceinfo."""
        return DeviceInfo(
            model=self._cast_info.model_name,
            address=f"{self._chromecast.uri}" if self._chromecast else "",
            manufacturer=self._cast_info.manufacturer,
        )

    @property
    def features(self) -> List[PlayerFeature]:
        """Return list of features this player supports."""
        return PLAYER_FEATURES

    @property
    def config_entries(self) -> List[ConfigEntry]:
        """Return player specific config entries (if any)."""
        return PLAYER_CONFIG_ENTRIES

    async def on_add(self) -> None:
        """Call when player is added to the player manager."""
        chromecast = await self.mass.loop.run_in_executor(
            None,
            pychromecast.get_chromecast_from_cast_info,
            pychromecast.discovery.CastInfo(
                self.services,
                self._cast_info.uuid,
                self._cast_info.model_name,
                self._cast_info.friendly_name,
                None,
                None,
            ),
            self.mass.zeroconf,
        )
        self._chromecast = chromecast
        self.mz_mgr: MultizoneManager = self.mass.get_provider(PROV_ID).mz_mgr
        self._status_listener = CastStatusListener(self, chromecast, self.mz_mgr)
        self._available = False
        self.cast_status = chromecast.status
        self.media_status = chromecast.media_controller.status
        if self._cast_info.is_audio_group:
            mz_controller = MultizoneController(chromecast.uuid)
            chromecast.register_handler(mz_controller)
            chromecast.mz_controller = mz_controller
        self._chromecast.start()

    def set_cast_info(self, cast_info: ChromecastInfo) -> None:
        """Set (or update) the cast discovery info."""
        self._cast_info = cast_info

    async def disconnect(self):
        """Disconnect Chromecast object if it is set."""
        if self._chromecast is None:
            # Can't disconnect if not connected.
            return
        LOGGER.debug(
            "[%s %s] Disconnecting from chromecast socket",
            self.player_id,
            self._cast_info.friendly_name,
        )
        self._available = False
        self.update_state()

        await self.mass.loop.run_in_executor(None, self._chromecast.disconnect)

        self._invalidate()
        self.update_state()

    def _invalidate(self) -> None:
        """Invalidate some attributes."""
        self._chromecast = None
        self.cast_status = None
        self.media_status = None
        self.media_status_received = None
        self.mz_mgr = None
        if self._status_listener is not None:
            self._status_listener.invalidate()
            self._status_listener = None

    async def on_remove(self) -> None:
        """Call when player is removed from the player manager."""
        await self.disconnect()

    # ========== Callbacks ==========

    def new_cast_status(self, cast_status) -> None:
        """Handle updates of the cast status."""
        self.cast_status = cast_status
        self._is_speaker_group = (
            self._cast_info.is_audio_group
            and self._chromecast.mz_controller
            and self._chromecast.mz_controller.members
            and compare_strings(
                self._chromecast.mz_controller.members[0], self.player_id
            )
        )
        self.update_state()

    def new_media_status(self, media_status) -> None:
        """Handle updates of the media status."""
        self.media_status = media_status
        self.update_state()

    def new_connection_status(self, connection_status) -> None:
        """Handle updates of connection status."""
        if connection_status.status == CONNECTION_STATUS_DISCONNECTED:
            self._available = False
            self._invalidate()
            self.update_state()
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
            self.update_state()
            if self._cast_info.is_audio_group and new_available:
                create_task(self._chromecast.mz_controller.update_members)

    # ========== Service Calls ==========

    async def cmd_stop(self) -> None:
        """Send stop command to player."""
        if self._chromecast.media_controller:
            await self.chromecast_command(self._chromecast.media_controller.stop)

    async def cmd_play(self) -> None:
        """Send play command to player."""
        if self._chromecast.media_controller:
            await self.chromecast_command(self._chromecast.media_controller.play)

    async def cmd_pause(self) -> None:
        """Send pause command to player."""
        if self._chromecast.media_controller:
            await self.chromecast_command(self._chromecast.media_controller.pause)

    async def cmd_next(self) -> None:
        """Send next track command to player."""
        if self._chromecast.media_controller:
            await self.chromecast_command(self._chromecast.media_controller.queue_next)

    async def cmd_previous(self) -> None:
        """Send previous track command to player."""
        if self._chromecast.media_controller:
            await self.chromecast_command(self._chromecast.media_controller.queue_prev)

    async def cmd_power_on(self) -> None:
        """Send power ON command to player."""
        if self.is_group_player:
            await self.launch_app()
        else:
            # chromecast has no real poweroff so we (ab)use mute instead
            await self.chromecast_command(self._chromecast.set_volume_muted, False)

    async def cmd_power_off(self) -> None:
        """Send power OFF command to player."""
        if self.is_group_player or (
            self._chromecast.media_controller.is_active
            and self.cast_status.app_id == self._chromecast.media_controller.app_id
        ):
            await self.chromecast_command(self._chromecast.quit_app)
        if not self.is_group_player:
            # chromecast has no real poweroff so we (ab)use mute instead
            await self.chromecast_command(self._chromecast.set_volume_muted, True)

    async def cmd_volume_set(self, volume_level: int) -> None:
        """Send new volume level command to player."""
        await self.chromecast_command(self._chromecast.set_volume, volume_level / 100)

    async def cmd_volume_mute(self, is_muted: bool = False) -> None:
        """Send mute command to player."""
        await self.chromecast_command(self._chromecast.set_volume_muted, is_muted)

    async def cmd_play_uri(self, uri: str) -> None:
        """Play single uri on player."""
        player_queue = self.mass.players.get_player_queue(self.player_id)
        if player_queue.use_queue_stream:
            # create (fake) CC queue so that skip and previous will work
            queue_item = QueueItem(
                item_id=uri, provider="mass", name="Music Assistant", stream_url=uri
            )
            await self.cmd_queue_load([queue_item, queue_item])
        else:
            await self.chromecast_command(
                self._chromecast.play_media, uri, "audio/flac"
            )

    async def cmd_queue_load(self, queue_items: List[QueueItem]) -> None:
        """Load (overwrite) queue with new items."""
        player_queue = self.mass.players.get_player_queue(self.player_id)
        cc_queue_items = self.__create_queue_items(queue_items[:25])
        repeat_enabled = player_queue.use_queue_stream or player_queue.repeat_enabled
        queuedata = {
            "type": "QUEUE_LOAD",
            "repeatMode": "REPEAT_ALL" if repeat_enabled else "REPEAT_OFF",
            "shuffle": False,  # handled by our queue controller
            "queueType": "PLAYLIST",
            "startIndex": 0,  # Item index to play after this request or keep same item if undefined
            "items": cc_queue_items,  # only load 25 tracks at once or the socket will crash
        }
        await self.launch_app()
        await self.chromecast_command(self.__send_player_queue, queuedata)
        if len(queue_items) > 25:
            await asyncio.sleep(5)
            await self.cmd_queue_append(queue_items[26:])

    async def cmd_queue_append(self, queue_items: List[QueueItem]) -> None:
        """Append new items at the end of the queue."""
        cc_queue_items = self.__create_queue_items(queue_items)
        async for chunk in yield_chunks(cc_queue_items, 25):
            queuedata = {
                "type": "QUEUE_INSERT",
                "insertBefore": None,
                "items": chunk,
            }
            await self.chromecast_command(self.__send_player_queue, queuedata)
            await asyncio.sleep(2)

    def __create_queue_items(self, tracks) -> None:
        """Create list of CC queue items from tracks."""
        return [self.__create_queue_item(track) for track in tracks]

    @staticmethod
    def __create_queue_item(queue_item: QueueItem):
        """Create CC queue item from track info."""
        return {
            "opt_itemId": queue_item.queue_item_id,
            "autoplay": True,
            "preloadTime": 0,
            "playbackDuration": int(queue_item.duration),
            "startTime": 0,
            "activeTrackIds": [],
            "media": {
                "contentId": queue_item.stream_url,
                "customData": {
                    "provider": queue_item.provider,
                    "uri": queue_item.stream_url,
                    "item_id": queue_item.queue_item_id,
                },
                "contentType": "audio/flac",
                "streamType": pychromecast.STREAM_TYPE_BUFFERED,
                "metadata": {
                    "title": queue_item.name,
                    "artist": "/".join(x.name for x in queue_item.artists),
                },
                "duration": int(queue_item.duration),
            },
        }

    def __send_player_queue(self, queuedata: dict) -> None:
        """Send new data to the CC queue."""
        media_controller = self._chromecast.media_controller
        queuedata["mediaSessionId"] = media_controller.status.media_session_id
        media_controller.send_message(queuedata, False)

    async def launch_app(self):
        """Launch the default media receiver app and wait until its launched."""
        media_controller = self._chromecast.media_controller
        event = asyncio.Event()

        def launched_callback():
            self.mass.loop.call_soon_threadsafe(event.set)

        # pylint: disable=protected-access
        receiver_ctrl = media_controller._socket_client.receiver_controller
        await self.mass.loop.run_in_executor(
            None,
            receiver_ctrl.launch_app,
            media_controller.app_id,
            False,
            launched_callback,
        )
        await event.wait()

    async def chromecast_command(self, func, *args):
        """Execute command on Chromecast."""
        if not self.available:
            LOGGER.warning(
                "Player %s is not available, command can't be executed", self.name
            )
            return
        await self.mass.loop.run_in_executor(None, func, *args)
