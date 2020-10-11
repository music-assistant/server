"""Representation of a Cast device on the network."""
import logging
import uuid
from datetime import datetime
from typing import List, Optional

import pychromecast
from asyncio_throttle import Throttler
from music_assistant.helpers.typing import MusicAssistantType
from music_assistant.helpers.util import async_yield_chunks, compare_strings
from music_assistant.models.config_entry import ConfigEntry
from music_assistant.models.player import (
    DeviceInfo,
    PlaybackState,
    Player,
    PlayerFeature,
)
from music_assistant.models.player_queue import QueueItem
from pychromecast.controllers.multizone import MultizoneController
from pychromecast.socket_client import (
    CONNECTION_STATUS_CONNECTED,
    CONNECTION_STATUS_DISCONNECTED,
)

from .const import PLAYER_CONFIG_ENTRIES, PROV_ID
from .models import CastStatusListener, ChromecastInfo

LOGGER = logging.getLogger(PROV_ID)
PLAYER_FEATURES = [PlayerFeature.QUEUE]


class ChromecastPlayer(Player):
    """Representation of a Cast device on the network.

    This class is the holder of the pychromecast.Chromecast object and
    handles all reconnects and audio group changing
    "elected leader" itself.
    """

    def __init__(self, mass: MusicAssistantType, cast_info: ChromecastInfo) -> None:
        """Initialize the cast device."""
        self.mass = mass
        self._cast_info = cast_info
        self._player_id = cast_info.uuid

        self.services = cast_info.services
        self._chromecast: Optional[pychromecast.Chromecast] = None
        self.cast_status = None
        self.media_status = None
        self.media_status_received = None
        self.mz_mgr = None
        self.mz_manager = None
        self._available = False
        self._status_listener: Optional[CastStatusListener] = None
        self._is_speaker_group = False
        self._throttler = Throttler(rate_limit=2, period=1)

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
        return (
            self._chromecast.name if self._chromecast else self._cast_info.friendly_name
        )

    @property
    def powered(self) -> bool:
        """Return power state of this player."""
        return not self.cast_status.volume_muted if self.cast_status else False

    @property
    def should_poll(self) -> bool:
        """Return bool if this player needs to be polled for state changes."""
        return self.media_status and self.media_status.player_is_playing

    @property
    def state(self) -> PlaybackState:
        """Return the state of the player."""
        if self.media_status is None:
            return PlaybackState.Stopped
        if self.media_status.player_is_playing:
            return PlaybackState.Playing
        if self.media_status.player_is_paused:
            return PlaybackState.Paused
        if self.media_status.player_is_idle:
            return PlaybackState.Stopped
        return PlaybackState.Stopped

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
    def elapsed_milliseconds(self) -> int:
        """Return (realtime) elapsed time of current playing media in milliseconds."""
        if self.media_status is None or not (
            self.media_status.player_is_playing
            or self.media_status.player_is_paused
            or self.media_status.player_is_idle
        ):
            return 0
        if self.media_status.player_is_playing:
            # Add time since last update
            return int(
                (
                    self.media_status.current_time
                    + (
                        datetime.utcnow().timestamp()
                        - self.media_status.last_updated.timestamp()
                    )
                )
                * 1000
            )
        # Not playing, return last reported seek time
        return self.media_status.current_time * 1000

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
        return int(self.cast_status.volume_level * 100 if self.cast_status else 0)

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
            address=f"{self._cast_info.host}:{self._cast_info.port}",
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

    def set_cast_info(self, cast_info: ChromecastInfo) -> None:
        """Set the cast information and set up the chromecast object."""
        self._cast_info = cast_info
        if self._chromecast and not self._available:
            try:
                self.disconnect()
            except Exception:  # pylint: disable=broad-except
                pass
        elif self._chromecast is not None:
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
        chromecast.register_handler(mz_controller)
        chromecast.mz_controller = mz_controller
        self._chromecast.start()

    def disconnect(self) -> None:
        """Disconnect Chromecast object if it is set."""
        self._available = False
        if self._chromecast is None:
            return
        LOGGER.warning(
            "[%s] Disconnecting from chromecast socket", self._cast_info.friendly_name
        )
        if (
            self._chromecast.socket_client
            and not self._chromecast.socket_client.is_stopped
        ):
            self._chromecast.disconnect()
        self._invalidate()

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

    async def async_on_remove(self) -> None:
        """Call when player is removed from the player manager."""
        self.mass.add_job(self.disconnect)

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
                self.__try_chromecast_command(
                    self._chromecast.mz_controller.update_members
                )

    async def async_on_update(self) -> None:
        """Call when player is periodically polled by the player manager (should_poll=True)."""
        if self.mass.players.get_player_state(self.player_id).active_queue.startswith(
            "group_player"
        ):
            # the group player wants very accurate elapsed_time state so we request it very often
            await self.__async_try_chromecast_command(
                self._chromecast.media_controller.update_status
            )
        self.update_state()

    # ========== Service Calls ==========

    async def async_cmd_stop(self) -> None:
        """Send stop command to player."""
        if self._chromecast and self._chromecast.media_controller:
            await self.__async_try_chromecast_command(
                self._chromecast.media_controller.stop
            )

    async def async_cmd_play(self) -> None:
        """Send play command to player."""
        if self._chromecast.media_controller:
            await self.__async_try_chromecast_command(
                self._chromecast.media_controller.play
            )

    async def async_cmd_pause(self) -> None:
        """Send pause command to player."""
        if self._chromecast.media_controller:
            await self.__async_try_chromecast_command(
                self._chromecast.media_controller.pause
            )

    async def async_cmd_next(self) -> None:
        """Send next track command to player."""
        if self._chromecast.media_controller:
            await self.__async_try_chromecast_command(
                self._chromecast.media_controller.queue_next
            )

    async def async_cmd_previous(self) -> None:
        """Send previous track command to player."""
        if self._chromecast.media_controller:
            await self.__async_try_chromecast_command(
                self._chromecast.media_controller.queue_prev
            )

    async def async_cmd_power_on(self) -> None:
        """Send power ON command to player."""
        await self.__async_try_chromecast_command(
            self._chromecast.set_volume_muted, False
        )

    async def async_cmd_power_off(self) -> None:
        """Send power OFF command to player."""
        if self.media_status and (
            self.media_status.player_is_playing
            or self.media_status.player_is_paused
            or self.media_status.player_is_idle
        ):
            await self.__async_try_chromecast_command(
                self._chromecast.media_controller.stop
            )
        # chromecast has no real poweroff so we send mute instead
        await self.__async_try_chromecast_command(
            self._chromecast.set_volume_muted, True
        )

    async def async_cmd_volume_set(self, volume_level: int) -> None:
        """Send new volume level command to player."""
        await self.__async_try_chromecast_command(
            self._chromecast.set_volume, volume_level / 100
        )

    async def async_cmd_volume_mute(self, is_muted: bool = False) -> None:
        """Send mute command to player."""
        await self.__async_try_chromecast_command(
            self._chromecast.set_volume_muted, is_muted
        )

    async def async_cmd_play_uri(self, uri: str) -> None:
        """Play single uri on player."""
        player_queue = self.mass.players.get_player_queue(self.player_id)
        if player_queue.use_queue_stream:
            # create CC queue so that skip and previous will work
            queue_item = QueueItem()
            queue_item.name = "Music Assistant"
            queue_item.uri = uri
            return await self.async_cmd_queue_load([queue_item, queue_item])
        await self.__async_try_chromecast_command(
            self._chromecast.play_media, uri, "audio/flac"
        )

    async def async_cmd_queue_load(self, queue_items: List[QueueItem]) -> None:
        """Load (overwrite) queue with new items."""
        player_queue = self.mass.players.get_player_queue(self.player_id)
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
        await self.__async_try_chromecast_command(self.__send_player_queue, queuedata)
        if len(queue_items) > 50:
            await self.async_cmd_queue_append(queue_items[51:])

    async def async_cmd_queue_append(self, queue_items: List[QueueItem]) -> None:
        """Append new items at the end of the queue."""
        cc_queue_items = self.__create_queue_items(queue_items)
        async for chunk in async_yield_chunks(cc_queue_items, 50):
            queuedata = {
                "type": "QUEUE_INSERT",
                "insertBefore": None,
                "items": chunk,
            }
            await self.__async_try_chromecast_command(
                self.__send_player_queue, queuedata
            )

    def __create_queue_items(self, tracks) -> None:
        """Create list of CC queue items from tracks."""
        queue_items = []
        for track in tracks:
            queue_item = self.__create_queue_item(track)
            queue_items.append(queue_item)
        return queue_items

    def __create_queue_item(self, track):
        """Create CC queue item from track info."""
        player_queue = self.mass.players.get_player_queue(self.player_id)
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

    def __send_player_queue(self, queuedata: dict) -> None:
        """Send new data to the CC queue."""
        media_controller = self._chromecast.media_controller
        # pylint: disable=protected-access
        receiver_ctrl = media_controller._socket_client.receiver_controller

        def send_queue():
            """Plays media after chromecast has switched to requested app."""
            queuedata["mediaSessionId"] = media_controller.status.media_session_id
            media_controller.send_message(queuedata, False)

        if not media_controller.status.media_session_id:
            receiver_ctrl.launch_app(
                media_controller.app_id,
                callback_function=send_queue,
            )
        else:
            send_queue()

    def __try_chromecast_command(self, func, *args, **kwargs):
        """Try to execute Chromecast command."""
        self.mass.add_job(self.__async_try_chromecast_command(func, *args, **kwargs))

    async def __async_try_chromecast_command(self, func, *args, **kwargs):
        """Try to execute Chromecast command."""

        def handle_command(func, *args, **kwarg):
            if (
                not self._chromecast
                or not self._chromecast.socket_client
                or not self._available
            ):
                LOGGER.error(
                    "Error while executing command %s on player %s: Chromecast is not available!",
                    func.__name__,
                    self.name,
                )
                return
            try:
                return func(*args, **kwargs)
            except (
                pychromecast.NotConnected,
                pychromecast.ChromecastConnectionError,
            ) as exc:
                LOGGER.warning(
                    "Error while executing command %s on player %s: %s",
                    func.__name__,
                    self.name,
                    str(exc),
                )
                self._available = False
            except Exception as exc:  # pylint: disable=broad-except
                LOGGER.exception(exc)

        async with self._throttler:
            self.mass.add_job(handle_command, func, *args, **kwargs)
