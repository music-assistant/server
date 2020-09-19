"""All classes and helpers for the Configuration."""

import logging
import os
import shutil
from collections import OrderedDict
from enum import Enum
from typing import List

from music_assistant.constants import (
    CONF_CROSSFADE_DURATION,
    CONF_ENABLED,
    CONF_FALLBACK_GAIN_CORRECT,
    CONF_KEY_BASE,
    CONF_KEY_PLAYERSETTINGS,
    CONF_KEY_PROVIDERS,
    CONF_NAME,
    EVENT_CONFIG_CHANGED,
)
from music_assistant.models.config_entry import ConfigEntry, ConfigEntryType
from music_assistant.utils import (
    decrypt_string,
    encrypt_string,
    get_external_ip,
    json,
    try_load_json_file,
)
from passlib.hash import pbkdf2_sha256

LOGGER = logging.getLogger("mass")

DEFAULT_PLAYER_CONFIG_ENTRIES = [
    ConfigEntry(
        entry_key=CONF_ENABLED,
        entry_type=ConfigEntryType.BOOL,
        default_value=True,
        description_key="player_enabled",
    ),
    ConfigEntry(
        entry_key=CONF_NAME,
        entry_type=ConfigEntryType.STRING,
        default_value=None,
        description_key="player_name",
    ),
    ConfigEntry(
        entry_key="max_sample_rate",
        entry_type=ConfigEntryType.INT,
        values=[41000, 48000, 96000, 176000, 192000, 384000],
        default_value=96000,
        description_key="max_sample_rate",
    ),
    ConfigEntry(
        entry_key="volume_normalisation",
        entry_type=ConfigEntryType.BOOL,
        default_value=True,
        description_key="enable_r128_volume_normalisation",
    ),
    ConfigEntry(
        entry_key="target_volume",
        entry_type=ConfigEntryType.INT,
        range=(-30, 0),
        default_value=-23,
        description_key="target_volume_lufs",
        depends_on="volume_normalisation",
    ),
    ConfigEntry(
        entry_key=CONF_FALLBACK_GAIN_CORRECT,
        entry_type=ConfigEntryType.INT,
        range=(-20, 0),
        default_value=-12,
        description_key=CONF_FALLBACK_GAIN_CORRECT,
        depends_on="volume_normalisation",
    ),
    ConfigEntry(
        entry_key=CONF_CROSSFADE_DURATION,
        entry_type=ConfigEntryType.INT,
        range=(0, 10),
        default_value=0,
        description_key=CONF_CROSSFADE_DURATION,
    ),
]

DEFAULT_PROVIDER_CONFIG_ENTRIES = [
    ConfigEntry(
        entry_key=CONF_ENABLED,
        entry_type=ConfigEntryType.BOOL,
        default_value=True,
        description_key="enabled",
    )
]

DEFAULT_BASE_CONFIG_ENTRIES = {
    "web": [
        ConfigEntry(
            entry_key="http_port",
            entry_type=ConfigEntryType.INT,
            default_value=8095,
            description_key="web_http_port",
        ),
        ConfigEntry(
            entry_key="https_port",
            entry_type=ConfigEntryType.INT,
            default_value=8096,
            description_key="web_https_port",
        ),
        ConfigEntry(
            entry_key="ssl_certificate",
            entry_type=ConfigEntryType.STRING,
            default_value="",
            description_key="web_ssl_cert",
        ),
        ConfigEntry(
            entry_key="ssl_key",
            entry_type=ConfigEntryType.STRING,
            default_value="",
            description_key="web_ssl_key",
        ),
        ConfigEntry(
            entry_key="external_url",
            entry_type=ConfigEntryType.STRING,
            default_value=f"http://{get_external_ip()}:8095",
            description_key="web_external_url",
        ),
    ],
    "security": [
        ConfigEntry(
            entry_key="username",
            entry_type=ConfigEntryType.STRING,
            default_value="admin",
            description_key="security_username",
        ),
        ConfigEntry(
            entry_key="password",
            entry_type=ConfigEntryType.PASSWORD,
            default_value="",
            description_key="security_password",
            store_hashed=True,
        ),
    ],
}


class ConfigBaseType(Enum):
    """Enum with config base types."""

    BASE = CONF_KEY_BASE
    PLAYER = CONF_KEY_PLAYERSETTINGS
    PROVIDER = CONF_KEY_PROVIDERS


class ConfigItem:
    """
    Configuration Item connected to Config Entries.

    Returns default value from config entry if no value present.
    """

    def __init__(self, mass, parent_item_key: str, base_type: ConfigBaseType):
        """Initialize class."""
        self._parent_item_key = parent_item_key
        self._base_type = base_type
        self.mass = mass
        self.stored_config = OrderedDict()

    def __repr__(self):
        """Print class."""
        return f"{OrderedDict}({self.to_dict()})"

    def to_dict(self) -> dict:
        """Return entire config as dict."""
        result = OrderedDict()
        for entry in self.get_config_entries():
            if entry.entry_key in self.stored_config:
                # use saved value
                entry.value = self.stored_config[entry.entry_key]
            else:
                # use default value for config entry
                entry.value = entry.default_value
            result[entry.entry_key] = entry
        return result

    def get(self, key, default=None):
        """Return value if key exists, default if not."""
        try:
            return self[key]
        except KeyError:
            return default

    def get_entry(self, key):
        """Return complete ConfigEntry for specified key."""
        for entry in self.get_config_entries():
            if entry.entry_key == key:
                if key in self.stored_config:
                    # use saved value
                    entry.value = self.stored_config[key]
                else:
                    # use default value for config entry
                    entry.value = entry.default_value
                return entry
        raise KeyError

    def __getitem__(self, key) -> ConfigEntry:
        """Return default value from ConfigEntry if needed."""
        entry = self.get_entry(key)
        if entry.entry_type == ConfigEntryType.PASSWORD:
            # decrypted password is only returned if explicitly asked for this key
            decrypted_value = decrypt_string(entry.value)
            if decrypted_value:
                return decrypted_value
        return entry.value

    def __setitem__(self, key, value):
        """Store value and validate."""
        for entry in self.get_config_entries():
            if entry.entry_key != key:
                continue
            # do some simple type checking
            if entry.multi_value:
                # multi value item
                if not isinstance(value, list):
                    raise ValueError
            else:
                # single value item
                if entry.entry_type == ConfigEntryType.STRING and not isinstance(
                    value, str
                ):
                    if not value:
                        value = ""
                    else:
                        raise ValueError
                if entry.entry_type == ConfigEntryType.BOOL and not isinstance(
                    value, bool
                ):
                    raise ValueError
                if entry.entry_type == ConfigEntryType.FLOAT and not isinstance(
                    value, (float, int)
                ):
                    raise ValueError
            if value != self[key]:
                if entry.store_hashed:
                    value = pbkdf2_sha256.hash(value)
                if entry.entry_type == ConfigEntryType.PASSWORD:
                    value = encrypt_string(value)
                self.stored_config[key] = value
                self.mass.signal_event(
                    EVENT_CONFIG_CHANGED, (self._base_type, self._parent_item_key)
                )
                self.mass.add_job(self.mass.config.save)
                # reload provider if value changed
                if self._base_type == ConfigBaseType.PROVIDER:
                    self.mass.add_job(
                        self.mass.get_provider(self._parent_item_key).async_on_reload()
                    )
                if self._base_type == ConfigBaseType.PLAYER:
                    # force update of player if it's config changed
                    player = self.mass.player_manager.get_player(self._parent_item_key)
                    if player:
                        self.mass.add_job(
                            self.mass.player_manager.async_update_player(player)
                        )
            return
        # raise KeyError if we're trying to set a value not defined as ConfigEntry
        raise KeyError

    def get_config_entries(self) -> List[ConfigEntry]:
        """Return config entries for this item."""
        if self._base_type == ConfigBaseType.PLAYER:
            return self.mass.config.get_player_config_entries(self._parent_item_key)
        if self._base_type == ConfigBaseType.PROVIDER:
            return self.mass.config.get_provider_config_entries(self._parent_item_key)
        return self.mass.config.get_base_config_entries(self._parent_item_key)


class ConfigBase(OrderedDict):
    """Configuration class with ConfigItem items."""

    def __init__(self, mass, base_type=ConfigBaseType):
        """Initialize class."""
        self.mass = mass
        self._base_type = base_type
        super().__init__()

    def __getitem__(self, item_key):
        """Return convenience method for get."""
        if item_key not in self:
            # create new ConfigDictItem on the fly
            super().__setitem__(
                item_key, ConfigItem(self.mass, item_key, self._base_type)
            )
        return super().__getitem__(item_key)


class MassConfig:
    """Class which holds our configuration."""

    def __init__(self, mass, data_path: str):
        """Initialize class."""
        self._data_path = data_path
        self.loading = False
        self.mass = mass
        self._conf_base = ConfigBase(mass, ConfigBaseType.BASE)
        self._conf_players = ConfigBase(mass, ConfigBaseType.PLAYER)
        self._conf_providers = ConfigBase(mass, ConfigBaseType.PROVIDER)
        if not os.path.isdir(data_path):
            raise FileNotFoundError(f"data directory {data_path} does not exist!")
        self.__load()

    @property
    def data_path(self):
        """Return the path where all (configuration) data is stored."""
        return self._data_path

    @property
    def base(self):
        """Return base config."""
        return self._conf_base

    @property
    def player_settings(self):
        """Return all player configs."""
        return self._conf_players

    @property
    def providers(self):
        """Return all provider configs."""
        return self._conf_providers

    def get_provider_config(self, provider_id):
        """Return config for given provider."""
        return self._conf_providers[provider_id]

    def get_player_config(self, player_id):
        """Return config for given player."""
        return self._conf_players[player_id]

    def get_provider_config_entries(self, provider_id: str) -> List[ConfigEntry]:
        """Return all config entries for the given provider."""
        provider = self.mass.get_provider(provider_id)
        if provider:
            return DEFAULT_PROVIDER_CONFIG_ENTRIES + provider.config_entries
        return DEFAULT_PROVIDER_CONFIG_ENTRIES

    def get_player_config_entries(self, player_id: str) -> List[ConfigEntry]:
        """Return all config entries for the given player."""
        player_conf = self.mass.player_manager.get_player_config_entries(player_id)
        return DEFAULT_PLAYER_CONFIG_ENTRIES + player_conf

    @staticmethod
    def get_base_config_entries(base_key) -> List[ConfigEntry]:
        """Return all base config entries."""
        return DEFAULT_BASE_CONFIG_ENTRIES[base_key]

    def validate_credentials(self, username, password):
        """Check if credentials matches."""
        if username != self.base["security"]["username"]:
            return False
        if not password and not self.base["security"]["password"]:
            return True
        return pbkdf2_sha256.verify(password, self.base["security"]["password"])

    def __getitem__(self, item_key):
        """Return item value by key."""
        return getattr(self, item_key)

    async def async_close(self):
        """Save config on exit."""
        self.save()

    def save(self):
        """Save config to file."""
        if self.loading:
            LOGGER.warning("save already running")
            return
        self.loading = True
        # backup existing file
        conf_file = os.path.join(self.data_path, "config.json")
        conf_file_backup = os.path.join(self.data_path, "config.json.backup")
        if os.path.isfile(conf_file):
            shutil.move(conf_file, conf_file_backup)
        # create dict for stored config
        stored_conf = {
            CONF_KEY_BASE: {},
            CONF_KEY_PLAYERSETTINGS: {},
            CONF_KEY_PROVIDERS: {},
        }
        for conf_key in stored_conf:
            for key, value in self[conf_key].items():
                stored_conf[conf_key][key] = value.stored_config

        # write current config to file
        with open(conf_file, "w") as _file:
            _file.write(json.dumps(stored_conf, indent=4))
        LOGGER.info("Config saved!")
        self.loading = False

    def __load(self):
        """Load config from file."""
        self.loading = True
        conf_file = os.path.join(self.data_path, "config.json")
        data = try_load_json_file(conf_file)
        if not data:
            # might be a corrupt config file, retry with backup file
            conf_file_backup = os.path.join(self.data_path, "config.json.backup")
            data = try_load_json_file(conf_file_backup)
        if data:

            if data.get(CONF_KEY_BASE):
                for base_key, base_value in data[CONF_KEY_BASE].items():
                    if base_key in ["homeassistant"]:
                        continue  # legacy - to be removed later
                    for key, value in base_value.items():
                        if key == "__desc__":
                            continue
                        self.base[base_key].stored_config[key] = value
            if data.get(CONF_KEY_PLAYERSETTINGS):
                for player_id, player in data[CONF_KEY_PLAYERSETTINGS].items():
                    for key, value in player.items():
                        if key == "__desc__":
                            continue
                        self.player_settings[player_id].stored_config[key] = value
            if data.get(CONF_KEY_PROVIDERS):
                for provider_id, provider in data[CONF_KEY_PROVIDERS].items():
                    for key, value in provider.items():
                        if key == "__desc__":
                            continue
                        self.providers[provider_id].stored_config[key] = value

        self.loading = False
