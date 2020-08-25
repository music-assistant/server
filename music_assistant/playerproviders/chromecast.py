#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import logging
import time
import types
import uuid
from typing import List, Optional, Tuple

import aiohttp
import attr
import pychromecast
import zeroconf
from music_assistant.constants import (
    CONF_ENABLED,
    CONF_HOSTNAME,
    CONF_PORT,
    EVENT_SHUTDOWN,
)
from music_assistant.mass import MusicAssistant
from music_assistant.models.player import Player, PlayerState, DeviceInfo
from music_assistant.models.player_queue import QueueItem
from music_assistant.models.playerprovider import PlayerProvider
from music_assistant.models.config_entry import ConfigEntry, ConfigEntryType
from music_assistant.utils import LOGGER, try_parse_int
from pychromecast.const import CAST_MANUFACTURERS
from pychromecast.controllers.multizone import MultizoneController, MultizoneManager
from pychromecast.socket_client import (
    CONNECTION_STATUS_CONNECTED,
    CONNECTION_STATUS_DISCONNECTED,
)

PROV_ID = "chromecast"
PROV_NAME = "Chromecast"
DEFAULT_PORT = 8009
CONF_GAPLESS = "gapless_enabled"

PROVIDER_CONFIG_ENTRIES = []

PLAYER_CONFIG_ENTRIES = [
    ConfigEntry(entry_key=CONF_GAPLESS,
                entry_type=ConfigEntryType.BOOL,
                default_value=True, description_key=CONF_GAPLESS)]


async def async_setup(mass, conf) -> PlayerProvider:
    """Perform async setup of this PlayerProvider."""
    logging.getLogger("pychromecast").setLevel(logging.WARNING)
    prov = ChromecastProvider()
    mass.loop.create_task(prov.chromecast_discovery())
    return prov


@attr.s(slots=True, frozen=True)
class ChromecastInfo:
    """Class to hold all data about a chromecast for creating connections.
    This also has the same attributes as the mDNS fields by zeroconf.
    """

    services: Optional[set] = attr.ib()
    host: Optional[str] = attr.ib(default=None)
    port: Optional[int] = attr.ib(default=0)
    uuid: Optional[str] = attr.ib(
        converter=attr.converters.optional(str), default=None
    )  # always convert UUID to string if not None
    model_name: str = attr.ib(default="")
    friendly_name: Optional[str] = attr.ib(default=None)

    @property
    def is_audio_group(self) -> bool:
        """Return if this is an audio group."""
        return self.port != DEFAULT_PORT

    @property
    def host_port(self) -> Tuple[str, int]:
        """Return the host+port tuple."""
        return self.host, self.port

    @property
    def manufacturer(self) -> str:
        """Return the manufacturer."""
        if not self.model_name:
            return None
        return CAST_MANUFACTURERS.get(self.model_name.lower(), "Google Inc.")


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
            provider_id=PROV_ID)
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

    @property
    def cur_time(self) -> int:
        """Position of current playing media in seconds."""
        if self.media_status is None or not (
            self.media_status.player_is_playing
            or self.media_status.player_is_paused
            or self.media_status.player_is_idle
        ):
            return None
        return self.media_status.current_time

    @property
    def cur_uri(self) -> str:
        """Return uri loaded in player property of this player."""
        return self.media_status.content_id if self.media_status else None

    @property
    def volume_level(self) -> int:
        """Volume level of the media player (0..100)."""
        return self.cast_status.volume_level * 100 if self.cast_status else None

    @property
    def muted(self) -> bool:
        """Boolean if volume is currently muted."""
        return self.cast_status.volume_muted if self.cast_status else None

    @property
    def device_info(self):
        """Return information about the device."""
        cast_info = self._cast_info
        return {
            "model": cast_info.model_name,
            "address": f"{cast_info.host}:{cast_info.port}"
        }

    async def async_on_remove(self):
        """Call when this player is removed from the player manager."""
        # if self.__cc_report_progress_task:
        #     self.__cc_report_progress_task.cancel()
        await self._async_disconnect()

    async def async_set_cast_info(self, cast_info: ChromecastInfo):
        """Set the cast information and set up the chromecast object."""
        self._cast_info = cast_info
        self._player.name = cast_info.friendly_name
        self._player.available = False
        self._player.is_group_player = cast_info.is_audio_group
        self._player.device_info = DeviceInfo(
            model=cast_info.model_name, 
            address=f"{cast_info.host}:{cast_info.port}"
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
        if self._chromecast is None:
            return
        LOGGER.warning(
            "[%s] Disconnecting from chromecast socket", self._cast_info.friendly_name)
        self._available = False
        await self.mass.async_add_executor_job(self._chromecast.disconnect)
        self._invalidate()
        await self.update()

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
        if media_status.player_state in ["PLAYING", "BUFFERING"]:
            self._player.state = PlayerState.Playing
            self._player.powered = True
        elif media_status.player_state == "PAUSED":
            self._player.state = PlayerState.Paused
        else:
            self.state = PlayerState.Stopped
        self.cur_uri = media_status.content_id
        self.cur_time = media_status.adjusted_current_time

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
        self.volume_level = volume_level

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


class ChromecastProvider(PlayerProvider):
    """Support for ChromeCast Audio PlayerProvider."""

    def __init__(self, *args, **kwargs):
        """Inititialize the PlayerProvider."""
        self.mz_mgr = MultizoneManager()
        self._players = {}
        self._listener = pychromecast.CastListener(
            self.__chromecast_add_update_callback,
            self.__chromecast_remove_callback,
            self.__chromecast_add_update_callback)
        self._browser = pychromecast.discovery.start_discovery(self._listener, self.mass.zeroconf)
        self.mass.add_event_listener(self.on_shutdown, EVENT_SHUTDOWN)
        super().__init__(*args, **kwargs)

    def on_shutdown(self, msg, msg_details=None):
        """Handle shutdown event."""
        pychromecast.stop_discovery(self._browser)
        LOGGER.debug("Chromecast discovery completed...")

    @property
    def provider_id(self) -> str:
        """Return Provider id of this provider."""
        return PROV_ID

    @property
    def name(self) -> str:
        """Return name of this provider."""
        return PROV_NAME

    @property
    def config_entries(self) -> List[ConfigEntry]:
        """Return custom config entries for this provider."""
        return PROVIDER_CONFIG_ENTRIES

    async def async_cmd_play_uri(self, player_id: str, uri: str):
        """
            Play the specified uri/url on the goven player.
                :param player_id: player_id of the player to handle the command.
        """
        await self.mass.async_run_job(self._players[player_id].play_uri, uri)

    async def async_cmd_stop(self, player_id: str):
        """
            Send STOP command to given player.
                :param player_id: player_id of the player to handle the command.
        """
        await self.mass.async_run_job(self._players[player_id].stop)

    async def async_cmd_play(self, player_id: str):
        """
            Send STOP command to given player.
                :param player_id: player_id of the player to handle the command.
        """
        await self.mass.async_run_job(self._players[player_id].play)

    async def async_cmd_pause(self, player_id: str):
        """
            Send PAUSE command to given player.
                :param player_id: player_id of the player to handle the command.
        """
        await self.mass.async_run_job(self._players[player_id].pause)

    async def async_cmd_next(self, player_id: str):
        """
            Send NEXT TRACK command to given player.
                :param player_id: player_id of the player to handle the command.
        """
        await self.mass.async_run_job(self._players[player_id].next)

    async def async_cmd_previous(self, player_id: str):
        """
            Send PREVIOUS TRACK command to given player.
                :param player_id: player_id of the player to handle the command.
        """
        await self.mass.async_run_job(self._players[player_id].previous)

    async def async_cmd_power_on(self, player_id: str):
        """
            Send POWER ON command to given player.
                :param player_id: player_id of the player to handle the command.
        """
        await self.mass.async_run_job(self._players[player_id].power_on)

    async def async_cmd_power_off(self, player_id: str):
        """
            Send POWER OFF command to given player.
                :param player_id: player_id of the player to handle the command.
        """
        await self.mass.async_run_job(self._players[player_id].power_off)

    async def async_cmd_volume_set(self, player_id: str, volume_level: int):
        """
            Send volume level command to given player.
                :param player_id: player_id of the player to handle the command.
                :param volume_level: volume level to set (0..100).
        """
        await self.mass.async_run_job(self._players[player_id].previous, volume_level / 100)

    async def async_cmd_volume_mute(self, player_id: str, is_muted=False):
        """
            Send volume MUTE command to given player.
                :param player_id: player_id of the player to handle the command.
                :param is_muted: bool with new mute state.
        """
        await self.mass.async_run_job(self._players[player_id].volume_mute, is_muted)

    async def async_queue_load(self, player_id: str, queue_items: List[QueueItem]):
        """
            Load/overwrite given items in the player's queue implementation
                :param player_id: player_id of the player to handle the command.
                :param queue_items: a list of QueueItems
        """
        await self.mass.async_run_job(self._players[player_id].queue_load, queue_items)

    async def async_cmd_queue_append(self, player_id: str, queue_items: List[QueueItem]):
        """
            Append new items at the end of the queue.
                :param player_id: player_id of the player to handle the command.
                :param queue_items: a list of QueueItems
        """
        await self.mass.async_run_job(self._players[player_id].queue_append, queue_items)

    def __chromecast_add_update_callback(self, uuid, service_name):
        """Handle zeroconf discovery of a new or updated chromecast."""
        if uuid is None:
            return  # Discovered chromecast without uuid
        service = self._listener.services[uuid]
        cast_info = ChromecastInfo(
            services=service[0],
            uuid=service[1],
            model_name=service[2],
            friendly_name=service[3],
            host=service[4],
            port=service[5],
        )
        LOGGER.debug("Chromecast discovered: %s - %s", cast_info.friendly_name, cast_info.uuid)
        player_id = cast_info.uuid
        if player_id in self._players:
            # player already added, the player will take care of reconnects itself.
            LOGGER.warning("Player is already added: %s", player_id)
            self.mass.loop.create_task(player.async_set_cast_info(cast_info))
        else:
            player = ChromecastPlayer(mass, cast_info)
            self.mass.run_task(self.mass.music_manager.add_player(player))

    def __chromecast_remove_callback(self, uuid, service_name, service):
        player_id = str(service[1])
        friendly_name = service[3]
        LOGGER.debug("Chromecast removed: %s - %s", friendly_name, player_id)


def chunks(l, n):
    """Yield successive n-sized chunks from l."""
    for i in range(0, len(l), n):
        yield l[i : i + n]


class StatusListener:
    def __init__(self, player_id, status_callback, mass):
        self.__handle_callback = status_callback
        self.mass = mass
        self.player_id = player_id

    def new_cast_status(self, status):
        """chromecast status changed (like volume etc.)"""
        self.mass.run_task(self.__handle_callback(caststatus=status))

    def new_media_status(self, status):
        """mediacontroller has new state"""
        self.mass.run_task(self.__handle_callback(mediastatus=status))

    def new_connection_status(self, status):
        """will be called when the connection changes"""
        if status.status == CONNECTION_STATUS_DISCONNECTED:
            # schedule a new scan which will handle reconnects and group parent changes
            self.mass.loop.run_in_executor(
                None, self.mass.player_manager.providers[PROV_ID].run_chromecast_discovery
            )


class MZListener:
    def __init__(self, mz, callback, loop):
        self._mz = mz
        self._loop = loop
        self.__handle_group_members_update = callback

    def multizone_member_added(self, uuid):
        asyncio.run_coroutine_threadsafe(
            self.__handle_group_members_update(self._mz, added_player=str(uuid)),
            self._loop,
        )

    def multizone_member_removed(self, uuid):
        asyncio.run_coroutine_threadsafe(
            self.__handle_group_members_update(self._mz, removed_player=str(uuid)),
            self._loop,
        )

    def multizone_status_received(self):
        asyncio.run_coroutine_threadsafe(
            self.__handle_group_members_update(self._mz), self._loop
        )


class CastStatusListener:
    """Helper class to handle pychromecast status callbacks.
    Necessary because a CastDevice entity can create a new socket client
    and therefore callbacks from multiple chromecast connections can
    potentially arrive. This class allows invalidating past chromecast objects.
    """

    def __init__(self, cast_device, chromecast, mz_mgr):
        """Initialize the status listener."""
        self._cast_device = cast_device
        self._uuid = chromecast.uuid
        self._valid = True
        self._mz_mgr = mz_mgr

        chromecast.register_status_listener(self)
        chromecast.socket_client.media_controller.register_status_listener(self)
        chromecast.register_connection_listener(self)
        if cast_device._cast_info.is_audio_group:
            self._mz_mgr.add_multizone(chromecast)
        else:
            self._mz_mgr.register_listener(chromecast.uuid, self)

    def new_cast_status(self, cast_status):
        """Handle reception of a new CastStatus."""
        if self._valid:
            self._cast_device.new_cast_status(cast_status)

    def new_media_status(self, media_status):
        """Handle reception of a new MediaStatus."""
        if self._valid:
            self._cast_device.new_media_status(media_status)

    def new_connection_status(self, connection_status):
        """Handle reception of a new ConnectionStatus."""
        if self._valid:
            self._cast_device.new_connection_status(connection_status)

    def added_to_multizone(self, group_uuid):
        """Handle the cast added to a group."""
        LOGGER.debug("Player %s is added to group %s", self._cast_device.name, group_uuid)

    def removed_from_multizone(self, group_uuid):
        """Handle the cast removed from a group."""
        if self._valid:
            self._cast_device.multizone_new_media_status(group_uuid, None)

    def multizone_new_cast_status(self, group_uuid, cast_status):
        """Handle reception of a new CastStatus for a group."""

    def multizone_new_media_status(self, group_uuid, media_status):
        """Handle reception of a new MediaStatus for a group."""
        if self._valid:
            self._cast_device.multizone_new_media_status(group_uuid, media_status)

    def invalidate(self):
        """Invalidate this status listener.
        All following callbacks won't be forwarded.
        """
        # pylint: disable=protected-access
        if self._cast_device._cast_info.is_audio_group:
            self._mz_mgr.remove_multizone(self._uuid)
        else:
            self._mz_mgr.deregister_listener(self._uuid, self)
        self._valid = False


# async def __report_progress(self):
    #     """report current progress while playing"""
    #     # chromecast does not send updates of the player's progress (cur_time)
    #     # so we need to send it in periodically
    #     while self._state == PlayerState.Playing:
    #         self.cur_time = self.media_status.adjusted_current_time
    #         await asyncio.sleep(1)
    #     self.__cc_report_progress_task = None

    # async def handle_player_state(self, caststatus=None, mediastatus=None):
    #     """handle a player state message from the socket"""
    #     # handle generic cast status
    #     if caststatus:
    #         self.muted = caststatus.volume_muted
    #         self.volume_level = caststatus.volume_level * 100
    #     self.name = self._chromecast.name
    #     # handle media status
    #     if mediastatus:
    #         if mediastatus.player_state in ["PLAYING", "BUFFERING"]:
    #             self.state = PlayerState.Playing
    #             self.powered = True
    #         elif mediastatus.player_state == "PAUSED":
    #             self.state = PlayerState.Paused
    #         else:
    #             self.state = PlayerState.Stopped
    #         self.cur_uri = mediastatus.content_id
    #         self.cur_time = mediastatus.adjusted_current_time
    #     if (
    #         self._state == PlayerState.Playing
    #         and self.__cc_report_progress_task is None
    #     ):
    #         self.__cc_report_progress_task = self.mass.loop.create_task(
    #             self.__report_progress()
    #         )

# def __chromecast_discovered(self, cast_info):
    #     """callback when a (new) chromecast device is discovered"""
    #     player_id = cast_info.uuid
    #     player = self.mass.player_manager.get_player_sync(player_id)
    #     if self.mass.player_manager.get_player_sync(player_id):
    #         # player already added, the player will take care of reconnects itself.
    #         LOGGER.warning("Player is already added: %s", player_id)
    #         self.mass.loop.create_task(player.async_set_cast_info(cast_info))
    #     else:
    #         player = ChromecastPlayer(self.mass, cast_info)
    #         # player.cc = chromecast
    #         # player.mz = None

    #         # # register status listeners
    #         # status_listener = StatusListener(
    #         #     player_id, player.handle_player_state, self.mass
    #         # )
    #         # if chromecast.cast_type == "group":
    #         #     mz = MultizoneController(chromecast.uuid)
    #         #     mz.register_listener(
    #         #         MZListener(mz, self.__handle_group_members_update, self.mass.loop)
    #         #     )
    #         #     chromecast.register_handler(mz)
    #         #     player.mz = mz
    #         # chromecast.register_connection_listener(status_listener)
    #         # chromecast.register_status_listener(status_listener)
    #         # chromecast.media_controller.register_status_listener(status_listener)
    #         # player.cc.wait()
    #         self.mass.run_task(self.add_player(player))

    # def __update_group_players(self):
    #     """update childs of all group players"""
    #     for player in self.players:
    #         if player.cc.cast_type == "group":
    #             player.mz.update_members()

# async def __handle_group_members_update(
    #     self, mz, added_player=None, removed_player=None
    # ):
    #     """handle callback from multizone manager"""
    #     group_player_id = str(uuid.UUID(mz._uuid))
    #     group_player = await self.get_player(group_player_id)
    #     if added_player:
    #         player_id = str(uuid.UUID(added_player))
    #         child_player = await self.get_player(player_id)
    #         if child_player and player_id != group_player_id:
    #             group_player.add_group_child(player_id)
    #             LOGGER.debug("%s added to %s", child_player.name, group_player.name)
    #     elif removed_player:
    #         player_id = str(uuid.UUID(removed_player))
    #         group_player.remove_group_child(player_id)
    #         LOGGER.debug("%s removed from %s", player_id, group_player.name)
    #     else:
    #         for member in mz.members:
    #             player_id = str(uuid.UUID(member))
    #             child_player = await self.get_player(player_id)
    #             if not child_player or player_id == group_player_id:
    #                 continue
    #             if not player_id in group_player.group_childs:
    #                 group_player.add_group_child(player_id)
    #                 LOGGER.debug("%s added to %s", child_player.name, group_player.name)
