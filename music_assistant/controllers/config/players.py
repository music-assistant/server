"""Player configuration handling for the ConfigController."""

from __future__ import annotations

import asyncio
import logging
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
)
from music_assistant_models.errors import (
    ActionUnavailable,
    UnsupportedFeaturedException,
)

from music_assistant.constants import (
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
    CONF_ENTRY_HTTP_PROFILE,
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
    CONF_PLAYERS,
    CONF_POWER_CONTROL,
    CONF_PRE_ANNOUNCE_CHIME_URL,
    CONF_PREFERRED_OUTPUT_PROTOCOL,
    CONF_PROTOCOL_CATEGORY_PREFIX,
    CONF_PROTOCOL_KEY_SPLITTER,
    CONF_REAPPLY_VOLUME_STEP,
    CONF_UNDERLYING_PLAYER_ID,
    CONF_VOLUME_CONTROL,
    NON_HTTP_PROVIDERS,
    PLAYER_CONTROL_PROTOCOL,
    REAPPLY_VOLUME_STEP_MAX,
)
from music_assistant.controllers.config.constants import BASE_KEYS, _ConfigValueT
from music_assistant.controllers.config.helpers import _with_translation_owner
from music_assistant.helpers.api import api_command
from music_assistant.helpers.util import validate_announcement_chime_url
from music_assistant.providers.sync_group.constants import SGP_PREFIX
from music_assistant.providers.universal_group.constants import UGP_PREFIX

if TYPE_CHECKING:
    from music_assistant_models.player import OutputProtocol

    from music_assistant import MusicAssistant
    from music_assistant.models.player import Player


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

        def set(self, key: str, value: Any) -> None: ...  # noqa: D102

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

            conf = cast("PlayerConfig", PlayerConfig.parse(config_entries, raw_conf))
            _apply_raw_player_icon_value(conf, raw_conf.get("values", {}))
            # parse() stamps every entry with this player's owner; injected protocol entries
            # belong to their own protocol provider, so restore that owner for string resolution.
            for entry in conf.values.values():
                if CONF_PROTOCOL_KEY_SPLITTER not in entry.key:
                    continue
                protocol_player_id = entry.key.split(CONF_PROTOCOL_KEY_SPLITTER, 1)[0]
                if protocol_player := self.mass.players.get_player(protocol_player_id, False):
                    entry.translation_owner = protocol_player.translation_owner
            return conf
        msg = f"No config found for player id {player_id}"
        raise KeyError(msg)

    @api_command("config/players/get_entries", required_scope=Scope.CONFIG_PLAYERS_READ)
    async def get_player_config_entries(self, player_id: str) -> list[ConfigEntry]:
        """
        Return Config entries to configure a player.

        :param player_id: id of an existing player instance.
        """
        if not (player := self.mass.players.get_player(player_id, False)):
            msg = f"Player {player_id} not found"
            raise KeyError(msg)

        default_entries: list[ConfigEntry]
        player_entries: list[ConfigEntry]
        if player.state.type == PlayerType.PROTOCOL:
            default_entries = []
            player_entries = await self._get_player_config_entries(player)
        else:
            # get default entries which are common for all (non protocol)players
            default_entries = self._get_default_player_config_entries(player)

            # get player(protocol) specific entries
            # this basically injects virtual config entries for each protocol output
            # this feels maybe a bit of a hack to do it this way but it keeps the UI logic simple
            # and maximizes api client compatibility because you can configure the whole player
            # including its protocols from a single config endpoint without needing special handling
            # for protocol players in the UI/api clients
            if protocol_entries := await self._create_output_protocol_config_entries(player):
                player_entries = protocol_entries
                if not any(protocol.is_native for protocol in player.output_protocols):
                    # A control-only player (e.g. a device that delegates playback to a
                    # linked DLNA protocol player) has no native output protocol, so the
                    # block above never injects the player's own entries. Append them here
                    # so it keeps its own config surface, skipping keys the protocol
                    # entries already cover.
                    protocol_keys = {entry.key for entry in protocol_entries}
                    player_entries = [
                        *protocol_entries,
                        *[
                            entry
                            for entry in await self._get_player_config_entries(player)
                            if entry.key not in protocol_keys
                        ],
                    ]
            else:
                player_entries = await self._get_player_config_entries(player)

        player_entries_keys = {entry.key for entry in player_entries}
        all_entries = [
            # ignore default entries that were overridden by the player specific ones
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

        A protocol-prefixed action (``<protocol_player_id>||protocol||<action>``) is routed to
        the linked protocol player; the parent player's entries are then re-rendered so the
        injected protocol entries pick up any state change. A ``ConfigActionResult`` holds the
        outcome to report to the user; an empty list means the action ran with nothing to
        report; a non-empty list holds the parent player's entries the config form should
        re-render with.

        :param player_id: The player whose config surface holds the action.
        :param action: The action id of the pressed button (may be protocol-prefixed).
        """
        if not (player := self.mass.players.get_player(player_id, False)):
            msg = f"Player {player_id} not found"
            raise KeyError(msg)
        if CONF_PROTOCOL_KEY_SPLITTER in action:
            protocol_player_id, protocol_action = action.split(CONF_PROTOCOL_KEY_SPLITTER, 1)
            if not (target := self.mass.players.get_player(protocol_player_id, False)):
                msg = f"Player {protocol_player_id} not found"
                raise KeyError(msg)
            result = await target.handle_config_action(protocol_action)
        else:
            target = player
            result = await player.handle_config_action(action)
        if result is None:
            return []
        if isinstance(result, ConfigActionResult):
            # the strings belong to the provider that handled the action, which for a
            # protocol-prefixed action is the protocol player's, not the host player's
            result.translation_owner = result.translation_owner or target.translation_owner
            return result
        # re-render the full (parent) player entries so injected protocol entries refresh
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

    @api_command("config/players/save", required_scope=Scope.CONFIG_PLAYERS_WRITE)
    async def save_player_config(
        self, player_id: str, values: dict[str, ConfigValueType]
    ) -> PlayerConfig:
        """Save/update PlayerConfig."""
        values = await self._update_output_protocol_config(values)
        conf_key = f"{CONF_PLAYERS}/{player_id}"
        existing_raw = self.get(conf_key) or {}
        existing_values = existing_raw.get("values", {})
        if values.get(CONF_ICON) is None and CONF_ICON not in existing_values:
            values = {key: value for key, value in values.items() if key != CONF_ICON}
        config = await self.get_player_config(player_id)
        changed_keys = config.update(values)
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
        if _reconcile_player_icon_value(values, existing_values, new_values):
            changed_keys.add(f"values/{CONF_ICON}")
        _apply_raw_player_icon_value(config, new_values)
        if not changed_keys:
            # no changes
            return config
        # store updated config first (to prevent issues with enabling/disabling players)
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

    @api_command("config/players/remove", required_scope=Scope.CONFIG_PLAYERS_WRITE)
    async def remove_player_config(self, player_id: str) -> None:
        """Remove PlayerConfig."""
        conf_key = f"{CONF_PLAYERS}/{player_id}"
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
        # all of the above passed, so wipe the config (incl. DSP and linked protocol players)
        self.mass.players.delete_player_config(player_id)

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
            # deliberately do NOT update player_type here: this is called from
            # Player.__init__ where the type can still be a transient class default.
            # Genuine type changes are persisted by update_state after registration.
            return
        # config does not yet exist, create a default one.
        # the name is stored as the default name only: a stored (custom) name means
        # the user renamed the player and must keep shadowing the default name.
        conf_key = f"{CONF_PLAYERS}/{player_id}"
        default_conf = PlayerConfig(
            values={},
            provider=provider,
            player_id=player_id,
            enabled=enabled,
            name=None,
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
            # also update the player's in-place config copy so object-local
            # value reads stay in sync with raw writes
            if (player := self.mass.players.get_player(player_id, False)) and (
                entry := player.config.values.get(key)
            ):
                entry.value = value

    async def _get_player_config_entries(
        self,
        player: Player,
    ) -> list[ConfigEntry]:
        """
        Return Player(protocol) specific config entries, without any default entries.

        In general this returns entries that are specific to this provider/player type only,
        and includes audio related entries that are not part of the default set.

        :param player: the player instance
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
                    CONF_ENTRY_PREFER_WAV_FOR_LIVE_SOURCES,
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
        player_entries = await player.get_config_entries()
        players_keys = {entry.key for entry in player_entries}
        # filter out any default entries that are already provided by the player
        default_entries = [entry for entry in default_entries if entry.key not in players_keys]
        return [*player_entries, *default_entries]

    def _get_default_player_config_entries(self, player: Player) -> list[ConfigEntry]:
        """
        Return the default (generic) player config entries.

        This does not return the per-output-protocol audio entries, those are handled elsewhere.
        """
        entries: list[ConfigEntry] = []
        # default protocol-player config entries
        if player.state.type == PlayerType.PROTOCOL:
            # protocol players have no generic config entries
            # only audio/protocol specific ones
            return []

        icon_entry = deepcopy(
            CONF_ENTRY_PLAYER_ICON_GROUP
            if player.state.type == PlayerType.GROUP
            else CONF_ENTRY_PLAYER_ICON
        )
        icon_entry.default_value = player.default_icon
        icon_entry.value = self.get_raw_player_config_value(player.player_id, CONF_ICON)

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
        # opt-in per-player. Gated on the resolved volume owner, not native VOLUME_SET, so a
        # wrapped Cast (volume on a linked protocol player, no native feature) still gets it while
        # fake/external do not. Below the group return: a group has no volume of its own.
        if self.mass.players.resolve_volume_owner(player) is not None:
            entries.append(
                ConfigEntry(
                    key=CONF_REAPPLY_VOLUME_STEP,
                    type=ConfigEntryType.FLOAT,
                    required=False,
                    default_value=None,
                    range=(0, int(REAPPLY_VOLUME_STEP_MAX)),
                    category="audio",
                    advanced=True,
                )
            )
        return entries

    def _create_player_control_config_entries(self, player: Player) -> list[ConfigEntry]:
        """Create config entries for player controls."""
        is_group = player.state.type == PlayerType.GROUP
        all_controls = self.mass.players.player_controls()
        power_controls = [x for x in all_controls if x.supports_power]
        volume_controls = [x for x in all_controls if x.supports_volume]
        mute_controls = [x for x in all_controls if x.supports_mute]
        auto_option = ConfigValueOption(PLAYER_CONTROL_PROTOCOL)
        # the "native" option is always listed (disabled when the feature is unsupported) so the
        # option set is consistent across players; the entry default skips disabled options.
        power_options: list[ConfigValueOption] = [
            ConfigValueOption(
                PLAYER_CONTROL_NATIVE,
                disabled=not player.supports_feature(PlayerFeature.POWER),
            )
        ]
        has_native_volume_control = player.supports_feature(PlayerFeature.VOLUME_SET)
        volume_options: list[ConfigValueOption] = [
            ConfigValueOption(PLAYER_CONTROL_NATIVE, disabled=not has_native_volume_control)
        ]
        mute_options: list[ConfigValueOption] = [
            ConfigValueOption(
                PLAYER_CONTROL_NATIVE,
                disabled=not player.supports_feature(PlayerFeature.VOLUME_MUTE),
            )
        ]
        # add player protocols as volume controls if native player has no volume control
        for linked_protocol in player.linked_output_protocols:
            if has_native_volume_control:
                break
            protocol_player = self.mass.players.get_player(linked_protocol.output_protocol_id)
            if not protocol_player or not protocol_player.available_for_playback:
                continue
            if protocol_player.supports_feature(PlayerFeature.VOLUME_SET):
                if auto_option not in volume_options:
                    volume_options.append(auto_option)
                if linked_protocol.protocol_domain in ("chromecast", "dlna"):
                    # for chromecast/dlna we can use the protocol player for volume control
                    # even if the protocol player is not the active protocol
                    volume_options.append(
                        ConfigValueOption(
                            protocol_player.player_id, title=protocol_player.provider.name
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
                            protocol_player.player_id, title=protocol_player.provider.name
                        )
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
        # usable volume path (native or via a linked protocol player)
        if player.supports_feature(PlayerFeature.VOLUME_SET) or auto_option in volume_options:
            mute_options.append(ConfigValueOption(PLAYER_CONTROL_FAKE))

        # return final config entries for all options
        return [
            # Power control config entry
            ConfigEntry(
                key=CONF_POWER_CONTROL,
                type=ConfigEntryType.STRING,
                default_value=_first_enabled_control_value(power_options),
                required=False,
                options=[
                    *power_options,
                    *(ConfigValueOption(x.id, title=x.name) for x in power_controls),
                ],
                category="player_controls",
            ),
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
            *([] if is_group else [CONF_ENTRY_AUTO_PLAY]),
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

        # Resolve derived-transport edges (e.g. a Sendspin bridge riding on the
        # AirPlay protocol) so derived protocols render as dependent on their base.
        base_protocols: dict[str, OutputProtocol] = {}
        for protocol in output_protocols:
            if protocol.is_native:
                continue
            underlying_id = self.get_raw_player_config_value(
                protocol.output_protocol_id, CONF_UNDERLYING_PLAYER_ID
            )
            if not underlying_id:
                continue
            if base_protocol := next(
                (p for p in output_protocols if p.output_protocol_id == underlying_id), None
            ):
                base_protocols[protocol.output_protocol_id] = base_protocol

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
            # must not depend on which linked protocols happen to be available right now.
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

        # Add config entries for all protocol players/outputs
        for protocol in output_protocols:
            domain = protocol.protocol_domain
            protocol_name = self._get_protocol_display_name(domain)
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
            category_translation_key = "protocol_output_settings"
            category_translation_params = [protocol_name]
            # Derived protocols (e.g. Sendspin over AirPlay) render with their base
            # protocol in the label and follow its enabled state.
            base_protocol = base_protocols.get(protocol.output_protocol_id)
            base_name: str | None = None
            base_enabled = True
            if base_protocol is not None:
                base_name = self._get_protocol_display_name(base_protocol.protocol_domain)
                base_raw_conf = self.get(f"{CONF_PLAYERS}/{base_protocol.output_protocol_id}") or {}
                base_enabled = bool(base_raw_conf.get("enabled", True))
                category_translation_key = "protocol_output_settings_via"
                category_translation_params = [protocol_name, base_name]
            if not protocol.is_native:
                all_entries.append(
                    ConfigEntry(
                        key=protocol_enabled_key,
                        type=ConfigEntryType.BOOLEAN,
                        # the key is per-protocol (dynamic), so pin a static catalog key
                        translation_key="protocol_enable_via" if base_name else "protocol_enable",
                        translation_params=[base_name] if base_name else None,
                        # a derived protocol cannot be active while its base is disabled
                        value=bool(protocol_player_enabled) and base_enabled,
                        read_only=not base_enabled,
                        default_value=True,
                        category=protocol_category,
                        category_translation_key=category_translation_key,
                        category_translation_params=category_translation_params,
                        requires_reload=False,
                    )
                )
            if protocol.is_native:
                # add protocol-specific entries from native player
                protocol_entries = await self._get_player_config_entries(player)
                for proto_entry in protocol_entries:
                    # deep copy to avoid mutating shared/constant ConfigEntry objects
                    entry = deepcopy(proto_entry)
                    entry.category = protocol_category
                    entry.category_translation_key = category_translation_key
                    entry.category_translation_params = category_translation_params
                    all_entries.append(entry)

            elif protocol_player := self.mass.players.get_player(protocol.output_protocol_id):
                # we grab the config entries from the protocol player
                # and then prefix them to avoid key collisions
                protocol_entries = await self._get_player_config_entries(protocol_player)
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

    def _output_protocol_unavailable_reason(self, player: Player, protocol: OutputProtocol) -> str:
        """
        Return the translation slug telling why an output protocol can not be selected.

        :param player: The player the output protocol belongs to.
        :param protocol: The output protocol that is currently unavailable.
        """
        if protocol.is_native:
            return "needs_setup" if player.needs_setup else "unavailable"
        if not self.get_raw_player_config_value(protocol.output_protocol_id, CONF_ENABLED, True):
            return "turned_off"
        protocol_player = self.mass.players.get_player(protocol.output_protocol_id)
        if protocol_player is not None and protocol_player.needs_setup:
            return "needs_setup"
        return "unavailable"

    def _get_protocol_display_name(self, protocol_domain: str) -> str:
        """Return the display name for a protocol domain."""
        if provider_manifest := self.mass.get_provider_manifest(protocol_domain):
            return provider_manifest.name
        return protocol_domain.upper()
