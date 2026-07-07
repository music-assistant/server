"""Logic to handle storage of persistent (configuration) settings."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast, overload
from uuid import uuid4

import aiofiles
import shortuuid
from cryptography.fernet import Fernet, InvalidToken
from music_assistant_models import config_entries
from music_assistant_models.config_entries import (
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
    CONF_ENABLED,
    CONF_ENCRYPTION_KEY,
    CONF_ENCRYPTION_KEY_MIGRATED,
    CONF_ENTRY_ANNOUNCE_VOLUME,
    CONF_ENTRY_ANNOUNCE_VOLUME_MAX,
    CONF_ENTRY_ANNOUNCE_VOLUME_MIN,
    CONF_ENTRY_ANNOUNCE_VOLUME_STRATEGY,
    CONF_ENTRY_AUTO_PLAY,
    CONF_ENTRY_CROSSFADE_DIFFERENT_SAMPLE_RATES,
    CONF_ENTRY_CROSSFADE_DURATION,
    CONF_ENTRY_ENABLE_ICY_METADATA,
    CONF_ENTRY_FLOW_MODE,
    CONF_ENTRY_FLOW_MODE_SAMPLE_RATE,
    CONF_ENTRY_HTTP_PROFILE,
    CONF_ENTRY_LIBRARY_SYNC_ALBUM_TRACKS,
    CONF_ENTRY_LIBRARY_SYNC_ALBUMS,
    CONF_ENTRY_LIBRARY_SYNC_ARTISTS,
    CONF_ENTRY_LIBRARY_SYNC_AUDIOBOOKS,
    CONF_ENTRY_LIBRARY_SYNC_BACK,
    CONF_ENTRY_LIBRARY_SYNC_DELETIONS,
    CONF_ENTRY_LIBRARY_SYNC_PLAYLIST_TRACKS,
    CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS,
    CONF_ENTRY_LIBRARY_SYNC_PODCASTS,
    CONF_ENTRY_LIBRARY_SYNC_RADIOS,
    CONF_ENTRY_LIBRARY_SYNC_TRACKS,
    CONF_ENTRY_MAX_VOLUME,
    CONF_ENTRY_MIN_VOLUME,
    CONF_ENTRY_OUTPUT_CHANNELS,
    CONF_ENTRY_OUTPUT_CODEC,
    CONF_ENTRY_OUTPUT_LIMITER,
    CONF_ENTRY_PLAY_MEDIA_OVERRIDES_GROUP,
    CONF_ENTRY_PLAYER_ICON,
    CONF_ENTRY_PLAYER_ICON_GROUP,
    CONF_ENTRY_SAMPLE_RATES,
    CONF_ENTRY_TTS_PRE_ANNOUNCE,
    CONF_ENTRY_VOLUME_NORMALIZATION,
    CONF_ENTRY_VOLUME_NORMALIZATION_TARGET,
    CONF_EXPOSE_PLAYER_TO_HA,
    CONF_HIDE_IN_UI,
    CONF_LINKED_PROTOCOL_IDS,
    CONF_MUTE_CONTROL,
    CONF_ONBOARD_DONE,
    CONF_PLAYER_DSP,
    CONF_PLAYER_DSP_PRESETS,
    CONF_PLAYERS,
    CONF_POWER_CONTROL,
    CONF_PRE_ANNOUNCE_CHIME_URL,
    CONF_PREFERRED_OUTPUT_PROTOCOL,
    CONF_PROTOCOL_CATEGORY_PREFIX,
    CONF_PROTOCOL_KEY_SPLITTER,
    CONF_PROTOCOL_PARENT_ID,
    CONF_PROVIDERS,
    CONF_SERVER_ID,
    CONF_SMART_FADES_MODE,
    CONF_VOLUME_CONTROL,
    CONFIGURABLE_CORE_CONTROLLERS,
    DEFAULT_CORE_CONFIG_ENTRIES,
    DEFAULT_PROVIDER_CONFIG_ENTRIES,
    ENCRYPT_SUFFIX,
    NON_HTTP_PROVIDERS,
    PLAYER_CONTROL_PROTOCOL,
)
from music_assistant.controllers.streams.constants import (
    CONF_BUFFER_SIZE,
    CONF_BUFFER_SIZE_DEFAULT,
    BufferSize,
)
from music_assistant.helpers.api import api_command
from music_assistant.helpers.json import (
    JSON_DECODE_EXCEPTIONS,
    async_json_dumps,
    async_json_loads,
    json_loads,
)
from music_assistant.helpers.util import load_provider_module, validate_announcement_chime_url
from music_assistant.models import ProviderModuleType
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.sync_group.constants import SGP_PREFIX
from music_assistant.providers.universal_group.constants import UGP_PREFIX

if TYPE_CHECKING:
    from music_assistant import MusicAssistant
    from music_assistant.models.core_controller import CoreController
    from music_assistant.models.player import Player

LOGGER = logging.getLogger(__name__)
DEFAULT_SAVE_DELAY = 5

BASE_KEYS = ("enabled", "name", "available", "default_name", "provider", "type")

# TypeVar for config value type inference
_ConfigValueT = TypeVar("_ConfigValueT", bound=ConfigValueType)


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
        self._save_lock = asyncio.Lock()

    async def setup(self) -> None:
        """Async initialize of controller."""
        await self._load()
        self.initialized = True
        # create default server ID if needed
        self.set_default(CONF_SERVER_ID, uuid4().hex)
        self._init_encryption()
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
    async def get_provider_config_entries(
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
            if provider and isinstance(provider, MusicProvider) and provider.is_streaming_provider:
                extra_entries.append(CONF_ENTRY_LIBRARY_SYNC_DELETIONS)

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
            for key, player_conf in list(self.get(CONF_PLAYERS, {}).items()):
                if not isinstance(player_conf, dict):
                    continue
                if player_conf.get("provider") == instance_id:
                    self.remove(f"{CONF_PLAYERS}/{player_conf.get('player_id') or key}")

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

    def update_provider_last_error(self, instance_id: str, error: str | None) -> None:
        """
        Persist (or clear) a provider's last_error.

        Only writes if the provider config still exists; this avoids re-creating a
        config entry that was removed while a load was still in flight, which would
        leave a stub entry without a domain. See #5728.
        """
        conf_key = f"{CONF_PROVIDERS}/{instance_id}"
        if not self.get(conf_key):
            return
        self.set(f"{conf_key}/last_error", error)

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
        for key, raw_conf in list(self.get(CONF_PLAYERS, {}).items()):
            # guard against malformed entries that lost their base keys
            # (can happen via race between delete_player_config and a stale player
            # update writing back a nested sub-key, which recreates a partial dict).
            if not isinstance(raw_conf, dict) or "player_id" not in raw_conf:
                LOGGER.warning("Removing malformed player config entry %s (missing player_id)", key)
                self.remove(f"{CONF_PLAYERS}/{key}")
                continue
            # optional provider filter
            if provider is not None and raw_conf.get("provider") != provider:
                continue
            # filter out unavailable players
            # (unless disabled, otherwise there is no way to re-enable them)
            # note that we only check for missing players in the player controller,
            # and we do allow players that are temporary unavailable
            # (player.state.available = false) because this can also mean that the
            # player needs additional configuration such as airplay devices that need pairing.
            player = self.mass.players.get_player(raw_conf["player_id"], False)
            if not include_unavailable and player is None and raw_conf.get("enabled", True):
                continue
            # filter out protocol players
            # their configuration is handled differently as part of their parent player
            if raw_conf.get("player_type") == PlayerType.PROTOCOL or (
                player and player.state.type == PlayerType.PROTOCOL
            ):
                continue
            # filter out disabled players
            if not include_disabled and not raw_conf.get("enabled", True):
                continue
            if include_values:
                result.append(await self.get_player_config(raw_conf["player_id"]))
            else:
                raw_conf["default_name"] = (
                    player.state.name if player else raw_conf.get("default_name")
                )
                raw_conf["available"] = player.state.available if player else False
                result.append(cast("PlayerConfig", PlayerConfig.parse([], raw_conf)))
        return result

    @api_command("config/players/get")
    async def get_player_config(
        self,
        player_id: str,
    ) -> PlayerConfig:
        """Return (full) configuration for a single player."""
        raw_conf: dict[str, Any]
        if raw_conf := self.get(f"{CONF_PLAYERS}/{player_id}"):
            raw_conf = deepcopy(raw_conf)
            # protocol-prefixed entries are virtual mirrors of the linked protocol player's own
            # config (the canonical store). Drop any that linger in this player's persisted values
            # so a stale copy can never shadow the protocol player's current value; the live values
            # are merged back in from the protocol player(s) below.
            if stored_values := raw_conf.get("values"):
                for key in [key for key in stored_values if CONF_PROTOCOL_KEY_SPLITTER in key]:
                    del stored_values[key]
            if player := self.mass.players.get_player(player_id, False):
                raw_conf["default_name"] = player.state.name
                raw_conf["provider"] = player.provider.instance_id
                config_entries = await self.get_player_config_entries(
                    player_id,
                )
                # also grab (raw) values for protocol outputs
                if protocol_values := await self._get_output_protocol_config_values(config_entries):
                    if "values" not in raw_conf:
                        raw_conf["values"] = {}
                    raw_conf["values"].update(protocol_values)
            else:
                # handle unavailable player and/or provider
                config_entries = []
                raw_conf["available"] = False
                raw_conf["default_name"] = (
                    raw_conf.get("default_name") or raw_conf.get("player_id") or player_id
                )
                raw_conf.setdefault("player_id", player_id)

            return cast("PlayerConfig", PlayerConfig.parse(config_entries, raw_conf))
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
        if not (player := self.mass.players.get_player(player_id, False)):
            msg = f"Player {player_id} not found"
            raise KeyError(msg)

        default_entries: list[ConfigEntry]
        player_entries: list[ConfigEntry]
        if player.state.type == PlayerType.PROTOCOL:
            default_entries = []
            player_entries = await self._get_player_config_entries(
                player, action=action, values=values
            )
        else:
            # get default entries which are common for all (non protocol)players
            default_entries = self._get_default_player_config_entries(player)

            # get player(protocol) specific entries
            # this basically injects virtual config entries for each protocol output
            # this feels maybe a bit of a hack to do it this way but it keeps the UI logic simple
            # and maximizes api client compatibility because you can configure the whole player
            # including its protocols from a single config endpoint without needing special handling
            # for protocol players in the UI/api clients
            if protocol_entries := await self._create_output_protocol_config_entries(
                player, action=action, values=values
            ):
                player_entries = protocol_entries
            else:
                player_entries = await self._get_player_config_entries(
                    player, action=action, values=values
                )

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
        values = await self._update_output_protocol_config(values)
        config = await self.get_player_config(player_id)
        changed_keys = config.update(values)
        if not changed_keys:
            # no changes
            return config
        # store updated config first (to prevent issues with enabling/disabling players)
        conf_key = f"{CONF_PLAYERS}/{player_id}"
        # Get existing raw config to preserve values that don't have config entries.
        # e.g. protocol links etc.
        existing_raw = self.get(conf_key) or {}
        existing_values = existing_raw.get("values", {})
        new_raw = config.to_raw()
        new_values = new_raw.get("values", {})
        # Preserve values from storage that don't have config entries in current context.
        config_entry_keys = set(config.values.keys())
        for key, value in existing_values.items():
            if key not in new_values and key not in config_entry_keys:
                new_values[key] = value
        # never persist protocol-prefixed (virtual) entries on this player; the linked protocol
        # player is the canonical store (handled by _update_output_protocol_config). Storing a copy
        # here would shadow the protocol player's value once it is reset back to its default.
        new_values = {
            key: value for key, value in new_values.items() if CONF_PROTOCOL_KEY_SPLITTER not in key
        }
        new_raw["values"] = new_values
        self.set(conf_key, new_raw)
        try:
            # validate/handle the update in the player manager
            await self.mass.players.on_player_config_change(config, changed_keys)
        except Exception:
            # rollback on error - use existing_raw to preserve all values
            self.set(conf_key, existing_raw)
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
        if self.mass.players.get_player(player_id):
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
        # skip if the player config root no longer exists, otherwise the
        # nested set would resurrect a partial entry (missing player_id etc).
        if not self.get(f"{CONF_PLAYERS}/{player_id}"):
            return
        conf_key = f"{CONF_PLAYERS}/{player_id}/default_name"
        self.set(conf_key, default_name)

    def set_player_type(self, player_id: str, player_type: PlayerType) -> None:
        """Set (or update) the type for a player."""
        # skip if the player config root no longer exists, otherwise the
        # nested set would resurrect a partial entry (missing player_id etc).
        if not self.get(f"{CONF_PLAYERS}/{player_id}"):
            return
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

        old_dsp_enabled = self.get_player_dsp_config(player_id).enabled
        # Save and apply the new config to the player
        self.set(f"{CONF_PLAYER_DSP}/{player_id}", config.to_dict())
        if old_dsp_enabled or config.enabled:
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

        This is meant as helper to create default configs for builtin/default providers.
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
        raw_conf = self.get(f"{CONF_CORE}/{domain}", {})
        if not isinstance(raw_conf, dict):
            raw_conf = {}
        if "domain" not in raw_conf:
            raw_conf = {**raw_conf, "domain": domain}
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

    def _init_encryption(self) -> None:
        """Set up encryption for SECURE_STRING config values."""
        self._fernet = self._load_or_create_encryption_key()
        if not self.get(CONF_ENCRYPTION_KEY_MIGRATED):
            self._migrate_legacy_secrets()
            self.set(CONF_ENCRYPTION_KEY_MIGRATED, True)

    def _load_or_create_encryption_key(self) -> Fernet:
        """Return the stored encryption key, generating a new one if it is absent or invalid."""
        encryption_key: Any = self.get(CONF_ENCRYPTION_KEY, "")
        if isinstance(encryption_key, str) and encryption_key:
            try:
                return Fernet(encryption_key.encode())
            except ValueError:
                LOGGER.warning("Stored encryption key is invalid; generating a new one")
                self.set(CONF_ENCRYPTION_KEY_MIGRATED, False)
        encryption_key = Fernet.generate_key().decode()
        self.set(CONF_ENCRYPTION_KEY, encryption_key)
        return Fernet(encryption_key.encode())

    def _migrate_legacy_secrets(self) -> None:
        """One-time re-encryption of secrets that were encrypted with the server_id-derived key."""
        server_id: str = self.get(CONF_SERVER_ID)
        assert server_id
        legacy_fernet = Fernet(base64.urlsafe_b64encode(server_id.encode()[:32]))
        migrated = self._rotate_encrypted_values(self._data, legacy_fernet)
        if migrated:
            LOGGER.info("Re-encrypted %s secret(s) with the dedicated encryption key", migrated)
            self.save(immediate=True)

    def _rotate_encrypted_values(self, node: Any, legacy_fernet: Fernet) -> int:
        """Recursively re-encrypt legacy-encrypted values, returning the count."""
        assert self._fernet is not None
        count = 0
        values = node.items() if isinstance(node, dict) else enumerate(node)
        for key, value in values:
            if isinstance(value, (dict, list)):
                count += self._rotate_encrypted_values(value, legacy_fernet)
            elif isinstance(value, str) and value.startswith(ENCRYPT_SUFFIX):
                token = value[len(ENCRYPT_SUFFIX) :].encode()
                try:
                    decrypted = legacy_fernet.decrypt(token)
                except InvalidToken:
                    continue
                node[key] = ENCRYPT_SUFFIX + self._fernet.encrypt(decrypted).decode()
                count += 1
        return count

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

    async def _migrate(self) -> None:
        changed = False

        # The background tasks controller originally persisted runtime state directly under
        # core/tasks, which could create a CoreConfig object without the required domain field.
        # Repair that single known corruption case on load.
        # TODO: remove after 2.9 release
        tasks_core_config = self._data.get(CONF_CORE, {}).get("tasks")
        if isinstance(tasks_core_config, dict) and "domain" not in tasks_core_config:
            tasks_core_config["domain"] = "tasks"
            LOGGER.warning("Repaired corrupt tasks core configuration")
            changed = True

        # Collapse legacy multi-instance Fully Kiosk provider configs into a single
        # provider instance with a list of devices (matching the MPD provider pattern).
        # TODO: remove after 2.10 release
        if self._migrate_fully_kiosk_multi_instance():
            changed = True
        # Migrate default_enqueue_option_radio -> default_enqueue_option_live_sources.
        # The same setting now covers both radio stations and plugin AudioSources
        # (Spotify Connect, AirPlay receiver, etc.); preserves the user's customised
        # value if they set one.
        # TODO: remove after 2.10 release
        player_queues_cfg = self._data.get(CONF_CORE, {}).get("player_queues")
        if isinstance(player_queues_cfg, dict):
            values = player_queues_cfg.get("values")
            if isinstance(values, dict) and "default_enqueue_option_radio" in values:
                radio_value = values.pop("default_enqueue_option_radio")
                values.setdefault("default_enqueue_option_live_sources", radio_value)
                LOGGER.info(
                    "Migrated default_enqueue_option_radio -> default_enqueue_option_live_sources"
                )
                changed = True

        # Migrate sync_group members_filter (exclusion) -> allowed_members (inclusion).
        # Inversion freezes the universe at migration time; speakers added after this
        # point must be added by the user explicitly, which matches the new design's
        # "limit to these" intent.
        # TODO: remove after 2.10 release
        all_player_configs = self._data.get(CONF_PLAYERS, {})
        if isinstance(all_player_configs, dict):
            group_provider_domains = {"sync_group", "universal_group"}
            universe = {
                pid
                for pid, cfg in all_player_configs.items()
                if isinstance(cfg, dict) and cfg.get("provider") not in group_provider_domains
            }
            for player_id, player_cfg in all_player_configs.items():
                if not isinstance(player_cfg, dict):
                    continue
                if player_cfg.get("provider") != "sync_group":
                    continue
                values = player_cfg.setdefault("values", {})
                old_exclude = values.get("members_filter") or []
                if not old_exclude or values.get("allowed_members") is not None:
                    continue
                values["allowed_members"] = sorted(universe - set(old_exclude))
                values["members_filter"] = []
                LOGGER.info(
                    "Migrated sync_group %s: members_filter (exclusion) "
                    "-> allowed_members (inclusion)",
                    player_id,
                )
                changed = True

        # Drop orphaned provider config stubs: a load failure could write last_error back to a
        # provider key whose config had already been removed (e.g. removing an unsupported
        # provider while a load/retry was still in flight), leaving an entry with only a
        # last_error and no 'domain'. Such stubs crash get_provider_configs on startup.
        # TODO: remove after 2.11 release
        if self._migrate_orphaned_provider_stubs():
            changed = True

        # Clear self-referential protocol links: a player whose protocol_parent_id or
        # linked_protocol_ids pointed at its own id was hidden as its own protocol child.
        # TODO: remove after 2.10 release
        if self._migrate_self_referential_protocol_links():
            changed = True

        # Drop the persisted schedule for the metadata maintenance tasks that were hardcoded
        # to run at 04:00 local. They are now registered under new ("_v2") task ids with a
        # randomized full-day schedule (to avoid spiking the shared MusicBrainz mirror), so the
        # old persisted state is orphaned and can be removed.
        # TODO: remove after 2.9 release
        if self._migrate_metadata_maintenance_schedule():
            changed = True

        if changed:
            await self._async_save()

    def _migrate_orphaned_provider_stubs(self) -> bool:
        """Remove provider config stubs left without a 'domain' key (see #5728)."""
        providers = self._data.get(CONF_PROVIDERS, {})
        if not isinstance(providers, dict):
            return False
        orphaned = [
            instance_id
            for instance_id, cfg in providers.items()
            if isinstance(cfg, dict) and "domain" not in cfg
        ]
        for instance_id in orphaned:
            del providers[instance_id]
            LOGGER.warning("Removed orphaned provider config stub %s", instance_id)
        return bool(orphaned)

    def _migrate_self_referential_protocol_links(self) -> bool:
        """Clear protocol links that point a player at its own id."""
        all_player_configs = self._data.get(CONF_PLAYERS, {})
        if not isinstance(all_player_configs, dict):
            return False
        changed = False
        for player_id, player_cfg in all_player_configs.items():
            if not isinstance(player_cfg, dict):
                continue
            values = player_cfg.get("values")
            if not isinstance(values, dict):
                continue
            repaired = False
            if values.get(CONF_PROTOCOL_PARENT_ID) == player_id:
                values[CONF_PROTOCOL_PARENT_ID] = None
                repaired = True
            linked = values.get(CONF_LINKED_PROTOCOL_IDS)
            if isinstance(linked, list) and player_id in linked:
                values[CONF_LINKED_PROTOCOL_IDS] = [pid for pid in linked if pid != player_id]
                repaired = True
            if repaired:
                LOGGER.warning("Repaired self-referential protocol link for %s", player_id)
                changed = True
        return changed

    def _migrate_metadata_maintenance_schedule(self) -> bool:
        """Remove the orphaned persisted state for the pre-randomization metadata task ids."""
        core_config = self._data.get(CONF_CORE)
        if not isinstance(core_config, dict):
            return False
        tasks_config = core_config.get("tasks")
        if not isinstance(tasks_config, dict):
            return False
        task_states = tasks_config.get("scheduled_task_states")
        if not isinstance(task_states, dict):
            return False
        legacy_task_ids = (
            "metadata_missing_artist_metadata_scan",
            "metadata_playlist_metadata_scan",
            "metadata_thumb_cache_cleanup",
        )
        removed = [task_id for task_id in legacy_task_ids if task_id in task_states]
        for task_id in removed:
            del task_states[task_id]
        if removed:
            LOGGER.info("Removed orphaned metadata maintenance schedule state for %s", removed)
        return bool(removed)

    def _migrate_fully_kiosk_multi_instance(self) -> bool:
        """Collapse legacy multi-instance Fully Kiosk configs into a single provider instance."""
        providers = self._data.get(CONF_PROVIDERS, {})
        legacy_ids = [
            iid
            for iid, conf in providers.items()
            if isinstance(conf, dict)
            and conf.get("domain") == "fully_kiosk"
            and iid != "fully_kiosk"
        ]
        if not legacy_ids:
            return False

        ip_entries: list[str] = []
        players = self._data.setdefault(CONF_PLAYERS, {})
        for iid in legacy_ids:
            old_values = providers[iid].get("values") or {}
            host = old_values.get("ip_address")
            if not host:
                del providers[iid]
                continue
            try:
                port = int(old_values.get("port") or 2323)
            except (TypeError, ValueError):
                port = 2323
            entry = host if port == 2323 else f"{host}:{port}"
            if entry not in ip_entries:
                ip_entries.append(entry)

            new_player_id = f"fully_kiosk_{host}_{port}"
            player_conf = players.setdefault(
                new_player_id,
                {
                    "player_id": new_player_id,
                    "provider": "fully_kiosk",
                    "enabled": True,
                    "values": {},
                },
            )
            player_values = player_conf.setdefault("values", {})
            for key in ("password", "use_ssl", "verify_ssl", "ssl_fingerprint"):
                if old_values.get(key) is not None and key not in player_values:
                    player_values[key] = old_values[key]

            del providers[iid]

        if "fully_kiosk" in providers:
            existing_values = providers["fully_kiosk"].setdefault("values", {})
            existing_ips = list(existing_values.get("manual_discovery_ip_addresses") or [])
            for entry in ip_entries:
                if entry not in existing_ips:
                    existing_ips.append(entry)
            existing_values["manual_discovery_ip_addresses"] = existing_ips
        else:
            providers["fully_kiosk"] = {
                "type": "player",
                "domain": "fully_kiosk",
                "instance_id": "fully_kiosk",
                "enabled": True,
                "values": {"manual_discovery_ip_addresses": ip_entries},
            }

        LOGGER.warning(
            "Migrated %d legacy Fully Kiosk provider instance(s) into a single instance. "
            "Devices and their passwords have been preserved, but any Fully Kiosk player "
            "that was part of a universal group will need to be re-added to it. ",
            len(legacy_ids),
        )
        return True

    async def _async_save(self) -> None:
        """Save persistent data to disk."""
        async with self._save_lock:
            json_data = await async_json_dumps(self._data, indent=True)
            await asyncio.to_thread(self._save_to_disk, json_data)
        LOGGER.debug("Saved data to persistent storage")

    def _save_to_disk(self, json_data: str) -> None:
        """Atomically write the settings file to disk, rotating the previous one to backup."""
        filename = Path(self.filename)
        filename_temp = Path(f"{self.filename}.tmp")
        with filename_temp.open("w", encoding="utf-8") as _file:
            _file.write(json_data)
            _file.flush()
            # fsync so a power failure can not leave a zero-length file behind (#5716)
            os.fsync(_file.fileno())
        with contextlib.suppress(FileNotFoundError, *JSON_DECODE_EXCEPTIONS):
            # only rotate a parseable file to the backup, so a corrupt
            # (crash leftover) file can never clobber a possibly good backup
            json_loads(filename.read_bytes())
            filename.replace(f"{self.filename}.backup")
        filename_temp.replace(filename)
        # best effort: fsync the directory as well so the renames themselves
        # survive a power failure (not supported on all platforms/filesystems)
        with contextlib.suppress(OSError):
            dir_fd = os.open(os.path.dirname(self.filename), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

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
        if config.enabled and prov_instance and available:
            # update config for existing/loaded provider instance
            await prov_instance.update_config(config, changed_keys)
            # push instance name to config (to persist it if it was autogenerated)
            if prov_instance.default_name != config.default_name:
                self.set_provider_default_name(
                    prov_instance.instance_id, prov_instance.default_name
                )
            if "name" in changed_keys:
                # signal providers updated so frontends refresh the provider name
                self.mass.signal_event(EventType.PROVIDERS_UPDATED, data=self.mass.get_providers())
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
        if prov.depends_on:
            dep_configs = await self.get_provider_configs(provider_domain=prov.depends_on)
            if not any(dep_conf.enabled for dep_conf in dep_configs):
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
            self.mass.music.queue_provider_mapping_correction_task()
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
        is_dedicated_group_player = player.state.type in (
            PlayerType.GROUP,
            PlayerType.STEREO_PAIR,
        ) and not player.player_id.startswith((UGP_PREFIX, SGP_PREFIX))
        is_http_based_player_protocol = player.provider.domain not in NON_HTTP_PROVIDERS
        if player.state.type == PlayerType.GROUP and not is_dedicated_group_player:
            # no audio related entries for universal group players or sync group players
            default_entries = []
        elif PlayerFeature.PLAY_MEDIA not in player.supported_features:
            # no audio related entries for players that do not support play_media
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
                    CONF_ENTRY_OUTPUT_CODEC,
                    CONF_ENTRY_HTTP_PROFILE,
                    CONF_ENTRY_ENABLE_ICY_METADATA,
                ]
                # only inject the sample-rates config when the player can't declare its rates itself
                if not player.declares_supported_sample_rates:
                    default_entries.append(CONF_ENTRY_SAMPLE_RATES)
                # add flow mode entry for http-based players that do not already enforce it
                if not player.requires_flow_mode:
                    default_entries.append(CONF_ENTRY_FLOW_MODE)
                default_entries.append(CONF_ENTRY_FLOW_MODE_SAMPLE_RATE)
        if PlayerFeature.GAPLESS_PLAYBACK in player.supported_features:
            default_entries.append(CONF_ENTRY_CROSSFADE_DIFFERENT_SAMPLE_RATES)
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
        if player.state.type == PlayerType.PROTOCOL:
            # protocol players have no generic config entries
            # only audio/protocol specific ones
            return []

        # some base entries for all player types
        # note that these may NOT be playback/audio related
        buffer_size = self.get_raw_core_config_value(
            "streams", CONF_BUFFER_SIZE, CONF_BUFFER_SIZE_DEFAULT
        )
        # smart crossfade needs a larger buffer for beat analysis
        smart_fades_options = [
            ConfigValueOption("Disabled", "disabled"),
            ConfigValueOption("Standard Crossfade", "standard_crossfade"),
        ]
        if buffer_size != BufferSize.MINIMAL:
            smart_fades_options.insert(1, ConfigValueOption("Smart Crossfade", "smart_crossfade"))

        entries.append(
            ConfigEntry(
                key=CONF_SMART_FADES_MODE,
                type=ConfigEntryType.STRING,
                label="Enable Smart Fades",
                options=smart_fades_options,
                default_value="disabled",
                description="Select the crossfade mode to use when transitioning "
                "between tracks.\n\n"
                "- 'Smart Crossfade': Uses beat matching and EQ filters to create "
                "smooth transitions between tracks.\n"
                "- 'Standard Crossfade': Regular crossfade that crossfades the "
                "last/first x-seconds of a track.",
                category="playback",
                requires_reload=True,
            )
        )
        if buffer_size == BufferSize.MINIMAL:
            entries.append(
                ConfigEntry(
                    key="smart_crossfade_unavailable",
                    type=ConfigEntryType.ALERT,
                    label="Smart Crossfade is unavailable because this system has limited "
                    "memory. It requires more RAM than is currently available for audio "
                    "buffering.",
                    category="playback",
                    required=False,
                )
            )

        entries += [
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
                description="Hide this player from the main players list and dashboard selection "
                "menus.  The player remains fully controllable and still appears in any sync group "
                "it currently belongs to and in the settings. Disable the player to exclude "
                "it everywhere.",
                default_value=player.hidden_by_default,
                category="generic",
                advanced=False,
            ),
            # add entry to expose player to HA
            ConfigEntry(
                key=CONF_EXPOSE_PLAYER_TO_HA,
                type=ConfigEntryType.BOOLEAN,
                label="Expose this player to Home Assistant",
                description="Expose this player to the Home Assistant integration. \n"
                "If disabled, this player will not be imported into Home Assistant.",
                category="generic",
                advanced=False,
                default_value=player.expose_to_ha_by_default,
            ),
        ]
        # group-player config entries
        if player.state.type == PlayerType.GROUP:
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
            # play_media-on-self preference (only relevant to non-group players)
            CONF_ENTRY_PLAY_MEDIA_OVERRIDES_GROUP,
        ]
        return entries

    def _create_player_control_config_entries(self, player: Player) -> list[ConfigEntry]:
        """Create config entries for player controls."""
        is_group = player.state.type == PlayerType.GROUP
        all_controls = self.mass.players.player_controls()
        power_controls = [x for x in all_controls if x.supports_power]
        volume_controls = [x for x in all_controls if x.supports_volume]
        mute_controls = [x for x in all_controls if x.supports_mute]
        auto_option = ConfigValueOption(
            title="Auto-select (based on active/preferred protocol)", value=PLAYER_CONTROL_PROTOCOL
        )
        # work out player supported features
        power_options: list[ConfigValueOption] = []
        if player.supports_feature(PlayerFeature.POWER):
            power_options.append(
                ConfigValueOption(title="Native power control", value=PLAYER_CONTROL_NATIVE),
            )
        volume_options: list[ConfigValueOption] = []
        has_native_volume_control = False
        if player.supports_feature(PlayerFeature.VOLUME_SET):
            has_native_volume_control = True
            volume_options.append(
                ConfigValueOption(title="Native volume control", value=PLAYER_CONTROL_NATIVE),
            )
        mute_options: list[ConfigValueOption] = []
        if player.supports_feature(PlayerFeature.VOLUME_MUTE):
            mute_options.append(
                ConfigValueOption(title="Native mute control", value=PLAYER_CONTROL_NATIVE),
            )
        # add player protocols as volume controls if native player has no volume control
        for linked_protocol in player.linked_output_protocols:
            if has_native_volume_control:
                break
            protocol_player = self.mass.players.get_player(linked_protocol.output_protocol_id)
            if not protocol_player or not protocol_player.available:
                continue
            if protocol_player.supports_feature(PlayerFeature.VOLUME_SET):
                if auto_option not in volume_options:
                    volume_options.append(auto_option)
                if linked_protocol.protocol_domain in ("chromecast", "dlna"):
                    # for chromecast/dlna we can use the protocol player for volume control
                    # even if the protocol player is not the active protocol
                    volume_options.append(
                        ConfigValueOption(
                            title=protocol_player.provider.name,
                            value=protocol_player.player_id,
                        )
                    )
            if protocol_player.supports_feature(PlayerFeature.VOLUME_MUTE):
                if auto_option not in mute_options:
                    mute_options.append(auto_option)
                if linked_protocol.protocol_domain in ("chromecast", "dlna"):
                    # for chromecast/dlna we can use the protocol player for volume control
                    # even if the protocol player is not the active protocol
                    mute_options.append(
                        ConfigValueOption(
                            title=protocol_player.provider.name,
                            value=protocol_player.player_id,
                        )
                    )

        # append none+fake options
        power_options += [
            ConfigValueOption(title="None", value=PLAYER_CONTROL_NONE),
            ConfigValueOption(title="Fake power control", value=PLAYER_CONTROL_FAKE),
        ]
        volume_options += [
            ConfigValueOption(title="None", value=PLAYER_CONTROL_NONE),
        ]
        mute_options.append(ConfigValueOption(title="None", value=PLAYER_CONTROL_NONE))
        if player.supports_feature(PlayerFeature.VOLUME_SET):
            mute_options.append(
                ConfigValueOption(title="Fake mute control", value=PLAYER_CONTROL_FAKE)
            )

        # return final config entries for all options
        return [
            # Power control config entry
            ConfigEntry(
                key=CONF_POWER_CONTROL,
                type=ConfigEntryType.STRING,
                label="Power Control",
                default_value=power_options[0].value if power_options else PLAYER_CONTROL_NONE,
                required=False,
                options=[
                    *power_options,
                    *(ConfigValueOption(x.name, x.id) for x in power_controls),
                ],
                category="player_controls",
            ),
            # Volume control config entry
            ConfigEntry(
                key=CONF_VOLUME_CONTROL,
                type=ConfigEntryType.STRING,
                label="Volume Control",
                default_value=volume_options[0].value if volume_options else PLAYER_CONTROL_NONE,
                required=True,
                options=[
                    *volume_options,
                    *(ConfigValueOption(x.name, x.id) for x in volume_controls),
                ],
                category="player_controls",
            ),
            # Mute control config entry
            ConfigEntry(
                key=CONF_MUTE_CONTROL,
                type=ConfigEntryType.STRING,
                label="Mute Control",
                default_value=mute_options[0].value if mute_options else PLAYER_CONTROL_NONE,
                required=True,
                options=[
                    *mute_options,
                    *[ConfigValueOption(x.name, x.id) for x in mute_controls],
                ],
                category="player_controls",
            ),
            # Volume limit entries
            CONF_ENTRY_MIN_VOLUME,
            CONF_ENTRY_MAX_VOLUME,
            # auto-play on power on — only meaningful for individual players.
            # For group players, power on/off is purely a "capture members"
            # toggle (Fake control) and auto-starting playback there causes
            # surprise playback when the user just wanted to pin the group.
            *([] if is_group else [CONF_ENTRY_AUTO_PLAY]),
        ]

    async def _create_output_protocol_config_entries(  # noqa: PLR0915
        self,
        player: Player,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """
        Create config entry for preferred output protocol.

        Returns empty list if there are no output protocol options (native only or no protocols).
        The player.output_protocols property includes native, active, and disabled protocols,
        with the available flag indicating their status.
        """
        all_entries: list[ConfigEntry] = []
        output_protocols = player.output_protocols

        # Build options from available output protocols, sorted by priority
        options: list[ConfigValueOption] = []

        # Add each available output protocol as an option, sorted by priority
        has_native = False
        for protocol in sorted(output_protocols, key=lambda p: p.priority):
            if provider_manifest := self.mass.get_provider_manifest(protocol.protocol_domain):
                protocol_name = provider_manifest.name
            else:
                protocol_name = protocol.protocol_domain.upper()
            if protocol.available:
                # Use "native" for native playback,
                # otherwise use the protocol output id (=player id)
                title = f"{protocol_name} (native)" if protocol.is_native else protocol_name
                value = "native" if protocol.is_native else protocol.output_protocol_id
                options.append(ConfigValueOption(title=title, value=value))
                has_native = has_native or protocol.is_native

        if has_native:
            default_value = "native"
        else:
            default_value = "auto"
            options.append(ConfigValueOption(title="Auto-select", value="auto"))

        all_entries.append(
            ConfigEntry(
                key=CONF_PREFERRED_OUTPUT_PROTOCOL,
                type=ConfigEntryType.STRING,
                label="Preferred Output Protocol",
                description="Select the preferred protocol for audio playback to this device.",
                default_value=default_value,
                required=True,
                options=options,
                category="protocol_general",
                requires_reload=False,
                hidden=len(output_protocols) <= 1,
            )
        )

        # Add config entries for all protocol players/outputs
        for protocol in output_protocols:
            domain = protocol.protocol_domain
            if provider_manifest := self.mass.get_provider_manifest(protocol.protocol_domain):
                protocol_name = provider_manifest.name
            else:
                protocol_name = protocol.protocol_domain.upper()
            protocol_player_enabled = self.get_raw_player_config_value(
                protocol.output_protocol_id, CONF_ENABLED, True
            )
            provider_available = self.mass.get_provider(protocol.protocol_domain) is not None
            if not provider_available:
                # protocol provider is not available, skip adding entries
                continue
            protocol_prefix = f"{protocol.output_protocol_id}{CONF_PROTOCOL_KEY_SPLITTER}"
            protocol_enabled_key = f"{protocol_prefix}enabled"
            protocol_category = f"{CONF_PROTOCOL_CATEGORY_PREFIX}_{domain}"
            category_translation_key = "settings.category.protocol_output_settings"
            if not protocol.is_native:
                all_entries.append(
                    ConfigEntry(
                        key=protocol_enabled_key,
                        type=ConfigEntryType.BOOLEAN,
                        label="Enable",
                        description="Enable or disable this output protocol for the player.",
                        value=protocol_player_enabled,
                        default_value=True,
                        category=protocol_category,
                        category_translation_key=category_translation_key,
                        category_translation_params=[protocol_name],
                        requires_reload=False,
                    )
                )
            if protocol.is_native:
                # add protocol-specific entries from native player
                protocol_entries = await self._get_player_config_entries(
                    player, action=action, values=values
                )
                for proto_entry in protocol_entries:
                    # deep copy to avoid mutating shared/constant ConfigEntry objects
                    entry = deepcopy(proto_entry)
                    entry.category = protocol_category
                    entry.category_translation_key = category_translation_key
                    entry.category_translation_params = [protocol_name]
                    all_entries.append(entry)

            elif protocol_player := self.mass.players.get_player(protocol.output_protocol_id):
                # we grab the config entries from the protocol player
                # and then prefix them to avoid key collisions

                if action and protocol_prefix in action:
                    protocol_action = action.replace(protocol_prefix, "")
                else:
                    protocol_action = None
                if values:
                    # extract only relevant values for this protocol player
                    protocol_values = {
                        key.replace(protocol_prefix, ""): val
                        for key, val in values.items()
                        if key.startswith(protocol_prefix)
                    }
                else:
                    protocol_values = None
                protocol_entries = await self._get_player_config_entries(
                    protocol_player, action=protocol_action, values=protocol_values
                )
                for proto_entry in protocol_entries:
                    # deep copy to avoid mutating shared/constant ConfigEntry objects
                    entry = deepcopy(proto_entry)
                    entry.category = protocol_category
                    entry.category_translation_key = category_translation_key
                    entry.category_translation_params = [protocol_name]
                    entry.key = f"{protocol_prefix}{entry.key}"
                    entry.depends_on = None if protocol.is_native else protocol_enabled_key
                    entry.action = f"{protocol_prefix}{entry.action}" if entry.action else None
                    all_entries.append(entry)

        return all_entries

    async def _update_output_protocol_config(
        self, values: dict[str, ConfigValueType]
    ) -> dict[str, ConfigValueType]:
        """
        Update output protocol related config for a player based on config values.

        Returns updated values dict with output protocol related entries removed.
        """
        protocol_values: dict[str, dict[str, ConfigValueType]] = {}
        for key, value in list(values.items()):
            if CONF_PROTOCOL_KEY_SPLITTER not in key:
                continue
            # extract protocol player id and actual key
            protocol_player_id, actual_key = key.split(CONF_PROTOCOL_KEY_SPLITTER)
            if protocol_player_id not in protocol_values:
                protocol_values[protocol_player_id] = {}
            protocol_values[protocol_player_id][actual_key] = value
            # remove from main values dict
            del values[key]
        for protocol_player_id, proto_values in protocol_values.items():
            await self.save_player_config(protocol_player_id, proto_values)
            if proto_values.get(CONF_ENABLED):
                # wait max 10 seconds for protocol to become available
                for _ in range(10):
                    protocol_player = self.mass.players.get_player(protocol_player_id)
                    if protocol_player is not None:
                        break
                    await asyncio.sleep(1)
            # wait max 10 seconds for protocol
        return values

    async def _get_output_protocol_config_values(
        self,
        entries: list[ConfigEntry],
    ) -> dict[str, ConfigValueType]:
        """Extract output protocol related config values for given (parent) player entries."""
        values: dict[str, ConfigValueType] = {}
        for entry in entries:
            if CONF_PROTOCOL_KEY_SPLITTER not in entry.key:
                continue
            protocol_player_id, actual_key = entry.key.split(CONF_PROTOCOL_KEY_SPLITTER)
            stored_value = self.get_raw_player_config_value(protocol_player_id, actual_key)
            if stored_value is None:
                continue
            values[entry.key] = stored_value
        return values
