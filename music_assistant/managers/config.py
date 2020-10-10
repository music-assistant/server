"""All classes and helpers for the Configuration."""

import copy
import logging
import os
import shutil
from typing import List

import orjson
from music_assistant.constants import (
    CONF_CROSSFADE_DURATION,
    CONF_ENABLED,
    CONF_EXTERNAL_URL,
    CONF_FALLBACK_GAIN_CORRECT,
    CONF_GROUP_DELAY,
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
    CONF_POWER_CONTROL,
    CONF_SSL_CERTIFICATE,
    CONF_SSL_KEY,
    CONF_TARGET_VOLUME,
    CONF_USERNAME,
    CONF_VOLUME_CONTROL,
    CONF_VOLUME_NORMALISATION,
    EVENT_CONFIG_CHANGED,
)
from music_assistant.helpers.encryption import decrypt_string, encrypt_string
from music_assistant.helpers.typing import MusicAssistantType
from music_assistant.helpers.util import get_external_ip, merge_dict, try_load_json_file
from music_assistant.models.config_entry import ConfigEntry, ConfigEntryType
from music_assistant.models.player import PlayerControlType
from music_assistant.models.provider import ProviderType
from passlib.hash import pbkdf2_sha256

LOGGER = logging.getLogger("config_manager")

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
            entry_key="__name__",
            entry_type=ConfigEntryType.LABEL,
            label=CONF_KEY_BASE_WEBSERVER,
            hidden=True,
        ),
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
            label=CONF_EXTERNAL_URL,
            description="desc_external_url",
        ),
    ],
    CONF_KEY_BASE_SECURITY: [
        ConfigEntry(
            entry_key="__name__",
            entry_type=ConfigEntryType.LABEL,
            label=CONF_KEY_BASE_SECURITY,
            hidden=True,
        ),
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


PROVIDER_TYPE_MAPPINGS = {
    CONF_KEY_MUSIC_PROVIDERS: ProviderType.MUSIC_PROVIDER,
    CONF_KEY_PLAYER_PROVIDERS: ProviderType.PLAYER_PROVIDER,
    CONF_KEY_METADATA_PROVIDERS: ProviderType.METADATA_PROVIDER,
    CONF_KEY_PLUGINS: ProviderType.PLUGIN,
}


class ConfigManager:
    """Class which holds our configuration."""

    def __init__(self, mass, data_path: str):
        """Initialize class."""
        self._data_path = data_path
        self._stored_config = {}
        self.loading = False
        self.mass = mass
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
        return BaseSettings(self)

    @property
    def player_settings(self):
        """Return all player configs."""
        return PlayerSettings(self)

    @property
    def music_providers(self):
        """Return all music provider configs."""
        return ProviderSettings(self, CONF_KEY_MUSIC_PROVIDERS)

    @property
    def player_providers(self):
        """Return all player provider configs."""
        return ProviderSettings(self, CONF_KEY_PLAYER_PROVIDERS)

    @property
    def metadata_providers(self):
        """Return all metadata provider configs."""
        return ProviderSettings(self, CONF_KEY_METADATA_PROVIDERS)

    @property
    def plugins(self):
        """Return all plugin configs."""
        return ProviderSettings(self, CONF_KEY_PLUGINS)

    @property
    def stored_config(self):
        """Return the config that is actually stored on disk."""
        return self._stored_config

    def get_provider_config(self, provider_id: str, provider_type: ProviderType = None):
        """Return config for given provider."""
        if not provider_type:
            provider = self.mass.get_provider(provider_id)
            if provider:
                provider_type = provider.type
        if provider_type == ProviderType.METADATA_PROVIDER:
            return self.metadata_providers[provider_id]
        if provider_type == ProviderType.MUSIC_PROVIDER:
            return self.music_providers[provider_id]
        if provider_type == ProviderType.PLAYER_PROVIDER:
            return self.player_providers[provider_id]
        if provider_type == ProviderType.PLUGIN:
            return self.plugins[provider_id]
        raise RuntimeError("Invalid provider type")

    def get_player_config(self, player_id: str):
        """Return config for given player."""
        return self.player_settings[player_id]

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

    def get_translation(self, org_string: str, language: str):
        """Get translated value for a string, fallback to english."""
        for lang in [language, "en"]:
            translated_value = self._translations.get(lang, {}).get(org_string)
            if translated_value:
                return translated_value
        return org_string

    @staticmethod
    def __get_all_translations() -> dict:
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
        # write current config to file
        with open(conf_file, "wb") as _file:
            _file.write(orjson.dumps(self._stored_config, option=orjson.OPT_INDENT_2))
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
            self._stored_config = data
        self.loading = False


class ConfigBaseItem:
    """Configuration class that holds the ConfigSubItem items."""

    def __init__(self, conf_mgr: ConfigManager, conf_key: str):
        """Initialize class."""
        self.conf_mgr = conf_mgr
        self.mass = conf_mgr.mass
        self.conf_key = conf_key

    @classmethod
    def all_keys(cls):
        """Return all possible keys of this Config object."""
        return []

    def __getitem__(self, item_key: str):
        """Return ConfigSubItem for given key."""
        return ConfigSubItem(self, item_key)

    def all_items(self, translation="en") -> dict:
        """Return entire config as dict."""
        return {
            key: copy.deepcopy(ConfigSubItem(self, key).all_items(translation))
            for key in self.all_keys()
        }


class BaseSettings(ConfigBaseItem):
    """Configuration class that holds the base settings."""

    def __init__(self, mass: MusicAssistantType):
        """Initialize class."""
        super().__init__(mass, CONF_KEY_BASE)

    def all_keys(self):
        """Return all possible keys of this Config object."""
        return list(DEFAULT_BASE_CONFIG_ENTRIES.keys())

    @staticmethod
    def get_config_entries(child_key) -> List[ConfigEntry]:
        """Return all base config entries."""
        return list(DEFAULT_BASE_CONFIG_ENTRIES[child_key])


class PlayerSettings(ConfigBaseItem):
    """Configuration class that holds the player settings."""

    def __init__(self, mass: MusicAssistantType):
        """Initialize class."""
        super().__init__(mass, CONF_KEY_PLAYER_SETTINGS)

    def all_keys(self):
        """Return all possible keys of this Config object."""
        return (item.player_id for item in self.mass.players.players)

    def get_config_entries(self, child_key: str) -> List[ConfigEntry]:
        """Return all config entries for the given child entry."""
        entries = []
        entries += DEFAULT_PLAYER_CONFIG_ENTRIES
        player_state = self.mass.players.get_player_state(child_key)
        if player_state:
            entries += player_state.player.config_entries
            # append power control config entries
            power_controls = self.mass.players.get_player_controls(
                PlayerControlType.POWER
            )
            if power_controls:
                controls = [
                    {"text": f"{item.provider}: {item.name}", "value": item.control_id}
                    for item in power_controls
                ]
                entries.append(
                    ConfigEntry(
                        entry_key=CONF_POWER_CONTROL,
                        entry_type=ConfigEntryType.STRING,
                        description=CONF_POWER_CONTROL,
                        values=controls,
                    )
                )
            # append volume control config entries
            volume_controls = self.mass.players.get_player_controls(
                PlayerControlType.VOLUME
            )
            if volume_controls:
                controls = [
                    {"text": f"{item.provider}: {item.name}", "value": item.control_id}
                    for item in volume_controls
                ]
                entries.append(
                    ConfigEntry(
                        entry_key=CONF_VOLUME_CONTROL,
                        entry_type=ConfigEntryType.STRING,
                        description=CONF_VOLUME_CONTROL,
                        values=controls,
                    )
                )
            # append special group player entries
            for parent_id in player_state.group_parents:
                parent_player = self.mass.players.get_player_state(parent_id)
                if parent_player and parent_player.provider_id == "group_player":
                    entries.append(
                        ConfigEntry(
                            entry_key=CONF_GROUP_DELAY,
                            entry_type=ConfigEntryType.INT,
                            default_value=0,
                            range=(0, 500),
                            description=CONF_GROUP_DELAY,
                        )
                    )
                    break
        return entries


class ProviderSettings(ConfigBaseItem):
    """Configuration class that holds the music provider settings."""

    def all_keys(self):
        """Return all possible keys of this Config object."""
        prov_type = PROVIDER_TYPE_MAPPINGS[self.conf_key]
        return (
            item.id
            for item in self.mass.get_providers(prov_type, include_unavailable=True)
        )

    def get_config_entries(self, child_key: str) -> List[ConfigEntry]:
        """Return all config entries for the given provider."""
        provider = self.mass.get_provider(child_key)
        if provider:
            # append a hidden label with the provider's name
            specials = [
                ConfigEntry(
                    "__name__", ConfigEntryType.LABEL, label=provider.name, hidden=True
                )
            ]
            return specials + DEFAULT_PROVIDER_CONFIG_ENTRIES + provider.config_entries
        return DEFAULT_PROVIDER_CONFIG_ENTRIES


class ConfigSubItem:
    """
    Configuration Item connected to Config Entries.

    Returns default value from config entry if no value present.
    """

    def __init__(self, conf_parent: ConfigBaseItem, conf_key: str):
        """Initialize class."""
        self.conf_parent = conf_parent
        self.conf_key = conf_key
        self.conf_mgr = conf_parent.conf_mgr
        self.parent_conf_key = conf_parent.conf_key

    def all_items(self, translation="en") -> dict:
        """Return entire config as dict."""
        return {
            item.entry_key: self.get_entry(item.entry_key, translation)
            for item in self.conf_parent.get_config_entries(self.conf_key)
        }

    def get(self, key, default=None):
        """Return value if key exists, default if not."""
        try:
            return self[key]
        except KeyError:
            return default

    def __getitem__(self, key) -> ConfigEntry:
        """Get value for ConfigEntry."""
        # always lookup the config entry because config entries are dynamic
        # and values may be transformed (e.g. encrypted)
        entry = self.get_entry(key)
        if entry.entry_type == ConfigEntryType.PASSWORD:
            # decrypted password is only returned if explicitly asked for this key
            decrypted_value = decrypt_string(entry.value)
            if decrypted_value:
                return decrypted_value
        return entry.value

    def get_entry(self, key, translation=None):
        """Return complete ConfigEntry for specified key."""
        stored_config = self.conf_mgr.stored_config.get(self.conf_parent.conf_key, {})
        stored_config = stored_config.get(self.conf_key, {})
        for conf_entry in self.conf_parent.get_config_entries(self.conf_key):
            if conf_entry.entry_key == key:
                if key in stored_config:
                    # use stored value
                    conf_entry.value = stored_config[key]
                else:
                    # use default value for config entry
                    conf_entry.value = conf_entry.default_value
                # get translated labels
                if translation is not None:
                    for entry_subkey in ["label", "description", "__name__"]:
                        org_value = getattr(conf_entry, entry_subkey, None)
                        if not org_value:
                            org_value = conf_entry.entry_key
                        translated_value = self.conf_parent.conf_mgr.get_translation(
                            org_value, translation
                        )
                        if translated_value and translated_value != org_value:
                            setattr(conf_entry, entry_subkey, translated_value)
                return conf_entry
        raise KeyError(
            "%s\\%s has no key %s!" % (self.conf_parent.conf_key, self.conf_key, key)
        )

    def __setitem__(self, key, value):
        """Store value and validate."""
        assert isinstance(key, str)
        for entry in self.conf_parent.get_config_entries(self.conf_key):
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

                # write value to stored config
                stored_conf = self.conf_mgr.stored_config
                if self.parent_conf_key not in stored_conf:
                    stored_conf[self.parent_conf_key] = {}
                if self.conf_key not in stored_conf[self.parent_conf_key]:
                    stored_conf[self.parent_conf_key][self.conf_key] = {}
                stored_conf[self.parent_conf_key][self.conf_key][key] = value

                self.conf_mgr.mass.add_job(self.conf_mgr.save)
                # reload provider/plugin if value changed
                if self.parent_conf_key in PROVIDER_TYPE_MAPPINGS:
                    self.conf_mgr.mass.add_job(
                        self.conf_mgr.mass.async_reload_provider(self.conf_key)
                    )
                if self.parent_conf_key == CONF_KEY_PLAYER_SETTINGS:
                    # force update of player if it's config changed
                    self.conf_mgr.mass.add_job(
                        self.conf_mgr.mass.players.async_trigger_player_update(
                            self.conf_key
                        )
                    )
                # signal config changed event
                self.conf_mgr.mass.signal_event(
                    EVENT_CONFIG_CHANGED, (self.parent_conf_key, self.conf_key)
                )
            return
        # raise KeyError if we're trying to set a value not defined as ConfigEntry
        raise KeyError
