"""Player configuration handling for the ConfigController."""

from __future__ import annotations

import asyncio
import logging
import time
from copy import deepcopy
from typing import TYPE_CHECKING, Any, Literal, cast, overload

from music_assistant_models.auth import Scope
from music_assistant_models.config_entries import (
    ConfigActionResult,
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
    PlayerConfig,
)
from music_assistant_models.constants import (
    PLAYER_CONTROL_FAKE,
    PLAYER_CONTROL_NATIVE,
    PLAYER_CONTROL_NONE,
)
from music_assistant_models.enums import (
    ConfigEntryType,
    EventType,
    PlayerFeature,
    PlayerType,
    ProtocolRole,
)

from music_assistant.constants import (
    CONF_CREATED_AT,
    CONF_ENABLED,
    CONF_ENTRY_ANNOUNCE_VOLUME,
    CONF_ENTRY_ANNOUNCE_VOLUME_MAX,
    CONF_ENTRY_ANNOUNCE_VOLUME_MIN,
    CONF_ENTRY_ANNOUNCE_VOLUME_STRATEGY,
    CONF_ENTRY_AUTO_PLAY,
    CONF_ENTRY_CROSSFADE_DIFFERENT_SAMPLE_RATES,
    CONF_ENTRY_ENABLE_ICY_METADATA,
    CONF_ENTRY_FLOW_MODE,
    CONF_ENTRY_FLOW_MODE_SAMPLE_RATE,
    CONF_ENTRY_IDLE_POWER_OFF,
    CONF_ENTRY_MAX_VOLUME,
    CONF_ENTRY_MIN_VOLUME,
    CONF_ENTRY_OUTPUT_CHANNELS,
    CONF_ENTRY_OUTPUT_CODEC,
    CONF_ENTRY_PLAY_MEDIA_OVERRIDES_GROUP,
    CONF_ENTRY_PLAYER_ICON,
    CONF_ENTRY_PLAYER_ICON_GROUP,
    CONF_ENTRY_PREFER_WAV_FOR_LIVE_SOURCES,
    CONF_ENTRY_SAMPLE_RATES,
    CONF_ENTRY_TTS_PRE_ANNOUNCE,
    CONF_EXPOSE_PLAYER_TO_HA,
    CONF_HIDE_IN_UI,
    CONF_ICON,
    CONF_MUTE_CONTROL,
    CONF_PLAYER_PROTOCOLS,
    CONF_PLAYERS,
    CONF_PLUGIN_KEY_SPLITTER,
    CONF_POWER_CONTROL,
    CONF_PRE_ANNOUNCE_CHIME_URL,
    CONF_PREFERRED_OUTPUT_PROTOCOL,
    CONF_PROTOCOL_CATEGORY_PREFIX,
    CONF_PROTOCOL_EXPERIMENTAL_NOTE,
    CONF_PROTOCOL_KEY_SPLITTER,
    CONF_PROVIDERS,
    CONF_SESSION_ONLY,
    CONF_UNDERLYING_PLAYER_ID,
    CONF_VOLUME_CONTROL,
    NATIVE_PRIORITY,
    NON_HTTP_PROVIDERS,
    PLAYER_CONTROL_PROTOCOL,
)
from music_assistant.controllers.config.constants import (
    BASE_KEYS,
    PLAYER_VALUE_KEYS,
    _ConfigValueT,
)
from music_assistant.controllers.config.helpers import (
    _mask_encrypted,
    _reject_encrypted_values,
    _with_translation_owner,
)
from music_assistant.helpers.api import api_command
from music_assistant.helpers.config_entries import CONF_CONNECTED_PLAYERS, PLAYBACK_TARGET_TYPES
from music_assistant.helpers.util import validate_announcement_chime_url
from music_assistant.models.player import OWN_PROTOCOL_ROLES, get_device_name
from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.sync_group.constants import SGP_PREFIX
from music_assistant.providers.universal_group.constants import UGP_PREFIX

if TYPE_CHECKING:
    from music_assistant_models.player import OutputProtocol

    from music_assistant import MusicAssistant
    from music_assistant.models.player import Player
    from music_assistant.models.player_protocol import PlayerProtocol


LOGGER = logging.getLogger(__name__)


def _first_enabled_control_value(options: list[ConfigValueOption]) -> ConfigValueType:
    """
    Return the value of the first selectable option, so a disabled option is never the default.

    Player-control selects always list the "native" option (shown disabled when the feature is
    unsupported); this picks the first enabled option as the entry default and falls back to the
    always-present "none" control.

    :param options: The control entry's options, in display order.
    """
    for option in options:
        if not option.disabled:
            return option.value
    return PLAYER_CONTROL_NONE


def _reconcile_player_icon_value(
    submitted_values: dict[str, ConfigValueType],
    stored_values: dict[str, Any],
    new_values: dict[str, ConfigValueType],
) -> bool:
    """Persist explicit icon selections while keeping None as automatic selection."""
    stored_icon = stored_values.get(CONF_ICON)
    has_explicit_icon = isinstance(stored_icon, str) and bool(stored_icon)
    if CONF_ICON not in submitted_values:
        if has_explicit_icon:
            new_values[CONF_ICON] = stored_icon
        else:
            new_values.pop(CONF_ICON, None)
        return False

    submitted_icon = submitted_values[CONF_ICON]
    if isinstance(submitted_icon, str) and submitted_icon:
        new_values[CONF_ICON] = submitted_icon
        return not has_explicit_icon or submitted_icon != stored_icon

    new_values.pop(CONF_ICON, None)
    return has_explicit_icon


def _apply_raw_player_icon_value(
    config: PlayerConfig,
    raw_values: dict[str, Any],
) -> None:
    """Apply the stored icon value to a parsed player config."""
    if icon_entry := config.values.get(CONF_ICON):
        stored_icon = raw_values.get(CONF_ICON)
        icon_entry.value = stored_icon if isinstance(stored_icon, str) and stored_icon else None


class PlayerConfigMixin:
    """Mixin providing player configuration handling for the ConfigController."""

    # Type hints for attributes/methods provided by the class this mixin is used with
    if TYPE_CHECKING:
        mass: MusicAssistant

        def get(self, key: str, default: Any = None) -> Any: ...  # noqa: D102

        def set(self, key: str, value: Any, immediate: bool = False) -> None: ...  # noqa: D102

        def remove(self, key: str) -> None: ...  # noqa: D102

    @api_command("config/players", required_scope=Scope.CONFIG_PLAYERS_READ)
    async def get_player_configs(
        self,
        provider: str | None = None,
        include_values: bool = False,
        include_unavailable: bool = True,
        include_disabled: bool = True,
    ) -> list[PlayerConfig]:
        """Return all known player configurations, optionally filtered by provider id."""
        result: list[PlayerConfig] = []
        stored_ids = list(self.get(CONF_PLAYERS, {}))
        # a player that only exists while its device is connected has no stored config
        # until the user changes something about it, but can be configured all the same
        live_ids = [
            player.player_id
            for player in self.mass.players.iter_players(return_disabled=True)
            if player.player_id not in stored_ids
        ]
        for player_id in (*stored_ids, *live_ids):
            # heal or drop a malformed entry that lost its base keys: a partial dict
            # resurrected by a nested config-set (a stale write-back racing a delete, or
            # a leftover from an older version). player_id/provider are recovered when
            # possible, else the unreconstructable entry is pruned.
            raw_conf = self._get_raw_player_config(player_id)
            if raw_conf is None:
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
            player = self.mass.players.get_player(player_id, False)
            if not include_unavailable and player is None and raw_conf.get("enabled", True):
                continue
            # filter out disabled players
            if not include_disabled and not raw_conf.get("enabled", True):
                continue
            if include_values:
                result.append(await self.get_player_config(player_id))
            else:
                summary_conf = deepcopy(raw_conf)
                summary_conf["default_name"] = (
                    player.state.name if player else summary_conf.get("default_name")
                )
                summary_conf["available"] = player.state.available if player else False
                result.append(cast("PlayerConfig", PlayerConfig.parse([], summary_conf)))
        return result

    @api_command("config/players/get", required_scope=Scope.CONFIG_PLAYERS_READ)
    async def get_player_config(
        self,
        player_id: str,
    ) -> PlayerConfig:
        """Return (full) configuration for a single player."""
        # recover the mandatory base keys (or prune an unreconstructable ghost) up front,
        # so a partial dict left by an older version cannot crash the value stripping or
        # PlayerConfig.parse below, and the repair is persisted for both branches
        if (raw_conf := self._get_raw_player_config(player_id)) is None:
            msg = f"No config found for player id {player_id}"
            raise KeyError(msg)
        player = self.mass.players.get_player(player_id, False)
        # the values of its own protocol are part of the player's own config
        raw_conf["values"] = deepcopy(self._get_stored_player_values(player_id))
        # protocol- and plugin-prefixed entries are virtual mirrors of another config
        # (the protocol resp. the plugin provider is the canonical store).
        # Drop any that linger in this player's persisted values so a stale copy can never
        # shadow the canonical value; the live values are merged back in below.
        if stored_values := raw_conf.get("values"):
            for key in [
                key
                for key in stored_values
                if CONF_PROTOCOL_KEY_SPLITTER in key or CONF_PLUGIN_KEY_SPLITTER in key
            ]:
                del stored_values[key]
        if player:
            raw_conf["default_name"] = player.state.name
            raw_conf["provider"] = player.provider_id
            config_entries = await self.get_player_config_entries(
                player_id,
            )
            # also grab (raw) values for protocol outputs
            if protocol_values := await self._get_output_protocol_config_values(config_entries):
                if "values" not in raw_conf:
                    raw_conf["values"] = {}
                raw_conf["values"].update(protocol_values)
            # ...and the (derived) values for the per-player plugin toggles
            if plugin_values := self._get_plugin_provider_config_values(config_entries):
                if "values" not in raw_conf:
                    raw_conf["values"] = {}
                raw_conf["values"].update(plugin_values)
        else:
            # handle a player that is not there: disabled, or away
            config_entries = []
            raw_conf["available"] = False
            raw_conf["default_name"] = (
                raw_conf.get("default_name") or raw_conf.get("player_id") or player_id
            )

        conf = cast("PlayerConfig", PlayerConfig.parse(config_entries, raw_conf))
        _apply_raw_player_icon_value(conf, raw_conf.get("values", {}))
        # parse() stamps every entry with this player's owner; injected protocol entries
        # belong to their own protocol provider, so restore that owner for string resolution.
        for entry in conf.values.values():
            if CONF_PROTOCOL_KEY_SPLITTER not in entry.key:
                continue
            protocol_id = entry.key.split(CONF_PROTOCOL_KEY_SPLITTER, 1)[0]
            if protocol := self.mass.players.get_protocol(protocol_id):
                entry.translation_owner = protocol.translation_owner
            elif owner := self._stored_protocol_translation_owner(protocol_id):
                # a protocol the user switched off has no object to ask
                entry.translation_owner = owner
        return conf

    @api_command("config/players/get_entries", required_scope=Scope.CONFIG_PLAYERS_READ)
    async def get_player_config_entries(self, player_id: str) -> list[ConfigEntry]:
        """
        Return Config entries to configure a player.

        :param player_id: id of an existing player instance.
        """
        if not (player := self.mass.players.get_player(player_id, False)):
            msg = f"Player {player_id} not found"
            raise KeyError(msg)

        # get default entries which are common for all players
        default_entries = self._get_default_player_config_entries(player)

        # get protocol specific entries
        # this basically injects virtual config entries for each protocol output
        # this feels maybe a bit of a hack to do it this way but it keeps the UI logic simple
        # and maximizes api client compatibility because you can configure the whole player
        # including its protocols from a single config endpoint without needing special handling
        # for the protocols in the UI/api clients
        player_entries = await self._create_output_protocol_config_entries(player)
        own = player.own_protocol
        if own is not None and not any(protocol.is_native for protocol in player.output_protocols):
            # The own protocol of a control-only player (e.g. a device that delegates
            # playback to its DLNA protocol) or of a group is no output, so the block
            # above never injects its entries. Append them here so the player keeps its
            # own config surface, skipping keys the protocol entries already cover.
            protocol_keys = {entry.key for entry in player_entries}
            player_entries = [
                *player_entries,
                *[
                    entry
                    for entry in await self._get_protocol_config_entries(own)
                    if entry.key not in protocol_keys
                ],
            ]

        # append the per-player plugin toggles (e.g. Spotify Connect), the same
        # virtual-entry approach as the protocol outputs above but backed by the
        # plugin provider's own config
        player_entries = [
            *player_entries,
            *self._create_plugin_provider_config_entries(player),
        ]

        player_entries_keys = {entry.key for entry in player_entries}
        all_entries = [
            # ignore default entries that were overridden by the protocol specific ones
            *[x for x in default_entries if x.key not in player_entries_keys],
            *player_entries,
        ]
        return _with_translation_owner(all_entries, player.translation_owner)

    @api_command("config/players/invoke_action", required_scope=Scope.CONFIG_PLAYERS_WRITE)
    async def invoke_player_config_action(
        self, player_id: str, action: str
    ) -> list[ConfigEntry] | ConfigActionResult:
        """
        Run a one-shot action button from a player's config.

        A protocol-prefixed action (``<protocol_id>||protocol||<action>``) is routed to
        that protocol of the player, any other to its own protocol; the player's entries
        are then re-rendered so the injected protocol entries pick up any state change. A
        ``ConfigActionResult`` holds the outcome to report to the user; an empty list means
        the action ran with nothing to report; a non-empty list holds the player's entries
        the config form should re-render with.

        :param player_id: The player whose config surface holds the action.
        :param action: The action id of the pressed button (may be protocol-prefixed).
        """
        if not (player := self.mass.players.get_player(player_id, False)):
            msg = f"Player {player_id} not found"
            raise KeyError(msg)
        if CONF_PROTOCOL_KEY_SPLITTER in action:
            protocol_id, action = action.split(CONF_PROTOCOL_KEY_SPLITTER, 1)
            target = player.attached_protocols.get(protocol_id)
        else:
            protocol_id = player_id
            target = player.own_protocol
        if target is None:
            msg = f"Player {protocol_id} not found"
            raise KeyError(msg)
        result = await target.handle_config_action(action)
        if result is None:
            return []
        if isinstance(result, ConfigActionResult):
            # the strings belong to the provider that handled the action
            result.translation_owner = result.translation_owner or target.translation_owner
            return result
        # re-render the full player entries so injected protocol entries refresh
        return await self.get_player_config_entries(player_id)

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

    @api_command("config/players/get_value", required_scope=Scope.CONFIG_PLAYERS_READ)
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
        raw_value = self.get_player_value(player_id, key)
        if raw_value is None and (protocol_id := self.get_primary_protocol_id(player_id)):
            # the values of its own protocol are part of the player's own config
            raw_value = self.get_protocol_value(protocol_id, key)
        if raw_value is not None and not unpack_splitted_values:
            return _mask_encrypted(raw_value)
        conf = await self.get_player_config(player_id)
        if key not in conf.values:
            if default is not None:
                return default
            msg = f"Config key {key} not found for player {player_id}"
            raise KeyError(msg)
        if unpack_splitted_values:
            return _mask_encrypted(conf.values[key].get_splitted_values())
        return _mask_encrypted(
            conf.values[key].value
            if conf.values[key].value is not None
            else conf.values[key].default_value
        )

    if TYPE_CHECKING:
        # Overload for when default is provided - return type matches default type
        @overload
        def get_player_value(
            self, player_id: str, key: str, default: _ConfigValueT
        ) -> _ConfigValueT: ...

        # Overload for when no default is provided - return ConfigValueType | None
        @overload
        def get_player_value(
            self, player_id: str, key: str, default: None = None
        ) -> ConfigValueType | None: ...

    def get_player_value(
        self, player_id: str, key: str, default: ConfigValueType = None
    ) -> ConfigValueType:
        """
        Return the stored value of a key of a player's own config.

        Note that this only returns the stored value without any validation or default.

        :param player_id: ID of the player.
        :param key: A base key (such as ``enabled`` or ``name``) or the key of a value of
            the player itself (such as its controls or its announcement settings).
        :param default: Value to return when nothing is stored for the key.
        """
        return self._get_stored_value(f"{CONF_PLAYERS}/{player_id}", key, default)

    def set_player_value(self, player_id: str, key: str, value: ConfigValueType) -> None:
        """
        Store the value of a key of a player's own config.

        Note that this only stores the value without any validation or default.

        :param player_id: ID of the player.
        :param key: A base key (such as ``name``) or the key of a value of the player itself.
        :param value: The value to store.
        :raises KeyError: If no config is stored for the player.
        """
        self._set_stored_value(f"{CONF_PLAYERS}/{player_id}", key, value)
        self._sync_config_copy(self.mass.players.get_player(player_id, False), key, value)

    if TYPE_CHECKING:
        # Overload for when default is provided - return type matches default type
        @overload
        def get_protocol_value(
            self, protocol_id: str, key: str, default: _ConfigValueT
        ) -> _ConfigValueT: ...

        # Overload for when no default is provided - return ConfigValueType | None
        @overload
        def get_protocol_value(
            self, protocol_id: str, key: str, default: None = None
        ) -> ConfigValueType | None: ...

    def get_protocol_value(
        self, protocol_id: str, key: str, default: ConfigValueType = None
    ) -> ConfigValueType:
        """
        Return the stored value of a key of a protocol's config.

        Note that this only returns the stored value without any validation or default.
        A protocol nothing is stored of (one that only exists while its device is
        connected) answers from the config it holds in memory.

        :param protocol_id: ID of the protocol object.
        :param key: A base key (such as ``enabled`` or ``provider``) or the key of a value
            of the protocol: one it declares a config entry for, an audio setting of its
            output, or one its provider stores for it.
        :param default: Value to return when nothing is stored for the key.
        """
        conf_key = f"{CONF_PLAYER_PROTOCOLS}/{protocol_id}"
        if self.get(conf_key) is None and (protocol := self.mass.players.get_protocol(protocol_id)):
            if key == CONF_ENABLED:
                return protocol.config.enabled
            if key == "provider":
                return protocol.provider_id
            if (entry := protocol.config.values.get(key)) and entry.value is not None:
                return entry.value
            return default
        return self._get_stored_value(conf_key, key, default)

    def set_protocol_value(self, protocol_id: str, key: str, value: ConfigValueType) -> None:
        """
        Store the value of a key of a protocol's config.

        Note that this only stores the value without any validation or default.
        For a protocol nothing is stored of, the value is only kept in memory.

        :param protocol_id: ID of the protocol object.
        :param key: A base key (such as ``enabled``) or the key of a value of the protocol.
        :param value: The value to store.
        :raises KeyError: If no config is stored for a protocol that is not attached.
        """
        protocol = self.mass.players.get_protocol(protocol_id)
        conf_key = f"{CONF_PLAYER_PROTOCOLS}/{protocol_id}"
        if protocol is None or self.get(conf_key) is not None:
            self._set_stored_value(conf_key, key, value)
        self._sync_config_copy(protocol, key, value)

    def get_protocol_record(self, protocol_id: str) -> dict[str, Any] | None:
        """
        Return the stored config of a protocol as it is, None when nothing is stored.

        :param protocol_id: ID of the protocol object.
        """
        record = self.get(f"{CONF_PLAYER_PROTOCOLS}/{protocol_id}")
        return record if isinstance(record, dict) else None

    def is_protocol_switched_on(self, protocol_id: str) -> bool:
        """
        Return whether the user has a protocol switched on.

        That is while its provider instance is enabled and the user did not switch the
        protocol off as an output of its player. A protocol without a stored config is.

        :param protocol_id: ID of the protocol object.
        """
        if (record := self.get_protocol_record(protocol_id)) is None:
            return True
        if not record.get(CONF_ENABLED, True):
            return False
        provider = self.get(f"{CONF_PROVIDERS}/{record.get('provider')}")
        return isinstance(provider, dict) and bool(provider.get(CONF_ENABLED, True))

    def get_protocol_ids_of_player(self, player_id: str) -> list[str]:
        """
        Return the ids of the protocols that are stored as belonging to a player.

        :param player_id: ID of the player.
        """
        return [
            protocol_id
            for protocol_id, record in self.get(CONF_PLAYER_PROTOCOLS, {}).items()
            if isinstance(record, dict) and record.get("player_id") == player_id
        ]

    def get_protocol_ids_of_provider(self, provider: str) -> list[str]:
        """
        Return the ids of the protocols a config is stored for that belong to a provider.

        :param provider: Instance id of the provider.
        """
        return [
            protocol_id
            for protocol_id, record in self.get(CONF_PLAYER_PROTOCOLS, {}).items()
            if isinstance(record, dict) and record.get("provider") == provider
        ]

    def get_protocol_ids_derived_from(self, protocol_id: str) -> list[str]:
        """
        Return the ids of the protocols that are stored as riding on another protocol.

        :param protocol_id: ID of the protocol they ride on.
        """
        return [
            derived_id
            for derived_id, record in self.get(CONF_PLAYER_PROTOCOLS, {}).items()
            if isinstance(record, dict)
            and derived_id != protocol_id
            and (record.get("values") or {}).get(CONF_UNDERLYING_PLAYER_ID) == protocol_id
        ]

    def create_player_record(
        self,
        player_id: str,
        *,
        provider: str,
        player_type: PlayerType,
        default_name: str | None,
        enabled: bool = True,
        values: dict[str, Any] | None = None,
    ) -> None:
        """
        Store the config of a new player.

        :param player_id: ID of the player.
        :param provider: Instance id of the provider the player names.
        :param player_type: Type of the player.
        :param default_name: Name its device reports.
        :param enabled: Whether the player is enabled.
        :param values: The values to store with it.
        """
        # the name is stored as the default name only: a stored (custom) name means
        # the user renamed the player and must keep shadowing the default name.
        default_conf = PlayerConfig(
            values={},
            provider=provider,
            player_id=player_id,
            enabled=enabled,
            name=None,
            default_name=default_name,
            player_type=player_type,
        )
        default_conf_raw = default_conf.to_raw()
        default_conf_raw.pop("setup_data", None)
        default_conf_raw["values"] = values or {}
        self.set(f"{CONF_PLAYERS}/{player_id}", default_conf_raw)

    def create_protocol_record(
        self,
        protocol_id: str,
        *,
        provider: str,
        player_id: str | None,
        role: ProtocolRole,
        default_name: str | None,
        enabled: bool = True,
    ) -> None:
        """
        Store the config of a protocol that has none yet.

        :param protocol_id: ID of the protocol object.
        :param provider: Instance id of its provider.
        :param player_id: ID of the player it belongs to.
        :param role: The role it has for that player.
        :param default_name: Name its device reports.
        :param enabled: Whether the user wants it used as an output of its player.
        """
        self.set(
            f"{CONF_PLAYER_PROTOCOLS}/{protocol_id}",
            {
                "protocol_id": protocol_id,
                "provider": provider,
                "player_id": player_id,
                "role": role.value,
                "enabled": enabled,
                "default_name": default_name,
                "values": {},
            },
        )

    def remove_protocol_record(self, protocol_id: str) -> None:
        """
        Delete the stored config of a protocol.

        :param protocol_id: ID of the protocol object.
        """
        self.remove(f"{CONF_PLAYER_PROTOCOLS}/{protocol_id}")

    def set_protocol_setup_value(
        self, protocol_id: str, key: str, value: ConfigValueType, immediate: bool = False
    ) -> None:
        """
        Store a value a setup flow collected for a protocol (credentials, pairing data).

        For a protocol nothing is stored of, the value is only kept in memory.

        :param protocol_id: ID of the protocol object.
        :param key: The setup data key.
        :param value: The value, encrypted already when it is a secret.
        :param immediate: Persist to disk right away instead of on the debounced save timer.
        """
        conf_key = f"{CONF_PLAYER_PROTOCOLS}/{protocol_id}"
        if self.get(conf_key) is not None:
            self.set(f"{conf_key}/setup_data/{key}", value, immediate=immediate)
        # keep the in-memory config copy in sync with storage
        if protocol := self.mass.players.get_protocol(protocol_id):
            protocol.config.setup_data[key] = value

    def set_protocol_setup_data(self, protocol_id: str, setup_data: dict[str, Any]) -> None:
        """
        Store everything the setup flows collected for a protocol.

        For a protocol nothing is stored of, it is only kept in memory.

        :param protocol_id: ID of the protocol object.
        :param setup_data: The setup data, secrets encrypted already.
        """
        conf_key = f"{CONF_PLAYER_PROTOCOLS}/{protocol_id}"
        if self.get(conf_key) is not None:
            self.set(f"{conf_key}/setup_data", dict(setup_data))
        if protocol := self.mass.players.get_protocol(protocol_id):
            protocol.config.setup_data = dict(setup_data)

    def get_base_player_config(self, player_id: str, provider: str) -> PlayerConfig:
        """
        Return base PlayerConfig for a player.

        This is used to get the base config for a player, without the values of any
        config entry, for initialization purposes.
        """
        raw_conf = self.get(f"{CONF_PLAYERS}/{player_id}")
        raw_conf = dict(raw_conf) if isinstance(raw_conf, dict) else {}
        # a partial entry left on disk by an older version can miss its base keys; the
        # caller passes the authoritative values, so recover them instead of crashing parse
        raw_conf.setdefault("player_id", player_id)
        raw_conf.setdefault("provider", provider)
        return cast("PlayerConfig", PlayerConfig.parse([], raw_conf))

    def get_base_protocol_config(self, protocol_id: str, provider: str) -> PlayerConfig:
        """
        Return the base config for a protocol, without the values of any config entry.

        :param protocol_id: ID of the protocol object.
        :param provider: Instance id of its provider.
        """
        return cast(
            "PlayerConfig",
            PlayerConfig.parse([], self._get_raw_protocol_config(protocol_id, provider)),
        )

    async def get_player_object_config(self, player: Player) -> PlayerConfig:
        """
        Return the config of a player itself, without the settings of its protocols.

        :param player: The player.
        """
        raw_conf = self._get_raw_player_config(player.player_id) or {
            "player_id": player.player_id,
            "provider": player.provider_id,
        }
        raw_conf["provider"] = player.provider_id
        entries = self._get_default_player_config_entries(player)
        conf = cast("PlayerConfig", PlayerConfig.parse(entries, raw_conf))
        _apply_raw_player_icon_value(conf, raw_conf.get("values") or {})
        return conf

    async def get_protocol_config(self, protocol: PlayerProtocol) -> PlayerConfig:
        """
        Return the config of a protocol: the settings it declares and those of its output.

        :param protocol: The protocol object.
        """
        raw_conf = self._get_raw_protocol_config(protocol.protocol_id, protocol.provider_id)
        if self.get(f"{CONF_PLAYER_PROTOCOLS}/{protocol.protocol_id}") is None:
            # nothing is stored of it: what it holds in memory is its config
            in_memory = protocol.config.to_raw()
            raw_conf["values"] = in_memory.get("values") or {}
            raw_conf["setup_data"] = in_memory.get("setup_data") or {}
            raw_conf["enabled"] = protocol.config.enabled
        entries = await self._get_protocol_config_entries(protocol)
        return cast("PlayerConfig", PlayerConfig.parse(entries, raw_conf))

    @api_command("config/players/save", required_scope=Scope.CONFIG_PLAYERS_WRITE)
    async def save_player_config(
        self, player_id: str, values: dict[str, ConfigValueType]
    ) -> PlayerConfig:
        """Save/update PlayerConfig."""
        _reject_encrypted_values(values)
        if self.get(f"{CONF_PLAYERS}/{player_id}") is not None:
            return await self._save_player_config(player_id, values)
        # the first save of a player that only exists while its device is connected
        # stores its config, of which nothing may be left when the save is rejected
        previous = self._promote_session_player(player_id)
        try:
            saved = await self._save_player_config(player_id, values)
        except Exception:
            self.remove(f"{CONF_PLAYERS}/{player_id}")
            for protocol_id, record in previous.items():
                if record is None:
                    self.remove_protocol_record(protocol_id)
                else:
                    self.set(f"{CONF_PLAYER_PROTOCOLS}/{protocol_id}", record)
            raise
        self._release_promoted_protocols(player_id, previous)
        return saved

    async def save_protocol_config(
        self, protocol_id: str, values: dict[str, ConfigValueType]
    ) -> None:
        """
        Save values of the config of one protocol of a player.

        :param protocol_id: ID of the protocol object.
        :param values: The values to store, by the key of their config entry; ``enabled``
            is whether the user wants the protocol used as an output of its player.
        :raises KeyError: If no config is stored for the protocol.
        """
        conf_key = f"{CONF_PLAYER_PROTOCOLS}/{protocol_id}"
        if not isinstance(existing_raw := self.get(conf_key), dict):
            msg = f"No config found for protocol id {protocol_id}"
            raise KeyError(msg)
        new_raw = deepcopy(existing_raw)
        new_values = dict(new_raw.get("values") or {})
        changed_keys: set[str] = set()
        values = dict(values)
        if CONF_ENABLED in values:
            enabled = bool(values.pop(CONF_ENABLED))
            if enabled != bool(existing_raw.get(CONF_ENABLED, True)):
                new_raw[CONF_ENABLED] = enabled
                changed_keys.add(CONF_ENABLED)
        if protocol := self.mass.players.get_protocol(protocol_id):
            config = await self.get_protocol_config(protocol)
            changed_keys.update(config.update(values))
            # Preserve values from storage that don't have config entries in current context.
            new_values = {
                **{key: value for key, value in new_values.items() if key not in config.values},
                **config.to_raw().get("values", {}),
            }
        else:
            # a protocol that is not attached declares no entries to validate against
            for key, value in values.items():
                if new_values.get(key) != value:
                    new_values[key] = value
                    changed_keys.add(f"values/{key}")
        if not changed_keys:
            return
        new_raw["values"] = new_values
        self.set(conf_key, new_raw)
        try:
            await self.mass.players.on_protocol_config_change(protocol_id, changed_keys)
        except Exception:
            self.set(conf_key, existing_raw)
            raise

    @api_command("config/players/remove", required_scope=Scope.CONFIG_PLAYERS_WRITE)
    async def remove_player_config(self, player_id: str) -> None:
        """Remove PlayerConfig."""
        if self._get_raw_player_config(player_id) is None:
            msg = f"Player configuration for {player_id} does not exist"
            raise KeyError(msg)
        # removing the player wipes its config (incl. DSP and the configs of its protocols)
        await self.mass.players.remove(player_id)

    def store_session_player(self, player_id: str) -> None:
        """
        Store the config of a player that only exists while its device is connected.

        From now on the player is kept like the player of a device: it stays, unavailable,
        while its device is away, and it is there after a restart.

        :param player_id: ID of the player, which has no stored config.
        :raises KeyError: If there is no such player.
        """
        previous = self._promote_session_player(player_id)
        self._release_promoted_protocols(player_id, previous)

    def set_player_default_name(self, player_id: str, default_name: str) -> None:
        """Set (or update) the default name for a player."""
        # skip if the player config root no longer exists, otherwise the
        # nested set would resurrect a partial entry (missing player_id etc).
        if not self.get(f"{CONF_PLAYERS}/{player_id}"):
            return
        conf_key = f"{CONF_PLAYERS}/{player_id}/default_name"
        self.set(conf_key, default_name)

    def get_primary_protocol_id(self, player_id: str) -> str | None:
        """
        Return the id of the protocol whose values are part of the player's own config.

        That is the protocol that stands for the device itself: its native protocol,
        else the one that controls it without an audio path, else the virtual protocol
        of a group. None for a player that only has audio protocols.

        :param player_id: ID of the player.
        """
        player = self.mass.players.get_player(player_id, False)
        if player is not None and player.own_protocol is not None:
            return player.own_protocol.protocol_id
        roles = [ProtocolRole.NATIVE.value, ProtocolRole.CONTROL.value, ProtocolRole.VIRTUAL.value]
        candidates: list[tuple[bool, int, int, str]] = []
        for protocol_id in self.get_protocol_ids_of_player(player_id):
            record = self.get_protocol_record(protocol_id) or {}
            if (role := record.get("role")) not in OWN_PROTOCOL_ROLES:
                continue
            # the best ranked protocol the user has switched on is the own one: what is
            # stored for a player that is not attached may hold the role of before
            domain = str(record.get("provider")).split("--", maxsplit=1)[0]
            candidates.append(
                (
                    not self.is_protocol_switched_on(protocol_id),
                    NATIVE_PRIORITY.get(domain, 0),
                    roles.index(str(role)),
                    protocol_id,
                )
            )
        return min(candidates)[3] if candidates else None

    async def _save_player_config(
        self, player_id: str, values: dict[str, ConfigValueType]
    ) -> PlayerConfig:
        """
        Save values of the stored config of a player and return the config.

        :param player_id: ID of the player.
        :param values: The values to store, by the key of their config entry.
        """
        conf_key = f"{CONF_PLAYERS}/{player_id}"
        values = await self._update_output_protocol_config(values)
        values = await self._update_plugin_provider_config(player_id, values)
        existing_raw = self.get(conf_key) or {}
        existing_values = self._get_stored_player_values(player_id)
        if values.get(CONF_ICON) is None and CONF_ICON not in existing_values:
            values = {key: value for key, value in values.items() if key != CONF_ICON}
        config = await self.get_player_config(player_id)
        changed_keys = config.update(values)
        new_raw = config.to_raw()
        # the config shows the display name as default name; the stored one is what the
        # device reported
        if "default_name" in existing_raw:
            new_raw["default_name"] = existing_raw["default_name"]
        # what a setup flow collected belongs to a protocol
        new_raw.pop("setup_data", None)
        new_values = new_raw.get("values", {})
        # Preserve values from storage that don't have config entries in current context.
        config_entry_keys = set(config.values.keys())
        for key, value in existing_values.items():
            if key not in new_values and key not in config_entry_keys:
                new_values[key] = value
        # never persist protocol- or plugin-prefixed (virtual) entries on this player; the
        # protocol resp. the plugin provider is the canonical store (handled above). Storing
        # a copy here would shadow the canonical value once it is reset back to its default.
        new_values = {
            key: value
            for key, value in new_values.items()
            if CONF_PROTOCOL_KEY_SPLITTER not in key and CONF_PLUGIN_KEY_SPLITTER not in key
        }
        if _reconcile_player_icon_value(values, existing_values, new_values):
            changed_keys.add(f"values/{CONF_ICON}")
        _apply_raw_player_icon_value(config, new_values)
        if not changed_keys:
            # no changes
            return config
        # store updated config first (to prevent issues with enabling/disabling players),
        # each value with the config of whoever declared its entry
        values_by_key = self._split_player_values(player_id, new_values)
        new_raw["values"] = values_by_key.pop(f"{conf_key}/values")
        existing_protocol_values = {key: self.get(key) for key in values_by_key}
        self.set(conf_key, new_raw)
        for values_key, protocol_values in values_by_key.items():
            self.set(values_key, protocol_values)
        try:
            # validate/handle the update in the players controller
            await self.mass.players.on_player_config_change(config, changed_keys)
        except Exception:
            # rollback on error - use existing_raw to preserve all values
            self.set(conf_key, existing_raw)
            for values_key, existing_protocol in existing_protocol_values.items():
                if existing_protocol is None:
                    # nothing was stored there before, and nothing may be left behind
                    self.remove(values_key)
                else:
                    self.set(values_key, existing_protocol)
            raise
        # send config updated event
        self.mass.signal_event(
            EventType.PLAYER_CONFIG_UPDATED,
            object_id=config.player_id,
            data=config,
        )
        # return full player config (just in case)
        return await self.get_player_config(player_id)

    def _get_stored_player_values(self, player_id: str) -> dict[str, Any]:
        """
        Return the stored values of a player: its own and those of its own protocol.

        :param player_id: ID of the player.
        """
        protocol_values: dict[str, Any] = {}
        if protocol_id := self.get_primary_protocol_id(player_id):
            protocol_values = self._get_raw_protocol_config(protocol_id, "").get("values") or {}
        return {
            **protocol_values,
            **(self.get(f"{CONF_PLAYERS}/{player_id}/values") or {}),
        }

    def _split_player_values(
        self, player_id: str, values: dict[str, Any]
    ) -> dict[str, dict[str, Any]]:
        """
        Return the values of a player by the storage key they are stored under.

        A value is stored with the config of whoever declared its entry: the player
        itself, or its own protocol.

        :param player_id: ID of the player.
        :param values: The values to store, by their key.
        """
        player_key = f"{CONF_PLAYERS}/{player_id}/values"
        values_by_key: dict[str, dict[str, Any]] = {player_key: {}}
        protocol_id = self.get_primary_protocol_id(player_id)
        if protocol_id is None or self.get_protocol_record(protocol_id) is None:
            # a player without a protocol of its own keeps every value itself
            values_by_key[player_key] = dict(values)
            return values_by_key
        protocol_key = f"{CONF_PLAYER_PROTOCOLS}/{protocol_id}/values"
        values_by_key[protocol_key] = {}
        for key, value in values.items():
            values_by_key[player_key if key in PLAYER_VALUE_KEYS else protocol_key][key] = value
        return values_by_key

    def _get_raw_player_config(self, player_id: str) -> dict[str, Any] | None:
        """
        Return a copy of the raw config of a player, None when it has none.

        A player that only exists while its device is connected has no stored config
        until the user changes something about it: it answers with what it is now.

        :param player_id: ID of the player.
        """
        player = self.mass.players.get_player(player_id, False)
        if (stored := self.get(f"{CONF_PLAYERS}/{player_id}")) is not None:
            return self._ensure_player_config_base_keys(
                player_id, deepcopy(stored), provider=player.provider_id if player else None
            )
        if player is None or player.player_id != player_id:
            return None
        return {
            "player_id": player_id,
            "provider": player.provider_id,
            "enabled": True,
            "default_name": player.state.name,
            "player_type": player.type,
            "values": {},
        }

    def _get_raw_protocol_config(self, protocol_id: str, provider: str) -> dict[str, Any]:
        """
        Return the raw config of a protocol in the shape a PlayerConfig is parsed from.

        :param protocol_id: ID of the protocol object.
        :param provider: Instance id of its provider, for a protocol nothing is stored of.
        """
        stored = self.get_protocol_record(protocol_id) or {}
        values = stored.get("values")
        setup_data = stored.get("setup_data")
        return {
            "player_id": protocol_id,
            "provider": stored.get("provider") or provider,
            "enabled": bool(stored.get("enabled", True)),
            "default_name": stored.get("default_name"),
            "values": deepcopy(values) if isinstance(values, dict) else {},
            "setup_data": deepcopy(setup_data) if isinstance(setup_data, dict) else {},
        }

    def _promote_session_player(self, player_id: str) -> dict[str, dict[str, Any] | None]:
        """
        Store the config of a player that only existed while its device is connected.

        From now on the player is kept like the player of a device: it stays, unavailable,
        while its device is away, and it is there after a restart. Returns the config each
        protocol stored with it had before, by protocol id: None for a protocol that had none.

        :param player_id: ID of the player, which has no stored config.
        :raises KeyError: If there is no such player.
        """
        player = self.mass.players.get_player(player_id, False)
        if player is None or player.player_id != player_id:
            msg = f"No config found for player id {player_id}"
            raise KeyError(msg)
        self.create_player_record(
            player_id,
            provider=player.provider_id,
            player_type=player.type,
            default_name=get_device_name(player.own_protocol, player.audio_protocols),
            values={CONF_SESSION_ONLY: True, CONF_CREATED_AT: time.time_ns()},
        )
        previous: dict[str, dict[str, Any] | None] = {}
        for protocol in player.attached_protocols.values():
            previous[protocol.protocol_id] = deepcopy(
                self.get_protocol_record(protocol.protocol_id)
            )
            in_memory = protocol.config.to_raw()
            self.set(
                f"{CONF_PLAYER_PROTOCOLS}/{protocol.protocol_id}",
                {
                    "protocol_id": protocol.protocol_id,
                    "provider": protocol.provider_id,
                    "player_id": player_id,
                    "role": (player.get_role(protocol.protocol_id) or protocol.role).value,
                    "enabled": protocol.config.enabled,
                    "default_name": protocol.name,
                    "values": in_memory.get("values") or {},
                    "setup_data": in_memory.get("setup_data") or {},
                },
            )
        player.protocol_ids.update(player.attached_protocols)
        player.store_protocol_defaults()
        return previous

    def _release_promoted_protocols(
        self, player_id: str, previous: dict[str, dict[str, Any] | None]
    ) -> None:
        """
        Let the other players whose protocol configs a promoted player took over know.

        :param player_id: ID of the promoted player.
        :param previous: The config each protocol had before, as the promotion returned it.
        """
        for protocol_id, record in previous.items():
            if (
                record
                and (other_id := record.get("player_id"))
                and other_id != player_id
                and (other := self.mass.players.get_player(other_id))
            ):
                # the config of the protocol names this player now, no longer the other one
                other.protocol_ids.discard(protocol_id)
                other.refresh_state()

    def _get_stored_value(
        self, conf_key: str, key: str, default: ConfigValueType
    ) -> ConfigValueType:
        """
        Return the stored value of a key of a stored config.

        :param conf_key: Storage key of the config.
        :param key: A base key or the key of a value.
        :param default: Value to return when nothing is stored for the key.
        """
        return cast(
            "ConfigValueType",
            self.get(f"{conf_key}/values/{key}", self.get(f"{conf_key}/{key}", default)),
        )

    def _set_stored_value(self, conf_key: str, key: str, value: ConfigValueType) -> None:
        """
        Store the value of a key of a stored config.

        :param conf_key: Storage key of the config.
        :param key: A base key or the key of a value.
        :param value: The value to store.
        :raises KeyError: If no config is stored under the storage key.
        """
        if not self.get(conf_key):
            # only allow setting raw values if main entry exists
            msg = f"No config found for {conf_key}"
            raise KeyError(msg)
        if key in BASE_KEYS:
            self.set(f"{conf_key}/{key}", value)
        else:
            self.set(f"{conf_key}/values/{key}", value)

    @staticmethod
    def _sync_config_copy(
        owner: Player | PlayerProtocol | None, key: str, value: ConfigValueType
    ) -> None:
        """
        Apply a stored value to the config copy its live owner holds.

        :param owner: The live object the config belongs to, if any.
        :param key: The key the value was stored under.
        :param value: The stored value.
        """
        # also update the owner's in-place config copy so object-local
        # value reads stay in sync with raw writes
        if owner and (entry := owner.config.values.get(key)):
            entry.value = value

    def _ensure_player_config_base_keys(
        self, player_id: str, raw_conf: Any, provider: str | None = None
    ) -> dict[str, Any] | None:
        """
        Return a copy of a stored raw player config with its base keys ensured, or None.

        A partial entry left on disk by an older version can miss the mandatory
        player_id/provider keys (a ghost resurrected by a nested config-set), which
        crashes PlayerConfig.parse. player_id is taken from the lookup key and provider
        from the passed value or the live player. When the entry is not a mapping, or its
        provider cannot be recovered, it is an unreconstructable ghost: it is removed and
        None is returned so the caller treats it as absent.
        """
        if not isinstance(raw_conf, dict):
            LOGGER.warning("Removing malformed player config entry %s (not a mapping)", player_id)
            self.remove(f"{CONF_PLAYERS}/{player_id}")
            return None
        conf = dict(raw_conf)
        add_player_id = "player_id" not in conf
        add_provider = "provider" not in conf
        if add_provider and provider is None:
            if not (player := self.mass.players.get_player(player_id, False)):
                LOGGER.warning(
                    "Removing malformed player config entry %s (missing provider)", player_id
                )
                self.remove(f"{CONF_PLAYERS}/{player_id}")
                return None
            provider = player.provider_id
        # recover the missing base keys and persist them so the repair survives (the entry
        # root exists here, so a nested set cannot resurrect a partial dict) and the stored
        # values are not lost to a later prune once the player goes offline
        if add_player_id:
            conf["player_id"] = player_id
            self.set(f"{CONF_PLAYERS}/{player_id}/player_id", player_id)
        if add_provider:
            conf["provider"] = provider
            self.set(f"{CONF_PLAYERS}/{player_id}/provider", provider)
        return conf

    async def _get_protocol_config_entries(
        self,
        player: PlayerProtocol,
    ) -> list[ConfigEntry]:
        """
        Return the config entries of one protocol, without any default player entries.

        In general this returns entries that are specific to this provider/player type only,
        and includes audio related entries that are not part of the default set.

        :param player: the protocol object
        """
        default_entries: list[ConfigEntry]
        is_dedicated_group_player = player.type in (
            PlayerType.GROUP,
            PlayerType.STEREO_PAIR,
        ) and not player.protocol_id.startswith((UGP_PREFIX, SGP_PREFIX))
        is_http_based_player_protocol = player.provider.domain not in NON_HTTP_PROVIDERS
        if player.type == PlayerType.GROUP and not is_dedicated_group_player:
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
                    CONF_ENTRY_PREFER_WAV_FOR_LIVE_SOURCES,
                    CONF_ENTRY_ENABLE_ICY_METADATA,
                ]
                # only inject the sample-rates config when the player can't declare its rates itself
                if not player.declares_supported_sample_rates:
                    default_entries.append(CONF_ENTRY_SAMPLE_RATES)
                # add flow mode entry for http-based players that do not already enforce it
                if not player.requires_flow_mode:
                    default_entries.append(CONF_ENTRY_FLOW_MODE)
                    default_entries.append(CONF_ENTRY_FLOW_MODE_SAMPLE_RATE)
                else:
                    # Flow mode is enforced for this player. Clear depends_on so the
                    # UI doesn't visually disable sample rate when entry is omitted.
                    forced_sample_rate_entry = deepcopy(CONF_ENTRY_FLOW_MODE_SAMPLE_RATE)
                    forced_sample_rate_entry.depends_on = None
                    default_entries.append(forced_sample_rate_entry)
        if PlayerFeature.GAPLESS_PLAYBACK in player.supported_features:
            default_entries.append(CONF_ENTRY_CROSSFADE_DIFFERENT_SAMPLE_RATES)
        # request player specific entries
        player_entries = await player.get_config_entries()
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

        icon_entry = deepcopy(
            CONF_ENTRY_PLAYER_ICON_GROUP
            if player.state.type == PlayerType.GROUP
            else CONF_ENTRY_PLAYER_ICON
        )
        icon_entry.default_value = player.default_icon
        icon_entry.value = self.get_player_value(player.player_id, CONF_ICON)

        # some base entries for all player types
        # note that these may NOT be playback/audio related
        entries += [
            CONF_ENTRY_TTS_PRE_ANNOUNCE,
            ConfigEntry(
                key=CONF_PRE_ANNOUNCE_CHIME_URL,
                type=ConfigEntryType.STRING,
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
                default_value=player.hidden_by_default,
                category="generic",
                advanced=False,
            ),
            # add entry to expose player to HA
            ConfigEntry(
                key=CONF_EXPOSE_PLAYER_TO_HA,
                type=ConfigEntryType.BOOLEAN,
                category="generic",
                advanced=False,
                default_value=player.expose_to_ha_by_default,
            ),
        ]
        # group-player config entries
        if player.state.type == PlayerType.GROUP:
            entries += [
                icon_entry,
            ]
            return entries
        # normal player (or stereo pair) config entries
        entries += [
            icon_entry,
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
        auto_option = ConfigValueOption(PLAYER_CONTROL_PROTOCOL)
        own_features = player.own_protocol.supported_features if player.own_protocol else set()
        # the "native" option is always listed (disabled when the feature is unsupported) so the
        # option set is consistent across players; the entry default skips disabled options.
        power_options: list[ConfigValueOption] = [
            ConfigValueOption(
                PLAYER_CONTROL_NATIVE,
                disabled=PlayerFeature.POWER not in own_features,
            )
        ]
        has_native_volume_control = PlayerFeature.VOLUME_SET in own_features
        volume_options: list[ConfigValueOption] = [
            ConfigValueOption(PLAYER_CONTROL_NATIVE, disabled=not has_native_volume_control)
        ]
        mute_options: list[ConfigValueOption] = [
            ConfigValueOption(
                PLAYER_CONTROL_NATIVE,
                disabled=PlayerFeature.VOLUME_MUTE not in own_features,
            )
        ]
        # add audio protocols as volume controls if the native protocol has no volume control
        for protocol in player.audio_protocols:
            if has_native_volume_control:
                break
            if not protocol.available_for_playback:
                continue
            if protocol.supports_feature(PlayerFeature.VOLUME_SET):
                if auto_option not in volume_options:
                    volume_options.append(auto_option)
                if protocol.provider.domain in ("chromecast", "dlna"):
                    # for chromecast/dlna we can use the protocol for volume control
                    # even if it is not the active protocol
                    volume_options.append(
                        ConfigValueOption(protocol.protocol_id, title=protocol.provider.name)
                    )
            if protocol.supports_feature(PlayerFeature.VOLUME_MUTE):
                if auto_option not in mute_options:
                    mute_options.append(auto_option)
                if protocol.provider.domain in ("chromecast", "dlna"):
                    # for chromecast/dlna we can use the protocol for volume control
                    # even if it is not the active protocol
                    mute_options.append(
                        ConfigValueOption(protocol.protocol_id, title=protocol.provider.name)
                    )

        # append none+fake options
        power_options += [
            ConfigValueOption(PLAYER_CONTROL_NONE),
            ConfigValueOption(PLAYER_CONTROL_FAKE),
        ]
        volume_options += [
            ConfigValueOption(PLAYER_CONTROL_NONE),
        ]
        mute_options.append(ConfigValueOption(PLAYER_CONTROL_NONE))
        # fake mute drives the volume control, so offer it when the player has any
        # usable volume path (native or via an audio protocol)
        if has_native_volume_control or auto_option in volume_options:
            mute_options.append(ConfigValueOption(PLAYER_CONTROL_FAKE))

        power_entry = ConfigEntry(
            key=CONF_POWER_CONTROL,
            type=ConfigEntryType.STRING,
            default_value=_first_enabled_control_value(power_options),
            required=False,
            options=[
                *power_options,
                *(ConfigValueOption(x.id, title=x.name) for x in power_controls),
            ],
            category="player_controls",
        )
        if is_group:
            # group volume and mute are always fanned out to the members and use each
            # member's own control, so a group only gets the (opt-in) power control
            return [power_entry]
        # return final config entries for all options
        return [
            power_entry,
            # Volume control config entry
            ConfigEntry(
                key=CONF_VOLUME_CONTROL,
                type=ConfigEntryType.STRING,
                default_value=_first_enabled_control_value(volume_options),
                required=True,
                options=[
                    *volume_options,
                    *(ConfigValueOption(x.id, title=x.name) for x in volume_controls),
                ],
                category="player_controls",
            ),
            # Mute control config entry
            ConfigEntry(
                key=CONF_MUTE_CONTROL,
                type=ConfigEntryType.STRING,
                default_value=_first_enabled_control_value(mute_options),
                required=True,
                options=[
                    *mute_options,
                    *[ConfigValueOption(x.id, title=x.name) for x in mute_controls],
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
            CONF_ENTRY_AUTO_PLAY,
            CONF_ENTRY_IDLE_POWER_OFF,
        ]

    async def _create_output_protocol_config_entries(  # noqa: PLR0915
        self,
        player: Player,
    ) -> list[ConfigEntry]:
        """
        Create the output protocol config entries for a player.

        The preferred output protocol entry is always returned, listing outputs that can not
        be selected right now as disabled options and hidden altogether when the player has
        at most one output. The settings of each output whose provider is loaded follow.

        :param player: The player to create the output protocol config entries for.
        """
        all_entries: list[ConfigEntry] = []
        output_protocols = player.output_protocols
        own = player.own_protocol

        # Resolve derived-transport edges (e.g. a Sendspin bridge riding on the
        # AirPlay protocol) so derived protocols render as dependent on their base.
        base_protocols: dict[str, OutputProtocol] = {}
        for protocol in output_protocols:
            if protocol.is_native or not protocol.derived_from:
                continue
            if base_protocol := next(
                (p for p in output_protocols if p.output_protocol_id == protocol.derived_from),
                None,
            ):
                base_protocols[protocol.output_protocol_id] = base_protocol

        def _base_enabled(base_protocol: OutputProtocol) -> bool:
            """Return whether the protocol a derived one rides on is enabled."""
            base_id = own.protocol_id if base_protocol.is_native and own else None
            return bool(
                self.get_protocol_value(
                    base_id or base_protocol.output_protocol_id, CONF_ENABLED, True
                )
            )

        # Build options from all output protocols, sorted by priority
        options: list[ConfigValueOption] = []

        # Add each output protocol as an option, sorted by priority. An output that can not be
        # used right now is offered disabled with the reason why, rather than left out entirely:
        # that keeps the device's outputs recognizable and explains what to do about it.
        has_native = False
        for protocol in sorted(output_protocols, key=lambda p: p.priority):
            protocol_name = self._get_protocol_display_name(protocol.protocol_domain)
            # Use "native" for native playback,
            # otherwise use the protocol output id (=player id)
            if protocol.is_native:
                title = f"{protocol_name} (native)"
            elif base_protocol := base_protocols.get(protocol.output_protocol_id):
                title = (
                    f"{protocol_name} "
                    f"(over {self._get_protocol_display_name(base_protocol.protocol_domain)})"
                )
            else:
                title = protocol_name
            value = "native" if protocol.is_native else protocol.output_protocol_id
            options.append(
                ConfigValueOption(
                    value,
                    title=title,
                    disabled=not protocol.available,
                    # the option's value is a player id, so its reason is keyed by this slug
                    translation_key=(
                        None
                        if protocol.available
                        else self._output_protocol_unavailable_reason(player, protocol)
                    ),
                )
            )
            # never default to an output that can not be selected
            has_native = has_native or (protocol.is_native and protocol.available)

        if has_native:
            default_value = "native"
        else:
            # Without a native output the entry default stays "auto": runtime selection
            # honours the player's default_output_protocol_domain (e.g. DLNA-first for a
            # LinkPlay shell) with plain priority fallback, so the stored config default
            # must not depend on which audio protocols happen to be available right now.
            options.append(ConfigValueOption("auto"))
            default_value = "auto"

        all_entries.append(
            ConfigEntry(
                key=CONF_PREFERRED_OUTPUT_PROTOCOL,
                type=ConfigEntryType.STRING,
                default_value=default_value,
                required=True,
                options=options,
                category="protocol_general",
                requires_reload=False,
                hidden=len(output_protocols) <= 1,
            )
        )

        # Add config entries for all protocols/outputs
        for protocol in output_protocols:
            domain = protocol.protocol_domain
            protocol_name = self._get_protocol_display_name(domain)
            # An output its provider flagged as experimental is opt-in: it defaults to
            # off and carries the provider's warning above the toggle.
            experimental_note = cast(
                "str | None",
                self.get_protocol_value(
                    protocol.output_protocol_id, CONF_PROTOCOL_EXPERIMENTAL_NOTE
                ),
            )
            protocol_player_enabled = self.get_protocol_value(
                protocol.output_protocol_id, CONF_ENABLED, True
            )
            provider_available = self.mass.get_provider(protocol.protocol_domain) is not None
            if not provider_available:
                # protocol provider is not available, skip adding entries
                continue
            protocol_prefix = f"{protocol.output_protocol_id}{CONF_PROTOCOL_KEY_SPLITTER}"
            protocol_enabled_key = f"{protocol_prefix}enabled"
            protocol_category = f"{CONF_PROTOCOL_CATEGORY_PREFIX}_{domain}"
            category_translation_key = "protocol_output_settings"
            category_translation_params = [protocol_name]
            # Derived protocols (e.g. Sendspin over AirPlay) render with their base
            # protocol in the label and follow its enabled state.
            base_protocol = base_protocols.get(protocol.output_protocol_id)
            base_name: str | None = None
            base_enabled = True
            if base_protocol is not None:
                base_name = self._get_protocol_display_name(base_protocol.protocol_domain)
                base_enabled = _base_enabled(base_protocol)
                category_translation_key = "protocol_output_settings_via"
                category_translation_params = [protocol_name, base_name]
            if not protocol.is_native:
                if experimental_note:
                    # the warning is added here, not by the protocol, because an
                    # output the user did not opt into has no attached protocol to speak
                    # for it
                    all_entries.append(
                        ConfigEntry(
                            # prefixed like the protocol's own entries, so the
                            # note resolves against its provider and not this player's
                            key=f"{protocol_prefix}{experimental_note}",
                            translation_key=experimental_note,
                            translation_owner=f"provider.{domain}",
                            type=ConfigEntryType.ALERT,
                            category=protocol_category,
                            category_translation_key=category_translation_key,
                            category_translation_params=category_translation_params,
                        )
                    )
                    enabled_translation_key = (
                        "protocol_enable_experimental_via"
                        if base_name
                        else "protocol_enable_experimental"
                    )
                else:
                    enabled_translation_key = (
                        "protocol_enable_via" if base_name else "protocol_enable"
                    )
                all_entries.append(
                    ConfigEntry(
                        key=protocol_enabled_key,
                        type=ConfigEntryType.BOOLEAN,
                        # the key is per-protocol (dynamic), so pin a static catalog key
                        translation_key=enabled_translation_key,
                        translation_params=[base_name] if base_name else None,
                        # A derived protocol follows the base enabled state in the UI; saving
                        # an enable while the base is disabled is rejected by the save validator.
                        value=bool(protocol_player_enabled) and base_enabled,
                        read_only=False,
                        default_value=not experimental_note,
                        category=protocol_category,
                        category_translation_key=category_translation_key,
                        category_translation_params=category_translation_params,
                        requires_reload=False,
                    )
                )
            if protocol.is_native:
                if own is None:
                    # the native protocol is away, with the entries it declares
                    continue
                # add protocol-specific entries from the native protocol
                protocol_entries = await self._get_protocol_config_entries(own)
                for proto_entry in protocol_entries:
                    # deep copy to avoid mutating shared/constant ConfigEntry objects
                    entry = deepcopy(proto_entry)
                    entry.category = protocol_category
                    entry.category_translation_key = category_translation_key
                    entry.category_translation_params = category_translation_params
                    all_entries.append(entry)

            elif protocol_player := player.attached_protocols.get(protocol.output_protocol_id):
                # we grab the config entries from the protocol
                # and then prefix them to avoid key collisions
                protocol_entries = await self._get_protocol_config_entries(protocol_player)
                protocol_entry_keys = {entry.key for entry in protocol_entries}
                for proto_entry in protocol_entries:
                    # deep copy to avoid mutating shared/constant ConfigEntry objects
                    entry = deepcopy(proto_entry)
                    entry.category = protocol_category
                    entry.category_translation_key = category_translation_key
                    entry.category_translation_params = category_translation_params
                    # the key gets prefixed below to avoid collisions; pin the catalog key to the
                    # original (bare) slug and the protocol's own provider so the label still
                    # resolves against provider.<domain>.config_entries.<original_key>
                    entry.translation_key = entry.translation_key or entry.key
                    entry.translation_owner = protocol_player.translation_owner
                    entry.key = f"{protocol_prefix}{entry.key}"
                    if entry.depends_on in protocol_entry_keys:
                        # the entry it depends on is copied into this same block, so follow it
                        # to its prefixed key and keep the value condition that goes with it
                        entry.depends_on = f"{protocol_prefix}{entry.depends_on}"
                    else:
                        # nothing of its own to depend on, so gate it on the protocol toggle.
                        # any value condition belonged to the original key and must not carry
                        # over, or it gets compared against the toggle's boolean instead.
                        entry.depends_on = protocol_enabled_key
                        entry.depends_on_value = None
                        entry.depends_on_value_not = None
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
            # extract protocol id and actual key
            protocol_player_id, actual_key = key.split(CONF_PROTOCOL_KEY_SPLITTER)
            if protocol_player_id not in protocol_values:
                protocol_values[protocol_player_id] = {}
            protocol_values[protocol_player_id][actual_key] = value
            # remove from main values dict
            del values[key]
        # Validate every dependency before saving any protocol configuration. This keeps a
        # contradictory request (for example, enabling a derived protocol while disabling its
        # underlying player) from becoming order-dependent or leaving a partial update behind.
        for protocol_player_id, proto_values in protocol_values.items():
            if not proto_values.get(CONF_ENABLED):
                continue
            underlying_player_id = self.get_protocol_value(
                protocol_player_id, CONF_UNDERLYING_PLAYER_ID
            )
            if (
                not isinstance(underlying_player_id, str)
                or underlying_player_id == protocol_player_id
            ):
                continue
            underlying_values = protocol_values.get(underlying_player_id)
            underlying_enabled = (
                underlying_values[CONF_ENABLED]
                if underlying_values and CONF_ENABLED in underlying_values
                else self.get_protocol_value(underlying_player_id, CONF_ENABLED, True)
            )
            if not underlying_enabled:
                raise RuntimeError(
                    f"Can not enable {protocol_player_id}: underlying player "
                    f"{underlying_player_id} is disabled"
                )
            provider_id = self.get_protocol_value(underlying_player_id, "provider")
            if not isinstance(provider_id, str) or self.mass.get_provider(provider_id) is None:
                raise RuntimeError(
                    f"Can not enable {protocol_player_id}: underlying player provider "
                    f"for {underlying_player_id} is disabled"
                )

        for protocol_player_id, proto_values in protocol_values.items():
            await self.save_protocol_config(protocol_player_id, proto_values)
            if proto_values.get(CONF_ENABLED):
                # wait max 10 seconds for protocol to become available
                for _ in range(10):
                    protocol = self.mass.players.get_protocol(protocol_player_id)
                    if protocol is not None and protocol.player is not None:
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
            stored_value = self.get_protocol_value(protocol_player_id, actual_key)
            if stored_value is None:
                continue
            values[entry.key] = stored_value
        return values

    def _output_protocol_unavailable_reason(self, player: Player, protocol: OutputProtocol) -> str:
        """
        Return the translation slug telling why an output protocol can not be selected.

        :param player: The player the output protocol belongs to.
        :param protocol: The output protocol that is currently unavailable.
        """
        if protocol.is_native:
            own = player.own_protocol
            return "needs_setup" if own is not None and own.needs_setup else "unavailable"
        if not self.get_protocol_value(protocol.output_protocol_id, CONF_ENABLED, True):
            return "turned_off"
        protocol_player = player.attached_protocols.get(protocol.output_protocol_id)
        if protocol_player is not None and protocol_player.needs_setup:
            return "needs_setup"
        return "unavailable"

    def _get_protocol_display_name(self, protocol_domain: str) -> str:
        """Return the display name for a protocol domain."""
        try:
            return self.mass.get_provider_manifest(protocol_domain).name
        except KeyError:
            # a stored protocol of a provider that is not installed
            return protocol_domain.upper()

    def _stored_protocol_translation_owner(self, protocol_player_id: str) -> str | None:
        """
        Return the translation namespace of a protocol from its stored config.

        :param protocol_player_id: The protocol to resolve the namespace for.
        """
        if not (provider := self.get_protocol_value(protocol_player_id, "provider")):
            return None
        return f"provider.{str(provider).split('--', maxsplit=1)[0]}"

    def _create_plugin_provider_config_entries(self, player: Player) -> list[ConfigEntry]:
        """
        Create the per-player plugin toggle entries for a player.

        Each loaded plugin that binds its AudioSources to individual players (e.g.
        Spotify Connect) renders as a single boolean toggle whose value reflects
        whether the player is in that plugin's connected players, also by the id of a
        player that was merged into it.

        :param player: The player to create the plugin toggle entries for.
        """
        # only players a plugin can offer as playback target get the toggles
        if player.state.type not in PLAYBACK_TARGET_TYPES:
            return []
        entries: list[ConfigEntry] = []
        plugin_providers = (
            provider for provider in self.mass.providers if isinstance(provider, PluginProvider)
        )
        for provider in sorted(plugin_providers, key=lambda prov: prov.name.lower()):
            # a player-bound plugin returns a list for any player id, unbound returns None
            if provider.get_player_audio_sources(player.player_id) is None:
                continue
            connected_players = cast(
                "list[str]",
                self.mass.config.get_raw_provider_config_value(
                    provider.instance_id, CONF_CONNECTED_PLAYERS
                )
                or [],
            )
            entries.append(
                ConfigEntry(
                    key=f"{provider.instance_id}{CONF_PLUGIN_KEY_SPLITTER}{CONF_ENABLED}",
                    type=ConfigEntryType.BOOLEAN,
                    # the key is per-plugin (dynamic), so pin a static catalog key
                    translation_key="plugin_enable",
                    translation_params=[provider.name],
                    value=any(
                        self.mass.players.resolve_player_id(connected_id) == player.player_id
                        for connected_id in connected_players
                    ),
                    default_value=False,
                    category="plugins",
                    requires_reload=False,
                )
            )
        return entries

    def _get_plugin_provider_config_values(
        self, entries: list[ConfigEntry]
    ) -> dict[str, ConfigValueType]:
        """Extract the plugin toggle values for given (parent) player entries."""
        return {
            entry.key: entry.value for entry in entries if CONF_PLUGIN_KEY_SPLITTER in entry.key
        }

    async def _update_plugin_provider_config(
        self, player_id: str, values: dict[str, ConfigValueType]
    ) -> dict[str, ConfigValueType]:
        """
        Update the connected players of per-player plugins from submitted toggle values.

        Returns the updated values dict with the plugin toggle entries removed.
        """
        for key in [key for key in values if CONF_PLUGIN_KEY_SPLITTER in key]:
            enabled = bool(values.pop(key))
            instance_id = key.split(CONF_PLUGIN_KEY_SPLITTER, 1)[0]
            connected_players = cast(
                "list[str]",
                self.mass.config.get_raw_provider_config_value(instance_id, CONF_CONNECTED_PLAYERS)
                or [],
            )
            # a connected player that was merged into this one counts as this one
            resolve = self.mass.players.resolve_player_id
            if enabled == any(resolve(x) == player_id for x in connected_players):
                continue
            if enabled:
                connected_players = [*connected_players, player_id]
            else:
                connected_players = [x for x in connected_players if resolve(x) != player_id]
            # the plugin provider's config is the canonical store; updating it also
            # reloads the plugin so it picks up the new player set
            await self.mass.config._update_provider_config(
                instance_id, {CONF_CONNECTED_PLAYERS: connected_players}
            )
        return values
