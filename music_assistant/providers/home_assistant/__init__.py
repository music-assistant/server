"""Plugin that enables integration with Home Assistant."""

import asyncio
import functools
import logging
import os
from typing import List

import slugify as slug
from hass_client import (
    EVENT_CONNECTED,
    EVENT_STATE_CHANGED,
    IS_SUPERVISOR,
    HomeAssistant,
)
from music_assistant.constants import (
    CONF_URL,
    EVENT_HASS_ENTITY_CHANGED,
    EVENT_PLAYER_ADDED,
    EVENT_PLAYER_CHANGED,
    EVENT_PLAYER_REMOVED,
)
from music_assistant.models.config_entry import ConfigEntry, ConfigEntryType
from music_assistant.models.media_types import MediaType
from music_assistant.models.player import Player, PlayerControl, PlayerControlType
from music_assistant.models.provider import Provider
from music_assistant.utils import callback, run_periodic, try_parse_float

PROV_ID = "homeassistant"
PROV_NAME = "Home Assistant integration"

CONF_PUBLISH_PLAYERS = "hass_publish_players"
CONF_POWER_ENTITIES = "hass_power_entities"
CONF_VOLUME_ENTITIES = "hass_volume_entities"
CONF_TOKEN = "hass_token"

LOGGER = logging.getLogger(PROV_ID)

CONFIG_ENTRY_URL = ConfigEntry(
    entry_key=CONF_URL, entry_type=ConfigEntryType.STRING, description_key="hass_url"
)
CONFIG_ENTRY_TOKEN = ConfigEntry(
    entry_key=CONF_TOKEN, entry_type=ConfigEntryType.PASSWORD, description_key="hass_token"
)
CONFIG_ENTRY_PUBLISH_PLAYERS = ConfigEntry(
    entry_key=CONF_PUBLISH_PLAYERS,
    entry_type=ConfigEntryType.BOOL,
    description_key=CONF_PUBLISH_PLAYERS,
    default_value=True,
)

# TODO: handle player removals and renames in publishing to hass

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

    def __init__(self, *args, **kwargs):
        """Initialize."""
        self._hass: HomeAssistant = None
        self._tasks = []
        self._tracked_entities = []
        self._sources = []
        self._published_players = {}
        self._power_entities = []
        self._volume_entities = []
        super().__init__(*args, **kwargs)

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
        entries = []
        if not IS_SUPERVISOR:
            entries.append(CONFIG_ENTRY_URL)
            entries.append(CONFIG_ENTRY_TOKEN)
        entries += [
            CONFIG_ENTRY_PUBLISH_PLAYERS,
            ConfigEntry(
                entry_key=CONF_POWER_ENTITIES,
                entry_type=ConfigEntryType.STRING,
                description_key=CONF_POWER_ENTITIES,
                default_value=[],
                values=self._power_entities,
                multi_value=True,
            ),
            ConfigEntry(
                entry_key=CONF_VOLUME_ENTITIES,
                entry_type=ConfigEntryType.STRING,
                description_key=CONF_VOLUME_ENTITIES,
                default_value=[],
                values=self._volume_entities,
                multi_value=True,
            ),
        ]
        return entries

    async def async_on_start(self) -> bool:
        """Called on startup. Handle initialization of the provider based on config."""
        config = self.mass.config.get_provider_config(PROV_ID)
        self._hass = HomeAssistant(config.get(CONF_URL), config.get(CONF_TOKEN))
        # register callbacks
        self._hass.register_event_callback(self.__async_hass_event)
        self.mass.add_event_listener(
            self.__async_mass_event,
            [EVENT_PLAYER_CHANGED, EVENT_PLAYER_ADDED, EVENT_PLAYER_REMOVED],
        )
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
        if event_type == EVENT_STATE_CHANGED:
            if event_data["entity_id"] in self._tracked_entities:
                new_state = event_data["new_state"]
                await self.__async_update_player_controls(new_state)
        elif event_type == "call_service" and event_data["domain"] == "media_player":
            await self.__async_handle_player_command(
                event_data["service"], event_data["service_data"]
            )
        elif event_type == EVENT_CONNECTED:
            # register player controls on connect
            self.mass.add_job(self.__async_register_player_controls())

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
        if not self.mass.config.providers[PROV_ID][CONF_PUBLISH_PLAYERS]:
            return False
        if not player.available:
            return
        player_id = player.player_id
        entity_id = "media_player.mass_" + slug.slugify(player.name, separator="_").lower()
        player_queue = self.mass.player_manager.get_player_queue(player_id)
        cur_item = player_queue.cur_item if player_queue else None
        state_attributes = {
            "supported_features": 65471,
            "friendly_name": player.name,
            "source_list": self._sources,
            "source": "unknown",
            "volume_level": player.volume_level / 100,
            "is_volume_muted": player.muted,
            "media_position_updated_at": player.updated_at.isoformat(),
            "media_duration": cur_item.duration if cur_item else None,
            "media_position": player_queue.cur_item_time if player_queue else None,
            "media_title": cur_item.name if cur_item else None,
            "media_artist": cur_item.artists[0].name if cur_item and cur_item.artists else None,
            "media_album_name": cur_item.album.name if cur_item and cur_item.album else None,
            "entity_picture": "",
            "mass_player_id": player_id,
        }
        if cur_item:
            host = f"{self.mass.web.local_ip}:{self.mass.web.http_port}"
            item_type = "radio" if cur_item.media_type == MediaType.Radio else "track"
            img_url = f"http://{host}/api/{item_type}/{cur_item.item_id}/thumb?provider={cur_item.provider}"
            state_attributes["entity_picture"] = img_url
        self._published_players[entity_id] = player.player_id
        await self._hass.async_set_state(entity_id, player.state, state_attributes)

    @run_periodic(600)
    async def __async_get_sources(self):
        """We build a list of all playlists to use as player sources."""
        # pylint: disable=attribute-defined-outside-init
        self._sources = [
            playlist.name
            async for playlist in self.mass.music_manager.async_get_library_playlists()
        ]
        self._sources += [
            playlist.name async for playlist in self.mass.music_manager.async_get_library_radios()
        ]

    @callback
    def __get_power_control_entities(self):
        """Return list of entities that can be used as power control."""
        if not self._hass or not self._hass.states:
            return []
        result = []
        for entity in self._hass.media_players + self._hass.switches:
            entity_id = entity["entity_id"]
            entity_name = entity["attributes"].get("friendly_name", entity_id)
            if entity_id.startswith("media_player.mass_"):
                continue
            source_list = entity["attributes"].get("source_list", [""])
            for source in source_list:
                result.append(
                    {
                        "value": f"power_{entity_id}_{source}",
                        "text": f"{entity_name}: {source}" if source else entity_name,
                        "entity_id": entity_id,
                        "source": source,
                    }
                )
        return result

    @callback
    def __get_volume_control_entities(self):
        """Return list of entities that can be used as volume control."""
        if not self._hass or not self._hass.states:
            return []
        result = []
        for entity in self._hass.media_players:
            entity_id = entity["entity_id"]
            entity_name = entity["attributes"].get("friendly_name", entity_id)
            if entity_id.startswith("media_player.mass_"):
                continue
            result.append(
                {"value": f"volume_{entity_id}", "text": entity_name, "entity_id": entity_id}
            )
        return result

    async def __async_update_player_controls(self, entity_obj):
        """Update player control(s) when a new entity state comes in."""
        for control_entity in self.__get_power_control_entities():
            if control_entity["entity_id"] != entity_obj["entity_id"]:
                continue
            cur_state = entity_obj["state"] != "off"
            if control_entity.get("source"):
                cur_state = entity_obj["attributes"].get("source") == control_entity["source"]
            await self.mass.player_manager.async_update_player_control(
                control_entity["value"], cur_state
            )
        for control_entity in self.__get_volume_control_entities():
            if control_entity["entity_id"] != entity_obj["entity_id"]:
                continue
            cur_state = int(try_parse_float(entity_obj["attributes"].get("volume_level")) * 100)
            await self.mass.player_manager.async_update_player_control(
                control_entity["value"], cur_state
            )

    async def __async_register_player_controls(self):
        """Register all (enabled) player controls."""
        self._volume_entities = self.__get_volume_control_entities()
        self._power_entities = self.__get_power_control_entities()
        await self.__async_register_power_controls()
        await self.__async_register_volume_controls()

    async def __async_register_power_controls(self):
        """Register all (enabled) power controls."""
        conf = self.mass.config.providers[PROV_ID]
        enabled_controls = conf[CONF_POWER_ENTITIES]
        for control_entity in self._power_entities:
            enabled_controls = conf[CONF_POWER_ENTITIES]
            if not control_entity["value"] in enabled_controls:
                continue
            entity_id = control_entity["entity_id"]
            if not entity_id in self._hass.states:
                LOGGER.warning("entity not found: %s", entity_id)
                continue
            state_obj = self._hass.states[entity_id]
            cur_state = state_obj["state"] != "off"
            source = control_entity.get("source")
            if source:
                cur_state = state_obj["attributes"].get("source") == control_entity["source"]

            control = PlayerControl(
                type=PlayerControlType.POWER,
                id=control_entity["value"],
                name=control_entity["text"],
                state=cur_state,
                set_state=self.async_power_control_set_state,
            )
            # store some vars on the control object for convenience
            control.entity_id = entity_id
            control.source = source
            await self.mass.player_manager.async_register_player_control(control)
            if not entity_id in self._tracked_entities:
                self._tracked_entities.append(entity_id)

    async def __async_register_volume_controls(self):
        """Register all (enabled) power controls."""
        conf = self.mass.config.providers[PROV_ID]
        enabled_controls = conf[CONF_VOLUME_ENTITIES]
        for control_entity in self._volume_entities:
            if not control_entity["value"] in enabled_controls:
                continue
            entity_id = control_entity["entity_id"]
            if not entity_id in self._hass.states:
                LOGGER.warning("entity not found: %s", entity_id)
                continue
            cur_volume = try_parse_float(self._hass.get_state(entity_id, "volume_level")) * 100
            control = PlayerControl(
                type=PlayerControlType.VOLUME,
                id=control_entity["value"],
                name=control_entity["text"],
                state=cur_volume,
                set_state=self.async_volume_control_set_state,
            )
            # store some vars on the control object for convenience
            control.entity_id = entity_id
            await self.mass.player_manager.async_register_player_control(control)
            if not entity_id in self._tracked_entities:
                self._tracked_entities.append(entity_id)

    async def async_power_control_set_state(self, control_id: str, new_state: bool):
        """Set state callback for power control."""
        control = self.mass.player_manager.get_player_control(control_id)
        if control.source:
            cur_source = self._hass.get_state(control.entity_id, "source")
            if cur_source is not None and cur_source != control.source:
                return
        if new_state and control.source:
            # select source
            await self._hass.async_call_service(
                "media_player",
                "select_source",
                {"source": control.source, "entity_id": control.entity_id},
            )
        elif new_state:
            # simple turn off
            await self._hass.async_call_service(
                "homeassistant", "turn_on", {"entity_id": control.entity_id}
            )
        else:
            # simple turn off
            await self._hass.async_call_service(
                "homeassistant", "turn_off", {"entity_id": control.entity_id}
            )

    async def async_volume_control_set_state(self, control_id: str, new_state: int):
        """Set state callback for volume control."""
        control = self.mass.player_manager.get_player_control(control_id)
        await self._hass.async_call_service(
            "media_player",
            "volume_set",
            {"volume_level": new_state / 100, "entity_id": control.entity_id},
        )
