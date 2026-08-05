"""
Base class/model for a Player within Music Assistant.

All providerspecific players should inherit from this class and implement the required methods.

Note that this is NOT the final state of the player,
as it may be overridden by (sync)group memberships, configuration options, or other factors.
This final state will be calculated and snapshotted in the PlayerState dataclass,
which is what is also what is sent over the API.
The final active source can be retrieved by using the 'state' property.
"""

from __future__ import annotations

import asyncio
import builtins
import time
from abc import ABC
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar, cast, final, overload

from music_assistant_models.config_entries import MULTI_VALUE_SPLITTER, ConfigValueType
from music_assistant_models.constants import (
    EXTRA_ATTRIBUTES_TYPES,
    PLAYER_CONTROL_FAKE,
    PLAYER_CONTROL_NATIVE,
    PLAYER_CONTROL_NONE,
)
from music_assistant_models.enums import MediaType, PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.errors import ActionUnavailable, UnsupportedFeaturedException
from music_assistant_models.player import (
    DeviceInfo,
    OutputProtocol,
    PlayerMedia,
    PlayerOption,
    PlayerOptionValueType,
    PlayerSoundMode,
    PlayerSource,
)
from music_assistant_models.player import Player as PlayerState
from music_assistant_models.unique_list import UniqueList
from propcache import under_cached_property as cached_property

from music_assistant.constants import (
    ACTIVE_PROTOCOL_FEATURES,
    ATTR_FAKE_MUTE,
    ATTR_FAKE_POWER,
    ATTR_FAKE_VOLUME,
    CONF_ENTRY_PLAYER_ICON,
    CONF_EXPOSE_PLAYER_TO_HA,
    CONF_FLOW_MODE,
    CONF_HIDE_IN_UI,
    CONF_LINKED_PROTOCOL_IDS,
    CONF_MUTE_CONTROL,
    CONF_PLAYERS,
    CONF_POWER_CONTROL,
    CONF_PREFERRED_OUTPUT_PROTOCOL,
    CONF_SAMPLE_RATES,
    CONF_UNDERLYING_PLAYER_ID,
    CONF_VOLUME_CONTROL,
    EXTERNAL_SOURCES,
    PLAYER_CONTROL_PROTOCOL,
    PROTOCOL_FEATURES,
    PROTOCOL_PRIORITY,
)
from music_assistant.helpers.util import html_to_markdown

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, PlayerConfig
    from music_assistant_models.media_items import MediaItemPalette
    from music_assistant_models.player_queue import PlayerQueue

    from .player_provider import PlayerProvider
    from .setup_flow import SetupSession

# TypeVar for config value type inference
_ConfigValueT = TypeVar("_ConfigValueT", bound=ConfigValueType)


def _clamp_elapsed_time(elapsed_time: float | None) -> float | None:
    """Return elapsed_time clamped to a non-negative value."""
    return max(0.0, elapsed_time) if elapsed_time is not None else None


# corrected-position jumps larger than this (in seconds) are treated as a discrete
# position change (seek/buffer correction) instead of regular playback progression
POSITION_JUMP_THRESHOLD = 1.0

# Changes to any of these state keys fire the (debounced) _on_player_media_updated
# callback. The palette is included because it resolves asynchronously (shortly
# after a track change) and players push it to their device from the callback.
MEDIA_IDENTITY_KEYS = frozenset(
    {
        "current_media",
        "current_media.uri",
        "current_media.title",
        "current_media.source_id",
        "current_media.queue_item_id",
        "current_media.image_url",
        "current_media.duration",
        "current_media.palette",
    }
)

# config-derived cached properties (propcache keys in Player._cache); these are
# only invalidated by set_config, all other cached properties (including those
# defined by player implementations) are invalidated on every update_state call
_CONFIG_CACHED_PROPS = frozenset({"icon", "hide_in_ui", "expose_to_ha"})


def _reconcile_position_anchor(
    prev_position: float | None,
    prev_timestamp: float | None,
    new_position: float | None,
    new_timestamp: float | None,
    prev_playing: bool,
    new_playing: bool,
    force_adopt: bool = False,
) -> tuple[float | None, float | None, bool]:
    """
    Reconcile a playback position anchor (position + timestamp pair) with its predecessor.

    The anchor is a value that only changes on discrete events: regular playback
    progression extrapolates to (nearly) the same corrected position as the previous
    anchor and therefore keeps the previous anchor, so steady playback yields no
    state change at all. The anchor is only adopted when the corrected position
    jumped more than POSITION_JUMP_THRESHOLD (seek/buffer correction) or when
    force_adopt is set.

    :param prev_position: Position of the previous anchor.
    :param prev_timestamp: Timestamp of the previous anchor.
    :param new_position: Position of the candidate anchor.
    :param new_timestamp: Timestamp of the candidate anchor.
    :param prev_playing: Whether the player was playing at the previous anchor.
    :param new_playing: Whether the player is playing at the candidate anchor.
    :param force_adopt: Always adopt the candidate anchor (still reports jumps).

    Returns a (position, timestamp, jumped) tuple where jumped indicates a
    corrected-position discontinuity larger than the threshold.
    """
    if (
        not isinstance(prev_position, int | float)
        or not isinstance(prev_timestamp, int | float)
        or not isinstance(new_position, int | float)
        or not isinstance(new_timestamp, int | float)
    ):
        # incomplete (or non-numeric) anchor data: adopt the candidate as-is
        return new_position, new_timestamp, False
    now = time.time()
    # a position anchor only advances (extrapolates) while playing
    prev_corrected = prev_position + (now - prev_timestamp) if prev_playing else prev_position
    new_corrected = new_position + (now - new_timestamp) if new_playing else new_position
    jumped = abs(prev_corrected - new_corrected) > POSITION_JUMP_THRESHOLD
    if force_adopt or jumped:
        return new_position, new_timestamp, jumped
    return prev_position, prev_timestamp, False


def _anchor_moved(
    prev_anchor: tuple[Any, Any] | None,
    new_anchor: tuple[Any, Any] | None,
    prev_playing: bool,
    new_playing: bool,
) -> bool:
    """Return whether a position anchor pair moved significantly since its predecessor."""
    if prev_anchor is None or new_anchor is None:
        return prev_anchor != new_anchor
    prev_position, prev_timestamp = prev_anchor
    new_position, new_timestamp = new_anchor
    if (
        not isinstance(prev_position, int | float)
        or not isinstance(prev_timestamp, int | float)
        or not isinstance(new_position, int | float)
        or not isinstance(new_timestamp, int | float)
    ):
        # incomplete anchor data can not extrapolate: any change counts as moved
        return prev_anchor != new_anchor
    _, _, jumped = _reconcile_position_anchor(
        prev_position, prev_timestamp, new_position, new_timestamp, prev_playing, new_playing
    )
    return jumped


def _freeze(value: Any) -> Any:
    """Return an immutable snapshot of a (serializable) attribute value."""
    if isinstance(value, dict):
        return tuple(sorted((key, _freeze(subvalue)) for key, subvalue in value.items()))
    if isinstance(value, set | frozenset):
        return frozenset(_freeze(item) for item in value)
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _media_fingerprint(fingerprint: dict[str, Any], prefix: str, media: PlayerMedia) -> None:
    """Add the leaf values of a PlayerMedia to a state fingerprint."""
    fingerprint[f"{prefix}.uri"] = media.uri
    fingerprint[f"{prefix}.media_type"] = media.media_type
    fingerprint[f"{prefix}.title"] = media.title
    fingerprint[f"{prefix}.artist"] = media.artist
    fingerprint[f"{prefix}.album"] = media.album
    fingerprint[f"{prefix}.album_artist"] = media.album_artist
    fingerprint[f"{prefix}.image_url"] = media.image_url
    fingerprint[f"{prefix}.duration"] = media.duration
    fingerprint[f"{prefix}.source_id"] = media.source_id
    fingerprint[f"{prefix}.queue_item_id"] = media.queue_item_id
    fingerprint[f"{prefix}.elapsed_time"] = media.elapsed_time
    fingerprint[f"{prefix}.elapsed_time_last_updated"] = media.elapsed_time_last_updated
    # the palette object is carried/reused as-is until the image changes,
    # so object identity suffices to detect a (re)resolved palette
    fingerprint[f"{prefix}.palette"] = id(media.palette) if media.palette is not None else None
    fingerprint[f"{prefix}.custom_data"] = (
        _freeze(media.custom_data) if media.custom_data is not None else None
    )


def _state_fingerprint(state: PlayerState) -> dict[str, Any]:
    """
    Collect a flat fingerprint of all event-relevant leaf values of a PlayerState.

    Used to detect changes between state calculations without deepcopying the
    previous state graph or recursively diffing dataclasses: the fingerprint
    holds only immutable snapshots, so it stays valid even for values the state
    references live (extra_attributes, device_info).
    """
    fingerprint: dict[str, Any] = {
        "player_id": state.player_id,
        "provider": state.provider,
        "type": state.type,
        "name": state.name,
        "available": state.available,
        "playback_state": state.playback_state,
        # NOTE: the player's own elapsed_time/elapsed_time_last_updated are
        # deliberately absent: current_media holds the final calculated position
        # and is the only position that is event-relevant
        "powered": state.powered,
        "volume_level": state.volume_level,
        "volume_muted": state.volume_muted,
        "group_members": tuple(state.group_members),
        "static_group_members": tuple(state.static_group_members),
        "can_group_with": frozenset(state.can_group_with),
        "synced_to": state.synced_to,
        "active_sound_mode": state.active_sound_mode,
        "active_source": state.active_source,
        "active_group": state.active_group,
        "enabled": state.enabled,
        "hide_in_ui": state.hide_in_ui,
        "expose_to_ha": state.expose_to_ha,
        "icon": state.icon,
        "group_volume": state.group_volume,
        "group_volume_muted": state.group_volume_muted,
        "power_control": state.power_control,
        "volume_control": state.volume_control,
        "mute_control": state.mute_control,
        "active_output_protocol": state.active_output_protocol,
        "needs_setup": state.needs_setup,
        "setup_reason": state.setup_reason,
        "has_setup_flow": state.has_setup_flow,
        "sleep_timer_expires_at": state.sleep_timer_expires_at,
        "supported_features": frozenset(state.supported_features),
        "sound_mode_list": tuple((m.id, m.name, m.passive) for m in state.sound_mode_list),
        "options": tuple((o.key, o.value, o.read_only) for o in state.options),
        "source_list": tuple(
            (s.id, s.name, s.passive, s.can_play_pause, s.can_seek, s.can_next_previous)
            for s in state.source_list
        ),
        "output_protocols": tuple(
            (
                o.output_protocol_id,
                o.name,
                o.protocol_domain,
                o.is_native,
                o.priority,
                o.available,
                o.derived_from,
            )
            for o in state.output_protocols
        ),
        "device_info.model": state.device_info.model,
        "device_info.manufacturer": state.device_info.manufacturer,
        "device_info.software_version": state.device_info.software_version,
        "device_info.model_id": state.device_info.model_id,
        "device_info.manufacturer_id": state.device_info.manufacturer_id,
        "device_info.identifiers": tuple(sorted(state.device_info.identifiers.items())),
        "current_media": state.current_media is not None,
    }
    for key, value in state.extra_attributes.items():
        if key in ("seq_no", "last_poll"):
            # noisy bookkeeping values, not relevant for the state
            continue
        fingerprint[f"extra_attributes.{key}"] = _freeze(value)
    if state.current_media is not None:
        _media_fingerprint(fingerprint, "current_media", state.current_media)
    return fingerprint


class Player(ABC):
    """
    Base representation of a Player within the Music Assistant Server.

    Player Provider implementations should inherit from this base model.
    """

    _attr_type: PlayerType = PlayerType.PLAYER
    _attr_supported_features: set[PlayerFeature]
    _attr_group_members: list[str]
    _attr_static_group_members: list[str]
    _attr_device_info: DeviceInfo
    _attr_can_group_with: set[str]
    _attr_source_list: list[PlayerSource]
    _attr_sound_mode_list: list[PlayerSoundMode]
    _attr_options: list[PlayerOption]
    _attr_available: bool = True
    _attr_name: str | None = None
    _attr_powered: bool | None = None
    _attr_playback_state: PlaybackState = PlaybackState.IDLE
    _attr_volume_level: int | None = None
    _attr_volume_muted: bool | None = None
    _attr_elapsed_time: float | None = None
    _attr_elapsed_time_last_updated: float | None = None
    _attr_active_source: str | None = None
    _attr_active_sound_mode: str | None = None
    _attr_current_media: PlayerMedia | None = None
    # Palette for the image currently shown, resolved asynchronously from the
    # cache controller and carried here so the (synchronous) state serialization
    # can read it back without blocking. See set_resolved_palette.
    _attr_current_palette: MediaItemPalette | None = None
    _attr_current_palette_url: str | None = None
    _attr_needs_poll: bool = False
    _attr_poll_interval: int = 30
    _attr_hidden_by_default: bool = False
    _attr_expose_to_ha_by_default: bool = True
    _attr_enabled_by_default: bool = True
    _attr_needs_setup: bool = False
    _attr_setup_reason: str | None = None
    _attr_supported_sample_rates: list[tuple[int, int]] | None = None
    _attr_underlying_player_id: str | None = None

    def __init__(self, provider: PlayerProvider, player_id: str) -> None:
        """Initialize the Player."""
        # set mass as public variable
        self.mass = provider.mass
        self.logger = provider.logger
        # initialize mutable attributes
        self._attr_supported_features = set()
        self._attr_group_members = []
        self._attr_static_group_members = []
        self._attr_device_info = DeviceInfo()
        self._attr_can_group_with = set()
        self._attr_source_list = []
        self._attr_sound_mode_list = []
        self._attr_options = []
        # do not override/overwrite these private attributes below!
        self._cache: dict[str, Any] = {}  # storage dict for cached properties
        self.__attr_linked_protocols: list[OutputProtocol] = []
        self.__attr_protocol_parent_id: str | None = None
        self.__attr_active_output_protocol: str | None = None
        self._player_id = player_id
        self._provider = provider
        self.mass.config.create_default_player_config(
            player_id, self.provider_id, self.type, self.name, self.enabled_by_default
        )
        self._config = self.mass.config.get_base_player_config(player_id, self.provider_id)
        self._extra_data: dict[str, Any] = {}
        self._extra_attributes: dict[str, Any] = {}
        self._on_unload_callbacks: list[Callable[[], None]] = []
        self.__active_mass_source: str | None = None
        self.__initialized = asyncio.Event()
        # Change-tracking internals for update_state:
        # - state_dirty forces a recalculation (state derived from other
        #   sources changed) - starts True so the first update always calculates
        # - input_snapshot/input_anchor hold the player's own inputs at the last
        #   calculation, so a no-change update_state call can return immediately
        # - state_fingerprint holds the flat leaf values of the last calculated
        #   PlayerState, used to determine the changed values without deepcopy
        self.__state_dirty: bool = True
        self.__input_snapshot: dict[str, Any] | None = None
        self.__input_anchor: tuple[tuple[Any, Any], tuple[Any, Any] | None, bool] | None = None
        self.__state_fingerprint: dict[str, Any] | None = None
        # only probe synced_to when a provider implementation overrides it with its
        # own (cheap) state; the base implementation derives it by scanning sibling
        # players, which is cross-player state covered by mark_state_dirty instead
        self.__probe_synced_to: bool = type(self).synced_to is not Player.synced_to
        # The PlayerState is the (snapshotted) final state of the player
        # after applying any config overrides and other transformations,
        # such as the display name and player controls.
        # the state is updated when calling 'update_state' and is what is sent over the API.
        self._state = PlayerState(
            player_id=self.player_id,
            provider=self.provider_id,
            type=self.type,
            name=self.display_name,
            available=self.available,
            device_info=self.device_info,
            supported_features=self.supported_features,
            playback_state=self.playback_state,
        )

    @property
    def available(self) -> bool:
        """Return if the player is available."""
        return self._attr_available

    @property
    def type(self) -> PlayerType:
        """Return the type of the player."""
        return self._attr_type

    @property
    def name(self) -> str | None:
        """Return the name of the player."""
        return self._attr_name

    @property
    def supported_features(self) -> set[PlayerFeature]:
        """Return the supported features of the player."""
        return self._attr_supported_features

    @property
    def playback_state(self) -> PlaybackState:
        """Return the current playback state of the player."""
        return self._attr_playback_state

    @property
    def requires_flow_mode(self) -> bool:
        """Return if the player needs flow mode for (queue) playback."""
        # Default implementation: True if the player does not support PlayerFeature.ENQUEUE
        return PlayerFeature.ENQUEUE not in self.supported_features

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info of the player."""
        return self._attr_device_info

    @property
    def elapsed_time(self) -> float | None:
        """Return the elapsed time in (fractional) seconds of the current track (if any)."""
        return _clamp_elapsed_time(self._attr_elapsed_time)

    @property
    def elapsed_time_last_updated(self) -> float | None:
        """
        Return when the elapsed time was last updated.

        return: The (UTC) timestamp when the elapsed time was last updated,
        or None if it was never updated (or unknown).
        """
        return self._attr_elapsed_time_last_updated

    @property
    def needs_poll(self) -> bool:
        """Return if the player needs to be polled for state updates."""
        return self._attr_needs_poll

    @property
    def poll_interval(self) -> int:
        """
        Return the (dynamic) poll interval for the player.

        Only used if 'needs_poll' is set to True.
        This should return the interval in seconds.
        """
        return self._attr_poll_interval

    @property
    def hidden_by_default(self) -> bool:
        """Return if the player should be hidden in the UI by default."""
        return self._attr_hidden_by_default

    @property
    def expose_to_ha_by_default(self) -> bool:
        """Return if the player should be exposed to Home Assistant by default."""
        return self._attr_expose_to_ha_by_default

    @property
    def enabled_by_default(self) -> bool:
        """Return if the player should be enabled by default."""
        return self._attr_enabled_by_default

    @property
    def static_group_members(self) -> list[str]:
        """
        Return the static group members for a player group.

        For PlayerType.GROUP return the player_ids of members that must/can not be removed by
        the user. For all other player types return an empty list.
        """
        return self._attr_static_group_members

    @property
    def needs_setup(self) -> bool:
        """
        Return if the player needs setup.

        If True, the player needs some sort of (initial) setup before it can be used,
        such as completing an authentication flow or providing additional configuration.
        """
        return self._attr_needs_setup

    @property
    def setup_reason(self) -> str | None:
        """
        Return a short (translatable) slug describing why the player needs setup.

        Only meaningful while ``needs_setup`` is True; surfaced next to the "Setup
        required" indicator so the UI can explain what to do (e.g. "pairing_required"
        or "password_required"). Returns None when there is no specific reason.
        """
        return self._attr_setup_reason

    @property
    @final
    def available_for_playback(self) -> bool:
        """
        Return if the player can currently be used to play (or control) audio.

        A device that is reachable but still needs setup - an unpaired AirPlay
        receiver, for example - can not accept a stream, so it must never be
        picked as an output protocol or command target.
        """
        return self.available and not self.needs_setup

    @property
    @final
    def implements_setup_flow(self) -> bool:
        """Return if this player implements its own interactive setup flow."""
        return type(self).run_setup_flow is not Player.run_setup_flow

    @property
    @final
    def has_setup_flow(self) -> bool:
        """
        Return if an interactive setup flow can be started for this player.

        True when the player implements its own setup flow, or when it wraps a
        (non-native) protocol child player that does. Unlike ``needs_setup`` this stays
        True once setup completed, so the UI can offer to re-run the flow on demand
        (e.g. to redo a pairing step that was skipped).
        """
        if self.implements_setup_flow:
            return True
        for output_protocol in self.output_protocols:
            if output_protocol.is_native:
                continue
            child = self.mass.players.get_player(output_protocol.output_protocol_id)
            if child is not None and child.implements_setup_flow:
                return True
        return False

    @property
    def powered(self) -> bool | None:
        """
        Return if the player is powered on.

        If the player does not support PlayerFeature.POWER,
        or the state is (currently) unknown, this property may return None.
        """
        return self._attr_powered

    @property
    def volume_level(self) -> int | None:
        """
        Return the current volume level (0..100) of the player.

        If the player does not support PlayerFeature.VOLUME_SET,
        or the state is (currently) unknown, this property may return None.
        """
        return self._attr_volume_level

    @property
    def volume_muted(self) -> bool | None:
        """
        Return the current mute state of the player.

        If the player does not support PlayerFeature.VOLUME_MUTE,
        or the state is (currently) unknown, this property may return None.
        """
        return self._attr_volume_muted

    @property
    def active_source(self) -> str | None:
        """
        Return the (id of) the active source of the player.

        Only required if the player supports PlayerFeature.SELECT_SOURCE.

        Set to None if the player is not currently playing a source or
        the player_id if the player is currently playing a MA queue.
        """
        return self._attr_active_source

    @property
    def group_members(self) -> list[str]:
        """
        Return the group members of the player.

        If there are other players synced/grouped with this player,
        this should return the id's of players synced to this player,
        and this should include the player's own id (as first item in the list).

        If there are currently no group members, this should return an empty list.
        """
        return self._attr_group_members

    @property
    def can_group_with(self) -> set[str]:
        """
        Return the id's of players this player can group with.

        This should return set of player_id's this player can group/sync with
        or just the provider's instance_id if all players can group with each other.
        """
        return self._attr_can_group_with

    @property
    def native_grouping_requires_own_stream(self) -> bool:
        """
        Return whether native grouping only works while this player renders its own stream.

        Device-side grouping keeps working whatever feeds the leader, so members can
        stay natively grouped while the leader streams over another protocol.
        Grouping that attaches members to the leader's own stream has nothing for
        them to join once the leader moves to a protocol.
        """
        return False

    @property
    def is_active_session(self) -> bool:
        """
        Return whether this group player is currently holding (capturing) its members.

        Used by :meth:`__final_active_group` to decide whether children should be
        considered "owned" by this group at this moment. Non-group players should
        always return ``False``. Group implementations should return ``True`` while
        a session is being formed, while it is actively playing/paused, and during
        any idle grace period; and ``False`` once the group is fully dormant.
        """
        return False

    @property
    def synced_to(self) -> str | None:
        """Return the id of the player this player is synced to (sync leader)."""
        # default implementation, feel free to override if your
        # provider has a more efficient way to determine this
        if self.type == PlayerType.GROUP:
            return None
        for player in self.mass.players.all_players(
            return_unavailable=False,
            provider_filter=self.provider.instance_id,
            return_protocol_players=True,
        ):
            if player.type == PlayerType.GROUP:
                continue
            if self.player_id in player.group_members and player.player_id != self.player_id:
                return player.player_id
        return None

    @property
    def current_media(self) -> PlayerMedia | None:
        """Return the current media being played by the player."""
        return self._attr_current_media

    @property
    def source_list(self) -> list[PlayerSource]:
        """Return list of available (native) sources for this player."""
        return self._attr_source_list

    @property
    def active_sound_mode(self) -> str | None:
        """Return active sound mode of this player."""
        return self._attr_active_sound_mode

    @cached_property
    def sound_mode_list(self) -> UniqueList[PlayerSoundMode]:
        """Return available PlayerSoundModes for Player."""
        return UniqueList(self._attr_sound_mode_list)

    @cached_property
    def options(self) -> UniqueList[PlayerOption]:
        """Return all PlayerOptions for Player."""
        return UniqueList(self._attr_options)

    @property
    def supported_sample_rates(self) -> list[tuple[int, int]] | None:
        """
        Return the (sample_rate, bit_depth) pairs this player natively supports.

        Example: [(44100, 16), (48000, 24)]

        Players with a known static set should set ``_attr_supported_sample_rates``.
        Players whose supported rates depend on runtime state (e.g. group players
        whose members can change) should override this property.

        Returning ``None`` defers to the user's per-player ``CONF_SAMPLE_RATES``
        selection — callers should use ``get_supported_sample_rates()`` to get a
        resolved, non-None list.
        """
        return self._attr_supported_sample_rates

    async def power(self, powered: bool) -> None:
        """
        Handle POWER command on the player.

        Will only be called if the PlayerFeature.POWER is supported.

        :param powered: bool if player should be powered on or off.
        """
        raise NotImplementedError("power needs to be implemented when PlayerFeature.POWER is set")

    async def volume_set(self, volume_level: int) -> None:
        """
        Handle VOLUME_SET command on the player.

        Will only be called if the PlayerFeature.VOLUME_SET is supported.

        :param volume_level: volume level (0..100) to set on the player.
        """
        raise NotImplementedError(
            "volume_set needs to be implemented when PlayerFeature.VOLUME_SET is set"
        )

    async def volume_mute(self, muted: bool) -> None:
        """
        Handle VOLUME MUTE command on the player.

        Will only be called if the PlayerFeature.VOLUME_MUTE is supported.

        :param muted: bool if player should be muted.
        """
        raise NotImplementedError(
            "volume_mute needs to be implemented when PlayerFeature.VOLUME_MUTE is set"
        )

    async def play(self) -> None:
        """Handle PLAY command on the player."""
        raise NotImplementedError("play needs to be implemented")

    async def stop(self) -> None:
        """
        Handle STOP command on the player.

        Will be called to stop the stream/playback if the player has play_media support.
        """
        raise NotImplementedError(
            "stop needs to be implemented when PlayerFeature.PLAY_MEDIA is set"
        )

    async def pause(self) -> None:
        """
        Handle PAUSE command on the player.

        Will only be called if the player reports PlayerFeature.PAUSE is supported.
        """
        raise NotImplementedError("pause needs to be implemented when PlayerFeature.PAUSE is set")

    async def next_track(self) -> None:
        """
        Handle NEXT_TRACK command on the player.

        Will only be called if the player reports PlayerFeature.NEXT_PREVIOUS
        is supported and the player's currently selected source supports it.
        """
        raise NotImplementedError(
            "next_track needs to be implemented when PlayerFeature.NEXT_PREVIOUS is set"
        )

    async def previous_track(self) -> None:
        """
        Handle PREVIOUS_TRACK command on the player.

        Will only be called if the player reports PlayerFeature.NEXT_PREVIOUS
        is supported and the player's currently selected source supports it.
        """
        raise NotImplementedError(
            "previous_track needs to be implemented when PlayerFeature.NEXT_PREVIOUS is set"
        )

    async def seek(self, position: int) -> None:
        """
        Handle SEEK command on the player.

        Seek to a specific position in the current track.
        Will only be called if the player reports PlayerFeature.SEEK is
        supported and the player is NOT currently playing a MA queue.

        :param position: The position to seek to, in seconds.
        """
        raise NotImplementedError("seek needs to be implemented when PlayerFeature.SEEK is set")

    async def play_media(
        self,
        media: PlayerMedia,
    ) -> None:
        """
        Handle PLAY MEDIA command on given player.

        This is called by the Player controller to start playing Media on the player,
        which can be a MA queue item/stream or a native source.
        The provider's own implementation should work out how to handle this request.

        :param media: Details of the item that needs to be played on the player.
        """
        raise NotImplementedError(
            "play_media needs to be implemented when PlayerFeature.PLAY_MEDIA is set"
        )

    async def on_protocol_playback(
        self,
        output_protocol: OutputProtocol,
    ) -> None:
        """
        Handle callback when playback starts on a protocol output.

        Called by the Player Controller after play_media is executed on a protocol player.
        Allows the native player implementation to perform special logic when protocol
        playback starts.

        Optional - providers can override to implement protocol-specific logic.

        :param output_protocol: The OutputProtocol object containing protocol details.
        """
        return  # Optional callback - no-op by default

    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        """
        Handle enqueuing of the next (queue) item on the player.

        Called when player reports it started buffering a queue item
        and when the queue items updated.

        A PlayerProvider implementation is in itself responsible for handling this
        so that the queue items keep playing until its empty or the player stopped.

        Will only be called if the player reports PlayerFeature.ENQUEUE is
        supported and the player is currently playing a MA queue.

        This will NOT be called if the end of the queue is reached (and repeat disabled).
        This will NOT be called if the player is using flow mode to playback the queue.

         :param media: Details of the item that needs to be enqueued on the player.
        """
        raise NotImplementedError(
            "enqueue_next_media needs to be implemented when PlayerFeature.ENQUEUE is set"
        )

    async def play_announcement(
        self, announcement: PlayerMedia, volume_level: int | None = None
    ) -> None:
        """
        Handle (native) playback of an announcement on the player.

        Will only be called if the PlayerFeature.PLAY_ANNOUNCEMENT is supported.

        :param announcement: Details of the announcement that needs to be played on the player.
        :param volume_level: The volume level to play the announcement at (0..100).
            If not set, the player should use the current volume level.
        """
        raise NotImplementedError(
            "play_announcement needs to be implemented when PlayerFeature.PLAY_ANNOUNCEMENT is set"
        )

    async def select_source(self, source: str) -> None:
        """
        Handle SELECT SOURCE command on the player.

        Will only be called if the PlayerFeature.SELECT_SOURCE is supported.

        :param source: The source(id) to select, as defined in the source_list.
        """
        raise NotImplementedError(
            "select_source needs to be implemented when PlayerFeature.SELECT_SOURCE is set"
        )

    async def select_sound_mode(self, sound_mode: str) -> None:
        """
        Handle SELECT SOUND MODE command on the player.

        Will only be called if the PlayerFeature.SELECT_SOUND_MODE is supported.

        :param source: The sound_mode(id) to select, as defined in the sound_mode_list.
        """
        raise NotImplementedError(
            "select_sound_mode needs to be implemented when PlayerFeature.SELECT_SOUND_MODE is set"
        )

    async def set_option(self, option_key: str, option_value: PlayerOptionValueType) -> None:
        """
        Handle SET_OPTION command on the player.

        Will only be called if the PlayerFeature.OPTIONS is supported.

        :param option_key: The option_key of the PlayerOption
        :param option_value: The new value of the PlayerOption
        """
        raise NotImplementedError(
            "set_option needs to be implemented when PlayerFeature.Option is set"
        )

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """
        Handle SET_MEMBERS command on the player.

        Group or ungroup the given child player(s) to/from this player.
        Will only be called if the PlayerFeature.SET_MEMBERS is supported.

        :param player_ids_to_add: List of player_id's to add to the group.
        :param player_ids_to_remove: List of player_id's to remove from the group.
        """
        raise NotImplementedError(
            "set_members needs to be implemented when PlayerFeature.SET_MEMBERS is set"
        )

    async def poll(self) -> None:
        """
        Poll player for state updates.

        This is called by the Player Manager;
        if the 'needs_poll' property is True.
        """
        raise NotImplementedError("poll needs to be implemented when needs_poll is True")

    async def get_config_entries(self) -> list[ConfigEntry]:
        """
        Return all (provider/player specific) Config Entries for the player.

        Called only for an existing player: read current values via ``self.config``/
        ``self.get_config_value`` and capabilities via ``self.supported_features``.
        To override a default config entry, define an entry with the same key.
        Include ``ConfigEntryType.ACTION`` entries for one-shot buttons and handle their
        presses in ``handle_config_action``.
        """
        return []

    async def handle_config_action(self, action: str) -> list[ConfigEntry]:
        """
        Handle a one-shot action button press from this player's config and re-render.

        Override to run the side effect for each ``ConfigEntryType.ACTION`` entry this
        player declares, then return the (possibly refreshed) config entries to display.

        :param action: The action id of the pressed button (an entry's ``action`` key).
        """
        raise ActionUnavailable(f"Unknown action: {action}")

    async def run_setup_flow(self, session: SetupSession) -> None:
        """
        Run the interactive setup flow for this player (e.g. pairing).

        Override in player implementations that require user interaction to become
        usable; players without an override report that there is nothing to set up.

        :param session: The setup flow session used to interact with the user.
        """
        raise NotImplementedError

    @overload
    def get_config_value(
        self, key: str, default: _ConfigValueT, *, return_type: builtins.type[_ConfigValueT] = ...
    ) -> _ConfigValueT: ...

    @overload
    def get_config_value(
        self, key: str, default: ConfigValueType = ..., *, return_type: builtins.type[_ConfigValueT]
    ) -> _ConfigValueT: ...

    @overload
    def get_config_value(
        self, key: str, default: ConfigValueType = ..., *, return_type: None = ...
    ) -> ConfigValueType: ...

    def get_config_value(
        self,
        key: str,
        default: ConfigValueType = None,
        *,
        return_type: builtins.type[_ConfigValueT | ConfigValueType] | None = None,
    ) -> _ConfigValueT | ConfigValueType:
        """
        Return a single config value from this player's active configuration.

        Entry defaults are already applied to the active configuration, so the
        default is only returned when the key itself is not present.

        :param key: The config key to retrieve.
        :param default: Value to return when the key is not present in the config.
        :param return_type: Optional type hint for type inference (e.g., str, int, bool).
            Note: This parameter is used purely for static type checking and does not
            perform runtime type validation. Callers are responsible for ensuring the
            specified type matches the actual config value type.
        """
        return self.config.get_value(key, default)

    def get_setup_value(self, key: str, default: ConfigValueType = None) -> ConfigValueType:
        """
        Return a value collected by this player's setup flow (from setup_data).

        Encrypted (string) values are decrypted transparently. Reads setup_data only
        (no fallback to the config values): player-owned credentials/pairing data live
        exclusively in setup_data, with a one-time migration moving any legacy values.

        :param key: The setup data key to retrieve.
        :param default: Value to return when the key is not present.
        """
        setup_data = self.mass.config.get(f"{CONF_PLAYERS}/{self.player_id}/setup_data") or {}
        if key in setup_data:
            value = setup_data[key]
            return self.mass.config.decrypt_string(value) if isinstance(value, str) else value
        return default

    @final
    def resolve_output_player(self) -> Player:
        """
        Return the player that actually renders this player's audio output.

        For a player playing via one of its linked output protocols this is the
        active protocol player; in all other cases (native output, protocol
        players themselves, group players serving their own stream) it is the
        player itself.
        """
        active_protocol = self.active_output_protocol
        if (
            active_protocol
            and active_protocol != "native"
            and (protocol_player := self.mass.players.get_player(active_protocol))
        ):
            return protocol_player
        return self

    @overload
    def get_output_config_value(
        self, key: str, default: _ConfigValueT, *, return_type: builtins.type[_ConfigValueT] = ...
    ) -> _ConfigValueT: ...

    @overload
    def get_output_config_value(
        self, key: str, default: ConfigValueType = ..., *, return_type: builtins.type[_ConfigValueT]
    ) -> _ConfigValueT: ...

    @overload
    def get_output_config_value(
        self, key: str, default: ConfigValueType = ..., *, return_type: None = ...
    ) -> ConfigValueType: ...

    def get_output_config_value(
        self,
        key: str,
        default: ConfigValueType = None,
        *,
        return_type: builtins.type[_ConfigValueT | ConfigValueType] | None = None,
    ) -> _ConfigValueT | ConfigValueType:
        """
        Return a config value resolved on the player that renders the audio output.

        Audio/output related settings (output codec, http profile, output channels,
        sample rates) live on the player(protocol) that actually renders the audio:
        the active linked protocol player when outputting via a protocol, otherwise
        this player itself. The output player's value (or its provider's entry
        default) takes precedence; this player's own value is the fallback for keys
        the output player has no entry for.

        :param key: The config key to retrieve.
        :param default: Value to return when the key is not present in any config.
        :param return_type: Optional type hint for type inference (e.g., str, int, bool).
            Note: This parameter is used purely for static type checking and does not
            perform runtime type validation. Callers are responsible for ensuring the
            specified type matches the actual config value type.
        """
        output_player = self.resolve_output_player()
        if output_player is not self and key in output_player.config.values:
            return output_player.config.get_value(key, default)
        return self.get_config_value(key, default)

    async def on_config_updated(self) -> None:
        """
        Handle logic when the player is loaded or updated.

        Override this method in your player implementation if you need
        to perform any additional setup logic after the player is registered and
        the self.config was loaded, and whenever the config changes.
        """
        return

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        for callback in self._on_unload_callbacks:
            try:
                callback()
            except Exception as err:
                self.logger.error(
                    "Error calling on_unload callback for player %s: %s",
                    self.player_id,
                    err,
                )

    async def group_with(self, target_player_id: str) -> None:
        """
        Handle GROUP_WITH command on the player.

        Group this player to the given syncleader/target.
        Will only be called if the PlayerFeature.SET_MEMBERS is supported.

        :param target_player: player_id of the target player / sync leader.
        """
        # convenience helper method
        # no need to implement unless your player/provider has an optimized way to execute this
        # default implementation will simply call set_members
        # to add the target player to the group.
        target_player = self.mass.players.get_player(target_player_id, raise_unavailable=True)
        assert target_player  # for type checking
        await target_player.set_members(player_ids_to_add=[self.player_id])

    async def ungroup(self) -> None:
        """
        Handle UNGROUP command on the player.

        Remove the player from any (sync)groups it currently is grouped to.
        If this player is the sync leader (or group player),
        all child's will be ungrouped and the group dissolved.

        Will only be called if the PlayerFeature.SET_MEMBERS is supported.
        """
        # convenience helper method
        # no need to implement unless your player/provider has an optimized way to execute this
        # default implementation will simply call set_members
        if self.synced_to:
            if parent_player := self.mass.players.get_player(self.synced_to):
                # if this player is synced to another player, remove self from that group
                await parent_player.set_members(player_ids_to_remove=[self.player_id])
        elif self.group_members:
            await self.set_members(player_ids_to_remove=self.group_members)

    def on_protocol_player_updated(
        self, protocol_player: Player, changed_values: dict[str, tuple[Any, Any]]
    ) -> None:
        """Handle callback when one of the linked protocol players of the player is updated."""
        # optional callback
        # default implementation will simply trigger an update for the state of the player
        self.mass.players.trigger_player_update(self.player_id)

    def on_protocol_parent_updated(
        self, protocol_parent: Player, changed_values: dict[str, tuple[Any, Any]]
    ) -> None:
        """Handle callback when the parent protocol player of the player is updated."""
        # optional callback
        # default implementation will simply trigger an update for the state of the player
        self.mass.players.trigger_player_update(self.player_id)

    def on_group_member_updated(
        self, member_player: Player, changed_values: dict[str, tuple[Any, Any]]
    ) -> None:
        """Handle callback when a group member of the group player is updated."""
        # optional callback
        # default implementation will simply trigger an update for the state of the player
        self.mass.players.trigger_player_update(self.player_id)

    def on_group_updated(
        self, group_player: Player, changed_values: dict[str, tuple[Any, Any]]
    ) -> None:
        """Handle callback when a group player is updated this player is a member of."""
        # optional callback
        # default implementation will simply trigger an update for the state of the player
        self.mass.players.trigger_player_update(self.player_id)

    def on_sync_parent_updated(
        self, sync_parent: Player, changed_values: dict[str, tuple[Any, Any]]
    ) -> None:
        """Handle callback when the sync parent of this player is updated."""
        # optional callback
        # default implementation will simply trigger an update for the state of the player
        self.mass.players.trigger_player_update(self.player_id)

    def _on_player_media_updated(self) -> None:  # noqa: B027
        """Handle callback when the current media of the player is updated."""
        # optional callback for players that want to be informed when the final
        # current media is updated (after applying group/sync membership logic).
        # for instance to update any display information on the physical player.

    # DO NOT OVERWRITE BELOW !
    # These properties and methods are either managed by core logic or they
    # are used to perform a very specific function. Overwriting these may
    # produce undesirable effects.

    @property
    @final
    def player_id(self) -> str:
        """Return the id of the player."""
        return self._player_id

    @property
    @final
    def provider(self) -> PlayerProvider:
        """Return the provider of the player."""
        return self._provider

    @property
    @final
    def provider_id(self) -> str:
        """Return the provider (instance) id of the player."""
        return self._provider.instance_id

    @property
    @final
    def translation_owner(self) -> str:
        """Return the translation owner namespace ("provider.<domain>") of the player's provider."""
        return self._provider.translation_owner

    @property
    @final
    def config(self) -> PlayerConfig:
        """Return the config of the player."""
        return self._config

    @property
    @final
    def extra_attributes(self) -> dict[str, EXTRA_ATTRIBUTES_TYPES]:
        """
        Return the extra attributes of the player.

        This is a dict that can be used to pass any extra (serializable)
        attributes over the API, to be consumed by the UI (or another APi client, such as HA).
        This is not persisted and not used or validated by the core logic.
        """
        return self._extra_attributes

    @property
    @final
    def extra_data(self) -> dict[str, Any]:
        """
        Return the extra data of the player.

        This is a dict that can be used to store any extra data
        that is not part of the player state or config.
        This is not persisted and not exposed on the API.
        """
        return self._extra_data

    @cached_property
    @final
    def display_name(self) -> str:
        """Return the (FINAL) display name of the player."""
        if custom_name := self._config.name:
            # always prefer the custom name over the default name
            return custom_name
        return self.name or self._config.default_name or self.player_id

    @cached_property
    @final
    def enabled(self) -> bool:
        """Return if the player is enabled."""
        return self._config.enabled

    @final
    def get_supported_sample_rates(self) -> list[tuple[int, int]]:
        """
        Return the resolved (sample_rate, bit_depth) pairs the player can play.

        Honors, in order:
        1. The ``supported_sample_rates`` property (declarative or overridden)
        2. The user's ``CONF_SAMPLE_RATES`` selection
        3. A safe ``[(44100, 16)]`` fallback
        """
        if (declared := self.supported_sample_rates) is not None:
            return declared
        config_rates: list[tuple[int, int]] = []
        if conf := self.config.get_value(CONF_SAMPLE_RATES):
            conf = cast("list[str]", conf)
            for item in conf:
                # tolerate legacy/malformed entries: anything that does not parse as
                # `<rate><splitter><bit_depth>` is skipped and we fall back to defaults
                try:
                    sample_rate_str, bit_depth_str = item.split(MULTI_VALUE_SPLITTER, 1)
                    config_rates.append((int(sample_rate_str.strip()), int(bit_depth_str.strip())))
                except ValueError, TypeError:
                    self.logger.warning(
                        "Ignoring malformed CONF_SAMPLE_RATES entry %r for player %s",
                        item,
                        self.player_id,
                    )
        return config_rates or [(44100, 16)]

    @property
    @final
    def declares_supported_sample_rates(self) -> bool:
        """
        Return True when this player exposes its supported rates without user config.

        Used by the config controller to decide whether to inject the generic
        ``CONF_ENTRY_SAMPLE_RATES`` option in the player config UI.
        """
        return self.supported_sample_rates is not None

    @property
    @final
    def initialized(self) -> asyncio.Event:
        """
        Return if the player is initialized.

        Used by player controller to indicate initial registration completed.
        """
        return self.__initialized

    @property
    def corrected_elapsed_time(self) -> float | None:
        """Return the corrected/realtime elapsed time."""
        if self.elapsed_time is None or self.elapsed_time_last_updated is None:
            return None
        if self.playback_state == PlaybackState.PLAYING:
            return _clamp_elapsed_time(
                self.elapsed_time + (time.time() - self.elapsed_time_last_updated)
            )
        return _clamp_elapsed_time(self.elapsed_time)

    @cached_property
    @final
    def icon(self) -> str:
        """Return the player icon."""
        # players without an icon config entry (e.g. protocol players) serve the fallback id
        icon = self._config.get_value(CONF_ENTRY_PLAYER_ICON.key)
        return cast("str", icon or CONF_ENTRY_PLAYER_ICON.default_value)

    @cached_property
    @final
    def power_control(self) -> str:
        """Return the power control type."""
        conf = self.mass.config.get_raw_player_config_value(self.player_id, CONF_POWER_CONTROL)
        if conf and conf in (PLAYER_CONTROL_NATIVE, PLAYER_CONTROL_FAKE, PLAYER_CONTROL_NONE):
            # validate that NATIVE is still backed by an actual POWER feature.
            # this handles graceful degradation for players (e.g. group players)
            # that previously advertised POWER but no longer do.
            if conf == PLAYER_CONTROL_NATIVE and PlayerFeature.POWER not in self.supported_features:
                return PLAYER_CONTROL_NONE
            return str(conf)
        if conf and (_control := self.mass.players.get_player_control(str(conf))):
            # the control type is explicitly set to a player control,
            return _control.id
        # handle auto-select logic if not explicitly set in config
        if PlayerFeature.POWER in self.supported_features:
            # player supports native power control, always prefer that
            return PLAYER_CONTROL_NATIVE
        return PLAYER_CONTROL_NONE

    @cached_property
    @final
    def volume_control(self) -> str:
        """Return the volume control type."""
        conf = self.mass.config.get_raw_player_config_value(self.player_id, CONF_VOLUME_CONTROL)
        if conf and conf in (PLAYER_CONTROL_NATIVE, PLAYER_CONTROL_FAKE, PLAYER_CONTROL_NONE):
            # the control type is explicitly set in the config, use that
            return str(conf)
        if conf and conf not in (PLAYER_CONTROL_PROTOCOL, "auto"):
            # the control type is explicitly set to a (protocol) player_id or player control,
            # check if it exists and is (currently) available
            if (_player := self.mass.players.get_player(str(conf))) and _player.available:
                return _player.player_id
            if _control := self.mass.players.get_player_control(str(conf)):
                return _control.id
        # handle auto-select logic if not explicitly set in config
        if PlayerFeature.VOLUME_SET in self.supported_features:
            # player supports native volume control, always prefer that
            return PLAYER_CONTROL_NATIVE
        # check for protocol player with volume support, and use that if found
        if protocol_player := self._get_protocol_player_for_feature(
            PlayerFeature.VOLUME_SET, require_active=False
        ):
            return protocol_player.player_id
        return PLAYER_CONTROL_NONE

    @cached_property
    @final
    def mute_control(self) -> str:
        """Return the mute control type."""
        conf = self.mass.config.get_raw_player_config_value(self.player_id, CONF_MUTE_CONTROL)
        if conf == PLAYER_CONTROL_FAKE and self.volume_control == PLAYER_CONTROL_NONE:
            # fake mute is simulated by setting the volume to zero, so without a volume
            # control to drive there is no way to mute this player at all
            return PLAYER_CONTROL_NONE
        if conf and conf in (PLAYER_CONTROL_NATIVE, PLAYER_CONTROL_FAKE, PLAYER_CONTROL_NONE):
            # the control type is explicitly set in the config, use that
            return str(conf)
        if conf and conf not in (PLAYER_CONTROL_PROTOCOL, "auto"):
            # the control type is explicitly set to a (protocol) player_id or player control,
            # check if it exists and is (currently) available
            if (_player := self.mass.players.get_player(str(conf))) and _player.available:
                return _player.player_id
            if _control := self.mass.players.get_player_control(str(conf)):
                return _control.id
        # handle auto-select logic if not explicitly set in config
        if PlayerFeature.VOLUME_MUTE in self.supported_features:
            # player supports native mute control, always prefer that
            return PLAYER_CONTROL_NATIVE
        # check for protocol player with mute support, and use that if found
        if protocol_player := self._get_protocol_player_for_feature(
            PlayerFeature.VOLUME_MUTE, require_active=False
        ):
            return protocol_player.player_id
        return PLAYER_CONTROL_NONE

    @cached_property
    @final
    def group_volume(self) -> int | None:
        """
        Return the group volume level.

        For group players or syncgroups, returns the maximum volume level of all
        powered-on child players, or None if no children support volume control.

        For non-group players, returns the player's own volume level.
        """
        if len(self.state.group_members) == 0:
            # player is not a group or syncgroup
            if self.state.volume_control == PLAYER_CONTROL_NONE:
                return None
            return self.state.volume_level
        # return the maximum volume of all (turned on) child players
        group_volume: int | None = None
        for child_player in self.mass.players.iter_group_members(
            self, only_powered=True, exclude_self=self.type != PlayerType.PLAYER
        ):
            if child_player.state.volume_control == PLAYER_CONTROL_NONE:
                continue
            if (child_volume := child_player.state.volume_level) is None:
                continue
            if group_volume is None or child_volume > group_volume:
                group_volume = child_volume
        return group_volume

    @cached_property
    @final
    def group_volume_muted(self) -> bool | None:
        """
        Return the group mute state.

        If this player is a group player or syncgroup, this will return True if all (powered on)
        child players in the group are muted, False if at least one is not muted, or None if
        none of the players within the group support mute control.

        If the player is not a group player or syncgroup, this will return the mute state of the
        player itself (if set), or None if not supported.
        """
        if len(self.state.group_members) == 0:
            # player is not a group or syncgroup
            if self.state.mute_control == PLAYER_CONTROL_NONE:
                return None
            return self.state.volume_muted
        # calculate group mute state from all (turned on) players
        any_unmuted = False
        any_muted = False
        for child_player in self.mass.players.iter_group_members(
            self, only_powered=True, exclude_self=self.type != PlayerType.PLAYER
        ):
            if child_player.state.mute_control == PLAYER_CONTROL_NONE:
                continue
            if (child_muted := child_player.state.volume_muted) is None:
                continue
            if child_muted:
                any_muted = True
            else:
                any_unmuted = True
        if any_unmuted and not any_muted:
            return False
        if any_muted and not any_unmuted:
            return True
        return None

    @cached_property
    @final
    def hide_in_ui(self) -> bool:
        """
        Return the hide player in UI options.

        This is a convenience property based on the config entry.
        """
        return bool(self._config.get_value(CONF_HIDE_IN_UI, self.hidden_by_default))

    @cached_property
    @final
    def expose_to_ha(self) -> bool:
        """
        Return if the player should be exposed to Home Assistant.

        This is a convenience property that returns True if the player is set to be exposed
        to Home Assistant, based on the config entry.
        """
        return bool(self._config.get_value(CONF_EXPOSE_PLAYER_TO_HA, self.expose_to_ha_by_default))

    @cached_property
    @final
    def flow_mode(self) -> bool:
        """
        Return if the player(protocol) needs flow mode.

        Will use 'requires_flow_mode' unless overridden by flow_mode config.
        """
        # Check config override
        if bool(self._config.get_value(CONF_FLOW_MODE)) is True:
            # flow mode explicitly enabled in config
            return True
        return self.requires_flow_mode

    @property
    @final
    def supports_enqueue(self) -> bool:
        """
        Return if the player supports enqueueing tracks.

        This considers the active output protocol's capabilities if one is active.
        If a protocol player is active, checks that protocol's ENQUEUE feature.
        Otherwise checks the native player's ENQUEUE feature.
        """
        return self._check_feature_with_active_protocol(PlayerFeature.ENQUEUE)

    @property
    @final
    def supports_gapless(self) -> bool:
        """
        Return if the player supports gapless playback.

        This considers the active output protocol's capabilities if one is active.
        If a protocol player is active, checks that protocol's GAPLESS_PLAYBACK feature.
        Otherwise checks the native player's GAPLESS_PLAYBACK feature.
        """
        return self._check_feature_with_active_protocol(PlayerFeature.GAPLESS_PLAYBACK)

    @property
    @final
    def state(self) -> PlayerState:
        """Return the current (and FINAL) PlayerState of the player."""
        return self._state

    # Protocol-related properties and helpers

    @cached_property
    @final
    def is_native_player(self) -> bool:
        """Return True if this player is a native player."""
        is_universal_player = self.provider.domain == "universal_player"
        has_play_media = PlayerFeature.PLAY_MEDIA in self.supported_features
        return self.type != PlayerType.PROTOCOL and not is_universal_player and has_play_media

    @cached_property
    @final
    def output_protocols(self) -> list[OutputProtocol]:
        """
        Return all output options for this player.

        Includes:
        - Native playback (if player supports PLAY_MEDIA and is not a protocol/universal player)
        - Active protocol players from linked_output_protocols
        - Disabled protocols from cached linked_protocol_ids in config

        Each entry has an available flag indicating current availability.
        """
        result: list[OutputProtocol] = []

        # Add native playback option if applicable
        if self.is_native_player:
            result.append(
                OutputProtocol(
                    output_protocol_id="native",
                    name=self.provider.name,
                    protocol_domain=self.provider.domain,
                    priority=0,  # Native is always highest priority
                    available=self.available_for_playback,
                    is_native=True,
                )
            )
        elif (
            self.provider.domain in PROTOCOL_PRIORITY
            and PlayerFeature.SET_MEMBERS in self.supported_features
        ):
            # Player is itself a native endpoint of a known protocol domain.
            result.append(
                OutputProtocol(
                    output_protocol_id=self.player_id,
                    name=self.provider.name,
                    protocol_domain=self.provider.domain,
                    priority=PROTOCOL_PRIORITY[self.provider.domain],
                    available=self.available_for_playback,
                    is_native=True,
                )
            )

        # Add active protocol players
        active_ids: set[str] = set()
        for linked in self.__attr_linked_protocols:
            active_ids.add(linked.output_protocol_id)
            # Check if the protocol player is actually available
            protocol_player = self.mass.players.get_player(linked.output_protocol_id)
            is_available = protocol_player.available_for_playback if protocol_player else False
            # Use provider name if available, else domain title
            if protocol_player:
                name = protocol_player.provider.name
            else:
                name = linked.protocol_domain.title() if linked.protocol_domain else "Unknown"
            result.append(
                OutputProtocol(
                    output_protocol_id=linked.output_protocol_id,
                    name=name,
                    protocol_domain=linked.protocol_domain,
                    priority=linked.priority,
                    available=is_available,
                    derived_from=linked.derived_from,
                )
            )

        # Add disabled protocols from cache
        cached_protocol_ids: list[str] = self.mass.config.get(
            f"{CONF_PLAYERS}/{self.player_id}/values/{CONF_LINKED_PROTOCOL_IDS}",
            [],
        )
        for protocol_id in cached_protocol_ids:
            if protocol_id in active_ids:
                continue  # Already included above
            # Get stored config to determine protocol domain
            if raw_conf := self.mass.config.get(f"{CONF_PLAYERS}/{protocol_id}"):
                provider_id = raw_conf.get("provider", "")
                protocol_domain = provider_id.split("--")[0] if provider_id else "unknown"
                priority = PROTOCOL_PRIORITY.get(protocol_domain, 100)
                # resolve the persisted derived-transport edge (if any) so derived
                # outputs keep their base reference even while not registered
                derived_from = raw_conf.get("values", {}).get(CONF_UNDERLYING_PLAYER_ID)
                if derived_from == self.player_id:
                    derived_from = "native"
                result.append(
                    OutputProtocol(
                        output_protocol_id=protocol_id,
                        name=protocol_domain.title(),
                        protocol_domain=protocol_domain,
                        priority=priority,
                        available=False,  # Disabled protocols are not available
                        derived_from=derived_from,
                    )
                )

        # Sort by priority (lower = more preferred)
        result.sort(key=lambda o: o.priority)
        return result

    @property
    @final
    def linked_output_protocols(self) -> list[OutputProtocol]:
        """Return the list of actively linked output protocol players."""
        return self.__attr_linked_protocols

    @property
    @final
    def protocol_parent_id(self) -> str | None:
        """Return the parent player_id if this is a protocol player linked to a native player."""
        return self.__attr_protocol_parent_id

    @property
    @final
    def underlying_player_id(self) -> str | None:
        """
        Return the player_id this (derived) protocol player runs on top of, if any.

        Set by bridge implementations (e.g. a Sendspin bridge riding on an AirPlay
        player) so the protocol linking layer can resolve the parent deterministically
        instead of relying on device identifier matching.
        """
        return self._attr_underlying_player_id

    @property
    @final
    def active_output_protocol(self) -> str | None:
        """Return the currently active output protocol ID."""
        return self.__attr_active_output_protocol

    @final
    def set_active_output_protocol(self, protocol_id: str | None) -> None:
        """
        Set the currently active output protocol ID.

        :param protocol_id: The protocol player_id to set as active, "native" for native playback,
            or None to clear the active protocol.
        """
        # cancel any pending scheduled protocol clear,
        # as we're explicitly setting it now
        self.mass.cancel_task(f"clear_active_protocol_{self.player_id}")
        if self.__attr_active_output_protocol == protocol_id:
            return  # No change
        if protocol_id == self.player_id:
            protocol_id = "native"  # Normalize to "native" for native player
        if protocol_id:
            protocol_name = protocol_id
            if protocol_id == "native":
                protocol_name = "Native"
            elif protocol_player := self.mass.players.get_player(protocol_id):
                protocol_name = protocol_player.provider.name
            self.logger.info(
                "Setting active output protocol on %s to %s",
                self.display_name,
                protocol_name,
            )
        else:
            self.logger.info(
                "Clearing active output protocol on %s",
                self.display_name,
            )
        self.__attr_active_output_protocol = protocol_id
        self.update_state()

    @final
    def set_linked_output_protocols(self, protocols: list[OutputProtocol]) -> None:
        """
        Set the actively linked output protocol players.

        :param protocols: List of OutputProtocol objects representing active protocol players.
        """
        self.__attr_linked_protocols = protocols
        self.mass.players.trigger_player_update(self.player_id)

    @final
    def set_protocol_parent_id(self, parent_id: str | None) -> None:
        """
        Set the parent player_id for protocol players.

        :param parent_id: The player_id of the parent player, or None to clear.
        """
        self.__attr_protocol_parent_id = parent_id
        self.mass.players.trigger_player_update(self.player_id)

    @final
    def get_linked_protocol(self, protocol_domain: str) -> OutputProtocol | None:
        """Get a linked protocol by domain with current availability."""
        for linked in self.__attr_linked_protocols:
            if linked.protocol_domain == protocol_domain:
                protocol_player = self.mass.players.get_player(linked.output_protocol_id)
                current_available = (
                    protocol_player.available_for_playback if protocol_player else False
                )
                return OutputProtocol(
                    output_protocol_id=linked.output_protocol_id,
                    name=protocol_player.provider.name
                    if protocol_player
                    else linked.protocol_domain.title(),
                    protocol_domain=linked.protocol_domain,
                    priority=linked.priority,
                    available=current_available,
                    is_native=False,
                    derived_from=linked.derived_from,
                )
        return None

    @final
    def get_output_protocol_by_domain(self, protocol_domain: str) -> OutputProtocol | None:
        """
        Get an output protocol by domain, including native protocol.

        Unlike get_linked_protocol, this also checks if the player's native protocol
        matches the requested domain.

        :param protocol_domain: The protocol domain to search for (e.g., "airplay", "sonos").
        """
        for output_protocol in self.output_protocols:
            if output_protocol.protocol_domain == protocol_domain:
                return output_protocol
        return None

    @final
    def get_protocol_player(self, player_id: str) -> Player | None:
        """Get the protocol Player for a given player_id."""
        if player_id == "native":
            return self if PlayerFeature.PLAY_MEDIA in self.supported_features else None
        return self.mass.players.get_player(player_id)

    @final
    def get_preferred_protocol_player(self) -> Player | None:
        """Get the best available protocol player by priority."""
        for linked in sorted(self.__attr_linked_protocols, key=lambda x: x.priority):
            if protocol_player := self.mass.players.get_player(linked.output_protocol_id):
                if protocol_player.available_for_playback:
                    return protocol_player
        return None

    @final
    def mark_state_dirty(self) -> None:
        """
        Mark the player's (final) state as dirty.

        Forces the next update_state call to recalculate the full PlayerState.
        Must be called when state the player derives from changed outside the
        player's own attributes (e.g. group topology, linked protocol players,
        the active queue) - the player controller does this automatically for
        all its notification paths (trigger_player_update and the state fan-out).
        """
        self.__state_dirty = True

    @final
    def refresh_state(self, signal_event: bool = True) -> None:
        """
        Recalculate the player state unconditionally.

        Convenience shorthand for mark_state_dirty() + update_state(), for core
        code reacting to changes outside the player's own attributes.

        :param signal_event: If True, signal the state update event to the PlayerController.
        """
        self.mark_state_dirty()
        self.update_state(signal_event=signal_event)

    @final
    def update_state(self, force_update: bool = False, signal_event: bool = True) -> None:
        """
        Update the PlayerState from the current state of the player.

        This method should be called to update the player's state
        and signal any changes to the PlayerController.

        :param force_update: If True, always recalculate the state, even when no
        (known) own input changed. An update event still only fires when the
        recalculated state actually differs.
        :param signal_event: If True, signal the state update event to the PlayerController.
        """
        self.mass.verify_event_loop_thread("player.update_state")
        # Invalidate the cached properties up front so both the input probe and
        # a recalculation read fresh values; only the config-derived cached
        # properties are retained (set_config invalidates those).
        for key in list(self._cache):
            if key not in _CONFIG_CACHED_PROPS:
                del self._cache[key]
        new_snapshot = self.__collect_input_snapshot()
        if (
            not force_update
            and not self.__state_dirty
            and self.__input_snapshot is not None
            and new_snapshot == self.__input_snapshot
            and not self.__own_position_anchor_moved()
        ):
            # None of the player's own inputs changed since the last calculation:
            # nothing to do. Changes the player derives from other sources
            # (players/queues/config) always come with a mark_state_dirty call.
            return
        self.__state_dirty = False
        self.__input_snapshot = new_snapshot
        current_media = self.current_media
        self.__input_anchor = (
            (self._attr_elapsed_time, self._attr_elapsed_time_last_updated),
            (current_media.elapsed_time, current_media.elapsed_time_last_updated)
            if current_media is not None
            else None,
            self.playback_state == PlaybackState.PLAYING,
        )
        # calculate the new state
        changed_values, position_jumped, media_position_jumped = self.__calculate_player_state()
        if not MEDIA_IDENTITY_KEYS.isdisjoint(changed_values.keys()):
            # current media changed, call the media updated callback
            # debounce the callback to avoid multiple calls when multiple
            # state updates happen in a short time
            self.mass.call_later(
                1, self._on_player_media_updated, task_id=f"player_media_updated_{self.player_id}"
            )
        # persist the default name if it changed
        if self.name and self.config.default_name != self.name:
            self.mass.config.set_player_default_name(self.player_id, self.name)
        # persist the player type if it changed
        if self.type != self._config.player_type:
            self.mass.config.set_player_type(self.player_id, self.type)
        if position_jumped and signal_event:
            # the corrected playback position jumped (seek or buffer correction):
            # this is not an event by itself (only current_media is event-relevant)
            # but the queue timing must re-base on the fresh position right away
            self.mass.players.on_player_position_jumped(self)
        # return early if nothing changed (unless force_update is True)
        if len(changed_values) == 0 and not force_update:
            return

        # signal the state update to the PlayerController
        if signal_event:
            self.mass.players.signal_player_state_update(
                self, changed_values, media_position_jumped=media_position_jumped
            )

    @final
    def set_current_media(  # noqa: PLR0913
        self,
        uri: str,
        media_type: MediaType = MediaType.UNKNOWN,
        title: str | None = None,
        artist: str | None = None,
        album: str | None = None,
        image_url: str | None = None,
        duration: int | None = None,
        source_id: str | None = None,
        queue_item_id: str | None = None,
        custom_data: dict[str, Any] | None = None,
        clear_all: bool = False,
    ) -> None:
        """
        Set current_media helper.

        Assumes use of '_attr_current_media'.
        """
        if self._attr_current_media is None or clear_all:
            self._attr_current_media = PlayerMedia(
                uri=uri,
                media_type=media_type,
            )
        self._attr_current_media.uri = uri
        if media_type != MediaType.UNKNOWN:
            self._attr_current_media.media_type = media_type
        if title:
            self._attr_current_media.title = title
        if artist:
            self._attr_current_media.artist = artist
        if album:
            self._attr_current_media.album = album
        if image_url:
            self._attr_current_media.image_url = image_url
        if duration:
            self._attr_current_media.duration = duration
        if source_id:
            self._attr_current_media.source_id = source_id
        if queue_item_id:
            self._attr_current_media.queue_item_id = queue_item_id
        if custom_data:
            self._attr_current_media.custom_data = custom_data

    @final
    def set_resolved_palette(self, image_url: str, palette: MediaItemPalette) -> None:
        """
        Store the resolved color palette for the currently shown image.

        The palette is resolved asynchronously (from the cache controller) by the
        PlayerController; it is carried on the player here so the synchronous state
        serialization can attach it without blocking. May only be called by the
        PlayerController.

        :param image_url: Image URL the palette was extracted from.
        :param palette: The extracted color palette.
        """
        self._attr_current_palette_url = image_url
        self._attr_current_palette = palette

    @final
    def set_config(self, config: PlayerConfig) -> None:
        """
        Set/update the player config.

        May only be called by the PlayerController.
        """
        # TODO: validate that caller is the PlayerController ?
        self._config = config
        # config feeds several (cached) state values, so invalidate all cached
        # properties (including the config-derived ones) and force a recalculation
        self._cache.clear()
        self.mark_state_dirty()

    @final
    def set_initialized(self) -> None:
        """Set the player as initialized."""
        self.__initialized.set()

    @final
    def to_dict(self) -> dict[str, Any]:
        """Return the (serializable) dict representation of the Player."""
        return self.state.to_dict()

    @final
    def supports_feature(self, feature: PlayerFeature) -> bool:
        """Return True if this player supports the given feature."""
        return feature in self.supported_features

    @final
    def check_feature(self, feature: PlayerFeature) -> None:
        """Check if this player supports the given feature."""
        if not self.supports_feature(feature):
            raise UnsupportedFeaturedException(
                f"Player {self.display_name} does not support feature {feature.name}"
            )

    def _update_setup_data(self, key: str, value: ConfigValueType, immediate: bool = True) -> None:
        """
        Update a single setup_data value for this player (e.g. a rotated pairing credential).

        :param key: The setup data key to update.
        :param value: The new value; strings are encrypted at rest.
        :param immediate: Persist to disk right away (the default) instead of on the
            debounced save timer, so a critical value survives a crash.
        """
        if not self.mass.config.get(f"{CONF_PLAYERS}/{self.player_id}"):
            # only allow setting setup data if the main config entry exists
            msg = f"Invalid player: {self.player_id}"
            raise KeyError(msg)
        stored_value = self.mass.config.encrypt_string(value) if isinstance(value, str) else value
        self.mass.config.set(
            f"{CONF_PLAYERS}/{self.player_id}/setup_data/{key}",
            stored_value,
            immediate=immediate,
        )
        # keep the in-memory config copy in sync with storage
        self.config.setup_data[key] = stored_value

    @final
    def _check_feature_with_active_protocol(
        self, feature: PlayerFeature, active_only: bool = False
    ) -> bool:
        """
        Check if a feature is supported considering the active output protocol.

        If an active output protocol is set (and not native), checks that protocol
        player's features. Otherwise checks the native player's features.

        :param feature: The PlayerFeature to check.
        :return: True if the feature is supported by the active protocol or native player.
        """
        # If active output protocol is set and not native, check protocol player's features
        if (
            self.__attr_active_output_protocol
            and self.__attr_active_output_protocol != "native"
            and (
                protocol_player := self.mass.players.get_player(self.__attr_active_output_protocol)
            )
        ):
            return feature in protocol_player.supported_features
        # Otherwise check native player's features
        return feature in self.supported_features

    @final
    def _get_protocol_player_for_feature(
        self,
        feature: PlayerFeature,
        require_active: bool = True,
    ) -> Player | None:
        """Get player(protocol) which has the given PlayerFeature."""
        # prefer native player
        if feature in self.supported_features:
            return self
        # prefer active (or preferred) protocol player with the feature
        active_protocol = self.active_output_protocol
        if active_protocol and active_protocol != "native":
            protocol_player = self.mass.players.get_player(active_protocol)
            if (
                protocol_player
                and protocol_player.available_for_playback
                and feature in protocol_player.supported_features
            ):
                return protocol_player
        if require_active:
            # if we require active and the active protocol
            # doesn't support the feature, return None
            return None

        # fallback to preferred protocol from config
        preferred_conf = self.mass.config.get_raw_player_config_value(
            self.player_id, CONF_PREFERRED_OUTPUT_PROTOCOL
        )
        if preferred_conf and preferred_conf not in ("auto", "native"):
            preferred_protocol = str(preferred_conf)
            if (
                (_player := self.mass.players.get_player(preferred_protocol))
                and _player.available_for_playback
                and feature in _player.supported_features
            ):
                return _player

        # Otherwise, use the first available linked protocol.
        # Prefer protocols that can process commands without active streaming
        # (cast/dlna can always handle volume, airplay/sendspin only while streaming).
        _control_priority = {"chromecast": 0, "dlna": 1, "airplay": 2, "sendspin": 3}
        for linked in sorted(
            self.linked_output_protocols,
            key=lambda o: _control_priority.get(o.protocol_domain, 10),
        ):
            if (
                (protocol_player := self.mass.players.get_player(linked.output_protocol_id))
                and protocol_player.available_for_playback
                and feature in protocol_player.supported_features
            ):
                return protocol_player

        return None

    @final
    def __collect_input_snapshot(self) -> dict[str, Any]:
        """
        Collect a snapshot of the player's own state-calculation inputs.

        Only covers inputs owned by the player itself (its _attr_ values and
        provider-overridden properties); state the player derives from other
        sources (players/queues/config) is covered by mark_state_dirty instead.
        The playback position anchors are deliberately excluded: they are
        tracked separately with jump detection (__own_position_anchor_moved).
        """
        current_media = self.current_media
        device_info = self._attr_device_info
        return {
            "type": self.type,
            "available": self.available,
            "name": self.name,
            "needs_setup": self.needs_setup,
            "setup_reason": self.setup_reason,
            "playback_state": self.playback_state,
            "powered": self.powered,
            "volume_level": self.volume_level,
            "volume_muted": self.volume_muted,
            "active_source": self.active_source,
            "active_sound_mode": self.active_sound_mode,
            "is_active_session": self.is_active_session,
            "synced_to": self.synced_to if self.__probe_synced_to else None,
            "supported_features": frozenset(self.supported_features),
            "group_members": tuple(self.group_members),
            "static_group_members": tuple(self.static_group_members),
            "can_group_with": frozenset(self.can_group_with),
            "device_info": (
                device_info.model,
                device_info.manufacturer,
                device_info.software_version,
                device_info.model_id,
                device_info.manufacturer_id,
                tuple(sorted(device_info.identifiers.items())),
            ),
            "source_list": tuple(
                (s.id, s.name, s.passive, s.can_play_pause, s.can_seek, s.can_next_previous)
                for s in self.source_list
            ),
            "sound_mode_list": tuple((m.id, m.name, m.passive) for m in self._attr_sound_mode_list),
            "options": tuple((o.key, o.value, o.read_only) for o in self._attr_options),
            "current_media": (
                (
                    current_media.uri,
                    current_media.media_type,
                    current_media.title,
                    current_media.artist,
                    current_media.album,
                    current_media.album_artist,
                    current_media.image_url,
                    current_media.duration,
                    current_media.source_id,
                    current_media.queue_item_id,
                )
                if current_media is not None
                else None
            ),
            "extra_attributes": tuple(
                sorted(
                    (key, _freeze(value))
                    for key, value in self._extra_attributes.items()
                    if key not in ("seq_no", "last_poll")
                )
            ),
            "fake_controls": (
                self._extra_data.get(ATTR_FAKE_POWER),
                self._extra_data.get(ATTR_FAKE_VOLUME),
                self._extra_data.get(ATTR_FAKE_MUTE),
            ),
            "linked_protocols": tuple(
                (p.output_protocol_id, p.protocol_domain, p.priority, p.derived_from)
                for p in self.__attr_linked_protocols
            ),
            "protocol_parent_id": self.__attr_protocol_parent_id,
            "active_output_protocol": self.__attr_active_output_protocol,
            "active_mass_source": self.__active_mass_source,
            "sleep_timer_expires_at": self.__sleep_timer_expires_at,
        }

    @final
    def __own_position_anchor_moved(self) -> bool:
        """Return whether one of the player's own position anchors moved significantly."""
        if (prev := self.__input_anchor) is None:
            return True
        prev_player_anchor, prev_media_anchor, prev_playing = prev
        playing = self.playback_state == PlaybackState.PLAYING
        media = self.current_media
        new_player_anchor = (self._attr_elapsed_time, self._attr_elapsed_time_last_updated)
        new_media_anchor = (
            (media.elapsed_time, media.elapsed_time_last_updated) if media is not None else None
        )
        return _anchor_moved(
            prev_player_anchor, new_player_anchor, prev_playing, playing
        ) or _anchor_moved(prev_media_anchor, new_media_anchor, prev_playing, playing)

    @final
    def __calculate_player_state(
        self,
    ) -> tuple[dict[str, tuple[Any, Any]], bool, bool]:
        """
        Calculate the (current) and FINAL PlayerState.

        This method is called when we're updating the player,
        and we compare the current state with the previous state to determine
        if we need to signal a state change to API consumers.

        Returns a tuple of (changed state values, player position jumped,
        current_media position jumped). The player's own elapsed_time values
        are not part of the changed values: they refresh on every calculation
        but only current_media - which holds the final calculated position -
        is event-relevant. The jump flags drive the position correction logic.
        """
        playback_state, elapsed_time, elapsed_time_last_updated = self.__final_playback_state
        prev_state = self._state
        prev_fingerprint = self.__state_fingerprint or _state_fingerprint(prev_state)
        prev_playing = prev_state.playback_state == PlaybackState.PLAYING
        new_playing = playback_state == PlaybackState.PLAYING
        # detect a discrete jump of the corrected position (seek/buffer correction);
        # the fresh anchor is always adopted into the state
        _, _, position_jumped = _reconcile_position_anchor(
            prev_state.elapsed_time,
            prev_state.elapsed_time_last_updated,
            elapsed_time,
            elapsed_time_last_updated,
            prev_playing,
            new_playing,
            force_adopt=True,
        )
        self._state = PlayerState(
            player_id=self.player_id,
            provider=self.provider_id,
            type=self.type,
            available=self.enabled and self.available and not self.needs_setup,
            device_info=self.device_info,
            supported_features=self.__final_supported_features,
            playback_state=playback_state,
            elapsed_time=elapsed_time,
            elapsed_time_last_updated=elapsed_time_last_updated,
            powered=self.__final_power_state,
            volume_level=self.__final_volume_level,
            volume_muted=self.__final_volume_muted_state,
            group_members=UniqueList(self.__final_group_members),
            static_group_members=UniqueList(self.static_group_members),
            can_group_with=self.__final_can_group_with,
            synced_to=self.__final_synced_to,
            active_source=self.__final_active_source,
            source_list=self.__final_source_list,
            active_group=self.__final_active_group,
            current_media=self.__final_current_media,
            active_sound_mode=self.active_sound_mode,
            sound_mode_list=self.sound_mode_list,
            options=self.options,
            name=self.display_name,
            enabled=self.enabled,
            hide_in_ui=self.hide_in_ui,
            expose_to_ha=self.expose_to_ha,
            icon=self.icon,
            group_volume=self.group_volume,
            group_volume_muted=self.group_volume_muted,
            extra_attributes=self.extra_attributes,
            power_control=self.power_control,
            volume_control=self.volume_control,
            mute_control=self.mute_control,
            output_protocols=self.output_protocols,
            active_output_protocol=self.__attr_active_output_protocol,
            needs_setup=self.needs_setup,
            setup_reason=self.setup_reason,
            has_setup_flow=self.has_setup_flow,
            sleep_timer_expires_at=self.sleep_timer_expires_at,
        )
        media_position_jumped = self.__reconcile_current_media_anchor(
            prev_state, prev_playing, new_playing
        )

        # track stop called state
        if (
            prev_state.playback_state == PlaybackState.IDLE
            and self._state.playback_state != PlaybackState.IDLE
        ):
            self.__stop_called = False
        elif (
            prev_state.playback_state != PlaybackState.IDLE
            and self._state.playback_state == PlaybackState.IDLE
        ):
            self.__stop_called = True
            # when we're going to idle,
            # we want to reset the active mass source after a short delay
            # this is done using a timer which gets reset if the player starts playing again
            # before the timer is up, using the task_id
            self.mass.call_later(
                5, self.set_active_mass_source, None, task_id=f"set_mass_source_{self.player_id}"
            )
        new_fingerprint = _state_fingerprint(self._state)
        self.__state_fingerprint = new_fingerprint
        changed_values: dict[str, tuple[Any, Any]] = {}
        for key in prev_fingerprint.keys() | new_fingerprint.keys():
            old_value = prev_fingerprint.get(key)
            new_value = new_fingerprint.get(key)
            if old_value != new_value:
                changed_values[key] = (old_value, new_value)
        if "current_media" in changed_values:
            # media appeared/disappeared: collapse the leaf keys into the single
            # top-level key carrying the actual (old, new) media objects
            for key in [key for key in changed_values if key.startswith("current_media.")]:
                del changed_values[key]
            changed_values["current_media"] = (prev_state.current_media, self._state.current_media)
        if "options" in changed_values:
            # the PLAYER_OPTIONS_UPDATED event carries the actual (old, new) options
            changed_values["options"] = (prev_state.options, self._state.options)
        return changed_values, position_jumped, media_position_jumped

    @final
    def __reconcile_current_media_anchor(
        self, prev_state: PlayerState, prev_playing: bool, new_playing: bool
    ) -> bool:
        """
        Reconcile the position anchor on the freshly calculated current_media.

        Keeps the previous anchor while it extrapolates to the same corrected
        position (steady playback), so regular ticks don't change the state.

        Returns True when the corrected current_media position jumped (seek or
        buffer correction reached the current media).
        """
        prev_media = prev_state.current_media
        new_media = self._state.current_media
        if new_media is None or prev_media is None:
            return False
        if (new_media.queue_item_id or new_media.uri) != (
            prev_media.queue_item_id or prev_media.uri
        ):
            # different item loaded - adopt the new anchor as-is
            return False
        # Players that mirror another player's media (grouped/synced members,
        # protocol children) share the owner's PlayerMedia object, which the owner
        # already reconciled - only report the jump, never mutate the shared object.
        mirrors_parent = bool(
            self.__final_active_group
            or self.__final_synced_to
            or (self.type == PlayerType.PROTOCOL and self.__attr_protocol_parent_id)
        )
        _, _, jumped = _reconcile_position_anchor(
            prev_media.elapsed_time,
            prev_media.elapsed_time_last_updated,
            new_media.elapsed_time,
            new_media.elapsed_time_last_updated,
            prev_playing,
            new_playing,
            force_adopt=mirrors_parent,
        )
        if not mirrors_parent and not jumped:
            # steady playback: keep the previous anchor so nothing changed
            new_media.elapsed_time = prev_media.elapsed_time
            new_media.elapsed_time_last_updated = prev_media.elapsed_time_last_updated
        return jumped

    @cached_property
    @final
    def __final_playback_state(self) -> tuple[PlaybackState, float | None, float | None]:
        """
        Return the FINAL playback state based on the playercontrol which may have been set-up.

        Returns a tuple of (playback_state, elapsed_time, elapsed_time_last_updated).
        """
        # Determine base state from protocol player, parent/group, or self.
        playback_state: PlaybackState
        elapsed_time: float | None
        elapsed_time_last_updated: float | None

        # If an output protocol is active (and not native),
        # use the protocol player's state as the source of truth
        if (
            self.__attr_active_output_protocol
            and self.__attr_active_output_protocol != "native"
            and (
                protocol_player := self.mass.players.get_player(self.__attr_active_output_protocol)
            )
        ):
            playback_state = protocol_player.state.playback_state
            elapsed_time = protocol_player.state.elapsed_time
            elapsed_time_last_updated = protocol_player.state.elapsed_time_last_updated
        # If we're synced to another player, mirror the leader's state so that
        # synced clients report the same playback info as their leader.
        elif (parent_id := self.__final_synced_to) and (
            parent_player := self.mass.players.get_player(parent_id)
        ):
            playback_state = parent_player.state.playback_state
            elapsed_time = parent_player.state.elapsed_time
            elapsed_time_last_updated = parent_player.state.elapsed_time_last_updated
        else:
            playback_state = self.playback_state
            elapsed_time = self.elapsed_time
            elapsed_time_last_updated = self.elapsed_time_last_updated

        # If the active queue item is an AudioSource with upstream-clock
        # metadata (e.g. Spotify Connect / AirPlay / Yandex Ynison reporting
        # the source's logical position), prefer that over the protocol /
        # self elapsed_time — the latter tracks bytes consumed, which is the
        # wrong clock for live plugin sources (loses upstream seeks and
        # pause-resume on the queue's corrected_elapsed_time, which the
        # player_queues controller and several player providers consume).
        # A group player outputs the AudioSource from its own queue, which
        # __final_active_source may not resolve to, so the group's own queue
        # is also consulted.
        candidate_source_ids = [self.__final_active_source]
        if self.type == PlayerType.GROUP:
            candidate_source_ids.append(self.player_id)
        for source_id in candidate_source_ids:
            if (
                source_id
                and (queue := self.mass.player_queues.get(source_id))
                and (current_item := queue.current_item) is not None
                and (sd := current_item.streamdetails) is not None
                and sd.media_type == MediaType.AUDIO_SOURCE
                and sd.stream_metadata is not None
                and sd.stream_metadata.elapsed_time is not None
            ):
                elapsed_time = sd.stream_metadata.elapsed_time
                elapsed_time_last_updated = (
                    sd.stream_metadata.elapsed_time_last_updated or time.time()
                )
                break

        return (playback_state, elapsed_time, elapsed_time_last_updated)

    @cached_property
    @final
    def __final_power_state(self) -> bool | None:
        """Return the FINAL power state based on the playercontrol which may have been set-up."""
        power_control = self.power_control
        if power_control == PLAYER_CONTROL_FAKE:
            return bool(self.extra_data.get(ATTR_FAKE_POWER, False))
        if power_control == PLAYER_CONTROL_NATIVE:
            return self.powered
        if power_control == PLAYER_CONTROL_NONE:
            return None
        # handle protocol player as power control
        if player_ctrl := self.mass.players.get_player(power_control):
            if player_ctrl.powered is not None:
                return player_ctrl.powered
        # handle player control for power if set
        if ext_ctrl := self.mass.players.get_player_control(power_control):
            return ext_ctrl.power_state
        return None

    @cached_property
    @final
    def __final_volume_level(self) -> int | None:
        """Return the FINAL volume level based on the playercontrol which may have been set-up."""
        volume_control = self.volume_control
        if volume_control == PLAYER_CONTROL_FAKE:
            # Fake volume is already stored as logical (0-100)
            return int(self.extra_data.get(ATTR_FAKE_VOLUME, 0))
        if volume_control == PLAYER_CONTROL_NATIVE:
            # Scale device volume back to logical (0-100)
            if self.volume_level is None:
                return None
            return self.mass.players.scale_volume_from_device(self.player_id, self.volume_level)
        if volume_control == PLAYER_CONTROL_NONE:
            return None
        # handle protocol player as volume control
        if control := self.mass.players.get_player(volume_control):
            control_volume = control.volume_level
            if (
                control_volume == 0
                and control.player_id != self.active_output_protocol
                and any(
                    linked.output_protocol_id == control.player_id
                    for linked in self.linked_output_protocols
                )
            ):
                # A linked protocol interface that is not actively rendering audio
                # may report volume 0 while the device is in standby (e.g. the cast
                # side of some devices), which doesn't reflect the real device volume.
                # Treat it as unknown so we fall back to other sources instead of
                # propagating a spurious hard mute.
                control_volume = None
            if control_volume is not None:
                return self.mass.players.scale_volume_from_device(self.player_id, control_volume)
        # handle player control for volume if set
        if player_control := self.mass.players.get_player_control(volume_control):
            if player_control.volume_level is not None:
                return self.mass.players.scale_volume_from_device(
                    self.player_id, player_control.volume_level
                )
        # control not (yet) available or has no volume, fall back to native
        if self.volume_level is None:
            return None
        return self.mass.players.scale_volume_from_device(self.player_id, self.volume_level)

    @cached_property
    @final
    def __final_volume_muted_state(self) -> bool | None:
        """Return the FINAL mute state based on any playercontrol which may have been set-up."""
        mute_control = self.mute_control
        if mute_control == PLAYER_CONTROL_FAKE:
            return bool(self.extra_data.get(ATTR_FAKE_MUTE, False))
        if mute_control == PLAYER_CONTROL_NATIVE:
            return self.volume_muted
        if mute_control == PLAYER_CONTROL_NONE:
            return None
        # handle protocol player as mute control
        if control := self.mass.players.get_player(mute_control):
            if control.volume_muted is not None:
                return control.volume_muted
        # handle player control for mute if set
        if player_control := self.mass.players.get_player_control(mute_control):
            if player_control.volume_muted is not None:
                return player_control.volume_muted
        # control not (yet) available or has no mute state, fall back to native
        return self.volume_muted

    @cached_property
    @final
    def __final_active_group(self) -> str | None:
        """
        Return the player id of any playergroup that is currently active for this player.

        This will return the id of the groupplayer if any groups are active.
        If no groups are currently active, this will return None.
        """
        if self.type == PlayerType.PROTOCOL:
            # protocol players should not have an active group,
            # they follow the group state of their parent player
            return None
        for group_player in self.mass.players.all_players(
            return_unavailable=False, return_disabled=False
        ):
            if group_player.type != PlayerType.GROUP:
                continue
            if group_player.player_id == self.player_id:
                continue
            # Use the raw `powered` attribute (not `state.powered`) so the
            # check reflects what the group player itself believes — for
            # native/fake control the group's `power()` method sets
            # `_attr_powered` directly. `state.powered` routes through
            # `__final_power_state` which may return None for power_control
            # == NONE even though the group is actively capturing members.
            powered = group_player.powered
            if powered is False:
                # explicit power-off (fake or native) - never captures members
                continue
            if powered is not True and not group_player.is_active_session:
                # no explicit power-on and no captured session - group is dormant,
                # configured members are free to be controlled individually
                continue
            if self.player_id in group_player.state.group_members:
                return group_player.player_id
        return None

    @cached_property
    @final
    def __final_current_media(self) -> PlayerMedia | None:
        """Return the FINAL current media for the player."""
        # if the player is grouped/synced, use the current_media of the group/parent player
        if parent_player_id := (self.__final_active_group or self.__final_synced_to):
            if parent_player_id != self.player_id and (
                parent_player := self.mass.players.get_player(parent_player_id)
            ):
                return parent_player.state.current_media
            return None  # if parent player not found, return None for current media
        # if this is a protocol player, use the current_media of the parent player
        if self.type == PlayerType.PROTOCOL and self.__attr_protocol_parent_id:
            if parent_player := self.mass.players.get_player(self.__attr_protocol_parent_id):
                return parent_player.state.current_media
        # if MA queue is active, return those details
        active_source = self.__final_active_source
        active_queue: PlayerQueue | None = None
        if not active_queue and active_source:
            active_queue = self.mass.player_queues.get(active_source)
        if not active_queue and self.active_source is None:
            active_queue = self.mass.player_queues.get(self.player_id)
        if active_queue and (current_item := active_queue.current_item):
            item_image_url = (
                # the image format needs to be 512x512 jpeg for maximum compatibility with players
                self.mass.metadata.get_image_url(current_item.image, size=512)
                if current_item.image
                else None
            )
            if current_item.streamdetails and (
                stream_metadata := current_item.streamdetails.stream_metadata
            ):
                # handle stream metadata in streamdetails (e.g. for radio stream)
                image_url = stream_metadata.image_url or item_image_url
                return PlayerMedia(
                    uri=current_item.uri,
                    media_type=current_item.media_type,
                    title=stream_metadata.title or current_item.name,
                    artist=stream_metadata.artist,
                    album=stream_metadata.album or stream_metadata.description or current_item.name,
                    image_url=image_url,
                    palette=self._resolved_palette(image_url),
                    duration=stream_metadata.duration or current_item.duration,
                    source_id=active_queue.queue_id,
                    queue_item_id=current_item.queue_item_id,
                    elapsed_time=stream_metadata.elapsed_time or int(active_queue.elapsed_time),
                    elapsed_time_last_updated=stream_metadata.elapsed_time_last_updated
                    or active_queue.elapsed_time_last_updated,
                )
            if media_item := current_item.media_item:
                # normal media item
                # we use getattr here to avoid issues with different media item types
                version = getattr(media_item, "version", None)
                album = getattr(media_item, "album", None)
                podcast = getattr(media_item, "podcast", None)
                metadata = getattr(media_item, "metadata", None)
                description = getattr(metadata, "description", None) if metadata else None
                if description:
                    # descriptions may contain HTML markup; the OSD shows plain text
                    description = html_to_markdown(description)
                image_url = (
                    self.mass.metadata.get_image_url(current_item.media_item.image, size=512)
                    or item_image_url
                    if current_item.media_item.image
                    else item_image_url
                )
                return PlayerMedia(
                    uri=str(media_item.uri),
                    media_type=media_item.media_type,
                    title=f"{media_item.name} ({version})" if version else media_item.name,
                    artist=getattr(media_item, "artist_str", None),
                    album=album.name if album else podcast.name if podcast else description,
                    album_artist=getattr(album, "artist_str", None),
                    image_url=image_url,
                    palette=self._resolved_palette(image_url),
                    duration=media_item.duration,
                    source_id=active_queue.queue_id,
                    queue_item_id=current_item.queue_item_id,
                    elapsed_time=int(active_queue.elapsed_time),
                    elapsed_time_last_updated=active_queue.elapsed_time_last_updated,
                )

            # fallback to basic current item details
            return PlayerMedia(
                uri=current_item.uri,
                media_type=current_item.media_type,
                title=current_item.name,
                image_url=item_image_url,
                palette=self._resolved_palette(item_image_url),
                duration=current_item.duration,
                source_id=active_queue.queue_id,
                queue_item_id=current_item.queue_item_id,
                elapsed_time=int(active_queue.elapsed_time),
                elapsed_time_last_updated=active_queue.elapsed_time_last_updated,
            )
        if active_queue:
            # queue is active but no current item
            return None
        # return native current media if no group/queue is active
        if self.current_media:
            image_url = self.current_media.image_url
            return PlayerMedia(
                uri=self.current_media.uri,
                media_type=self.current_media.media_type,
                title=self.current_media.title,
                artist=self.current_media.artist,
                album=self.current_media.album,
                image_url=image_url,
                palette=self._resolved_palette(image_url),
                duration=self.current_media.duration,
                source_id=self.current_media.source_id or active_source,
                queue_item_id=self.current_media.queue_item_id,
                elapsed_time=self.current_media.elapsed_time or int(self.elapsed_time)
                if self.elapsed_time
                else None,
                elapsed_time_last_updated=self.current_media.elapsed_time_last_updated
                or self.elapsed_time_last_updated,
            )
        return None

    def _resolved_palette(self, image_url: str | None) -> MediaItemPalette | None:
        """Return the carried palette if it matches image_url, else None."""
        if image_url and image_url == self._attr_current_palette_url:
            return self._attr_current_palette
        return None

    @cached_property
    @final
    def __final_source_list(self) -> UniqueList[PlayerSource]:
        """Return the FINAL source list for the player."""
        sources = UniqueList(self.source_list)
        if self.type == PlayerType.PROTOCOL:
            return sources
        # always ensure the Music Assistant Queue is in the source list
        mass_source = next((x for x in sources if x.id == self.player_id), None)
        if mass_source is None:
            # if the MA queue is not in the source list, add it.
            # The capability flags reflect what the queue can actually do right now, so clients can
            # grey out controls instead of issuing commands that can only fail: an empty queue has
            # nothing to play, seek or skip through, and a queue that played to its end can only be
            # started over, with nothing left to seek within or skip to.
            queue = self.mass.player_queues.get(self.player_id)
            queue_has_items = bool(queue and queue.items)
            queue_running = queue_has_items and not (queue and queue.ended)
            mass_source = PlayerSource(
                id=self.player_id,
                name="Music Assistant Queue",
                passive=False,
                can_play_pause=queue_has_items,
                can_seek=queue_running,
                can_next_previous=queue_running,
            )
            sources.append(mass_source)
        return sources

    @cached_property
    @final
    def __final_group_members(self) -> list[str]:
        """Return the FINAL group members of this player."""
        if self.__final_synced_to:
            # If player is synced to another player, it has no group members itself
            return []

        # Start by translating native group_members to visible player IDs
        # This handles cases where a native player (e.g., native AirPlay) has grouped
        # protocol players (e.g., Sonos AirPlay protocol players) that need translation
        members: list[str] = []
        if self.type == PlayerType.PROTOCOL:
            # protocol players use their own group members without translation
            members.extend(self.group_members)
        else:
            translated_members = self._translate_protocol_ids_to_visible(set(self.group_members))
            for member in translated_members:
                if member.player_id not in members:
                    members.append(member.player_id)

        # If there's an active linked protocol, include its group members (translated)
        if self.__attr_active_output_protocol and self.__attr_active_output_protocol != "native":
            if protocol_player := self.mass.players.get_player(self.__attr_active_output_protocol):
                # Translate protocol player IDs to visible player IDs
                protocol_members = self._translate_protocol_ids_to_visible(
                    set(protocol_player.group_members)
                )
                for member in protocol_members:
                    if member.player_id not in members:
                        members.append(member.player_id)

        if self.type != PlayerType.GROUP:
            # Ensure the player_id is first in the group_members list
            if len(members) > 0 and members[0] != self.player_id:
                members = [self.player_id, *[m for m in members if m != self.player_id]]
            # If the only member is self, return empty list
            if members == [self.player_id]:
                return []
        return members

    @cached_property
    @final
    def __final_synced_to(self) -> str | None:
        """
        Return the FINAL synced_to state.

        This checks both native sync state and protocol player sync state,
        translating protocol player IDs to visible player IDs.
        """
        # First check the native synced_to from the property
        if native_synced_to := self.synced_to:
            if sync_parent := self.mass.players.get_player(native_synced_to):
                return sync_parent.protocol_parent_id or sync_parent.player_id

            return native_synced_to
        # check if any of the linked protocol players are synced,
        # and if so, return the visible player they are synced to
        for linked in self.__attr_linked_protocols:
            if not (protocol_player := self.mass.players.get_player(linked.output_protocol_id)):
                continue
            if protocol_player.synced_to:
                # Protocol player is synced, translate to visible player
                if proto_sync_parent := self.mass.players.get_player(protocol_player.synced_to):
                    if proto_sync_parent.type != PlayerType.PROTOCOL:
                        # Sync parent is already a visible player (e.g., native AirPlay player)
                        return proto_sync_parent.player_id
                    if proto_sync_parent.protocol_parent_id and (
                        parent := self.mass.players.get_player(proto_sync_parent.protocol_parent_id)
                    ):
                        # Sync parent is a protocol player, return its visible parent
                        return parent.player_id

        return None

    @cached_property
    @final
    def __final_supported_features(self) -> set[PlayerFeature]:
        """Return the FINAL supported features based supported output protocol(s)."""
        base_features = self.supported_features.copy()
        if self.__attr_active_output_protocol and self.__attr_active_output_protocol != "native":
            # Active linked protocol: add from that specific protocol
            if protocol_player := self.mass.players.get_player(self.__attr_active_output_protocol):
                for feature in protocol_player.supported_features:
                    if feature in ACTIVE_PROTOCOL_FEATURES:
                        base_features.add(feature)
        # Append (allowed features) from all linked protocols
        for linked in self.__attr_linked_protocols:
            if protocol_player := self.mass.players.get_player(linked.output_protocol_id):
                for feature in protocol_player.supported_features:
                    if feature in PROTOCOL_FEATURES:
                        base_features.add(feature)
        if self.power_control != PLAYER_CONTROL_NONE:
            base_features.add(PlayerFeature.POWER)
        else:
            base_features.discard(PlayerFeature.POWER)
        if self.volume_control != PLAYER_CONTROL_NONE:
            base_features.add(PlayerFeature.VOLUME_SET)
        else:
            base_features.discard(PlayerFeature.VOLUME_SET)
        if self.mute_control != PLAYER_CONTROL_NONE:
            base_features.add(PlayerFeature.VOLUME_MUTE)
        else:
            base_features.discard(PlayerFeature.VOLUME_MUTE)
        if sum(1 for s in self.__final_source_list if not s.passive) >= 2:
            base_features.add(PlayerFeature.SELECT_SOURCE)
        return base_features

    @cached_property
    @final
    def __final_can_group_with(self) -> set[str]:
        """
        Return the FINAL set of player id's this player can group with.

        This is a convenience property which calculates the final can_group_with set
        based on any linked protocol players and current player/grouped state.

        If player is synced to a native parent: return empty set (already grouped).
        If player is synced to a protocol: can still group with other players.
        If no active linked protocol: return can_group_with from all active output protocols.
        If active linked protocol: return native can_group_with + active protocol's.

        All protocol player IDs are translated to their visible parent player IDs.
        """

        def _should_include_player(player: Player) -> bool:
            """Check if a player should be included in the can-group-with set."""
            if not player.available:
                return False
            if player.player_id == self.player_id:
                return False  # Don't include self
            # Don't include (playing) players that have group members (they are group leaders)
            if (  # noqa: SIM103
                player.state.playback_state in (PlaybackState.PLAYING, PlaybackState.PAUSED)
                and player.group_members
            ):
                return False
            return True

        if self.__final_synced_to:
            # player is already synced/grouped, cannot group with others
            return set()

        expanded_can_group_with = self._expand_can_group_with()
        # Scenario 1: Player is a protocol player - just return the (expanded) result
        if self.type == PlayerType.PROTOCOL:
            return {x.player_id for x in expanded_can_group_with}

        result: set[str] = set()
        # always start with the native can_group_with options (expanded from provider instance IDs)
        # NOTE we need to translate protocol player IDs to visible player IDs here as well,
        # to cover cases where a native player (e.g., native AirPlay) has grouped protocol players
        # (e.g., Sonos AirPlay protocol players)
        for player in expanded_can_group_with:
            if player.type == PlayerType.PROTOCOL:
                if not player.protocol_parent_id:
                    continue
                parent_player = self.mass.players.get_player(player.protocol_parent_id)
                if not parent_player or not _should_include_player(parent_player):
                    continue
                result.add(parent_player.player_id)
            elif _should_include_player(player):
                result.add(player.player_id)

        # Scenario 2: External source is active - don't include protocol-based grouping
        # When an external source (e.g., Spotify Connect, TV) is active, grouping via
        # protocols (AirPlay, Sendspin, etc.) wouldn't work - only native grouping is available.
        if self._has_external_source_active():
            return result

        # Translate can_group_with from active linked protocol(s) and add to result
        for linked in self.__attr_linked_protocols:
            if protocol_player := self.mass.players.get_player(linked.output_protocol_id):
                for player in self._translate_protocol_ids_to_visible(
                    protocol_player.state.can_group_with
                ):
                    if not _should_include_player(player):
                        continue
                    result.add(player.player_id)
        return result

    @cached_property
    @final
    def __final_active_source(self) -> str | None:
        """
        Calculate the final active source based on any group memberships, source plugins etc.

        This is rather complicated as we need to account for various scenarios like:
            - player is grouped/synced: use the active source of the group/parent player
            - protocol player: prefer the active source of the parent player
            - plugin source active: return the active plugin source
            - linked protocol active: prefer the active source of the linked protocol player
            - a protocol player may report an active source that is actually from an
                active output protocol (e.g. AirPlay)
            - a protocol player that has a 3rd party source active
        """
        # if the player is grouped/synced, use the active source of the group/parent player
        if parent_player_id := (self.__final_synced_to or self.__final_active_group):
            if parent_player := self.mass.players.get_player(parent_player_id):
                return parent_player.state.active_source
            return None  # should not happen but just in case

        # if this is a protocol player, prefer the active source of the parent player
        # a protocol player can not have an active source on its own.
        if (
            self.type == PlayerType.PROTOCOL
            and self.protocol_parent_id
            and (parent_player := self.mass.players.get_player(self.protocol_parent_id))
        ):
            return parent_player.state.active_source

        # always prefer active MA source but add a guard to detect if player is really playing
        # something different, such as a line-in or TV input, we use an explicit list here
        # because many players do not accurately report the active_source
        # this way, for the obvious cases, we can detect a source "takeover"
        if self.__active_mass_source and (
            not self.active_source or self.active_source.lower() not in EXTERNAL_SOURCES
        ):
            return self.__active_mass_source

        # active source as reported by the player itself
        if (
            self.active_source
            and self.active_source != self.player_id
            and self.playback_state != PlaybackState.IDLE
            # If an output protocol is active, we simply overrule the active source of the player.
            # Trying to handle this differently is a hot mess and leads to all kinds of edge cases,
            # because many players do not report the active source correctly, especially not when
            # an output protocol is active.
            and self.active_output_protocol in (None, "native")
        ):
            return self.active_source

        # return the (last) known MA source - fallback to player's own queue source if none
        return self.__active_mass_source or self.player_id

    @final
    def _translate_protocol_ids_to_visible(self, player_ids: set[str]) -> set[Player]:
        """
        Translate protocol player IDs to their visible parent players.

        Protocol players are hidden and users interact with visible players
        (native or universal). This method translates protocol player IDs
        back to the visible (parent) players.

        :param player_ids: Set of player IDs.
        :return: Set of visible players.
        """
        result: set[Player] = set()
        if not player_ids:
            return result
        for player_id in player_ids:
            target_player = self.mass.players.get_player(player_id)
            if not target_player:
                continue
            if target_player.type != PlayerType.PROTOCOL:
                # Non-protocol player is already visible - include directly
                result.add(target_player)
                continue
            # This is a protocol player - find its visible parent
            if not target_player.protocol_parent_id:
                continue
            parent_player = self.mass.players.get_player(target_player.protocol_parent_id)
            if not parent_player:
                continue
            result.add(parent_player)
        return result

    @final
    def _has_external_source_active(self) -> bool:
        """
        Check if an external (non-MA-managed) source is currently active.

        External sources include things like Spotify Connect, TV input, etc.
        When an external source is active, protocol-based grouping is not available.

        :return: True if an external source is active, False otherwise.
        """
        active_source = self.__final_active_source
        if active_source is None:
            return False

        # Player's own ID means MA queue is (or was) active
        if active_source == self.player_id:
            return False

        # If it's a known queue ID it's MA-managed; anything else is external
        # (line-in, TV input, etc.)
        return self.mass.player_queues.get(active_source) is None

    @final
    def _expand_can_group_with(self) -> set[Player]:
        """
        Expand the 'can-group-with' to include all players from provider instance IDs.

        This method expands any provider instance IDs (e.g., "airplay", "chromecast")
        in the group members to all (available) players of that provider

        :return: Set of available players in the can-group-with.
        """
        result = set()

        for member_id in self.can_group_with:
            if player := self.mass.players.get_player(member_id):
                result.add(player)
                continue  # already a player ID
            # Check if member_id is a provider instance ID
            if provider := self.mass.get_provider(member_id):
                for player in self.mass.players.all_players(
                    return_unavailable=False,  # Only include available players
                    provider_filter=provider.instance_id,
                    return_protocol_players=True,
                ):
                    result.add(player)
        return result

    # The id of the (last) active mass source.
    # This is to keep track of the last active MA source for the player,
    # so we can restore it when needed (e.g. after switching to a plugin source).
    __active_mass_source: str | None = None

    @final
    def set_active_mass_source(self, value: str | None) -> None:
        """
        Set the id of the (last) active mass source.

        This is to keep track of the last active MA source for the player,
        so we can restore it when needed (e.g. after switching to a plugin source).
        """
        self.mass.cancel_timer(f"set_mass_source_{self.player_id}")
        self.__active_mass_source = value
        self.update_state()

    __sleep_timer_expires_at: float | None = None

    @final
    def set_sleep_timer_expires_at(self, value: float | None) -> None:
        """
        Set the unix (utc) timestamp at which the active sleep timer stops playback.

        :param value: The expiry timestamp, or None to clear the sleep timer.
        """
        self.__sleep_timer_expires_at = value

    @property
    @final
    def sleep_timer_expires_at(self) -> float | None:
        """Return the unix (utc) timestamp at which the active sleep timer stops playback."""
        return self.__sleep_timer_expires_at

    __stop_called: bool = False

    @final
    def mark_stop_called(self) -> None:
        """Mark that the STOP command was called on the player."""
        self.__stop_called = True

    @property
    @final
    def stop_called(self) -> bool:
        """
        Return True if the STOP command was called on the player.

        This is used to differentiate between a user-initiated stop
        and a natural end of playback (e.g. end of track/queue).
        mainly for debugging/logging purposes by the streams controller.
        """
        return self.__stop_called

    def __hash__(self) -> int:
        """Return a hash of the Player."""
        return hash(self.player_id)

    def __str__(self) -> str:
        """Return a string representation of the Player."""
        return f"Player {self.name} ({self.player_id})"

    def __repr__(self) -> str:
        """Return a string representation of the Player."""
        return f"<Player name={self.name} id={self.player_id} available={self.available}>"

    def __eq__(self, other: object) -> bool:
        """Check equality of two Player objects."""
        if not isinstance(other, Player):
            return False
        return self.player_id == other.player_id

    def __ne__(self, other: object) -> bool:
        """Check inequality of two Player objects."""
        return not self.__eq__(other)


__all__ = [
    # explicitly re-export the models we imported from the models package,
    # for convenience reasons
    "EXTRA_ATTRIBUTES_TYPES",
    "DeviceInfo",
    "Player",
    "PlayerMedia",
    "PlayerSource",
    "PlayerState",
]
