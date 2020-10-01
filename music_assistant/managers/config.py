"""All classes and helpers for the Configuration."""

import logging
import os
import shutil
from collections import OrderedDict
from enum import Enum
from typing import List

import orjson
from music_assistant.constants import (
    CONF_CROSSFADE_DURATION,
    CONF_ENABLED,
    CONF_EXTERNAL_URL,
    CONF_FALLBACK_GAIN_CORRECT,
    CONF_HTTP_PORT,
    CONF_HTTPS_PORT,
    CONF_KEY_BASE,
    CONF_KEY_BASE_SECURITY,
    CONF_KEY_BASE_WEBSERVER,
    CONF_KEY_METADATA_PROVIDERS,
    CONF_KEY_MUSIC_PROVIDERS,
    CONF_KEY_PLAYER_PROVIDERS,
    CONF_KEY_PLAYER_SETTINGS,
    CONF_KEY_PLUGINS,
    CONF_MAX_SAMPLE_RATE,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_SSL_CERTIFICATE,
    CONF_SSL_KEY,
    CONF_TARGET_VOLUME,
    CONF_USERNAME,
    CONF_VOLUME_NORMALISATION,
    EVENT_CONFIG_CHANGED,
)
from music_assistant.helpers.util import (
    decrypt_string,
    encrypt_string,
    get_external_ip,
    merge_dict,
    try_load_json_file,
)
from music_assistant.models.config_entry import ConfigEntry, ConfigEntryType
from music_assistant.models.provider import ProviderType
from passlib.hash import pbkdf2_sha256

LOGGER = logging.getLogger("mass")

DEFAULT_PLAYER_CONFIG_ENTRIES = [
    ConfigEntry(
        entry_key=CONF_ENABLED,
        entry_type=ConfigEntryType.BOOL,
        default_value=True,
        label="enable_player",
    ),
    ConfigEntry(
        entry_key=CONF_NAME,
        entry_type=ConfigEntryType.STRING,
        default_value=None,
        label=CONF_NAME,
        description="desc_player_name",
    ),
    ConfigEntry(
        entry_key=CONF_MAX_SAMPLE_RATE,
        entry_type=ConfigEntryType.INT,
        values=[41000, 48000, 96000, 176000, 192000, 384000],
        default_value=96000,
        label=CONF_MAX_SAMPLE_RATE,
        description="desc_sample_rate",
    ),
    ConfigEntry(
        entry_key=CONF_VOLUME_NORMALISATION,
        entry_type=ConfigEntryType.BOOL,
        default_value=True,
        label=CONF_VOLUME_NORMALISATION,
        description="desc_volume_normalisation",
    ),
    ConfigEntry(
        entry_key=CONF_TARGET_VOLUME,
        entry_type=ConfigEntryType.INT,
        range=(-30, 0),
        default_value=-23,
        label=CONF_TARGET_VOLUME,
        description="desc_target_volume",
        depends_on=CONF_VOLUME_NORMALISATION,
    ),
    ConfigEntry(
        entry_key=CONF_FALLBACK_GAIN_CORRECT,
        entry_type=ConfigEntryType.INT,
        range=(-20, 0),
        default_value=-12,
        label=CONF_FALLBACK_GAIN_CORRECT,
        description="desc_gain_correct",
        depends_on=CONF_VOLUME_NORMALISATION,
    ),
    ConfigEntry(
        entry_key=CONF_CROSSFADE_DURATION,
        entry_type=ConfigEntryType.INT,
        range=(0, 10),
        default_value=0,
        label=CONF_CROSSFADE_DURATION,
        description="desc_crossfade",
    ),
]

DEFAULT_PROVIDER_CONFIG_ENTRIES = [
    ConfigEntry(
        entry_key=CONF_ENABLED,
        entry_type=ConfigEntryType.BOOL,
        default_value=True,
        label=CONF_ENABLED,
        description="desc_enable_provider",
    )
]

DEFAULT_BASE_CONFIG_ENTRIES = {
    CONF_KEY_BASE_WEBSERVER: [
        ConfigEntry(
            entry_key=CONF_HTTP_PORT,
            entry_type=ConfigEntryType.INT,
            default_value=8095,
            label=CONF_HTTP_PORT,
            description="desc_http_port",
        ),
        ConfigEntry(
            entry_key=CONF_HTTPS_PORT,
            entry_type=ConfigEntryType.INT,
            default_value=8096,
            label=CONF_HTTPS_PORT,
            description="desc_https_port",
        ),
        ConfigEntry(
            entry_key=CONF_SSL_CERTIFICATE,
            entry_type=ConfigEntryType.STRING,
            default_value="",
            label=CONF_SSL_CERTIFICATE,
            description="desc_ssl_certificate",
        ),
        ConfigEntry(
            entry_key=CONF_SSL_KEY,
            entry_type=ConfigEntryType.STRING,
            default_value="",
            label=CONF_SSL_KEY,
            description="desc_ssl_key",
        ),
        ConfigEntry(
            entry_key=CONF_EXTERNAL_URL,
            entry_type=ConfigEntryType.STRING,
            default_value=f"http://{get_external_ip()}:8095",
            label="External url (fqdn)",
            description="desc_external_url",
        ),
    ],
    CONF_KEY_BASE_SECURITY: [
        ConfigEntry(
            entry_key=CONF_USERNAME,
            entry_type=ConfigEntryType.STRING,
            default_value="admin",
            label=CONF_USERNAME,
            description="desc_base_username",
        ),
        ConfigEntry(
            entry_key=CONF_PASSWORD,
            entry_type=ConfigEntryType.PASSWORD,
            default_value="",
            label=CONF_PASSWORD,
            description="desc_base_password",
            store_hashed=True,
        ),
    ],
}


class ConfigBaseType(Enum):
    """Enum with config base types."""

    BASE = CONF_KEY_BASE
    PLAYER_SETTINGS = CONF_KEY_PLAYER_SETTINGS
    MUSIC_PROVIDERS = CONF_KEY_MUSIC_PROVIDERS
    PLAYER_PROVIDERS = CONF_KEY_PLAYER_PROVIDERS
    METADATA_PROVIDERS = CONF_KEY_METADATA_PROVIDERS
    PLUGINS = CONF_KEY_PLUGINS


PROVIDER_TYPES = [
    ConfigBaseType.MUSIC_PROVIDERS,
    ConfigBaseType.PLAYER_PROVIDERS,
    ConfigBaseType.METADATA_PROVIDERS,
    ConfigBaseType.PLUGINS,
]


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

    def to_dict(self, lang="en") -> dict:
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
            # get translated values
            for entry_key in ["label", "description"]:
                org_value = getattr(result[entry.entry_key], entry_key, None)
                if not org_value:
                    org_value = entry.entry_key
                translated_value = self.mass.config.get_translation(org_value, lang)
                if translated_value != org_value:
                    setattr(result[entry.entry_key], entry_key, translated_value)
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
        raise KeyError(
            "%s\\%s has no key %s!" % (self._base_type, self._parent_item_key, key)
        )

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

                self.mass.add_job(self.mass.config.save)
                # reload provider/plugin if value changed
                if self._base_type in PROVIDER_TYPES:
                    self.mass.add_job(
                        self.mass.async_reload_provider(self._parent_item_key)
                    )
                if self._base_type == ConfigBaseType.PLAYER_SETTINGS:
                    # force update of player if it's config changed
                    self.mass.add_job(
                        self.mass.players.async_trigger_player_update(
                            self._parent_item_key
                        )
                    )
                # signal config changed event
                self.mass.signal_event(
                    EVENT_CONFIG_CHANGED, (self._base_type, self._parent_item_key)
                )
            return
        # raise KeyError if we're trying to set a value not defined as ConfigEntry
        raise KeyError

    def get_config_entries(self) -> List[ConfigEntry]:
        """Return config entries for this item."""
        if self._base_type == ConfigBaseType.PLAYER_SETTINGS:
            return self.mass.config.get_player_config_entries(self._parent_item_key)
        if self._base_type in PROVIDER_TYPES:
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

    def to_dict(self, lang="en") -> dict:
        """Return entire config as dict."""
        return {key: value.to_dict(lang) for key, value in self.items()}


class ConfigManager:
    """Class which holds our configuration."""

    def __init__(self, mass, data_path: str):
        """Initialize class."""
        self._data_path = data_path
        self.loading = False
        self.mass = mass
        self._conf_base = ConfigBase(mass, ConfigBaseType.BASE)
        self._conf_player_settings = ConfigBase(mass, ConfigBaseType.PLAYER_SETTINGS)
        self._conf_player_providers = ConfigBase(mass, ConfigBaseType.PLAYER_PROVIDERS)
        self._conf_music_providers = ConfigBase(mass, ConfigBaseType.MUSIC_PROVIDERS)
        self._conf_metadata_providers = ConfigBase(
            mass, ConfigBaseType.METADATA_PROVIDERS
        )
        self._conf_plugins = ConfigBase(mass, ConfigBaseType.PLUGINS)
        if not os.path.isdir(data_path):
            raise FileNotFoundError(f"data directory {data_path} does not exist!")
        self._translations = self.__get_all_translations()
        self.__load()

    @property
    def data_path(self):
        """Return the path where all (configuration) data is stored."""
        return self._data_path

    @property
    def translations(self):
        """Return all translations."""
        return self._translations

    @property
    def base(self):
        """Return base config."""
        return self._conf_base

    @property
    def player_settings(self):
        """Return all player configs."""
        return self._conf_player_settings

    @property
    def music_providers(self):
        """Return all music provider configs."""
        return self._conf_music_providers

    @property
    def player_providers(self):
        """Return all player provider configs."""
        return self._conf_player_providers

    @property
    def metadata_providers(self):
        """Return all metadata provider configs."""
        return self._conf_metadata_providers

    @property
    def plugins(self):
        """Return all plugin configs."""
        return self._conf_plugins

    def get_provider_config(self, provider_id: str, provider_type: ProviderType = None):
        """Return config for given provider."""
        if not provider_type:
            provider = self.mass.get_provider(provider_id)
            if provider:
                provider_type = provider.type
        if provider_type == ProviderType.METADATA_PROVIDER:
            return self._conf_metadata_providers[provider_id]
        if provider_type == ProviderType.MUSIC_PROVIDER:
            return self._conf_music_providers[provider_id]
        if provider_type == ProviderType.PLAYER_PROVIDER:
            return self._conf_player_providers[provider_id]
        if provider_type == ProviderType.PLUGIN:
            return self._conf_plugins[provider_id]
        raise RuntimeError("Invalid provider type")

    def get_player_config(self, player_id):
        """Return config for given player."""
        return self._conf_player_settings[player_id]

    def get_provider_config_entries(self, provider_id: str) -> List[ConfigEntry]:
        """Return all config entries for the given provider."""
        provider = self.mass.get_provider(provider_id)
        if provider:
            specials = [
                ConfigEntry(
                    "__name__", ConfigEntryType.LABEL, label=provider.name, hidden=True
                )
            ]
            return specials + DEFAULT_PROVIDER_CONFIG_ENTRIES + provider.config_entries
        return DEFAULT_PROVIDER_CONFIG_ENTRIES

    def get_player_config_entries(self, player_id: str) -> List[ConfigEntry]:
        """Return all config entries for the given player."""
        player_state = self.mass.players.get_player_state(player_id)
        if player_state:
            return DEFAULT_PLAYER_CONFIG_ENTRIES + player_state.config_entries
        return DEFAULT_PLAYER_CONFIG_ENTRIES

    @staticmethod
    def get_base_config_entries(base_key) -> List[ConfigEntry]:
        """Return all base config entries."""
        return DEFAULT_BASE_CONFIG_ENTRIES[base_key]

    def validate_credentials(self, username: str, password: str) -> bool:
        """Check if credentials matches."""
        if username != self.base["security"]["username"]:
            return False
        if not password and not self.base["security"]["password"]:
            return True
        try:
            return pbkdf2_sha256.verify(password, self.base["security"]["password"])
        except ValueError:
            return False

    def __getitem__(self, item_key):
        """Return item value by key."""
        return getattr(self, item_key)

    async def async_close(self):
        """Save config on exit."""
        self.save()

    def get_translation(self, org_string: str, lang: str):
        """Get translated value for a string, fallback to english."""
        for lang in [lang, "en"]:
            translated_value = self.mass.config.translations.get(lang, {}).get(
                org_string
            )
            if translated_value:
                return translated_value
        return org_string

    def __get_all_translations(self) -> dict:
        """Build a list of all translations."""
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # get base translations
        translations_file = os.path.join(base_dir, "translations.json")
        res = try_load_json_file(translations_file)
        if res is not None:
            translations = res
        else:
            translations = {}
        # append provider translations but do not overwrite keys
        modules_path = os.path.join(base_dir, "providers")
        # load modules
        for dir_str in os.listdir(modules_path):
            dir_path = os.path.join(modules_path, dir_str)
            translations_file = os.path.join(dir_path, "translations.json")
            if not os.path.isfile(translations_file):
                continue
            res = try_load_json_file(translations_file)
            if res is not None:
                translations = merge_dict(translations, res)
        return translations

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
            CONF_KEY_PLAYER_SETTINGS: {},
            CONF_KEY_MUSIC_PROVIDERS: {},
            CONF_KEY_METADATA_PROVIDERS: {},
            CONF_KEY_PLAYER_PROVIDERS: {},
            CONF_KEY_PLUGINS: {},
        }
        for conf_key in stored_conf:
            for key, value in self[conf_key].items():
                stored_conf[conf_key][key] = value.stored_config

        # write current config to file
        with open(conf_file, "wb") as _file:
            _file.write(orjson.dumps(stored_conf, option=orjson.OPT_INDENT_2))
        LOGGER.info("Config saved!")
        self.loading = False

    def __load(self):
        """Load stored config from file."""
        self.loading = True
        conf_file = os.path.join(self.data_path, "config.json")
        data = try_load_json_file(conf_file)
        if not data:
            # might be a corrupt config file, retry with backup file
            conf_file_backup = os.path.join(self.data_path, "config.json.backup")
            data = try_load_json_file(conf_file_backup)
        if data:

            for conf_key in [
                CONF_KEY_BASE,
                CONF_KEY_PLAYER_SETTINGS,
                CONF_KEY_MUSIC_PROVIDERS,
                CONF_KEY_METADATA_PROVIDERS,
                CONF_KEY_PLAYER_PROVIDERS,
                CONF_KEY_PLUGINS,
            ]:
                if not data.get(conf_key):
                    continue
                for key, value in data[conf_key].items():
                    for subkey, subvalue in value.items():
                        self[conf_key][key].stored_config[subkey] = subvalue

        self.loading = False
