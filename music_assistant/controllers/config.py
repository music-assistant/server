"""Logic to handle storage of persistent (configuration) settings."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
from copy import deepcopy
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast, overload
from uuid import uuid4

import aiofiles
import shortuuid
from aiofiles.os import wrap
from cryptography.fernet import Fernet, InvalidToken
from music_assistant_models import config_entries
from music_assistant_models.config_entries import (
    MULTI_VALUE_SPLITTER,
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
    CoreConfig,
    PlayerConfig,
    ProviderConfig,
)
from music_assistant_models.constants import (
    PLAYER_CONTROL_FAKE,
    PLAYER_CONTROL_NATIVE,
    PLAYER_CONTROL_NONE,
)
from music_assistant_models.dsp import DSPConfig, DSPConfigPreset
from music_assistant_models.enums import (
    ConfigEntryType,
    EventType,
    PlayerFeature,
    PlayerType,
    ProviderFeature,
    ProviderType,
)
from music_assistant_models.errors import (
    ActionUnavailable,
    InvalidDataError,
    UnsupportedFeaturedException,
)

from music_assistant.constants import (
    CONF_CORE,
    CONF_ENTRY_ANNOUNCE_VOLUME,
    CONF_ENTRY_ANNOUNCE_VOLUME_MAX,
    CONF_ENTRY_ANNOUNCE_VOLUME_MIN,
    CONF_ENTRY_ANNOUNCE_VOLUME_STRATEGY,
    CONF_ENTRY_AUTO_PLAY,
    CONF_ENTRY_CROSSFADE_DURATION,
    CONF_ENTRY_ENABLE_ICY_METADATA,
    CONF_ENTRY_FLOW_MODE,
    CONF_ENTRY_HTTP_PROFILE,
    CONF_ENTRY_LIBRARY_SYNC_ALBUM_TRACKS,
    CONF_ENTRY_LIBRARY_SYNC_ALBUMS,
    CONF_ENTRY_LIBRARY_SYNC_ARTISTS,
    CONF_ENTRY_LIBRARY_SYNC_AUDIOBOOKS,
    CONF_ENTRY_LIBRARY_SYNC_BACK,
    CONF_ENTRY_LIBRARY_SYNC_PLAYLIST_TRACKS,
    CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS,
    CONF_ENTRY_LIBRARY_SYNC_PODCASTS,
    CONF_ENTRY_LIBRARY_SYNC_RADIOS,
    CONF_ENTRY_LIBRARY_SYNC_TRACKS,
    CONF_ENTRY_OUTPUT_CHANNELS,
    CONF_ENTRY_OUTPUT_CODEC,
    CONF_ENTRY_OUTPUT_LIMITER,
    CONF_ENTRY_PLAYER_ICON,
    CONF_ENTRY_PLAYER_ICON_GROUP,
    CONF_ENTRY_PROVIDER_SYNC_INTERVAL_ALBUMS,
    CONF_ENTRY_PROVIDER_SYNC_INTERVAL_ARTISTS,
    CONF_ENTRY_PROVIDER_SYNC_INTERVAL_AUDIOBOOKS,
    CONF_ENTRY_PROVIDER_SYNC_INTERVAL_PLAYLISTS,
    CONF_ENTRY_PROVIDER_SYNC_INTERVAL_PODCASTS,
    CONF_ENTRY_PROVIDER_SYNC_INTERVAL_RADIOS,
    CONF_ENTRY_PROVIDER_SYNC_INTERVAL_TRACKS,
    CONF_ENTRY_SAMPLE_RATES,
    CONF_ENTRY_SMART_FADES_MODE,
    CONF_ENTRY_TTS_PRE_ANNOUNCE,
    CONF_ENTRY_VOLUME_NORMALIZATION,
    CONF_ENTRY_VOLUME_NORMALIZATION_TARGET,
    CONF_EXPOSE_PLAYER_TO_HA,
    CONF_HIDE_IN_UI,
    CONF_MUTE_CONTROL,
    CONF_ONBOARD_DONE,
    CONF_PLAYER_DSP,
    CONF_PLAYER_DSP_PRESETS,
    CONF_PLAYERS,
    CONF_POWER_CONTROL,
    CONF_PRE_ANNOUNCE_CHIME_URL,
    CONF_PROVIDERS,
    CONF_SERVER_ID,
    CONF_SMART_FADES_MODE,
    CONF_VOLUME_CONTROL,
    CONFIGURABLE_CORE_CONTROLLERS,
    DEFAULT_CORE_CONFIG_ENTRIES,
    DEFAULT_PROVIDER_CONFIG_ENTRIES,
    ENCRYPT_SUFFIX,
    NON_HTTP_PROVIDERS,
    SYNCGROUP_PREFIX,
)
from music_assistant.helpers.api import api_command
from music_assistant.helpers.json import JSON_DECODE_EXCEPTIONS, async_json_dumps, async_json_loads
from music_assistant.helpers.util import load_provider_module, validate_announcement_chime_url
from music_assistant.models import ProviderModuleType
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant import MusicAssistant
    from music_assistant.models.core_controller import CoreController
    from music_assistant.models.player import Player

LOGGER = logging.getLogger(__name__)
DEFAULT_SAVE_DELAY = 5

BASE_KEYS = ("enabled", "name", "available", "default_name", "provider", "type")

# TypeVar for config value type inference
_ConfigValueT = TypeVar("_ConfigValueT", bound=ConfigValueType)

isfile = wrap(os.path.isfile)
remove = wrap(os.remove)
rename = wrap(os.rename)


class ConfigController:
    """Controller that handles storage of persistent configuration settings."""

    _fernet: Fernet | None = None

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize storage controller."""
        self.mass = mass
        self.initialized = False
        self._data: dict[str, Any] = {}
        self.filename = os.path.join(self.mass.storage_path, "settings.json")
        self._timer_handle: asyncio.TimerHandle | None = None

    async def setup(self) -> None:
        """Async initialize of controller."""
        await self._load()
        self.initialized = True
        # create default server ID if needed (also used for encrypting passwords)
        self.set_default(CONF_SERVER_ID, uuid4().hex)
        server_id: str = self.get(CONF_SERVER_ID)
        assert server_id
        fernet_key = base64.urlsafe_b64encode(server_id.encode()[:32])
        self._fernet = Fernet(fernet_key)
        config_entries.ENCRYPT_CALLBACK = self.encrypt_string
        config_entries.DECRYPT_CALLBACK = self.decrypt_string
        if not self.onboard_done:
            self.mass.register_api_command(
                "config/onboard_complete",
                self.set_onboard_complete,
                authenticated=True,
                alias=True,  # hide from public API docs
            )
        LOGGER.debug("Started.")

    @property
    def onboard_done(self) -> bool:
        """Return True if onboarding is done."""
        return bool(self.get(CONF_ONBOARD_DONE, False))

    async def set_onboard_complete(self) -> None:
        """
        Mark onboarding as complete.

        This is called by the frontend after the user has completed the onboarding wizard.
        Only available when onboarding is not yet complete.
        """
        if self.onboard_done:
            msg = "Onboarding already completed"
            raise InvalidDataError(msg)

        self.set(CONF_ONBOARD_DONE, True)
        self.save(immediate=True)
        LOGGER.info("Onboarding completed")

    async def close(self) -> None:
        """Handle logic on server stop."""
        if not self._timer_handle:
            # no point in forcing a save when there are no changes pending
            return
        await self._async_save()
        LOGGER.debug("Stopped.")

    def get(self, key: str, default: Any = None) -> Any:
        """Get value(s) for a specific key/path in persistent storage."""
        assert self.initialized, "Not yet (async) initialized"
        # we support a multi level hierarchy by providing the key as path,
        # with a slash (/) as splitter. Sort that out here.
        parent = self._data
        subkeys = key.split("/")
        for index, subkey in enumerate(subkeys):
            if index == (len(subkeys) - 1):
                value = parent.get(subkey, default)
                if value is None:
                    # replace None with default
                    return default
                return value
            if subkey not in parent:
                # requesting subkey from a non existing parent
                return default
            parent = parent[subkey]
        return default

    def set(self, key: str, value: Any) -> None:
        """Set value(s) for a specific key/path in persistent storage."""
        assert self.initialized, "Not yet (async) initialized"
        # we support a multi level hierarchy by providing the key as path,
        # with a slash (/) as splitter.
        parent = self._data
        subkeys = key.split("/")
        for index, subkey in enumerate(subkeys):
            if index == (len(subkeys) - 1):
                parent[subkey] = value
            else:
                parent.setdefault(subkey, {})
                parent = parent[subkey]
        self.save()

    def set_default(self, key: str, default_value: Any) -> None:
        """Set default value(s) for a specific key/path in persistent storage."""
        assert self.initialized, "Not yet (async) initialized"
        cur_value = self.get(key, "__MISSING__")
        if cur_value == "__MISSING__":
            self.set(key, default_value)

    def remove(
        self,
        key: str,
    ) -> None:
        """Remove value(s) for a specific key/path in persistent storage."""
        assert self.initialized, "Not yet (async) initialized"
        parent = self._data
        subkeys = key.split("/")
        for index, subkey in enumerate(subkeys):
            if subkey not in parent:
                return
            if index == (len(subkeys) - 1):
                parent.pop(subkey)
            else:
                parent.setdefault(subkey, {})
                parent = parent[subkey]

        self.save()

    @api_command("config/providers")
    async def get_provider_configs(
        self,
        provider_type: ProviderType | None = None,
        provider_domain: str | None = None,
        include_values: bool = False,
    ) -> list[ProviderConfig]:
        """Return all known provider configurations, optionally filtered by ProviderType."""
        raw_values = self.get(CONF_PROVIDERS, {})
        prov_entries = {x.domain for x in self.mass.get_provider_manifests()}
        return [
            await self.get_provider_config(prov_conf["instance_id"])
            if include_values
            else cast("ProviderConfig", ProviderConfig.parse([], prov_conf))
            for prov_conf in raw_values.values()
            if (provider_type is None or prov_conf["type"] == provider_type)
            and (provider_domain is None or prov_conf["domain"] == provider_domain)
            # guard for deleted providers
            and prov_conf["domain"] in prov_entries
        ]

    @api_command("config/providers/get")
    async def get_provider_config(self, instance_id: str) -> ProviderConfig:
        """Return configuration for a single provider."""
        if raw_conf := self.get(f"{CONF_PROVIDERS}/{instance_id}", {}):
            config_entries = await self.get_provider_config_entries(
                raw_conf["domain"],
                instance_id=instance_id,
                values=raw_conf.get("values"),
            )
            for prov in self.mass.get_provider_manifests():
                if prov.domain == raw_conf["domain"]:
                    break
            else:
                msg = f"Unknown provider domain: {raw_conf['domain']}"
                raise KeyError(msg)
            return cast("ProviderConfig", ProviderConfig.parse(config_entries, raw_conf))
        msg = f"No config found for provider id {instance_id}"
        raise KeyError(msg)

    @overload
    async def get_provider_config_value(
        self,
        instance_id: str,
        key: str,
        *,
        default: _ConfigValueT,
        return_type: type[_ConfigValueT] = ...,
    ) -> _ConfigValueT: ...

    @overload
    async def get_provider_config_value(
        self,
        instance_id: str,
        key: str,
        *,
        default: ConfigValueType = ...,
        return_type: type[_ConfigValueT] = ...,
    ) -> _ConfigValueT: ...

    @overload
    async def get_provider_config_value(
        self,
        instance_id: str,
        key: str,
        *,
        default: ConfigValueType = ...,
        return_type: None = ...,
    ) -> ConfigValueType: ...

    @api_command("config/providers/get_value")
    async def get_provider_config_value(
        self,
        instance_id: str,
        key: str,
        *,
        default: ConfigValueType = None,
        return_type: type[_ConfigValueT | ConfigValueType] | None = None,
    ) -> _ConfigValueT | ConfigValueType:
        """
        Return single configentry value for a provider.

        :param instance_id: The provider instance ID.
        :param key: The config key to retrieve.
        :param default: Optional default value to return if key is not found.
        :param return_type: Optional type hint for type inference (e.g., str, int, bool).
            Note: This parameter is used purely for static type checking and does not
            perform runtime type validation. Callers are responsible for ensuring the
            specified type matches the actual config value type.
        """
        # prefer stored value so we don't have to retrieve all config entries every time
        if (raw_value := self.get_raw_provider_config_value(instance_id, key)) is not None:
            return raw_value
        conf = await self.get_provider_config(instance_id)
        if key not in conf.values:
            if default is not None:
                return default
            msg = f"Config key {key} not found for provider {instance_id}"
            raise KeyError(msg)
        return (
            conf.values[key].value
            if conf.values[key].value is not None
            else conf.values[key].default_value
        )

    @api_command("config/providers/get_entries")
    async def get_provider_config_entries(  # noqa: PLR0915
        self,
        provider_domain: str,
        instance_id: str | None = None,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """
        Return Config entries to setup/configure a provider.

        provider_domain: (mandatory) domain of the provider.
        instance_id: id of an existing provider instance (None for new instance setup).
        action: [optional] action key called from config entries UI.
        values: the (intermediate) raw values for config entries sent with the action.
        """
        # lookup provider manifest and module
        prov_mod: ProviderModuleType | None
        for manifest in self.mass.get_provider_manifests():
            if manifest.domain == provider_domain:
                try:
                    prov_mod = await load_provider_module(provider_domain, manifest.requirements)
                except Exception as e:
                    msg = f"Failed to load provider module for {provider_domain}: {e}"
                    LOGGER.exception(msg)
                    return []
                break
        else:
            msg = f"Unknown provider domain: {provider_domain}"
            LOGGER.exception(msg)
            return []

        # add dynamic optional config entries that depend on features
        if instance_id and (provider := self.mass.get_provider(instance_id)):
            supported_features = provider.supported_features
        else:
            provider = None
            supported_features = getattr(prov_mod, "SUPPORTED_FEATURES", set())
        extra_entries: list[ConfigEntry] = []
        if manifest.type == ProviderType.MUSIC:
            # library sync settings
            if ProviderFeature.LIBRARY_ARTISTS in supported_features:
                extra_entries.append(CONF_ENTRY_LIBRARY_SYNC_ARTISTS)
            if ProviderFeature.LIBRARY_ALBUMS in supported_features:
                extra_entries.append(CONF_ENTRY_LIBRARY_SYNC_ALBUMS)
                if (
                    provider
                    and isinstance(provider, MusicProvider)
                    and provider.is_streaming_provider
                ):
                    extra_entries.append(CONF_ENTRY_LIBRARY_SYNC_ALBUM_TRACKS)
            if ProviderFeature.LIBRARY_TRACKS in supported_features:
                extra_entries.append(CONF_ENTRY_LIBRARY_SYNC_TRACKS)
            if ProviderFeature.LIBRARY_PLAYLISTS in supported_features:
                extra_entries.append(CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS)
                if (
                    provider
                    and isinstance(provider, MusicProvider)
                    and provider.is_streaming_provider
                ):
                    extra_entries.append(CONF_ENTRY_LIBRARY_SYNC_PLAYLIST_TRACKS)
            if ProviderFeature.LIBRARY_AUDIOBOOKS in supported_features:
                extra_entries.append(CONF_ENTRY_LIBRARY_SYNC_AUDIOBOOKS)
            if ProviderFeature.LIBRARY_PODCASTS in supported_features:
                extra_entries.append(CONF_ENTRY_LIBRARY_SYNC_PODCASTS)
            if ProviderFeature.LIBRARY_RADIOS in supported_features:
                extra_entries.append(CONF_ENTRY_LIBRARY_SYNC_RADIOS)
            # sync interval settings
            if ProviderFeature.LIBRARY_ARTISTS in supported_features:
                extra_entries.append(CONF_ENTRY_PROVIDER_SYNC_INTERVAL_ARTISTS)
            if ProviderFeature.LIBRARY_ALBUMS in supported_features:
                extra_entries.append(CONF_ENTRY_PROVIDER_SYNC_INTERVAL_ALBUMS)
            if ProviderFeature.LIBRARY_TRACKS in supported_features:
                extra_entries.append(CONF_ENTRY_PROVIDER_SYNC_INTERVAL_TRACKS)
            if ProviderFeature.LIBRARY_PLAYLISTS in supported_features:
                extra_entries.append(CONF_ENTRY_PROVIDER_SYNC_INTERVAL_PLAYLISTS)
            if ProviderFeature.LIBRARY_AUDIOBOOKS in supported_features:
                extra_entries.append(CONF_ENTRY_PROVIDER_SYNC_INTERVAL_AUDIOBOOKS)
            if ProviderFeature.LIBRARY_PODCASTS in supported_features:
                extra_entries.append(CONF_ENTRY_PROVIDER_SYNC_INTERVAL_PODCASTS)
            if ProviderFeature.LIBRARY_RADIOS in supported_features:
                extra_entries.append(CONF_ENTRY_PROVIDER_SYNC_INTERVAL_RADIOS)
            # sync export settings
            if supported_features.intersection(
                {
                    ProviderFeature.LIBRARY_ARTISTS_EDIT,
                    ProviderFeature.LIBRARY_ALBUMS_EDIT,
                    ProviderFeature.LIBRARY_TRACKS_EDIT,
                    ProviderFeature.LIBRARY_PLAYLISTS_EDIT,
                    ProviderFeature.LIBRARY_AUDIOBOOKS_EDIT,
                    ProviderFeature.LIBRARY_PODCASTS_EDIT,
                    ProviderFeature.LIBRARY_RADIOS_EDIT,
                }
            ):
                extra_entries.append(CONF_ENTRY_LIBRARY_SYNC_BACK)

        all_entries = [
            *DEFAULT_PROVIDER_CONFIG_ENTRIES,
            *extra_entries,
            *await prov_mod.get_config_entries(
                self.mass, instance_id=instance_id, action=action, values=values
            ),
        ]
        if action and values is not None:
            # set current value from passed values for config entries
            # only do this if we're passed values (e.g. during an action)
            # deepcopy here to avoid modifying original entries
            all_entries = [deepcopy(entry) for entry in all_entries]
            for entry in all_entries:
                if entry.value is None:
                    entry.value = values.get(entry.key, entry.default_value)
        return all_entries

    @api_command("config/providers/save", required_role="admin")
    async def save_provider_config(
        self,
        provider_domain: str,
        values: dict[str, ConfigValueType],
        instance_id: str | None = None,
    ) -> ProviderConfig:
        """
        Save Provider(instance) Config.

        provider_domain: (mandatory) domain of the provider.
        values: the raw values for config entries that need to be stored/updated.
        instance_id: id of an existing provider instance (None for new instance setup).
        """
        if instance_id is not None:
            config = await self._update_provider_config(instance_id, values)
        else:
            config = await self._add_provider_config(provider_domain, values)
        # return full config, just in case
        return await self.get_provider_config(config.instance_id)

    @api_command("config/providers/remove", required_role="admin")
    async def remove_provider_config(self, instance_id: str) -> None:
        """Remove ProviderConfig."""
        conf_key = f"{CONF_PROVIDERS}/{instance_id}"
        existing = self.get(conf_key)
        if not existing:
            msg = f"Provider {instance_id} does not exist"
            raise KeyError(msg)
        prov_manifest = self.mass.get_provider_manifest(existing["domain"])
        if prov_manifest.builtin:
            msg = f"Builtin provider {prov_manifest.name} can not be removed."
            raise RuntimeError(msg)
        self.remove(conf_key)
        await self.mass.unload_provider(instance_id, True)
        if existing["type"] == "music":
            # cleanup entries in library
            await self.mass.music.cleanup_provider(instance_id)
        if existing["type"] == "player":
            # all players should already be removed by now through unload_provider
            for player in list(self.mass.players):
                if player.provider.instance_id != instance_id:
                    continue
                self.mass.players.delete_player_config(player.player_id)
            # cleanup remaining player configs
            for player_conf in list(self.get(CONF_PLAYERS, {}).values()):
                if player_conf["provider"] == instance_id:
                    self.remove(f"{CONF_PLAYERS}/{player_conf['player_id']}")

    async def remove_provider_config_value(self, instance_id: str, key: str) -> None:
        """Remove/reset single Provider config value."""
        conf_key = f"{CONF_PROVIDERS}/{instance_id}/values/{key}"
        existing = self.get(conf_key)
        if not existing:
            return
        self.remove(conf_key)

    def set_provider_default_name(self, instance_id: str, default_name: str) -> None:
        """Set (or update) the default name for a provider."""
        conf_key = f"{CONF_PROVIDERS}/{instance_id}/default_name"
        self.set(conf_key, default_name)

    @api_command("config/players")
    async def get_player_configs(
        self,
        provider: str | None = None,
        include_values: bool = False,
        include_unavailable: bool = True,
        include_disabled: bool = True,
    ) -> list[PlayerConfig]:
        """Return all known player configurations, optionally filtered by provider id."""
        result: list[PlayerConfig] = []
        for raw_conf in list(self.get(CONF_PLAYERS, {}).values()):
            # optional provider filter
            if provider is not None and raw_conf["provider"] != provider:
                continue
            # filter out unavailable players
            # (unless disabled, otherwise there is no way to re-enable them)
            player = self.mass.players.get(raw_conf["player_id"], False)
            if (
                not include_unavailable
                and (not player or not player.available)
                and raw_conf.get("enabled", True)
            ):
                continue
            # filter out disabled players
            if not include_disabled and not raw_conf.get("enabled", True):
                continue
            if include_values:
                result.append(await self.get_player_config(raw_conf["player_id"]))
            else:
                raw_conf["default_name"] = (
                    player.display_name if player else raw_conf.get("default_name")
                )
                raw_conf["available"] = player.available if player else False
                result.append(cast("PlayerConfig", PlayerConfig.parse([], raw_conf)))
        return result

    @api_command("config/players/get")
    async def get_player_config(
        self,
        player_id: str,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> PlayerConfig:
        """Return (full) configuration for a single player."""
        raw_conf: dict[str, Any]
        if raw_conf := self.get(f"{CONF_PLAYERS}/{player_id}"):
            if player := self.mass.players.get(player_id, False):
                raw_conf["default_name"] = player.display_name
                raw_conf["provider"] = player.provider.instance_id
                # pass action and values to get_config_entries
                if values is None:
                    values = raw_conf.get("values", {})
                conf_entries = await self.get_player_config_entries(
                    player_id, action=action, values=values
                )
            else:
                # handle unavailable player and/or provider
                conf_entries = []
                raw_conf["available"] = False
                raw_conf["default_name"] = raw_conf.get("default_name") or raw_conf["player_id"]
            return cast("PlayerConfig", PlayerConfig.parse(conf_entries, raw_conf))
        msg = f"No config found for player id {player_id}"
        raise KeyError(msg)

    @api_command("config/players/get_entries")
    async def get_player_config_entries(
        self,
        player_id: str,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """
        Return Config entries to configure a player.

        player_id: id of an existing player instance.
        action: [optional] action key called from config entries UI.
        values: the (intermediate) raw values for config entries sent with the action.
        """
        if not (player := self.mass.players.get(player_id, False)):
            msg = f"Player {player_id} not found"
            raise KeyError(msg)
        # get player(protocol) specific entries
        player_entries = await self._get_player_config_entries(player, action=action, values=values)
        # get default entries which are common for all players
        default_entries = self._get_default_player_config_entries(player)
        player_entries_keys = {entry.key for entry in player_entries}
        all_entries = [
            # ignore default entries that were overridden by the player specific ones
            *[x for x in default_entries if x.key not in player_entries_keys],
            *player_entries,
        ]
        if action and values is not None:
            # set current value from passed values for config entries
            # only do this if we're passed values (e.g. during an action)
            # deepcopy here to avoid modifying original entries
            all_entries = [deepcopy(entry) for entry in all_entries]
            for entry in all_entries:
                if entry.value is None:
                    entry.value = values.get(entry.key, entry.default_value)
        return all_entries

    @overload
    async def get_player_config_value(
        self,
        player_id: str,
        key: str,
        unpack_splitted_values: Literal[True],
        *,
        default: ConfigValueType = ...,
        return_type: type[_ConfigValueT] | None = ...,
    ) -> tuple[str, ...] | list[tuple[str, ...]]: ...

    @overload
    async def get_player_config_value(
        self,
        player_id: str,
        key: str,
        unpack_splitted_values: Literal[False] = False,
        *,
        default: _ConfigValueT,
        return_type: type[_ConfigValueT] = ...,
    ) -> _ConfigValueT: ...

    @overload
    async def get_player_config_value(
        self,
        player_id: str,
        key: str,
        unpack_splitted_values: Literal[False] = False,
        *,
        default: ConfigValueType = ...,
        return_type: type[_ConfigValueT] = ...,
    ) -> _ConfigValueT: ...

    @overload
    async def get_player_config_value(
        self,
        player_id: str,
        key: str,
        unpack_splitted_values: Literal[False] = False,
        *,
        default: ConfigValueType = ...,
        return_type: None = ...,
    ) -> ConfigValueType: ...

    @api_command("config/players/get_value")
    async def get_player_config_value(
        self,
        player_id: str,
        key: str,
        unpack_splitted_values: bool = False,
        *,
        default: ConfigValueType = None,
        return_type: type[_ConfigValueT | ConfigValueType] | None = None,
    ) -> _ConfigValueT | ConfigValueType | tuple[str, ...] | list[tuple[str, ...]]:
        """
        Return single configentry value for a player.

        :param player_id: The player ID.
        :param key: The config key to retrieve.
        :param unpack_splitted_values: Whether to unpack multi-value config entries.
        :param default: Optional default value to return if key is not found.
        :param return_type: Optional type hint for type inference (e.g., str, int, bool).
            Note: This parameter is used purely for static type checking and does not
            perform runtime type validation. Callers are responsible for ensuring the
            specified type matches the actual config value type.
        """
        # prefer stored value so we don't have to retrieve all config entries every time
        if (raw_value := self.get_raw_player_config_value(player_id, key)) is not None:
            if not unpack_splitted_values:
                return raw_value
        conf = await self.get_player_config(player_id)
        if key not in conf.values:
            if default is not None:
                return default
            msg = f"Config key {key} not found for player {player_id}"
            raise KeyError(msg)
        if unpack_splitted_values:
            return conf.values[key].get_splitted_values()
        return (
            conf.values[key].value
            if conf.values[key].value is not None
            else conf.values[key].default_value
        )

    if TYPE_CHECKING:
        # Overload for when default is provided - return type matches default type
        @overload
        def get_raw_player_config_value(
            self, player_id: str, key: str, default: _ConfigValueT
        ) -> _ConfigValueT: ...

        # Overload for when no default is provided - return ConfigValueType | None
        @overload
        def get_raw_player_config_value(
            self, player_id: str, key: str, default: None = None
        ) -> ConfigValueType | None: ...

    def get_raw_player_config_value(
        self, player_id: str, key: str, default: ConfigValueType = None
    ) -> ConfigValueType:
        """
        Return (raw) single configentry value for a player.

        Note that this only returns the stored value without any validation or default.
        """
        return cast(
            "ConfigValueType",
            self.get(
                f"{CONF_PLAYERS}/{player_id}/values/{key}",
                self.get(f"{CONF_PLAYERS}/{player_id}/{key}", default),
            ),
        )

    def get_base_player_config(self, player_id: str, provider: str) -> PlayerConfig:
        """
        Return base PlayerConfig for a player.

        This is used to get the base config for a player, without any provider specific values,
        for initialization purposes.
        """
        if not (raw_conf := self.get(f"{CONF_PLAYERS}/{player_id}")):
            raw_conf = {
                "player_id": player_id,
                "provider": provider,
            }
        return cast("PlayerConfig", PlayerConfig.parse([], raw_conf))

    @api_command("config/players/save", required_role="admin")
    async def save_player_config(
        self, player_id: str, values: dict[str, ConfigValueType]
    ) -> PlayerConfig:
        """Save/update PlayerConfig."""
        config = await self.get_player_config(player_id)
        old_config = deepcopy(config)
        changed_keys = config.update(values)
        if not changed_keys:
            # no changes
            return config
        # store updated config first (to prevent issues with enabling/disabling players)
        conf_key = f"{CONF_PLAYERS}/{player_id}"
        self.set(conf_key, config.to_raw())
        try:
            # validate/handle the update in the player manager
            await self.mass.players.on_player_config_change(config, changed_keys)
        except Exception:
            # rollback on error
            self.set(conf_key, old_config.to_raw())
            raise
        # send config updated event
        self.mass.signal_event(
            EventType.PLAYER_CONFIG_UPDATED,
            object_id=config.player_id,
            data=config,
        )
        # return full player config (just in case)
        return await self.get_player_config(player_id)

    @api_command("config/players/remove", required_role="admin")
    async def remove_player_config(self, player_id: str) -> None:
        """Remove PlayerConfig."""
        conf_key = f"{CONF_PLAYERS}/{player_id}"
        dsp_conf_key = f"{CONF_PLAYER_DSP}/{player_id}"
        player_config = self.get(conf_key)
        if not player_config:
            msg = f"Player configuration for {player_id} does not exist"
            raise KeyError(msg)
        if self.mass.players.get(player_id):
            try:
                await self.mass.players.remove(player_id)
            except UnsupportedFeaturedException:
                # removing a player config while it is active is not allowed
                # unless the provider reports it has the remove_player feature
                raise ActionUnavailable("Can not remove config for an active player!")
            # tell the player manager to remove the player if its lingering around
            # set permanent to false otherwise we end up in an infinite loop
            await self.mass.players.unregister(player_id, permanent=False)
        # remove the actual config if all of the above passed
        self.remove(conf_key)
        # Also remove the DSP config if it exists
        self.remove(dsp_conf_key)

    def set_player_default_name(self, player_id: str, default_name: str) -> None:
        """Set (or update) the default name for a player."""
        conf_key = f"{CONF_PLAYERS}/{player_id}/default_name"
        self.set(conf_key, default_name)

    def set_player_type(self, player_id: str, player_type: PlayerType) -> None:
        """Set (or update) the type for a player."""
        conf_key = f"{CONF_PLAYERS}/{player_id}/player_type"
        self.set(conf_key, player_type)

    def create_default_player_config(
        self,
        player_id: str,
        provider: str,
        player_type: PlayerType,
        name: str | None = None,
        enabled: bool = True,
        values: dict[str, ConfigValueType] | None = None,
    ) -> None:
        """
        Create default/empty PlayerConfig.

        This is meant as helper to create default configs when a player is registered.
        Called by the player manager on player register.
        """
        # return early if the config already exists
        if existing_conf := self.get(f"{CONF_PLAYERS}/{player_id}"):
            # update default name if needed
            if name and name != existing_conf.get("default_name"):
                self.set(f"{CONF_PLAYERS}/{player_id}/default_name", name)
            # update player_type if needed
            if existing_conf.get("player_type") != player_type:
                self.set(f"{CONF_PLAYERS}/{player_id}/player_type", player_type.value)
            return
        # config does not yet exist, create a default one
        conf_key = f"{CONF_PLAYERS}/{player_id}"
        default_conf = PlayerConfig(
            values={},
            provider=provider,
            player_id=player_id,
            enabled=enabled,
            name=name,
            default_name=name,
            player_type=player_type,
        )
        default_conf_raw = default_conf.to_raw()
        if values is not None:
            default_conf_raw["values"] = values
        self.set(
            conf_key,
            default_conf_raw,
        )

    @api_command("config/players/dsp/get")
    def get_player_dsp_config(self, player_id: str) -> DSPConfig:
        """
        Return the DSP Configuration for a player.

        In case the player does not have a DSP configuration, a default one is returned.
        """
        if raw_conf := self.get(f"{CONF_PLAYER_DSP}/{player_id}"):
            return DSPConfig.from_dict(raw_conf)
        # return default DSP config
        dsp_config = DSPConfig()
        # The DSP config does not do anything by default, so we disable it
        dsp_config.enabled = False
        return dsp_config

    @api_command("config/players/dsp/save", required_role="admin")
    async def save_dsp_config(self, player_id: str, config: DSPConfig) -> DSPConfig:
        """
        Save/update DSPConfig for a player.

        This method will validate the config and apply it to the player.
        """
        # validate the new config
        config.validate()

        # Save and apply the new config to the player
        self.set(f"{CONF_PLAYER_DSP}/{player_id}", config.to_dict())
        await self.mass.players.on_player_dsp_change(player_id)
        # send the dsp config updated event
        self.mass.signal_event(
            EventType.PLAYER_DSP_CONFIG_UPDATED,
            object_id=player_id,
            data=config,
        )
        return config

    @api_command("config/dsp_presets/get")
    async def get_dsp_presets(self) -> list[DSPConfigPreset]:
        """Return all user-defined DSP presets."""
        raw_presets = self.get(CONF_PLAYER_DSP_PRESETS, {})
        return [DSPConfigPreset.from_dict(preset) for preset in raw_presets.values()]

    @api_command("config/dsp_presets/save", required_role="admin")
    async def save_dsp_presets(self, preset: DSPConfigPreset) -> DSPConfigPreset:
        """
        Save/update a user-defined DSP presets.

        This method will validate the config before saving it to the persistent storage.
        """
        preset.validate()

        if preset.preset_id is None:
            # Generate a new preset_id if it does not exist
            preset.preset_id = shortuuid.random(8).lower()

        # Save the preset to the persistent storage
        self.set(f"{CONF_PLAYER_DSP_PRESETS}/preset_{preset.preset_id}", preset.to_dict())

        all_presets = await self.get_dsp_presets()

        self.mass.signal_event(
            EventType.DSP_PRESETS_UPDATED,
            data=all_presets,
        )

        return preset

    @api_command("config/dsp_presets/remove", required_role="admin")
    async def remove_dsp_preset(self, preset_id: str) -> None:
        """Remove a user-defined DSP preset."""
        self.mass.config.remove(f"{CONF_PLAYER_DSP_PRESETS}/preset_{preset_id}")

        all_presets = await self.get_dsp_presets()

        self.mass.signal_event(
            EventType.DSP_PRESETS_UPDATED,
            data=all_presets,
        )

    async def create_builtin_provider_config(self, provider_domain: str) -> None:
        """
        Create builtin ProviderConfig.

        This is meant as helper to create default configs for builtin providers.
        Called by the server initialization code which load all providers at startup.
        """
        for _ in await self.get_provider_configs(provider_domain=provider_domain):
            # return if there is already any config
            return
        for prov in self.mass.get_provider_manifests():
            if prov.domain == provider_domain:
                manifest = prov
                break
        else:
            msg = f"Unknown provider domain: {provider_domain}"
            raise KeyError(msg)
        config_entries = await self.get_provider_config_entries(provider_domain)
        if manifest.multi_instance:
            instance_id = f"{manifest.domain}--{shortuuid.random(8)}"
        else:
            instance_id = manifest.domain
        default_config = cast(
            "ProviderConfig",
            ProviderConfig.parse(
                config_entries,
                {
                    "type": manifest.type.value,
                    "domain": manifest.domain,
                    "instance_id": instance_id,
                    "name": manifest.name,
                    # note: this will only work for providers that do
                    # not have any required config entries or provide defaults
                    "values": {},
                },
            ),
        )
        default_config.validate()
        conf_key = f"{CONF_PROVIDERS}/{default_config.instance_id}"
        self.set_default(conf_key, default_config.to_raw())

    @api_command("config/core")
    async def get_core_configs(self, include_values: bool = False) -> list[CoreConfig]:
        """Return all core controllers config options."""
        return [
            await self.get_core_config(core_controller)
            if include_values
            else cast(
                "CoreConfig",
                CoreConfig.parse(
                    [],
                    self.get(f"{CONF_CORE}/{core_controller}", {"domain": core_controller}),
                ),
            )
            for core_controller in CONFIGURABLE_CORE_CONTROLLERS
        ]

    @api_command("config/core/get")
    async def get_core_config(self, domain: str) -> CoreConfig:
        """Return configuration for a single core controller."""
        raw_conf = self.get(f"{CONF_CORE}/{domain}", {"domain": domain})
        config_entries = await self.get_core_config_entries(domain)
        return cast("CoreConfig", CoreConfig.parse(config_entries, raw_conf))

    @overload
    async def get_core_config_value(
        self,
        domain: str,
        key: str,
        *,
        default: _ConfigValueT,
        return_type: type[_ConfigValueT] = ...,
    ) -> _ConfigValueT: ...

    @overload
    async def get_core_config_value(
        self,
        domain: str,
        key: str,
        *,
        default: ConfigValueType = ...,
        return_type: type[_ConfigValueT] = ...,
    ) -> _ConfigValueT: ...

    @overload
    async def get_core_config_value(
        self,
        domain: str,
        key: str,
        *,
        default: ConfigValueType = ...,
        return_type: None = ...,
    ) -> ConfigValueType: ...

    @api_command("config/core/get_value")
    async def get_core_config_value(
        self,
        domain: str,
        key: str,
        *,
        default: ConfigValueType = None,
        return_type: type[_ConfigValueT | ConfigValueType] | None = None,
    ) -> _ConfigValueT | ConfigValueType:
        """
        Return single configentry value for a core controller.

        :param domain: The core controller domain.
        :param key: The config key to retrieve.
        :param default: Optional default value to return if key is not found.
        :param return_type: Optional type hint for type inference (e.g., str, int, bool).
            Note: This parameter is used purely for static type checking and does not
            perform runtime type validation. Callers are responsible for ensuring the
            specified type matches the actual config value type.
        """
        # prefer stored value so we don't have to retrieve all config entries every time
        if (raw_value := self.get_raw_core_config_value(domain, key)) is not None:
            return raw_value
        conf = await self.get_core_config(domain)
        if key not in conf.values:
            if default is not None:
                return default
            msg = f"Config key {key} not found for core controller {domain}"
            raise KeyError(msg)
        return (
            conf.values[key].value
            if conf.values[key].value is not None
            else conf.values[key].default_value
        )

    @api_command("config/core/get_entries")
    async def get_core_config_entries(
        self,
        domain: str,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """
        Return Config entries to configure a core controller.

        core_controller: name of the core controller
        action: [optional] action key called from config entries UI.
        values: the (intermediate) raw values for config entries sent with the action.
        """
        controller: CoreController = getattr(self.mass, domain)
        all_entries = list(
            await controller.get_config_entries(action=action, values=values)
            + DEFAULT_CORE_CONFIG_ENTRIES
        )
        if action and values is not None:
            # set current value from passed values for config entries
            # only do this if we're passed values (e.g. during an action)
            # deepcopy here to avoid modifying original entries
            all_entries = [deepcopy(entry) for entry in all_entries]
            for entry in all_entries:
                if entry.value is None:
                    entry.value = values.get(entry.key, entry.default_value)
        return all_entries

    @api_command("config/core/save", required_role="admin")
    async def save_core_config(
        self,
        domain: str,
        values: dict[str, ConfigValueType],
    ) -> CoreConfig:
        """Save CoreController Config values."""
        config = await self.get_core_config(domain)
        prev_config = config.to_raw()
        changed_keys = config.update(values)
        # validate the new config
        config.validate()
        if not changed_keys:
            # no changes
            return config
        # save the config first before reloading to avoid issues on reload
        # for example when reloading the webserver we might be cancelled here
        conf_key = f"{CONF_CORE}/{domain}"
        self.set(conf_key, config.to_raw())
        self.save(immediate=True)
        try:
            controller: CoreController = getattr(self.mass, domain)
            await controller.update_config(config, changed_keys)
        except asyncio.CancelledError:
            pass
        except Exception:
            # revert to previous config on error
            self.set(conf_key, prev_config)
            self.save(immediate=True)
            raise
        # reload succeeded; clear last_error and persist the final state
        config.last_error = None
        # return full config
        return await self.get_core_config(domain)

    if TYPE_CHECKING:
        # Overload for when default is provided - return type matches default type
        @overload
        def get_raw_core_config_value(
            self, core_module: str, key: str, default: _ConfigValueT
        ) -> _ConfigValueT: ...

        # Overload for when no default is provided - return ConfigValueType | None
        @overload
        def get_raw_core_config_value(
            self, core_module: str, key: str, default: None = None
        ) -> ConfigValueType | None: ...

    def get_raw_core_config_value(
        self, core_module: str, key: str, default: ConfigValueType = None
    ) -> ConfigValueType:
        """
        Return (raw) single configentry value for a core controller.

        Note that this only returns the stored value without any validation or default.
        """
        return cast(
            "ConfigValueType",
            self.get(
                f"{CONF_CORE}/{core_module}/values/{key}",
                self.get(f"{CONF_CORE}/{core_module}/{key}", default),
            ),
        )

    if TYPE_CHECKING:
        # Overload for when default is provided - return type matches default type
        @overload
        def get_raw_provider_config_value(
            self, provider_instance: str, key: str, default: _ConfigValueT
        ) -> _ConfigValueT: ...

        # Overload for when no default is provided - return ConfigValueType | None
        @overload
        def get_raw_provider_config_value(
            self, provider_instance: str, key: str, default: None = None
        ) -> ConfigValueType | None: ...

    def get_raw_provider_config_value(
        self, provider_instance: str, key: str, default: ConfigValueType = None
    ) -> ConfigValueType:
        """
        Return (raw) single config(entry) value for a provider.

        Note that this only returns the stored value without any validation or default.
        """
        return cast(
            "ConfigValueType",
            self.get(
                f"{CONF_PROVIDERS}/{provider_instance}/values/{key}",
                self.get(f"{CONF_PROVIDERS}/{provider_instance}/{key}", default),
            ),
        )

    def set_raw_provider_config_value(
        self,
        provider_instance: str,
        key: str,
        value: ConfigValueType,
        encrypted: bool = False,
    ) -> None:
        """
        Set (raw) single config(entry) value for a provider.

        Note that this only stores the (raw) value without any validation or default.
        """
        if not self.get(f"{CONF_PROVIDERS}/{provider_instance}"):
            # only allow setting raw values if main entry exists
            msg = f"Invalid provider_instance: {provider_instance}"
            raise KeyError(msg)
        if encrypted:
            if not isinstance(value, str):
                msg = f"Cannot encrypt non-string value for key {key}"
                raise ValueError(msg)
            value = self.encrypt_string(value)
        if key in BASE_KEYS:
            self.set(f"{CONF_PROVIDERS}/{provider_instance}/{key}", value)
            return
        self.set(f"{CONF_PROVIDERS}/{provider_instance}/values/{key}", value)

    def set_raw_core_config_value(self, core_module: str, key: str, value: ConfigValueType) -> None:
        """
        Set (raw) single config(entry) value for a core controller.

        Note that this only stores the (raw) value without any validation or default.
        """
        if not self.get(f"{CONF_CORE}/{core_module}"):
            # create base object first if needed
            self.set(f"{CONF_CORE}/{core_module}", CoreConfig({}, core_module).to_raw())
        self.set(f"{CONF_CORE}/{core_module}/values/{key}", value)

    def set_raw_player_config_value(self, player_id: str, key: str, value: ConfigValueType) -> None:
        """
        Set (raw) single config(entry) value for a player.

        Note that this only stores the (raw) value without any validation or default.
        """
        if not self.get(f"{CONF_PLAYERS}/{player_id}"):
            # only allow setting raw values if main entry exists
            msg = f"Invalid player_id: {player_id}"
            raise KeyError(msg)
        if key in BASE_KEYS:
            self.set(f"{CONF_PLAYERS}/{player_id}/{key}", value)
        else:
            self.set(f"{CONF_PLAYERS}/{player_id}/values/{key}", value)

    def save(self, immediate: bool = False) -> None:
        """Schedule save of data to disk."""
        if self._timer_handle is not None:
            self._timer_handle.cancel()
            self._timer_handle = None

        if immediate:
            self.mass.loop.create_task(self._async_save())
        else:
            # schedule the save for later
            self._timer_handle = self.mass.loop.call_later(
                DEFAULT_SAVE_DELAY, self.mass.create_task, self._async_save
            )

    def encrypt_string(self, str_value: str) -> str:
        """Encrypt a (password)string with Fernet."""
        if str_value.startswith(ENCRYPT_SUFFIX):
            return str_value
        assert self._fernet is not None
        return ENCRYPT_SUFFIX + self._fernet.encrypt(str_value.encode()).decode()

    def decrypt_string(self, encrypted_str: str) -> str:
        """Decrypt a (password)string with Fernet."""
        if not encrypted_str:
            return encrypted_str
        if not encrypted_str.startswith(ENCRYPT_SUFFIX):
            return encrypted_str
        assert self._fernet is not None
        try:
            return self._fernet.decrypt(encrypted_str.replace(ENCRYPT_SUFFIX, "").encode()).decode()
        except InvalidToken as err:
            msg = "Password decryption failed"
            raise InvalidDataError(msg) from err

    async def _load(self) -> None:
        """Load data from persistent storage."""
        assert not self._data, "Already loaded"

        for filename in (self.filename, f"{self.filename}.backup"):
            try:
                async with aiofiles.open(filename, encoding="utf-8") as _file:
                    self._data = await async_json_loads(await _file.read())
                    LOGGER.debug("Loaded persistent settings from %s", filename)
                    await self._migrate()
                    return
            except FileNotFoundError:
                pass
            except JSON_DECODE_EXCEPTIONS:
                LOGGER.exception("Error while reading persistent storage file %s", filename)
        LOGGER.debug("Started with empty storage: No persistent storage file found.")

    async def _migrate(self) -> None:  # noqa: PLR0915
        changed = False

        # some type hints to help with the code below
        instance_id: str
        provider_config: dict[str, Any]
        player_config: dict[str, Any]

        # Older versions of MA can create corrupt entries with no domain if retrying
        # logic runs after a provider has been removed. Remove those corrupt entries.
        for instance_id, provider_config in {**self._data.get(CONF_PROVIDERS, {})}.items():
            if "domain" not in provider_config:
                self._data[CONF_PROVIDERS].pop(instance_id, None)
                LOGGER.warning("Removed corrupt provider configuration: %s", instance_id)
                changed = True

        # migrate manual_ips to new format
        for instance_id, provider_config in self._data.get(CONF_PROVIDERS, {}).items():
            if not (values := provider_config.get("values")):
                continue
            if not (ips := values.get("ips")):
                continue
            values["manual_discovery_ip_addresses"] = ips.split(",")
            del values["ips"]
            changed = True

        # migrate sample_rates config entry
        for player_config in self._data.get(CONF_PLAYERS, {}).values():
            if not (values := player_config.get("values")):
                continue
            if not (sample_rates := values.get("sample_rates")):
                continue
            if not isinstance(sample_rates, list):
                del player_config["values"]["sample_rates"]
            if not any(isinstance(x, list) for x in sample_rates):
                continue
            player_config["values"]["sample_rates"] = [
                f"{x[0]}{MULTI_VALUE_SPLITTER}{x[1]}" if isinstance(x, list) else x
                for x in sample_rates
            ]
            changed = True

        # migrate player_group entries
        ugp_found = False
        for player_config in self._data.get(CONF_PLAYERS, {}).values():
            provider = player_config.get("provider")
            if (
                not provider
                or not isinstance(provider, str)
                or not provider.startswith("player_group")
            ):
                continue
            if not (values := player_config.get("values")):
                continue
            if (group_type := values.pop("group_type", None)) is None:
                continue
            # this is a legacy player group, migrate the values
            changed = True
            if group_type == "universal":
                player_config["provider"] = "universal_group"
                ugp_found = True
            else:
                player_config["provider"] = group_type
        for provider_config in list(self._data.get(CONF_PROVIDERS, {}).values()):
            instance_id = provider_config["instance_id"]
            if not instance_id.startswith("player_group"):
                continue
            # this is the legacy player_group provider, migrate into 'universal_group'
            changed = True
            self._data[CONF_PROVIDERS].pop(instance_id, None)
            if not ugp_found:
                continue
            provider_config["domain"] = "universal_group"
            provider_config["instance_id"] = "universal_group"
            self._data[CONF_PROVIDERS]["universal_group"] = provider_config

        # Migrate resonate provider to sendspin (renamed in 2.7 beta 19)
        for instance_id, provider_config in list(self._data.get(CONF_PROVIDERS, {}).items()):
            if provider_config.get("domain") == "resonate":
                self._data[CONF_PROVIDERS].pop(instance_id, None)
                provider_config["domain"] = "sendspin"
                provider_config["instance_id"] = "sendspin"
                self._data[CONF_PROVIDERS]["sendspin"] = provider_config
                changed = True

        # Migrate smart_fades mode value to smart_crossfade
        for player_config in self._data.get(CONF_PLAYERS, {}).values():
            if not (values := player_config.get("values")):
                continue
            if values.get(CONF_SMART_FADES_MODE) == "smart_fades":
                # Update old 'smart_fades' value to new 'smart_crossfade' value
                values[CONF_SMART_FADES_MODE] = "smart_crossfade"
                changed = True

        # Remove obsolete builtin_player configurations (provider was deleted in 2.7)
        for player_id, player_config in list(self._data.get(CONF_PLAYERS, {}).items()):
            if player_config.get("provider") != "builtin_player":
                continue
            self._data[CONF_PLAYERS].pop(player_id, None)
            # Also remove any DSP config for this player
            if CONF_PLAYER_DSP in self._data:
                self._data[CONF_PLAYER_DSP].pop(player_id, None)
            LOGGER.warning("Removed obsolete builtin_player configuration: %s", player_id)
            changed = True

        # migrate player configs: always use instance_id for provider
        for player_config in self._data.get(CONF_PLAYERS, {}).values():
            if "provider" not in player_config:
                continue
            player_provider = player_config["provider"]
            try:
                if not (prov := self.mass.get_provider(player_provider)):
                    continue
            except KeyError:
                # removed provider
                continue
            if player_config["provider"] == prov.instance_id:
                continue
            player_config["provider"] = prov.instance_id
            changed = True

        # Migrate AirPlay legacy credentials (ap_credentials) to protocol-specific keys
        # The old key was used for both RAOP and AirPlay, now we have separate keys
        for player_id, player_config in self._data.get(CONF_PLAYERS, {}).items():
            if player_config.get("provider") != "airplay":
                continue
            if not (values := player_config.get("values")):
                continue
            if "ap_credentials" not in values:
                continue
            # Migrate to raop_credentials (RAOP is the default/fallback protocol)
            # The new code will use the correct key based on the protocol
            old_creds = values.pop("ap_credentials")
            if old_creds and "raop_credentials" not in values:
                values["raop_credentials"] = old_creds
                LOGGER.info("Migrated AirPlay credentials for player %s", player_id)
            changed = True

        if changed:
            await self._async_save()

    async def _async_save(self) -> None:
        """Save persistent data to disk."""
        filename_backup = f"{self.filename}.backup"
        # make backup before we write a new file
        if await isfile(self.filename):
            with contextlib.suppress(FileNotFoundError):
                await remove(filename_backup)
            await rename(self.filename, filename_backup)

        async with aiofiles.open(self.filename, "w", encoding="utf-8") as _file:
            await _file.write(await async_json_dumps(self._data, indent=True))
        LOGGER.debug("Saved data to persistent storage")

    @api_command("config/providers/reload", required_role="admin")
    async def _reload_provider(self, instance_id: str) -> None:
        """Reload provider."""
        try:
            config = await self.get_provider_config(instance_id)
        except KeyError:
            # Edge case: Provider was removed before we could reload it
            return
        await self.mass.load_provider_config(config)

    async def _update_provider_config(
        self, instance_id: str, values: dict[str, ConfigValueType]
    ) -> ProviderConfig:
        """Update ProviderConfig."""
        config = await self.get_provider_config(instance_id)
        changed_keys = config.update(values)
        prov_instance = self.mass.get_provider(instance_id)
        available = prov_instance.available if prov_instance else False
        if not changed_keys and (config.enabled == available):
            # no changes
            return config
        # validate the new config
        config.validate()
        # save the config first to prevent issues when the
        # provider wants to manipulate the config during load
        conf_key = f"{CONF_PROVIDERS}/{config.instance_id}"
        raw_conf = config.to_raw()
        self.set(conf_key, raw_conf)
        if config.enabled and prov_instance is None:
            await self.mass.load_provider_config(config)
        if config.enabled and prov_instance and available:
            # update config for existing/loaded provider instance
            await prov_instance.update_config(config, changed_keys)
            # push instance name to config (to persist it if it was autogenerated)
            if prov_instance.default_name != config.default_name:
                self.set_provider_default_name(
                    prov_instance.instance_id, prov_instance.default_name
                )
        elif config.enabled:
            # provider is enabled but not available, try to load it
            await self.mass.load_provider_config(config)
        else:
            # disable provider
            prov_manifest = self.mass.get_provider_manifest(config.domain)
            if not prov_manifest.allow_disable:
                msg = "Provider can not be disabled."
                raise RuntimeError(msg)
            # also unload any other providers dependent of this provider
            for dep_prov in self.mass.providers:
                if dep_prov.manifest.depends_on == config.domain:
                    await self.mass.unload_provider(dep_prov.instance_id)
            await self.mass.unload_provider(config.instance_id)
            # For player providers, unload_provider should have removed all its players by now
        return config

    async def _add_provider_config(
        self,
        provider_domain: str,
        values: dict[str, ConfigValueType],
    ) -> ProviderConfig:
        """
        Add new Provider (instance).

        params:
        - provider_domain: domain of the provider for which to add an instance of.
        - values: the raw values for config entries.

        Returns: newly created ProviderConfig.
        """
        # lookup provider manifest and module
        for prov in self.mass.get_provider_manifests():
            if prov.domain == provider_domain:
                manifest = prov
                break
        else:
            msg = f"Unknown provider domain: {provider_domain}"
            raise KeyError(msg)
        if prov.depends_on and not self.mass.get_provider(prov.depends_on):
            msg = f"Provider {manifest.name} depends on {prov.depends_on}"
            raise ValueError(msg)
        # create new provider config with given values
        existing = {
            x.instance_id for x in await self.get_provider_configs(provider_domain=provider_domain)
        }
        # determine instance id based on previous configs
        if existing and not manifest.multi_instance:
            msg = f"Provider {manifest.name} does not support multiple instances"
            raise ValueError(msg)
        if manifest.multi_instance:
            instance_id = f"{manifest.domain}--{shortuuid.random(8)}"
        else:
            instance_id = manifest.domain
        # all checks passed, create config object
        config_entries = await self.get_provider_config_entries(
            provider_domain=provider_domain, instance_id=instance_id, values=values
        )
        config = cast(
            "ProviderConfig",
            ProviderConfig.parse(
                config_entries,
                {
                    "type": manifest.type.value,
                    "domain": manifest.domain,
                    "instance_id": instance_id,
                    "default_name": manifest.name,
                    "values": values,
                },
            ),
        )
        # validate the new config
        config.validate()
        # save the config first to prevent issues when the
        # provider wants to manipulate the config during load
        conf_key = f"{CONF_PROVIDERS}/{config.instance_id}"
        self.set(conf_key, config.to_raw())
        # try to load the provider
        try:
            await self.mass.load_provider_config(config)
        except Exception:
            # loading failed, remove config
            self.remove(conf_key)
            raise
        if not self.onboard_done:
            # mark onboard as complete as soon as the first provider is added
            await self.set_onboard_complete()
        if manifest.type == ProviderType.MUSIC:
            # correct any multi-instance provider mappings
            self.mass.create_task(self.mass.music.correct_multi_instance_provider_mappings())
        return config

    async def _get_player_config_entries(
        self,
        player: Player,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """
        Return Player(protocol) specific config entries, without any default entries.

        In general this returns entries that are specific to this provider/player type only,
        and includes audio related entries that are not part of the default set.

        player: the player instance
        action: [optional] action key called from config entries UI.
        values: the (intermediate) raw values for config entries sent with the action.
        """
        default_entries: list[ConfigEntry]
        is_dedicated_group_player = player.type in (
            PlayerType.GROUP,
            PlayerType.STEREO_PAIR,
        ) and not player.player_id.startswith(("universal_", SYNCGROUP_PREFIX))
        is_http_based_player_protocol = player.provider.domain not in NON_HTTP_PROVIDERS
        if player.type == PlayerType.GROUP and not is_dedicated_group_player:
            # no audio related entries for universal group players or sync group players
            default_entries = []
        else:
            # default output/audio related entries
            default_entries = [
                # output channel is always configurable per player(protocol)
                CONF_ENTRY_OUTPUT_CHANNELS
            ]
            if is_http_based_player_protocol:
                # for http based players we can add the http streaming related entries
                default_entries += [
                    CONF_ENTRY_SAMPLE_RATES,
                    CONF_ENTRY_OUTPUT_CODEC,
                    CONF_ENTRY_HTTP_PROFILE,
                    CONF_ENTRY_ENABLE_ICY_METADATA,
                ]
                # add flow mode entry for http-based players that do not already enforce it
                if not player.requires_flow_mode:
                    default_entries.append(CONF_ENTRY_FLOW_MODE)
        # request player specific entries
        player_entries = await player.get_config_entries(action=action, values=values)
        players_keys = {entry.key for entry in player_entries}
        # filter out any default entries that are already provided by the player
        default_entries = [entry for entry in default_entries if entry.key not in players_keys]
        return [*player_entries, *default_entries]

    def _get_default_player_config_entries(self, player: Player) -> list[ConfigEntry]:
        """
        Return the default (generic) player config entries.

        This does not return audio/protocol specific entries, those are handled elsewhere.
        """
        entries: list[ConfigEntry] = []
        # default protocol-player config entries
        if player.type == PlayerType.PROTOCOL:
            # protocol players have no generic config entries
            # only audio/protocol specific ones
            return []

        # some base entries for all player types
        # note that these may NOT be playback/audio related
        entries += [
            CONF_ENTRY_SMART_FADES_MODE,
            CONF_ENTRY_CROSSFADE_DURATION,
            # we allow volume normalization/output limiter here as it is a per-queue(player) setting
            CONF_ENTRY_VOLUME_NORMALIZATION,
            CONF_ENTRY_OUTPUT_LIMITER,
            CONF_ENTRY_VOLUME_NORMALIZATION_TARGET,
            CONF_ENTRY_TTS_PRE_ANNOUNCE,
            ConfigEntry(
                key=CONF_PRE_ANNOUNCE_CHIME_URL,
                type=ConfigEntryType.STRING,
                label="Custom (pre)announcement chime URL",
                description="URL to a custom audio file to play before announcements.\n"
                "Leave empty to use the default chime.\n"
                "Supports http:// and https:// URLs pointing to "
                "audio files (.mp3, .wav, .flac, .ogg, .m4a, .aac).\n"
                "Example: http://homeassistant.local:8123/local/audio/custom_chime.mp3",
                category="announcements",
                required=False,
                depends_on=CONF_ENTRY_TTS_PRE_ANNOUNCE.key,
                depends_on_value=True,
                validate=lambda val: validate_announcement_chime_url(cast("str", val)),
            ),
            # add player control entries
            *self._create_player_control_config_entries(player),
            # add entry to hide player in UI
            ConfigEntry(
                key=CONF_HIDE_IN_UI,
                type=ConfigEntryType.BOOLEAN,
                label="Hide this player in the user interface",
                default_value=player.hidden_by_default,
                category="advanced",
            ),
            # add entry to expose player to HA
            ConfigEntry(
                key=CONF_EXPOSE_PLAYER_TO_HA,
                type=ConfigEntryType.BOOLEAN,
                label="Expose this player to Home Assistant",
                description="Expose this player to the Home Assistant integration. \n"
                "If disabled, this player will not be imported into Home Assistant.",
                category="advanced",
                default_value=player.expose_to_ha_by_default,
            ),
        ]
        # group-player config entries
        if player.type == PlayerType.GROUP:
            entries += [
                CONF_ENTRY_PLAYER_ICON_GROUP,
            ]
            return entries
        # normal player (or stereo pair) config entries
        entries += [
            CONF_ENTRY_PLAYER_ICON,
            # add default entries for announce feature
            CONF_ENTRY_ANNOUNCE_VOLUME_STRATEGY,
            CONF_ENTRY_ANNOUNCE_VOLUME,
            CONF_ENTRY_ANNOUNCE_VOLUME_MIN,
            CONF_ENTRY_ANNOUNCE_VOLUME_MAX,
        ]
        return entries

    def _create_player_control_config_entries(self, player: Player) -> list[ConfigEntry]:
        """Create config entries for player controls."""
        all_controls = self.mass.players.player_controls()
        power_controls = [x for x in all_controls if x.supports_power]
        volume_controls = [x for x in all_controls if x.supports_volume]
        mute_controls = [x for x in all_controls if x.supports_mute]
        # work out player supported features
        supports_power = PlayerFeature.POWER in player.supported_features
        supports_volume = PlayerFeature.VOLUME_SET in player.supported_features
        supports_mute = PlayerFeature.VOLUME_MUTE in player.supported_features
        # create base options per control type (and add defaults like native and fake)
        base_power_options: list[ConfigValueOption] = [
            ConfigValueOption(title="None", value=PLAYER_CONTROL_NONE),
            ConfigValueOption(title="Fake power control", value=PLAYER_CONTROL_FAKE),
        ]
        if supports_power:
            base_power_options.append(
                ConfigValueOption(title="Native power control", value=PLAYER_CONTROL_NATIVE),
            )
        base_volume_options: list[ConfigValueOption] = [
            ConfigValueOption(title="None", value=PLAYER_CONTROL_NONE),
        ]
        if supports_volume:
            base_volume_options.append(
                ConfigValueOption(title="Native volume control", value=PLAYER_CONTROL_NATIVE),
            )
        base_mute_options: list[ConfigValueOption] = [
            ConfigValueOption(title="None", value=PLAYER_CONTROL_NONE),
            ConfigValueOption(title="Fake mute control", value=PLAYER_CONTROL_FAKE),
        ]
        if supports_mute:
            base_mute_options.append(
                ConfigValueOption(title="Native mute control", value=PLAYER_CONTROL_NATIVE),
            )
        # return final config entries for all options
        return [
            # Power control config entry
            ConfigEntry(
                key=CONF_POWER_CONTROL,
                type=ConfigEntryType.STRING,
                label="Power Control",
                default_value=PLAYER_CONTROL_NATIVE if supports_power else PLAYER_CONTROL_NONE,
                required=True,
                options=[
                    *base_power_options,
                    *(ConfigValueOption(x.name, x.id) for x in power_controls),
                ],
                category="player_controls",
                hidden=player.type == PlayerType.GROUP,
            ),
            # Volume control config entry
            ConfigEntry(
                key=CONF_VOLUME_CONTROL,
                type=ConfigEntryType.STRING,
                label="Volume Control",
                default_value=PLAYER_CONTROL_NATIVE if supports_volume else PLAYER_CONTROL_NONE,
                required=True,
                options=[
                    *base_volume_options,
                    *(ConfigValueOption(x.name, x.id) for x in volume_controls),
                ],
                category="player_controls",
                hidden=player.type == PlayerType.GROUP,
            ),
            # Mute control config entry
            ConfigEntry(
                key=CONF_MUTE_CONTROL,
                type=ConfigEntryType.STRING,
                label="Mute Control",
                default_value=PLAYER_CONTROL_NATIVE if supports_mute else PLAYER_CONTROL_NONE,
                required=True,
                options=[
                    *base_mute_options,
                    *[ConfigValueOption(x.name, x.id) for x in mute_controls],
                ],
                category="player_controls",
                hidden=player.type == PlayerType.GROUP,
            ),
            # auto-play on power on control config entry
            CONF_ENTRY_AUTO_PLAY,
        ]
