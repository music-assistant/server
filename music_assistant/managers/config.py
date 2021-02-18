"""All classes and helpers for the Configuration."""

import copy
import datetime
import json
import logging
import os
import shutil
from typing import Any, List

from music_assistant.constants import (
    CONF_CROSSFADE_DURATION,
    CONF_ENABLED,
    CONF_GROUP_DELAY,
    CONF_KEY_BASE,
    CONF_KEY_METADATA_PROVIDERS,
    CONF_KEY_MUSIC_PROVIDERS,
    CONF_KEY_PLAYER_PROVIDERS,
    CONF_KEY_PLAYER_SETTINGS,
    CONF_KEY_PLUGINS,
    CONF_KEY_SECURITY,
    CONF_KEY_SECURITY_APP_TOKENS,
    CONF_KEY_SECURITY_LOGIN,
    CONF_MAX_SAMPLE_RATE,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_POWER_CONTROL,
    CONF_TARGET_VOLUME,
    CONF_USERNAME,
    CONF_VOLUME_CONTROL,
    CONF_VOLUME_NORMALISATION,
    EVENT_CONFIG_CHANGED,
)
from music_assistant.helpers.encryption import _decrypt_string, _encrypt_string
from music_assistant.helpers.util import merge_dict, try_load_json_file
from music_assistant.helpers.web import api_route
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

DEFAULT_BASE_CONFIG_ENTRIES = {}

DEFAULT_SECURITY_CONFIG_ENTRIES = {
    CONF_KEY_SECURITY_LOGIN: [
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
    CONF_KEY_SECURITY_APP_TOKENS: [],
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
        self._translations = {}
        self.loading = False
        self.mass = mass
        if not os.path.isdir(data_path):
            raise FileNotFoundError(f"data directory {data_path} does not exist!")
        self.__load()

    async def setup(self):
        """Async initialize of module."""
        self._translations = await self._fetch_translations()

    @api_route("config/:conf_base?/:conf_key?")
    def all_items(self, conf_base: str = "", conf_key: str = "") -> dict:
        """Return entire config as dict."""
        if conf_base and conf_key:
            obj = getattr(self, conf_base)[conf_key]
            if isinstance(obj, dict):
                return obj
            return obj.all_items()
        if conf_base:
            obj = getattr(self, conf_base)
            if isinstance(obj, dict):
                return obj
            return obj.all_items()
        return {
            key: getattr(self, key).all_items()
            for key in [
                CONF_KEY_BASE,
                CONF_KEY_SECURITY,
                CONF_KEY_MUSIC_PROVIDERS,
                CONF_KEY_PLAYER_PROVIDERS,
                CONF_KEY_METADATA_PROVIDERS,
                CONF_KEY_PLUGINS,
                CONF_KEY_PLAYER_SETTINGS,
            ]
        }

    @api_route("config/:conf_base/:conf_key/:conf_val")
    def set_config(
        self, conf_base: str, conf_key: str, conf_val: str, new_value: Any
    ) -> dict:
        """Set value of the given config item."""
        if new_value is None:
            return self[conf_base][conf_key].pop(conf_val)
        self[conf_base][conf_key][conf_val] = new_value
        return self[conf_base][conf_key].all_items()

    @api_route("config/delete/:conf_base/:conf_key")
    def delete_config(self, conf_base: str, conf_key: str) -> dict:
        """Delete value from stored configuration."""
        return self[conf_base].pop(conf_key)

    @property
    def data_path(self):
        """Return the path where all (configuration) data is stored."""
        return self._data_path

    @property
    def server_id(self):
        """Return the unique identifier for this server."""
        return self.stored_config["server_id"]

    @property
    def base(self):
        """Return base config."""
        return BaseSettings(self)

    @property
    def security(self):
        """Return security config."""
        return SecuritySettings(self)

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

    @property
    def translations(self):
        """Return all translations."""
        return self._translations

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

    def __getitem__(self, item_key):
        """Return item value by key."""
        return getattr(self, item_key)

    async def close(self):
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
        # write current config to file
        with open(conf_file, "w") as _file:
            _file.write(json.dumps(self._stored_config, indent=4))
        LOGGER.info("Config saved!")
        self.loading = False

    @staticmethod
    async def _fetch_translations() -> dict:
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

    def all_keys(self):
        """Return all possible keys of this Config object."""
        return self.conf_mgr.stored_config.get(self.conf_key, {}).keys()

    def __getitem__(self, item_key: str):
        """Return ConfigSubItem for given key."""
        return ConfigSubItem(self, item_key)

    def all_items(self) -> dict:
        """Return entire config as dict."""
        return {
            key: copy.deepcopy(ConfigSubItem(self, key).all_items())
            for key in self.all_keys()
        }


class BaseSettings(ConfigBaseItem):
    """Configuration class that holds the base settings."""

    def __init__(self, conf_mgr: ConfigManager):
        """Initialize class."""
        super().__init__(conf_mgr, CONF_KEY_BASE)

    def all_keys(self):
        """Return all possible keys of this Config object."""
        return list(DEFAULT_BASE_CONFIG_ENTRIES.keys())

    @staticmethod
    def get_config_entries(child_key) -> List[ConfigEntry]:
        """Return all base config entries."""
        return list(DEFAULT_BASE_CONFIG_ENTRIES[child_key])


class SecuritySettings(ConfigBaseItem):
    """Configuration class that holds the security settings."""

    def __init__(self, conf_mgr: ConfigManager):
        """Initialize class."""
        super().__init__(conf_mgr, CONF_KEY_SECURITY)
        # make sure the keys exist in config dict
        if CONF_KEY_SECURITY not in conf_mgr.stored_config:
            conf_mgr.stored_config[CONF_KEY_SECURITY] = {}
        if (
            CONF_KEY_SECURITY_APP_TOKENS
            not in conf_mgr.stored_config[CONF_KEY_SECURITY]
        ):
            conf_mgr.stored_config[CONF_KEY_SECURITY][CONF_KEY_SECURITY_APP_TOKENS] = {}

    def all_keys(self):
        """Return all possible keys of this Config object."""
        return DEFAULT_SECURITY_CONFIG_ENTRIES.keys()

    def add_app_token(self, token_info: dict):
        """Add token to config."""
        client_id = token_info["client_id"]
        self.conf_mgr.stored_config[CONF_KEY_SECURITY][CONF_KEY_SECURITY_APP_TOKENS][
            client_id
        ] = token_info
        self.conf_mgr.save()

    def set_last_login(self, client_id: str):
        """Set last login to client."""
        if (
            client_id
            not in self.conf_mgr.stored_config[CONF_KEY_SECURITY][
                CONF_KEY_SECURITY_APP_TOKENS
            ]
        ):
            return
        self.conf_mgr.stored_config[CONF_KEY_SECURITY][CONF_KEY_SECURITY_APP_TOKENS][
            client_id
        ]["last_login"] = datetime.datetime.utcnow().timestamp()
        self.conf_mgr.save()

    def revoke_app_token(self, client_id):
        """Revoke a token registered for an app."""
        return_info = self.conf_mgr.stored_config[CONF_KEY_SECURITY][
            CONF_KEY_SECURITY_APP_TOKENS
        ].pop(client_id)
        self.conf_mgr.save()
        self.conf_mgr.mass.signal_event(
            EVENT_CONFIG_CHANGED, (CONF_KEY_SECURITY, CONF_KEY_SECURITY_APP_TOKENS)
        )
        return return_info

    def is_token_revoked(self, token_info: dict):
        """Return bool if token is revoked."""
        if not token_info.get("app_id"):
            # short lived token does not have app_id and is not stored so can't be revoked
            return False
        return self[CONF_KEY_SECURITY_APP_TOKENS].get(token_info["client_id"]) is None

    def validate_credentials(self, username: str, password: str) -> bool:
        """Check if credentials matches."""
        if username != self[CONF_KEY_SECURITY_LOGIN][CONF_USERNAME]:
            return False
        try:
            return pbkdf2_sha256.verify(
                password, self[CONF_KEY_SECURITY_LOGIN][CONF_PASSWORD]
            )
        except ValueError:
            return False

    def get_config_entries(self, child_key) -> List[ConfigEntry]:
        """Return all base config entries."""
        if child_key == CONF_KEY_SECURITY_LOGIN:
            return list(DEFAULT_SECURITY_CONFIG_ENTRIES[CONF_KEY_SECURITY_LOGIN])
        if child_key == CONF_KEY_SECURITY_APP_TOKENS:
            return [
                ConfigEntry(
                    entry_key=client_id,
                    entry_type=ConfigEntryType.DICT,
                    default_value={},
                    label=token_info["app_id"],
                    description="App connected to MusicAssistant API",
                    store_hashed=False,
                    value={
                        "expires": token_info.get("exp"),
                        "last_login": token_info.get("last_login"),
                    },
                )
                for client_id, token_info in self.conf_mgr.stored_config[
                    CONF_KEY_SECURITY
                ][CONF_KEY_SECURITY_APP_TOKENS].items()
            ]
        return []


class PlayerSettings(ConfigBaseItem):
    """Configuration class that holds the player settings."""

    def __init__(self, conf_mgr: ConfigManager):
        """Initialize class."""
        super().__init__(conf_mgr, CONF_KEY_PLAYER_SETTINGS)

    def all_keys(self):
        """Return all possible keys of this Config object."""
        return {player.player_id for player in self.mass.players}

    def get_config_entries(self, child_key: str) -> List[ConfigEntry]:
        """Return all config entries for the given child entry."""
        entries = []
        entries += DEFAULT_PLAYER_CONFIG_ENTRIES
        player = self.mass.players.get_player(child_key)
        if player:
            entries += player.config_entries
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
                        label=CONF_POWER_CONTROL,
                        description="desc_power_control",
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
                        label=CONF_VOLUME_CONTROL,
                        description="desc_volume_control",
                        values=controls,
                    )
                )
            # append special group player entries
            for parent_id in player.group_parents:
                parent_player = self.mass.players.get_player(parent_id)
                if parent_player and parent_player.provider_id == "group_player":
                    entries.append(
                        ConfigEntry(
                            entry_key=CONF_GROUP_DELAY,
                            entry_type=ConfigEntryType.INT,
                            default_value=0,
                            range=(0, 500),
                            label=CONF_GROUP_DELAY,
                            description="desc_group_delay",
                        )
                    )
                    break
        return entries


class ProviderSettings(ConfigBaseItem):
    """Configuration class that holds the provider settings."""

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
            return DEFAULT_PROVIDER_CONFIG_ENTRIES + provider.config_entries
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

    def all_items(self) -> dict:
        """Return entire config as dict."""
        return {
            item.entry_key: self.get_entry(item.entry_key)
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
            decrypted_value = _decrypt_string(entry.value)
            if decrypted_value:
                return decrypted_value
        return entry.value

    def get_entry(self, key):
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
                if value is None:
                    value = []
                if not isinstance(value, list):
                    raise ValueError
            else:
                # single value item
                if entry.entry_type == ConfigEntryType.STRING and not isinstance(
                    value, str
                ):
                    if value is None:
                        value = ""
                    else:
                        raise ValueError
                if entry.entry_type == ConfigEntryType.BOOL and not isinstance(
                    value, bool
                ):
                    if value is None:
                        value = False
                    else:
                        raise ValueError
                if entry.entry_type == ConfigEntryType.FLOAT and not isinstance(
                    value, (float, int)
                ):
                    if value is None:
                        value = 0
                    else:
                        raise ValueError
            if value != self[key]:
                if entry.store_hashed:
                    value = pbkdf2_sha256.hash(value)
                if entry.entry_type == ConfigEntryType.PASSWORD:
                    value = _encrypt_string(value)

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
                        self.conf_mgr.mass.reload_provider(self.conf_key)
                    )
                if self.parent_conf_key == CONF_KEY_PLAYER_SETTINGS:
                    # force update of player if it's config changed
                    self.conf_mgr.mass.add_job(
                        self.conf_mgr.mass.players.trigger_player_update(self.conf_key)
                    )
                # signal config changed event
                self.conf_mgr.mass.signal_event(
                    EVENT_CONFIG_CHANGED, (self.parent_conf_key, self.conf_key)
                )
            return
        # raise KeyError if we're trying to set a value not defined as ConfigEntry
        raise KeyError

    def pop(self, key):
        """Delete ConfigEntry for specified key if exists."""
        stored_config = self.conf_mgr.stored_config.get(self.conf_parent.conf_key, {})
        stored_config = stored_config.get(self.conf_key, {})
        cur_val = stored_config.get(key, None)
        if cur_val:
            del stored_config[key]
            self.conf_mgr.save()
        return cur_val
