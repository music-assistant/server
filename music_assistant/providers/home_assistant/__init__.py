"""Plugin that enables integration with Home Assistant."""

import logging
from typing import List

from hass_client import (
    EVENT_CONNECTED,
    EVENT_STATE_CHANGED,
    IS_SUPERVISOR,
    HomeAssistant,
)
from music_assistant.constants import CONF_URL
from music_assistant.models.config_entry import ConfigEntry, ConfigEntryType
from music_assistant.models.player import PlayerControl, PlayerControlType
from music_assistant.models.provider import Provider
from music_assistant.utils import callback, try_parse_float

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
    entry_key=CONF_TOKEN,
    entry_type=ConfigEntryType.PASSWORD,
    description_key="hass_token",
)


async def async_setup(mass):
    """Perform async setup of this Plugin/Provider."""
    prov = HomeAssistantPlugin()
    await mass.async_register_provider(prov)


class HomeAssistantPlugin(Provider):
    """Homeassistant plugin.

    allows using hass entities (like switches, media_players or gui inputs) to be triggered
    """

    def __init__(self, *args, **kwargs):
        """Initialize."""
        self._hass: HomeAssistant = None
        self._tasks = []
        self._tracked_entities = []
        self._sources = []
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
            ConfigEntry(
                entry_key=CONF_POWER_ENTITIES,
                entry_type=ConfigEntryType.STRING,
                description_key=CONF_POWER_ENTITIES,
                default_value=[],
                values=self.__get_power_control_entities(),
                multi_value=True,
            ),
            ConfigEntry(
                entry_key=CONF_VOLUME_ENTITIES,
                entry_type=ConfigEntryType.STRING,
                description_key=CONF_VOLUME_ENTITIES,
                default_value=[],
                values=self.__get_volume_control_entities(),
                multi_value=True,
            ),
        ]
        return entries

    async def async_on_start(self) -> bool:
        """Handle initialization of the provider based on config."""
        config = self.mass.config.get_provider_config(PROV_ID)
        if IS_SUPERVISOR:
            self._hass = HomeAssistant(loop=self.mass.loop)
        else:
            self._hass = HomeAssistant(
                config[CONF_URL], config[CONF_TOKEN], loop=self.mass.loop
            )
        # register callbacks
        self._hass.register_event_callback(self.__async_hass_event)
        await self._hass.async_connect()
        return True

    async def async_on_stop(self):
        """Handle correct close/cleanup of the provider on exit."""
        for task in self._tasks:
            task.cancel()
        if self._hass:
            await self._hass.async_close()

    async def __async_hass_event(self, event_type, event_data):
        """Receive event from Home Assistant."""
        if event_type == EVENT_STATE_CHANGED:
            if event_data["entity_id"] in self._tracked_entities:
                new_state = event_data["new_state"]
                await self.__async_update_player_controls(new_state)
        elif event_type == EVENT_CONNECTED:
            # register player controls on connect
            self.mass.add_job(self.__async_register_player_controls())

    @callback
    def __get_power_control_entities(self):
        """Return list of entities that can be used as power control."""
        if not self._hass or not self._hass.states:
            return []
        result = []
        for entity in self._hass.media_players + self._hass.switches:
            if not entity:
                continue
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
            if not entity:
                continue
            entity_id = entity["entity_id"]
            entity_name = entity["attributes"].get("friendly_name", entity_id)
            if entity_id.startswith("media_player.mass_"):
                continue
            result.append(
                {
                    "value": f"volume_{entity_id}",
                    "text": entity_name,
                    "entity_id": entity_id,
                }
            )
        return result

    async def __async_update_player_controls(self, entity_obj):
        """Update player control(s) when a new entity state comes in."""
        for control_entity in self.__get_power_control_entities():
            if control_entity["entity_id"] != entity_obj["entity_id"]:
                continue
            cur_state = entity_obj["state"] not in ["off", "unavailable"]
            if control_entity.get("source"):
                cur_state = (
                    entity_obj["attributes"].get("source") == control_entity["source"]
                )
            await self.mass.player_manager.async_update_player_control(
                control_entity["value"], cur_state
            )
        for control_entity in self.__get_volume_control_entities():
            if control_entity["entity_id"] != entity_obj["entity_id"]:
                continue
            cur_state = int(
                try_parse_float(entity_obj["attributes"].get("volume_level")) * 100
            )
            await self.mass.player_manager.async_update_player_control(
                control_entity["value"], cur_state
            )

    async def __async_register_player_controls(self):
        """Register all (enabled) player controls."""
        await self.__async_register_power_controls()
        await self.__async_register_volume_controls()

    async def __async_register_power_controls(self):
        """Register all (enabled) power controls."""
        conf = self.mass.config.providers[PROV_ID]
        enabled_controls = conf[CONF_POWER_ENTITIES]
        for control_entity in self.__get_power_control_entities():
            enabled_controls = conf[CONF_POWER_ENTITIES]
            if not control_entity["value"] in enabled_controls:
                continue
            entity_id = control_entity["entity_id"]
            if entity_id not in self._hass.states:
                LOGGER.warning("entity not found: %s", entity_id)
                continue
            state_obj = self._hass.states[entity_id]
            cur_state = state_obj["state"] not in ["off", "unavailable"]
            source = control_entity.get("source")
            if source:
                cur_state = (
                    state_obj["attributes"].get("source") == control_entity["source"]
                )

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
            if entity_id not in self._tracked_entities:
                self._tracked_entities.append(entity_id)

    async def __async_register_volume_controls(self):
        """Register all (enabled) power controls."""
        conf = self.mass.config.providers[PROV_ID]
        enabled_controls = conf[CONF_VOLUME_ENTITIES]
        for control_entity in self.__get_volume_control_entities():
            if not control_entity["value"] in enabled_controls:
                continue
            entity_id = control_entity["entity_id"]
            if entity_id not in self._hass.states:
                LOGGER.warning("entity not found: %s", entity_id)
                continue
            cur_volume = (
                try_parse_float(self._hass.get_state(entity_id, "volume_level")) * 100
            )
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
            if entity_id not in self._tracked_entities:
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
