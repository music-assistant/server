"""Plugin for Music Assistant that enables integration with Home Assistant."""

import asyncio
import logging
import os
from typing import List

import slugify as slug
from hass_client import HomeAssistant
from music_assistant.constants import (
    CONF_TOKEN,
    CONF_URL,
    EVENT_HASS_ENTITY_CHANGED,
    EVENT_PLAYER_ADDED,
    EVENT_PLAYER_CHANGED,
    EVENT_PLAYER_REMOVED,
)
from music_assistant.models.config_entry import ConfigEntry, ConfigEntryType
from music_assistant.models.player import Player
from music_assistant.models.provider import Provider
from music_assistant.utils import run_periodic

PROV_ID = "homeassistant"
PROV_NAME = "Home Assistant integration"

IS_HASSIO = os.path.isfile("/data/options.json")
CONF_PUBLISH_PLAYERS = "publish_players"
LOGGER = logging.getLogger(PROV_ID)

CONFIG_ENTRIES = [
    ConfigEntry(entry_key=CONF_URL, entry_type=ConfigEntryType.STRING, description_key=CONF_URL),
    ConfigEntry(
        entry_key=CONF_TOKEN, entry_type=ConfigEntryType.PASSWORD, description_key=CONF_TOKEN
    ),
    ConfigEntry(
        entry_key=CONF_PUBLISH_PLAYERS,
        entry_type=ConfigEntryType.BOOL,
        description_key=CONF_TOKEN,
        default_value=True,
    ),
]


async def async_setup(mass):
    """Perform async setup of this Plugin/Provider."""
    prov = HomeAssistantPlugin()
    await mass.async_register_provider(prov)


class HomeAssistantPlugin(Provider):
    """
    Homeassistant plugin
    allows publishing of our players to hass
    allows using hass entities (like switches, media_players or gui inputs) to be triggered
    """

    _hass = None
    _tasks = []
    _tracked_entities = []
    _sources = []
    _published_players = {}

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
        return CONFIG_ENTRIES

    async def async_on_start(self) -> bool:
        """Called on startup. Handle initialization of the provider based on config."""
        config = self.mass.config.get_provider_config(PROV_ID)
        if IS_HASSIO:
            config[CONF_TOKEN] = os.environ["HASSIO_TOKEN"]
            config[CONF_URL] = "hassio/homeassistant"
        # pylint: disable=attribute-defined-outside-init
        if not (config[CONF_URL] and config[CONF_TOKEN]):
            LOGGER.warning("Invalid configuration for Home Assistant")
            return False
        self._hass = HomeAssistant(config[CONF_URL], config[CONF_TOKEN])
        # register callbacks
        self._hass.register_event_callback(self.__async_hass_event)
        self.mass.add_event_listener(self.__async_mass_event, EVENT_PLAYER_CHANGED)
        self.mass.add_event_listener(self.__async_mass_event, EVENT_PLAYER_ADDED)
        self.mass.add_event_listener(self.__async_mass_event, EVENT_PLAYER_REMOVED)
        await self._hass.async_connect()
        self._tasks.append(self.mass.add_job(self.__async_get_sources()))
        return True

    async def async_on_stop(self):
        """Called on shutdown. Handle correct close/cleanup of the provider on exit."""
        for task in self._tasks:
            task.cancel()
        if self._hass:
            await self._hass.async_close()

    async def __async_mass_event(self, event, event_data):
        """Received event from Music Assistant"""
        if event in [EVENT_PLAYER_CHANGED, EVENT_PLAYER_ADDED]:
            await self.__async_publish_player(event_data)
        # TODO: player removals

    async def __async_hass_event(self, event_type, event_data):
        """Received event from Home Assistant"""
        if event_type == "state_changed":
            if event_data["entity_id"] in self._tracked_entities:
                new_state = event_data["new_state"]
                LOGGER.info(
                    "Received new state for %s: %s", event_data["entity_id"], new_state["state"]
                )
                # TODO
        elif event_type == "call_service" and event_data["domain"] == "media_player":
            await self.__async_handle_player_command(
                event_data["service"], event_data["service_data"]
            )

    async def __async_handle_player_command(self, service, service_data):
        """Handle forwarded service call for one of our players."""
        if isinstance(service_data["entity_id"], list):
            # can be a list of entity ids if action fired on multiple items
            entity_ids = service_data["entity_id"]
        else:
            entity_ids = [service_data["entity_id"]]
        for entity_id in entity_ids:
            if entity_id in self._published_players:
                # call is for one of our players so handle it
                player_id = self._published_players[entity_id]
                if not self.mass.player_manager.get_player(player_id):
                    return
                if service == "turn_on":
                    await self.mass.player_manager.async_cmd_power_on(player_id)
                elif service == "turn_off":
                    await self.mass.player_manager.async_cmd_power_off(player_id)
                elif service == "toggle":
                    await self.mass.player_manager.async_cmd_power_toggle(player_id)
                elif service == "volume_mute":
                    await self.mass.player_manager.async_cmd_volume_mute(
                        player_id, service_data["is_volume_muted"]
                    )
                elif service == "volume_up":
                    await self.mass.player_manager.async_cmd_volume_up(player_id)
                elif service == "volume_down":
                    await self.mass.player_manager.async_cmd_volume_down(player_id)
                elif service == "volume_set":
                    volume_level = service_data["volume_level"] * 100
                    await self.mass.player_manager.async_cmd_volume_set(player_id, volume_level)
                elif service == "media_play":
                    await self.mass.player_manager.async_cmd_play(player_id)
                elif service == "media_pause":
                    await self.mass.player_manager.async_cmd_pause(player_id)
                elif service == "media_stop":
                    await self.mass.player_manager.async_cmd_stop(player_id)
                elif service == "media_next_track":
                    await self.mass.player_manager.async_cmd_next(player_id)
                elif service == "media_play_pause":
                    await self.mass.player_manager.async_cmd_play_pause(player_id)
                elif service in ["play_media", "select_source"]:
                    return await self.__async_handle_play_media(player_id, service_data)

    async def __async_handle_play_media(self, player_id, service_data):
        """Handle play media request from homeassistant."""
        media_content_id = service_data.get("media_content_id")
        if not media_content_id:
            media_content_id = service_data.get("source")
        queue_opt = "add" if service_data.get("enqueue") else "play"
        if not "://" in media_content_id:
            media_items = []
            for playlist_str in media_content_id.split(","):
                playlist_str = playlist_str.strip()
                playlist = await self.mass.music_manager.async_get_library_playlist_by_name(
                    playlist_str
                )
                if playlist:
                    media_items.append(playlist)
                else:
                    radio = await self.mass.music_manager.async_get_radio_by_name(playlist_str)
                    if radio:
                        media_items.append(radio)
                        queue_opt = "play"
            return await self.mass.player_manager.async_play_media(
                player_id, media_items, queue_opt
            )
        elif "spotify://playlist" in media_content_id:
            # TODO: handle parsing of other uri's here
            playlist = await self.mass.music_manager.async_getplaylist(
                "spotify", media_content_id.split(":")[-1]
            )
            return await self.mass.player_manager.async_play_media(player_id, playlist, queue_opt)

    async def __async_publish_player(self, player: Player):
        """Publish player details to Home Assistant."""
        if not self.mass.config.providers[PROV_ID]["publish_players"]:
            return False
        # TODO: throttle updates to home assistant ?
        player_id = player.player_id
        entity_id = "media_player.mass_" + slug.slugify(player.name, separator="_").lower()
        state = player.state
        state_attributes = {
            "supported_features": 65471,
            "friendly_name": player.name,
            "source_list": self._sources,
            "source": "unknown",
            "volume_level": player.volume_level / 100,
            "is_volume_muted": player.muted,
            # "media_position_updated_at": player.media_position_updated_at,
            "media_duration": None,
            "media_position": player.elapsed_time,
            "media_title": None,
            "media_artist": None,
            "media_album_name": None,
            "entity_picture": None,
            "mass_player_id": player_id,
        }
        # if state != "off":
        #     player = await self.mass.player_manager.get_player(player_id)
        #     if player.queue.cur_item:
        #         state_attributes["media_duration"] = player.queue.cur_item.duration
        #         state_attributes["media_title"] = player.queue.cur_item.name
        #         if player.queue.cur_item.artists:
        #             state_attributes["media_artist"] = player.queue.cur_item.artists[
        #                 0
        #             ].name
        #         if player.queue.cur_item.album:
        #             state_attributes[
        #                 "media_album_name"
        #             ] = player.queue.cur_item.album.name
        #             state_attributes[
        #                 "entity_picture"
        #             ] = player.queue.cur_item.album.metadata.get("image")
        self._published_players[entity_id] = player.player_id
        await self._hass.async_set_state(entity_id, state, state_attributes)

    @run_periodic(600)
    async def __async_get_sources(self):
        """We build a list of all playlists to use as player sources."""
        self._sources = [
            playlist.name
            async for playlist in self.mass.music_manager.async_get_library_playlists()
        ]
        self._sources += [
            playlist.name async for playlist in self.mass.music_manager.async_get_library_radios()
        ]
