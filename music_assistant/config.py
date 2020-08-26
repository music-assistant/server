"""All classes and helpers for the Configuration."""

import os
import shutil

from typing import List
from enum import Enum

from music_assistant.constants import (
    CONF_KEY_BASE,
    CONF_KEY_PROVIDERS,
    CONF_KEY_PLAYERSETTINGS,
    EVENT_CONFIG_CHANGED,
    CONF_ENABLED,
    CONF_NAME
)
from music_assistant.utils import LOGGER, json, try_load_json_file
# from music_assistant.mass import MusicAssistant
from music_assistant.models.config_entry import ConfigEntry, ConfigEntryType

DEFAULT_PLAYER_CONFIG_ENTRIES = [
    ConfigEntry(entry_key=CONF_ENABLED, entry_type=ConfigEntryType.BOOL,
                default_value=True, description_key="player_enabled"),
    ConfigEntry(entry_key=CONF_NAME, entry_type=ConfigEntryType.BOOL,
                default_value=True, description_key="player_name"),
    ConfigEntry(entry_key="mute_as_power", entry_type=ConfigEntryType.BOOL,
                default_value=True, description_key="player_mute_power"),
    ConfigEntry(entry_key="max_sample_rate", entry_type=ConfigEntryType.INT,
                values=[41000, 48000, 96000, 176000, 192000, 384000],
                default_value=96000, description_key="max_sample_rate"),
    ConfigEntry(entry_key="volume_normalisation", entry_type=ConfigEntryType.BOOL,
                default_value=True, description_key="enable_r128_volume_normalisation"),
    ConfigEntry(entry_key="target_volume", entry_type=ConfigEntryType.INT, range=(-30, 0),
                default_value=-23, description_key="target_volume_lufs"),
    ConfigEntry(entry_key="fallback_gain_correct", entry_type=ConfigEntryType.INT, range=(-20, 0),
                default_value=-12, description_key="fallback_gain_correct"),
    ConfigEntry(entry_key="crossfade_duration", entry_type=ConfigEntryType.INT, range=(0, 10),
                default_value=0, description_key="fallback_gain_correct"),
]

DEFAULT_PROVIDER_CONFIG_ENTRIES = [
    ConfigEntry(entry_key=CONF_ENABLED, entry_type=ConfigEntryType.BOOL,
                default_value=True, description_key="enabled")]

DEFAULT_BASE_CONFIG_ENTRIES = [
    ConfigEntry(entry_key="http_port", entry_type=ConfigEntryType.INT,
                default_value=8095, description_key="web_http_port"),
    ConfigEntry(entry_key="https_port", entry_type=ConfigEntryType.INT,
                default_value=8096, description_key="web_https_port"),
    ConfigEntry(entry_key="ssl_certificate", entry_type=ConfigEntryType.STRING,
                default_value="", description_key="web_ssl_cert"),
    ConfigEntry(entry_key="ssl_key", entry_type=ConfigEntryType.STRING,
                default_value="", description_key="ssl_key")
]


class ConfigBaseType(Enum):
    """Enum with config base types."""
    BASE = CONF_KEY_BASE
    PLAYER = CONF_KEY_PLAYERSETTINGS
    PROVIDER = CONF_KEY_PROVIDERS


class ConfigItem():
    """
        Configuration Item connected to Config Entries.
        Returns default value from config entry if no value present.
    """

    def __init__(self, mass, parent_item_key: str, base_type: ConfigBaseType):
        self.parent_item_key = parent_item_key
        self.base_type = base_type
        self.mass = mass
        self.stored_config = dict()

    def get(self, key, default=None):
        """Return value for specified key."""
        try:
            return self.__getitem__(key)
        except KeyError:
            return default

    def __getitem__(self, key):
        """Return default value from ConfigEntry if needed."""
        if key in self.stored_config:
            # TODO: validate value in stored config against config entry ?
            return self.stored_config[key]
        for entry in self.get_config_entries():
            if entry.entry_key == key:
                return entry.default_value
        raise KeyError

    def __setitem__(self, key, value):
        """Store value and validate."""
        for entry in self.get_config_entries():
            if entry.entry_key != key:
                continue
            # do some simple type checking
            if entry.multi_value and not isinstance(value, list):
                raise ValueError
            elif entry.entry_type == ConfigEntryType.STRING and not isinstance(value, str):
                raise ValueError
            elif entry.entry_type == ConfigEntryType.BOOL and not isinstance(value, bool):
                raise ValueError
            elif entry.entry_type == ConfigEntryType.FLOAT and not isinstance(value, (float, int)):
                raise ValueError
            if value != entry.default_value:
                self.stored_config[key] = value
                self.mass.signal_event(EVENT_CONFIG_CHANGED, (self.base_type, self.parent_item_key))
            return
        # raise KeyError if we're trying to set a value not defined as ConfigEntry
        raise KeyError

    def get_config_entries(self) -> List[ConfigEntry]:
        """Return config entries for this item."""
        if self.base_type == ConfigBaseType.PLAYER:
            return self.mass.config.get_player_config_entries(self.parent_item_key)
        if self.base_type == ConfigBaseType.PROVIDER:
            self.mass.config.get_provider_config_entries(self.parent_item_key)
        return self.mass.config.get_base_config_entries()


class ConfigBase():
    """Configuration class with ConfigItem items."""

    def __init__(self, mass, base_type=ConfigBaseType):
        self.mass = mass
        self.base_type = base_type
        self.stored_config = dict()

    def get(self, key):
        """Return configuration for specified item."""
        if not key in self.stored_config:
            # create new ConfigDictItem on the fly
            self.stored_config[key] = ConfigItem(self.mass, key, self.base_type)
        return self.stored_config[key]

    def __getitem__(self, item_key):
        """Convenience method for get."""
        return self.get(item_key)


class MassConfig():
    """Class which holds our configuration"""

    def __init__(self, mass):
        self.loading = False
        self.mass = mass
        self._conf_base = ConfigItem(mass, "base", ConfigBaseType.BASE)
        self._conf_players = ConfigBase(mass, ConfigBaseType.PLAYER)
        self._conf_providers = ConfigBase(mass, ConfigBaseType.PROVIDER)
        self.__load()

    @property
    def base(self):
        """return base config"""
        return self._conf_base

    @property
    def players(self):
        """return player settings"""
        return self._conf_players

    @property
    def providers(self):
        """return playerprovider settings"""
        return self._conf_providers

    def get_provider_config(self, provider_id):
        """Return config for given provider."""
        return self.providers[provider_id]

    def get_player_config(self, player_id):
        """Return config for given player."""
        return self.players[player_id]

    def get_provider_config_entries(self, provider_id: str) -> List[ConfigEntry]:
        """Return all config entries for the given provider."""
        conf_entries = DEFAULT_PROVIDER_CONFIG_ENTRIES
        provider = self.mass.get_provider(provider_id)
        if provider:
            conf_entries += provider.config_entries
        return conf_entries

    def get_player_config_entries(self, player_id: str) -> List[ConfigEntry]:
        """Return all config entries for the given player."""
        conf_entries = DEFAULT_PLAYER_CONFIG_ENTRIES
        player = self.mass.player_manager.get_player(player_id)
        if player:
            conf_entries += player.config_entries
        return conf_entries

    def get_base_config_entries(self) -> List[ConfigEntry]:
        """Return all base config entries."""
        return DEFAULT_BASE_CONFIG_ENTRIES

    def as_dict(self):
        """Return entire config as dict."""
        return {
            CONF_KEY_BASE: self.base.stored_config,
            CONF_KEY_PLAYERSETTINGS: self.players.stored_config,
            CONF_KEY_PROVIDERS: self.providers.stored_config
        }

    async def async_save(self):
        """Save config."""
        await self.mass.async_run_job(self.save)

    def save(self):
        """Save config to file."""
        if self.loading:
            LOGGER.warning("save already running")
            return
        self.loading = True
        # backup existing file
        conf_file = os.path.join(self.mass.datapath, "config.json")
        conf_file_backup = os.path.join(self.mass.datapath, "config.json.backup")
        if os.path.isfile(conf_file):
            shutil.move(conf_file, conf_file_backup)
        # write current config to file
        with open(conf_file, "w") as _file:
            _file.write(json.dumps(self.as_dict(), indent=4))
        LOGGER.info("Config saved!")
        self.loading = False

    def __load(self):
        """load config from file"""
        self.loading = True
        conf_file = os.path.join(self.mass.datapath, "config.json")
        data = try_load_json_file(conf_file)
        if not data:
            # might be a corrupt config file, retry with backup file
            conf_file_backup = os.path.join(self.mass.datapath, "config.json.backup")
            data = try_load_json_file(conf_file_backup)
        if data:
            if CONF_KEY_BASE in data and not data[CONF_KEY_BASE].get("web"):
                for key, value in data[CONF_KEY_BASE].items():
                    if key == "__desc__":
                        continue
                    self.base.stored_config[key] = value
            if CONF_KEY_PLAYERSETTINGS in data:
                for player_id, player in data[CONF_KEY_PLAYERSETTINGS].items():
                    for key, value in player.items():
                        if key == "__desc__":
                            continue
                        self.players[player_id].stored_config[key] = value
            if CONF_KEY_PROVIDERS in data:
                for provider_id, provider in data[CONF_KEY_PROVIDERS].items():
                    for key, value in provider.items():
                        if key == "__desc__":
                            continue
                        self.providers[provider_id].stored_config[key] = value
            # migrate from previous format
            if CONF_KEY_BASE in data and data[CONF_KEY_BASE].get("web"):
                for key, value in data[CONF_KEY_BASE]["web"].items():  # legacy
                    if key == "__desc__":
                        continue
                    self.base.stored_config[key] = value
            for legacy_key in ["musicproviders", "playerproviders"]:
                if legacy_key in data:
                    for provider_id, provider in data[legacy_key].items():
                        for key, value in provider.items():
                            if key == "__desc__":
                                continue
                            self.providers[provider_id].stored_config[key] = value

        self.loading = False
