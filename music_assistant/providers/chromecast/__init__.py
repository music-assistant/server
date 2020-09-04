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
from music_assistant.constants import CONF_ENABLED, CONF_HOSTNAME, CONF_PORT
from music_assistant.mass import MusicAssistant
from music_assistant.models.config_entry import ConfigEntry, ConfigEntryType
from music_assistant.models.player import DeviceInfo, Player, PlayerState
from music_assistant.models.player_queue import QueueItem
from music_assistant.models.playerprovider import PlayerProvider
from music_assistant.utils import try_parse_int
from pychromecast.controllers.multizone import MultizoneController, MultizoneManager
from pychromecast.socket_client import (
    CONNECTION_STATUS_CONNECTED,
    CONNECTION_STATUS_DISCONNECTED,
)

from .const import PROV_ID, PROV_NAME, PROVIDER_CONFIG_ENTRIES
from .models import ChromecastInfo
from .player import ChromecastPlayer

LOGGER = logging.getLogger(PROV_ID)


async def async_setup(mass):
    """Perform async setup of this Plugin/Provider."""
    logging.getLogger("pychromecast").setLevel(logging.WARNING)
    prov = ChromecastProvider()
    await mass.async_register_provider(prov)


class ChromecastProvider(PlayerProvider):
    """Support for ChromeCast Audio PlayerProvider."""

    @property
    def id(self) -> str:
        """Return provider ID for this provider."""
        return PROV_ID

    @property
    def name(self) -> str:
        """Return provider Name for this provider."""
        return PROV_NAME

    @property
    def config_entries(self) -> List[ConfigEntry]:
        """Return Config Entries for this provider."""
        return PROVIDER_CONFIG_ENTRIES

    async def async_on_start(self) -> bool:
        """Called on startup. Handle initialization of the provider based on config."""
        # pylint: disable=attribute-defined-outside-init
        self.mz_mgr = MultizoneManager()
        self._players = {}
        self._listener = pychromecast.CastListener(
            self.__chromecast_add_update_callback,
            self.__chromecast_remove_callback,
            self.__chromecast_add_update_callback,
        )
        self._browser = pychromecast.discovery.start_discovery(self._listener, self.mass.zeroconf)
        self.available = True
        return True

    async def async_on_stop(self):
        """Called on shutdown. Handle correct close/cleanup of the provider on exit."""
        if not self.available:
            return
        # stop discovery
        pychromecast.stop_discovery(self._browser)
        # stop cast socket clients
        for player in self._players.values():
            await player.async_disconnect()

    async def async_cmd_play_uri(self, player_id: str, uri: str):
        """
        Play the specified uri/url on the goven player.
            :param player_id: player_id of the player to handle the command.
        """
        self.mass.add_job(self._players[player_id].play_uri, uri)

    async def async_cmd_stop(self, player_id: str):
        """
        Send STOP command to given player.
            :param player_id: player_id of the player to handle the command.
        """
        self.mass.add_job(self._players[player_id].stop)

    async def async_cmd_play(self, player_id: str):
        """
        Send STOP command to given player.
            :param player_id: player_id of the player to handle the command.
        """
        self.mass.add_job(self._players[player_id].play)

    async def async_cmd_pause(self, player_id: str):
        """
        Send PAUSE command to given player.
            :param player_id: player_id of the player to handle the command.
        """
        self.mass.add_job(self._players[player_id].pause)

    async def async_cmd_next(self, player_id: str):
        """
        Send NEXT TRACK command to given player.
            :param player_id: player_id of the player to handle the command.
        """
        self.mass.add_job(self._players[player_id].next)

    async def async_cmd_previous(self, player_id: str):
        """
        Send PREVIOUS TRACK command to given player.
            :param player_id: player_id of the player to handle the command.
        """
        self.mass.add_job(self._players[player_id].previous)

    async def async_cmd_power_on(self, player_id: str):
        """
        Send POWER ON command to given player.
            :param player_id: player_id of the player to handle the command.
        """
        self.mass.add_job(self._players[player_id].power_on)

    async def async_cmd_power_off(self, player_id: str):
        """
        Send POWER OFF command to given player.
            :param player_id: player_id of the player to handle the command.
        """
        self.mass.add_job(self._players[player_id].power_off)

    async def async_cmd_volume_set(self, player_id: str, volume_level: int):
        """
        Send volume level command to given player.
            :param player_id: player_id of the player to handle the command.
            :param volume_level: volume level to set (0..100).
        """
        self.mass.add_job(self._players[player_id].volume_set, volume_level / 100)

    async def async_cmd_volume_mute(self, player_id: str, is_muted=False):
        """
        Send volume MUTE command to given player.
            :param player_id: player_id of the player to handle the command.
            :param is_muted: bool with new mute state.
        """
        self.mass.add_job(self._players[player_id].volume_mute, is_muted)

    async def async_cmd_queue_load(self, player_id: str, queue_items: List[QueueItem]):
        """
        Load/overwrite given items in the player's queue implementation
            :param player_id: player_id of the player to handle the command.
            :param queue_items: a list of QueueItems
        """
        self.mass.add_job(self._players[player_id].queue_load, queue_items)

    async def async_cmd_queue_append(self, player_id: str, queue_items: List[QueueItem]):
        """
        Append new items at the end of the queue.
            :param player_id: player_id of the player to handle the command.
            :param queue_items: a list of QueueItems
        """
        self.mass.add_job(self._players[player_id].queue_append, queue_items)

    def __chromecast_add_update_callback(self, cast_uuid, cast_service_name):
        """Handle zeroconf discovery of a new or updated chromecast."""
        # pylint: disable=unused-argument
        if uuid is None:
            return  # Discovered chromecast without uuid
        service = self._listener.services[cast_uuid]
        cast_info = ChromecastInfo(
            services=service[0],
            uuid=service[1],
            model_name=service[2],
            friendly_name=service[3],
            host=service[4],
            port=service[5],
        )
        player_id = cast_info.uuid
        LOGGER.debug("Chromecast discovered: %s (%s)", cast_info.friendly_name, player_id)
        if player_id in self._players:
            # player already added, the player will take care of reconnects itself.
            LOGGER.debug("Player is already added:  %s (%s)", cast_info.friendly_name, player_id)
        else:
            player = ChromecastPlayer(self.mass, cast_info)
            self._players[player_id] = player
            self.mass.add_job(self.mass.player_manager.async_add_player(player))
        self.mass.add_job(self._players[player_id].async_set_cast_info, cast_info)

    def __chromecast_remove_callback(self, cast_uuid, cast_service_name, cast_service):
        # pylint: disable=unused-argument
        player_id = str(cast_service[1])
        friendly_name = cast_service[3]
        LOGGER.debug("Chromecast removed: %s - %s", friendly_name, player_id)
        self.mass.add_job(self.mass.player_manager.async_remove_player(player_id))


# class StatusListener:
#     def __init__(self, player_id, status_callback, mass):
#         self.__handle_callback = status_callback
#         self.mass = mass
#         self.player_id = player_id

#     def new_cast_status(self, status):
#         """chromecast status changed (like volume etc.)"""
#         self.mass.run_task(self.__handle_callback(caststatus=status))

#     def new_media_status(self, status):
#         """mediacontroller has new state"""
#         self.mass.run_task(self.__handle_callback(mediastatus=status))

#     def new_connection_status(self, status):
#         """will be called when the connection changes"""
#         if status.status == CONNECTION_STATUS_DISCONNECTED:
#             # schedule a new scan which will handle reconnects and group parent changes
#             self.mass.loop.run_in_executor(
#                 None, self.mass.player_manager.providers[PROV_ID].run_chromecast_discovery
#             )


# class MZListener:
#     def __init__(self, mz, callback, loop):
#         self._mz = mz
#         self._loop = loop
#         self.__handle_group_members_update = callback

#     def multizone_member_added(self, uuid):
#         asyncio.run_coroutine_threadsafe(
#             self.__handle_group_members_update(self._mz, added_player=str(uuid)),
#             self._loop,
#         )

#     def multizone_member_removed(self, uuid):
#         asyncio.run_coroutine_threadsafe(
#             self.__handle_group_members_update(self._mz, removed_player=str(uuid)),
#             self._loop,
#         )

#     def multizone_status_received(self):
#         asyncio.run_coroutine_threadsafe(
#             self.__handle_group_members_update(self._mz), self._loop
#         )


# async def async_ __report_progress(self):
#     """report current progress while playing"""
#     # chromecast does not send updates of the player's progress (cur_time)
#     # so we need to send it in periodically
#     while self._state == PlayerState.Playing:
#         self.cur_time = self.media_status.adjusted_current_time
#         await asyncio.sleep(1)
#     self.__cc_report_progress_task = None

# async def async_ handle_player_state(self, caststatus=None, mediastatus=None):
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
#         self.current_uri = mediastatus.content_id
#         self.cur_time = mediastatus.adjusted_current_time
#     if (
#         self._state == PlayerState.Playing
#         and self.__cc_report_progress_task is None
#     ):
#         self.__cc_report_progress_task = self.mass.add_job(
#             self.__report_progress()
#         )

# def __chromecast_discovered(self, cast_info):
#     """callback when a (new) chromecast device is discovered"""
#     player_id = cast_info.uuid
#     player = self.mass.player_manager.get_player_sync(player_id)
#     if self.mass.player_manager.get_player_sync(player_id):
#         # player already added, the player will take care of reconnects itself.
#         LOGGER.warning("Player is already added: %s", player_id)
#         self.mass.add_job(player.async_set_cast_info(cast_info))
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

# async def async_ __handle_group_members_update(
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
