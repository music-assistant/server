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
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, TypeVar, cast, final, overload

from music_assistant_models.config_entries import MULTI_VALUE_SPLITTER, ConfigValueType
from music_assistant_models.constants import (
    EXTRA_ATTRIBUTES_TYPES,
    PLAYER_CONTROL_FAKE,
    PLAYER_CONTROL_NATIVE,
    PLAYER_CONTROL_NONE,
)
from music_assistant_models.enums import (
    MediaType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
    ProviderFeature,
    SleepTimerMode,
)
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
    ATTR_FAKE_MUTE,
    ATTR_FAKE_POWER,
    ATTR_FAKE_VOLUME,
    ATTR_POWER_ON_IN_PROGRESS,
    ATTR_REACHABLE,
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
    PLAYER_CONTROL_PROTOCOL,
    PROTOCOL_PRIORITY,
)
from music_assistant.helpers.player import get_default_player_icon
from music_assistant.models.player_state import (
    PeerSnapshot,
    ProtocolSnapshot,
    QueueSnapshot,
    StateInputs,
    derive_player_state,
    reconcile_position_anchor,
    resolve_synced_to,
)
from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import (
        ConfigActionResult,
        ConfigEntry,
        PlayerConfig,
    )
    from music_assistant_models.enums import RepeatMode
    from music_assistant_models.media_items import AudioSource, MediaItemPalette
    from music_assistant_models.player_queue import PlayerQueue

    from .player_provider import PlayerProvider
    from .setup_flow import SetupSession

# TypeVar for config value type inference
_ConfigValueT = TypeVar("_ConfigValueT", bound=ConfigValueType)


def _clamp_elapsed_time(elapsed_time: float | None) -> float | None:
    """Return elapsed_time clamped to a non-negative value."""
    return max(0.0, elapsed_time) if elapsed_time is not None else None


# Changes to any of these state keys fire the (debounced) on_player_media_updated
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
_CONFIG_CACHED_PROPS = frozenset({"hide_in_ui", "expose_to_ha"})


class AnnouncementFeature(Enum):
    """
    Server-side hints about how a player behaves during an announcement.

    Read by the players controller to route and time an announcement. Not part of
    PlayerFeature and never sent to clients.
    """

    SUPPORTS_VOLUME = "supports_volume"
    """The native announcement route applies a requested volume level."""

    APPLIES_VOLUME = "applies_volume"
    """The player mixes the clip into running audio and sets/restores the level itself."""

    COORDINATES_START = "coordinates_start"
    """
    The player lines up its start with the other members of a group announcement.

    A player reporting this is expected to also report SUPPORTS_VOLUME, so a group
    announcement that fans out to its members is never diverted to the builtin path.
    """


@dataclass(frozen=True, slots=True)
class LinkedOutputProtocol:
    """
    A protocol player linked to a parent player.

    Records the link itself (which protocol player, on which domain, how preferred)
    and nothing about its current state: whether the protocol can actually be reached
    right now is answered by Player.output_protocols / Player.playback_domains, which
    resolve it from the live protocol player on every read.

    :param output_protocol_id: player_id of the linked protocol player.
    :param protocol_domain: Domain of the protocol, e.g. "airplay" or "dlna".
    :param priority: Selection preference, lower is more preferred.
    :param derived_from: output_protocol_id of the base output this transport runs on
        top of ("native" when it rides on the parent player itself), if any.
    """

    output_protocol_id: str
    protocol_domain: str
    priority: int = 100
    derived_from: str | None = None


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
    _, _, jumped = reconcile_position_anchor(
        prev_position,
        prev_timestamp,
        new_position,
        new_timestamp,
        prev_playing,
        new_playing,
        time.time(),
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
    fingerprint[f"{prefix}.queue_session_id"] = media.queue_session_id
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
        "active_source_audio": (
            _freeze(state.active_source_audio.to_dict())
            if state.active_source_audio is not None
            else None
        ),
        "active_group": state.active_group,
        "enabled": state.enabled,
        "hide_in_ui": state.hide_in_ui,
        "private": state.private,
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
        "sleep_timer_mode": state.sleep_timer_mode,
        "supported_features": frozenset(state.supported_features),
        "sound_mode_list": tuple((m.id, m.name, m.passive) for m in state.sound_mode_list),
        "options": tuple((o.key, o.value, o.read_only) for o in state.options),
        "source_list": tuple(
            (
                s.id,
                s.name,
                s.passive,
                s.can_play_pause,
                s.can_seek,
                s.can_next_previous,
                s.can_shuffle,
                s.can_repeat,
                s.shuffle_enabled,
                s.repeat_mode,
            )
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


def _protocol_snapshot(protocol_id: str, player: Player, synced_to: str | None) -> ProtocolSnapshot:
    """
    Return what a provider object reports for its player.

    :param protocol_id: Player id the provider object is registered under.
    :param player: The provider object.
    :param synced_to: Id of the player it reports to be synced to.
    """
    return ProtocolSnapshot(
        protocol_id=protocol_id,
        name=player.name,
        available=player.available,
        available_for_playback=player.available_for_playback,
        needs_setup=player.needs_setup,
        setup_reason=player.setup_reason,
        supported_features=player.supported_features,
        playback_state=player.playback_state,
        elapsed_time=player.elapsed_time,
        elapsed_time_last_updated=player.elapsed_time_last_updated,
        active_source=player.active_source,
        source_list=player.source_list,
        current_media=player.current_media,
        group_members=player.group_members,
        synced_to=synced_to,
    )


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
    _attr_private: bool = False
    _attr_expose_to_ha_by_default: bool = True
    _attr_enabled_by_default: bool = True
    _attr_needs_setup: bool = False
    _attr_setup_reason: str | None = None
    _attr_supported_sample_rates: list[tuple[int, int]] | None = None
    _attr_underlying_player_id: str | None = None
    # Seconds an external source may sit paused before its session is considered ended.
    # Set this on players whose device keeps a source such as Spotify Connect loaded and
    # paused after the app released it, offering nothing that tells an abandoned session
    # apart from a real pause - time is then the only signal left. Leave at None for
    # devices that report a source they no longer play as stopped by themselves.
    _attr_external_pause_idle_timeout: int | None = None
    # Set this on players whose device names the service it plays itself, so a source
    # on its own source list counts as a takeover of the remembered MA queue. Leave at
    # False for devices that also list the transport our own stream arrives on.
    _attr_trusts_reported_source: bool = False

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
        self.__attr_linked_protocols: list[LinkedOutputProtocol] = []
        self.__attr_protocol_parent_id: str | None = None
        self.__attr_active_output_protocol: str | None = None
        self._player_id = player_id
        self._provider = provider
        self._config = self.mass.config.get_base_player_config(player_id, self.provider_id)
        self._extra_data: dict[str, Any] = {}
        self._extra_attributes: dict[str, Any] = {}
        self._on_unload_callbacks: list[Callable[[], None]] = []
        self.__active_mass_source: str | None = None
        self.__external_pause_since: float | None = None
        self.__ended_external_source: str | None = None
        self.__foreign_protocol_id: str | None = None
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
    def private(self) -> bool:
        """Return if the player may not be offered to other clients as a target."""
        return self._attr_private

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
        picked as an output protocol or command target. Neither can a disabled player.

        Use this, not state.available, for a device that must be reachable right now:
        the state also reports a player that is switched off at its power control as
        available (see offline_by_power_control).
        """
        return self.enabled and self.available and not self.needs_setup

    @property
    @final
    def offline_by_power_control(self) -> bool:
        """
        Return if the device is offline because it is switched off at its external power control.

        Also True while Music Assistant powers it on through that control and waits for it
        to come online. Such a player is reported to clients as available and powered off,
        so it can be powered on (or played to) from there, while the device itself is not
        reachable.
        """
        if self.available or self.type in (PlayerType.GROUP, PlayerType.PROTOCOL):
            return False
        control = self.mass.players.get_player_control(self.power_control)
        if control is None or not control.supports_power:
            return False
        return not control.power_state or ATTR_POWER_ON_IN_PROGRESS in self.extra_data

    @property
    @final
    def implements_setup_flow(self) -> bool:
        """Return if this player implements its own interactive setup flow."""
        return type(self).run_setup_flow is not Player.run_setup_flow

    @property
    def setup_flow_available(self) -> bool:
        """
        Return if this player's setup flow currently has anything to offer.

        Override to hide the reconfigure action while there is nothing left to set up,
        so the user is not sent into a flow that can only abort. Only consulted for a
        player that implements a flow of its own.
        """
        return True

    @property
    @final
    def has_setup_flow(self) -> bool:
        """
        Return if an interactive setup flow can be started for this player.

        True when the player implements its own setup flow and that flow currently has
        something to offer, or when it wraps a (non-native) protocol child player that
        does. Unlike ``needs_setup`` this stays True once setup completed, so the UI can
        offer to re-run the flow on demand (e.g. to redo a pairing step that was skipped).
        """
        if self.implements_setup_flow:
            return self.setup_flow_available
        for output_protocol in self.output_protocols:
            if output_protocol.is_native:
                continue
            child = self.mass.players.get_player(output_protocol.output_protocol_id)
            if child is not None and child.has_setup_flow:
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
    def trusts_reported_source(self) -> bool:
        """
        Return whether a source this player lists for itself counts as a takeover.

        Most devices do not report their active source accurately, so only the sources
        in EXTERNAL_SOURCES are trusted. A provider that does report accurately can opt
        in, so a service Music Assistant has no name of its own for is recognised too.
        """
        return self._attr_trusts_reported_source

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
    def live_session_members(self) -> list[str]:
        """
        Return the id's of the players that take part in this player's live playback session.

        Defaults to :attr:`group_members`, which is the right answer where grouping and
        the playback session are the same thing. Providers where the two drift apart
        (a member the session dropped, one that never managed to join it, or no session
        at all) should override this and answer from the session itself, so callers can
        tell actual playback partners from tracked group membership.
        """
        return self.group_members

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

        Used by the state derivation to decide whether children should be
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
        for player in self.mass.players.iter_players(
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

    @asynccontextmanager
    async def prepare_play_media(self) -> AsyncIterator[None]:
        """Scope provider preparation across auto-power and the ensuing playback."""
        yield

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

    async def set_shuffle(self, shuffle_enabled: bool) -> None:
        """
        Handle SET SHUFFLE command on the player.

        Will only be called if the player's currently active source declares
        ``can_shuffle``.

        :param shuffle_enabled: Whether the source should play its content shuffled.
        """
        raise NotImplementedError(
            "set_shuffle needs to be implemented when a source declares can_shuffle"
        )

    async def set_repeat(self, repeat_mode: RepeatMode) -> None:
        """
        Handle SET REPEAT command on the player.

        Will only be called if the player's currently active source declares
        ``can_repeat``.

        :param repeat_mode: The repeat mode the source should apply.
        """
        raise NotImplementedError(
            "set_repeat needs to be implemented when a source declares can_repeat"
        )

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

    async def on_group_content_takeover(self) -> object | None:
        """Prepare a protocol group before a new content owner joins it."""
        return None

    async def on_group_content_takeover_finished(self, token: object) -> None:
        """Finish a protocol-group content takeover transaction."""
        return

    async def on_group_content_takeover_aborted(self, token: object) -> None:
        """Abort a protocol-group content takeover without publishing old content."""
        return

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

    @property
    def announcement_features(self) -> set[AnnouncementFeature]:
        """
        Return the announcement-behaviour hints for this player.

        SUPPORTS_VOLUME is reported by default: an announcement played through the
        builtin path always applies the level via the device volume, and most native
        routes honour it too. A provider whose native announcement ignores the level
        drops it, so the controller uses the builtin path once a level is requested.
        APPLIES_VOLUME and COORDINATES_START are opt-in (see AnnouncementFeature).
        """
        return {AnnouncementFeature.SUPPORTS_VOLUME}

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

    async def handle_config_action(
        self, action: str
    ) -> list[ConfigEntry] | ConfigActionResult | None:
        """
        Run the one-shot side effect for a pressed action button from this player's config.

        Override to run the side effect for each ``ConfigEntryType.ACTION`` entry this
        player declares. Return a ``ConfigActionResult`` to report the outcome (a message
        to show and/or a url to open), or None when there is nothing to report. Raise to
        report failure to the caller. Returning entries re-renders the config form from the
        owning player's freshly resolved entries; the returned entries themselves are not
        shown, so they serve only as the signal that a re-render is needed.

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
        if self._attr_external_pause_idle_timeout is not None:
            self.mass.cancel_timer(f"external_pause_{self.player_id}")
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

    def on_queue_position_served(self, queue_index: int, queue_item_id: str) -> None:  # noqa: B027
        """
        Handle callback when a stream request addressed by queue position was served.

        :param queue_index: The queue position the player fetched.
        :param queue_item_id: The queue item whose audio went out for that position.
        """
        # optional callback, for a player that fetches its queue items by position

    def on_sync_parent_updated(
        self, sync_parent: Player, changed_values: dict[str, tuple[Any, Any]]
    ) -> None:
        """Handle callback when the sync parent of this player is updated."""
        # optional callback
        # default implementation will simply trigger an update for the state of the player
        self.mass.players.trigger_player_update(self.player_id)

    @cached_property
    @final
    def default_icon(self) -> str:
        """Return the default player icon."""
        return get_default_player_icon(
            self.type,
            self.provider.domain,
            self.device_info.manufacturer,
            self.device_info.model,
        )

    def on_player_media_updated(self) -> None:  # noqa: B027
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
        icon = self.mass.config.get_raw_player_config_value(
            self.player_id, CONF_ENTRY_PLAYER_ICON.key
        )
        return icon if isinstance(icon, str) and icon else self.default_icon

    @cached_property
    @final
    def power_control(self) -> str:
        """Return the power control type."""
        conf = self.__stored_control_conf(CONF_POWER_CONTROL, PlayerFeature.POWER)
        if conf and conf in (PLAYER_CONTROL_NATIVE, PLAYER_CONTROL_FAKE, PLAYER_CONTROL_NONE):
            # the control type is explicitly set in the config, use that
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
        conf = self.__stored_control_conf(CONF_VOLUME_CONTROL, PlayerFeature.VOLUME_SET)
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
        conf = self.__stored_control_conf(CONF_MUTE_CONTROL, PlayerFeature.VOLUME_MUTE)
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
        # check for protocol player with mute support, and use that if found.
        # this resolves independently from volume_control, so a device whose interfaces
        # advertise volume and mute separately (DLNA derives both from its own
        # RenderingControl actions) can end up with the two controls on different
        # siblings.
        if protocol_player := self._get_protocol_player_for_feature(
            PlayerFeature.VOLUME_MUTE, require_active=False
        ):
            return protocol_player.player_id
        return PLAYER_CONTROL_NONE

    @property
    @final
    def group_volume(self) -> int | None:
        """
        Return the group volume level.

        For group players or syncgroups, returns the maximum volume level of all
        powered-on child players, or None if no children support volume control.

        For non-group players, returns the player's own volume level.
        """
        return self._state.group_volume

    @property
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
        return self._state.group_volume_muted

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

    @cached_property
    @final
    def playback_domains(self) -> set[str]:
        """
        Return the protocol domains this player can be reached on right now.

        Only outputs that are available at this moment are included, so a protocol
        whose player went offline is left out. A wrapper player (UniversalPlayer)
        contributes its linked protocols but never its own domain.
        """
        return {output.protocol_domain for output in self.output_protocols if output.available}

    @property
    @final
    def linked_output_protocols(self) -> list[LinkedOutputProtocol]:
        """Return the list of actively linked output protocol players."""
        return self.__attr_linked_protocols

    @property
    @final
    def protocol_parent_id(self) -> str | None:
        """Return the parent player_id if this is a protocol player linked to a native player."""
        return self.__attr_protocol_parent_id

    @property
    def default_output_protocol_domain(self) -> str | None:
        """
        Return the protocol domain this player prefers as its default output, if any.

        A player that has no native audio path of its own (e.g. a control/grouping shell
        for a device whose playback runs over a linked protocol) can point the automatic
        output selection at a specific protocol domain (such as ``dlna``). The base player
        has no preference; an explicit user selection always overrides this default.
        """
        return None

    @property
    def grouping_locked(self) -> bool:
        """
        Return whether ALL grouping must be suppressed in this player's exposed state.

        This is the broad, final lock: while it holds, ``SET_MEMBERS`` is withdrawn and no
        group targets are offered in the final state, even ones a linked protocol player
        would otherwise supply. It must therefore be reserved for a genuinely read-only
        topology — for example a device in an externally-created cross-backend group that
        Music Assistant keeps read-only — and NOT used merely because a device's own native
        grouping capability is unavailable. A provider that only wants to disable its native
        grouping path (e.g. while its control API is unreachable) should instead withhold the
        native ``SET_MEMBERS`` from ``supported_features`` and return no native
        ``can_group_with`` candidates, leaving core free to still group via a linked protocol.
        """
        return False

    @property
    def prefer_native_grouping(self) -> bool:
        """
        Return whether this player should group natively before any linked protocol.

        A device that runs its own multiroom (e.g. a LinkPlay speaker exposed as a control
        shell) should keep grouping on its native path rather than route it through a linked
        AirPlay/DLNA protocol that merely happens to be its preferred playback output. When
        this is set, grouping selection tries native grouping first; the usual compatibility
        checks still decide whether native grouping is actually possible, and every other
        player keeps the default protocol-first ordering. Playback output selection is
        unaffected.
        """
        return False

    def is_native_group_compatible(self, other: Player) -> bool:
        """
        Return whether this player can natively group with the given player.

        Native grouping normally works between any two players of the same provider
        instance. A provider that hosts several incompatible device backends behind a
        single instance can narrow this so the grouping layer never routes a cross-backend
        pair onto a native group it cannot form.

        :param other: The player considered for a native group with this one.
        """
        return self.provider.instance_id == other.provider.instance_id

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

    @property
    @final
    def foreign_protocol_player(self) -> Player | None:
        """Return the linked protocol player whose app to show and control on this player."""
        if self.__foreign_protocol_id is None:
            return None
        return self.mass.players.get_player(self.__foreign_protocol_id)

    @final
    def linked_foreign_player(self) -> Player | None:
        """Return the linked protocol player that plays an app Music Assistant did not start."""
        for linked in self.__attr_linked_protocols:
            protocol_player = self.mass.players.get_player(linked.output_protocol_id)
            if (
                protocol_player is not None
                and protocol_player.available
                and protocol_player.playback_state in (PlaybackState.PLAYING, PlaybackState.PAUSED)
                # Music Assistant's own playback through a protocol reports no source, and
                # following a group is no source of its own
                and any(x.id == protocol_player.active_source for x in protocol_player.source_list)
            ):
                return protocol_player
        return None

    @final
    def set_linked_output_protocols(self, protocols: list[LinkedOutputProtocol]) -> None:
        """
        Set the actively linked output protocol players.

        :param protocols: List of links to the active protocol players.
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
    def get_linked_protocol(self, output_protocol_id: str) -> OutputProtocol | None:
        """
        Get a linked output protocol by its id, with its name and availability resolved.

        :param output_protocol_id: player_id of the linked protocol player.
        """
        for linked in self.__attr_linked_protocols:
            if linked.output_protocol_id == output_protocol_id:
                protocol_player = self.mass.players.get_player(output_protocol_id)
                return OutputProtocol(
                    output_protocol_id=linked.output_protocol_id,
                    name=protocol_player.provider.name
                    if protocol_player
                    else linked.protocol_domain.title(),
                    protocol_domain=linked.protocol_domain,
                    priority=linked.priority,
                    available=protocol_player.available_for_playback if protocol_player else False,
                    derived_from=linked.derived_from,
                )
        return None

    @final
    def get_output_protocol_by_domain(self, protocol_domain: str) -> OutputProtocol | None:
        """
        Get an output protocol by domain, including native protocol.

        Unlike get_linked_protocol, this also covers the player's own native output.

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
        self.__expire_stale_external_pause()
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
        prev_state = self._state
        self._state, effects = derive_player_state(self.__collect_state_inputs())
        self.__foreign_protocol_id = effects.foreign_protocol_id
        # track stop called state
        if effects.left_idle:
            self.__stop_called = False
            self.mass.cancel_timer(f"set_mass_source_{self.player_id}")
        elif effects.went_idle:
            self.__stop_called = True
            # when we're going to idle,
            # we want to reset the active mass source after a short delay
            # this is done using a timer which gets reset if the player starts playing again
            # before the timer is up, using the task_id
            self.mass.call_later(
                5, self.set_active_mass_source, None, task_id=f"set_mass_source_{self.player_id}"
            )
        changed_values = self.__diff_state(prev_state)
        if not MEDIA_IDENTITY_KEYS.isdisjoint(changed_values.keys()):
            # current media changed, call the media updated callback
            # debounce the callback to avoid multiple calls when multiple
            # state updates happen in a short time
            self.mass.call_later(
                1, self.on_player_media_updated, task_id=f"player_media_updated_{self.player_id}"
            )
        # persist the default name if it changed
        if effects.default_name:
            self.mass.config.set_player_default_name(self.player_id, effects.default_name)
        # persist the player type if it changed
        if self.type != self._config.player_type:
            self.mass.config.set_player_type(self.player_id, self.type)
        if effects.position_jumped and signal_event:
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
                self, changed_values, media_position_jumped=effects.media_position_jumped
            )

    @final
    def mark_external_source_ended(self) -> None:
        """
        Stop presenting the external source the device has loaded as something to resume.

        Call this when the device makes clear that the session is gone, for example when
        it refuses to resume playback. Normal queue handling takes over from there.
        Call :meth:`update_state` afterwards to publish the change.
        """
        if self._attr_active_source is not None:
            # the updates that follow rebuild the state from the device, which keeps
            # reporting the very same source as paused, so remembering which source we
            # gave up on is what keeps this applied
            self.__ended_external_source = self._attr_active_source
        self.__external_pause_since = None
        self._attr_playback_state = PlaybackState.IDLE
        self._attr_active_source = None
        self._attr_current_media = None

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

    @final
    def volume_control_for_output(self, output_protocol_id: str) -> str:
        """
        Return the volume control that owns audio rendered over the given output.

        Unlike :attr:`volume_control`, which answers where a volume command should go
        right now, this answers who owns the volume of one specific output. Callers that
        know which interface is about to carry the audio must use this, so the answer does
        not depend on whether that output has been marked active yet.

        :param output_protocol_id: Player id of the output protocol rendering the audio.
        :return: A control id, or NATIVE/FAKE/NONE. NONE means nothing in the signal path
            of that output owns the volume; no sibling interface is offered as a fallback.
        """
        return self.__control_for_output(
            PlayerFeature.VOLUME_SET, CONF_VOLUME_CONTROL, output_protocol_id
        )

    @final
    def mute_control_for_output(self, output_protocol_id: str) -> str:
        """
        Return the mute control that owns audio rendered over the given output.

        The mute counterpart of :meth:`volume_control_for_output`.

        :param output_protocol_id: Player id of the output protocol rendering the audio.
        """
        control = self.__control_for_output(
            PlayerFeature.VOLUME_MUTE, CONF_MUTE_CONTROL, output_protocol_id
        )
        if (
            control == PLAYER_CONTROL_FAKE
            and self.volume_control_for_output(output_protocol_id) == PLAYER_CONTROL_NONE
        ):
            # fake mute is simulated by setting the volume to zero, so without a volume
            # control on this output there is no way to mute it at all
            return PLAYER_CONTROL_NONE
        return control

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

    __sleep_timer_mode: SleepTimerMode | None = None

    @final
    def set_sleep_timer_mode(self, value: SleepTimerMode | None) -> None:
        """
        Set the point at which the active sleep timer stops playback.

        :param value: The sleep timer mode, or None to clear the sleep timer.
        """
        self.__sleep_timer_mode = value

    @property
    @final
    def sleep_timer_mode(self) -> SleepTimerMode | None:
        """Return the point at which the active sleep timer stops playback."""
        return self.__sleep_timer_mode

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
        """
        Get player(protocol) which has the given PlayerFeature.

        Resolves control-plane features (volume, mute), so the native player wins even
        while a protocol renders the audio: a device's own volume is its own volume,
        whichever interface the sound arrives on. Commands that travel with the audio
        resolve through :meth:`PlayerController._get_control_target` instead, which
        prefers the rendering output.

        :param feature: The feature the resolved player has to support.
        :param require_active: Only accept the output that is already rendering,
            instead of falling back to an idle one.
        """
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

        # fallback to preferred protocol from config. the stored value survives a
        # relink, so it only counts while it still names one of this player's own
        # outputs - otherwise the command would land on another speaker.
        preferred_conf = self.mass.config.get_raw_player_config_value(
            self.player_id, CONF_PREFERRED_OUTPUT_PROTOCOL
        )
        if preferred_conf and preferred_conf not in ("auto", "native"):
            preferred_protocol = str(preferred_conf)
            for linked in self.linked_output_protocols:
                if linked.output_protocol_id != preferred_protocol:
                    continue
                if (
                    (_player := self.mass.players.get_player(preferred_protocol))
                    and _player.available_for_playback
                    and feature in _player.supported_features
                ):
                    return _player
                break

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
    def __stored_control_conf(self, conf_key: str, feature: PlayerFeature) -> ConfigValueType:
        """
        Return the stored control selection, dropping a NATIVE the player can no longer back.

        A NATIVE selection is only meaningful while the player advertises the matching feature.
        Dropping a stale one makes the caller fall back to its auto-select logic instead of
        re-exposing a control the provider can no longer drive - the resolved control is what
        the final feature set is derived from, so an unchecked value would put the feature back.

        :param conf_key: Config key holding the control selection.
        :param feature: Feature a NATIVE selection requires the player to advertise.
        """
        conf = self.mass.config.get_raw_player_config_value(self.player_id, conf_key)
        if conf == PLAYER_CONTROL_NATIVE and not self.supports_feature(feature):
            return None
        return conf

    @final
    def __control_for_output(
        self, feature: PlayerFeature, conf_key: str, output_protocol_id: str
    ) -> str:
        """Resolve the control owning the given feature for one specific output."""
        conf = self.__stored_control_conf(conf_key, feature)
        if conf and conf in (PLAYER_CONTROL_NATIVE, PLAYER_CONTROL_FAKE, PLAYER_CONTROL_NONE):
            return str(conf)
        if conf and conf not in (PLAYER_CONTROL_PROTOCOL, "auto"):
            # An explicitly configured control is a statement about the device as a whole,
            # so it stays in charge no matter which of its interfaces renders the audio.
            if (_player := self.mass.players.get_player(str(conf))) and _player.available:
                return _player.player_id
            if _control := self.mass.players.get_player_control(str(conf)):
                return _control.id
        if feature in self.supported_features:
            return PLAYER_CONTROL_NATIVE
        # Deliberately no fallback to a sibling interface: the caller named the output that
        # carries the audio, so anything else is by definition not in that signal path.
        # Availability is not checked either - the named output is the one about to render.
        if (
            protocol_player := self.mass.players.get_player(output_protocol_id)
        ) and feature in protocol_player.supported_features:
            return protocol_player.player_id
        return PLAYER_CONTROL_NONE

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
        device_info = self.device_info
        extra_data = self.extra_data
        return {
            "type": self.type,
            "private": self.private,
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
                (
                    s.id,
                    s.name,
                    s.passive,
                    s.can_play_pause,
                    s.can_seek,
                    s.can_next_previous,
                    s.can_shuffle,
                    s.can_repeat,
                    s.shuffle_enabled,
                    s.repeat_mode,
                )
                for s in self.source_list
            ),
            "sound_mode_list": tuple((m.id, m.name, m.passive) for m in self.sound_mode_list),
            "options": tuple((o.key, o.value, o.read_only) for o in self.options),
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
                    for key, value in self.extra_attributes.items()
                    if key not in ("seq_no", "last_poll")
                )
            ),
            "fake_controls": (
                extra_data.get(ATTR_FAKE_POWER),
                extra_data.get(ATTR_FAKE_VOLUME),
                extra_data.get(ATTR_FAKE_MUTE),
            ),
            "linked_protocols": tuple(self.__attr_linked_protocols),
            "protocol_parent_id": self.__attr_protocol_parent_id,
            "active_output_protocol": self.__attr_active_output_protocol,
            "active_mass_source": self.__active_mass_source,
            "sleep_timer_expires_at": self.__sleep_timer_expires_at,
            "sleep_timer_mode": self.__sleep_timer_mode,
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
    def __diff_state(self, prev_state: PlayerState) -> dict[str, tuple[Any, Any]]:
        """
        Return the values of the state that changed since the previous calculation.

        The player's own elapsed_time values are not part of the changed values: they
        refresh on every calculation but only current_media - which holds the final
        calculated position - is event-relevant.

        :param prev_state: The state the player had before the current one.
        """
        prev_fingerprint = self.__state_fingerprint or _state_fingerprint(prev_state)
        new_fingerprint = _state_fingerprint(self._state)
        # not part of the state: whether the device can be reached, which its availability
        # does not tell for a player that is switched off at its power control
        new_fingerprint[ATTR_REACHABLE] = self.available
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
        return changed_values

    @final
    def __collect_state_inputs(self) -> StateInputs:
        """Collect everything the state of the player is derived from."""
        own = _protocol_snapshot(self.player_id, self, self.synced_to)
        peers: dict[str, PeerSnapshot] = {}
        linked, active_protocol, foreign_app = self.__collect_protocols(
            peers, probe_synced_to=not own.synced_to
        )
        for player_id in own.group_members:
            self.__collect_peer(peers, player_id)
        if own.synced_to:
            self.__collect_peer(peers, own.synced_to)
        if self.__attr_protocol_parent_id:
            self.__collect_peer(peers, self.__attr_protocol_parent_id)
        can_group_with: set[str] = set()
        # a player that is synced or keeps its grouping locked has nothing to group with,
        # which saves looking up all players of the providers it can group through
        if not self.grouping_locked and resolve_synced_to(own, linked, peers) is None:
            can_group_with = self.__collect_can_group_with(peers, linked)
        groups = self.__collect_groups(peers)
        session = self.mass.players.get_audio_source_session(self.player_id)
        source_ids = {self.__active_mass_source, own.active_source}
        queues = {
            queue_id: self.__queue_snapshot(queue)
            for queue_id in {self.player_id, *source_ids}
            if queue_id is not None and (queue := self.mass.player_queues.get(queue_id))
        }
        source_ids.update(group.state.active_source for group in groups)
        if session is not None:
            source_ids.add(session.active_source)
        if foreign_app:
            source_ids.add(foreign_app.active_source)
        return StateInputs(
            player_id=self.player_id,
            provider=self.provider_id,
            player_type=self.type,
            now=time.time(),
            previous_state=self._state,
            enabled=self.enabled,
            name=self.display_name,
            default_name=self._config.default_name,
            hide_in_ui=self.hide_in_ui,
            expose_to_ha=self.expose_to_ha,
            icon=self.icon,
            power_control=self.power_control,
            volume_control=self.volume_control,
            mute_control=self.mute_control,
            powered=self.__power_control_state(),
            volume_level=self.__volume_control_level(),
            volume_muted=self.__mute_control_state(),
            offline_by_power_control=self.offline_by_power_control,
            own=own,
            device_info=self.device_info,
            private=self.private,
            trusts_reported_source=self.trusts_reported_source,
            grouping_locked=self.grouping_locked,
            active_sound_mode=self.active_sound_mode,
            sound_mode_list=self.sound_mode_list,
            options=self.options,
            static_group_members=self.static_group_members,
            can_group_with=can_group_with,
            has_setup_flow=self.has_setup_flow,
            linked=linked,
            active_protocol=active_protocol,
            foreign_app=foreign_app,
            protocol_parent_id=self.__attr_protocol_parent_id,
            output_protocols=self.output_protocols,
            active_output_protocol=self.__attr_active_output_protocol,
            active_mass_source=self.__active_mass_source,
            sleep_timer_expires_at=self.__sleep_timer_expires_at,
            sleep_timer_mode=self.__sleep_timer_mode,
            extra_attributes=self.extra_attributes,
            palette_url=self._attr_current_palette_url,
            palette=self._attr_current_palette,
            peers=peers,
            groups=groups,
            queues=queues,
            mass_sources=frozenset(
                source_id
                for source_id in source_ids
                if source_id is not None and self.__is_mass_source(source_id, queues)
            ),
            session=session,
            session_image_url=(
                self.mass.metadata.get_image_url(session.source.image, size=512)
                if session is not None and session.source.image
                else None
            ),
            standing_sources=self.__collect_standing_sources(),
        )

    @final
    def __collect_protocols(
        self, peers: dict[str, PeerSnapshot], probe_synced_to: bool
    ) -> tuple[tuple[ProtocolSnapshot, ...], ProtocolSnapshot | None, ProtocolSnapshot | None]:
        """
        Collect what the linked protocol players report, and the players they refer to.

        Returns the linked protocol players, the one of the active output and the one
        that plays an app Music Assistant did not start.

        :param peers: The peers collected so far, by id.
        :param probe_synced_to: Read which player each linked protocol player is synced
            to. That only counts for a player that reports no sync leader itself, and
            finding it takes a pass over the players of the protocol's provider.
        """
        foreign_player = self.linked_foreign_player()
        foreign_app: ProtocolSnapshot | None = None
        active_output_protocol = self.__attr_active_output_protocol
        active_protocol: ProtocolSnapshot | None = None
        linked: list[ProtocolSnapshot] = []
        for link in self.__attr_linked_protocols:
            protocol_id = link.output_protocol_id
            if not (protocol_player := self.__collect_peer(peers, protocol_id)):
                continue
            protocol = _protocol_snapshot(
                protocol_id, protocol_player, protocol_player.synced_to if probe_synced_to else None
            )
            linked.append(protocol)
            if protocol_player is foreign_player:
                foreign_app = protocol
            if protocol_id == active_output_protocol:
                active_protocol = protocol
            if protocol.synced_to:
                self.__collect_peer(peers, protocol.synced_to)
        if (
            active_protocol is None
            and active_output_protocol
            and active_output_protocol != "native"
            and (protocol_player := self.__collect_peer(peers, active_output_protocol))
        ):
            active_protocol = _protocol_snapshot(active_output_protocol, protocol_player, None)
        if active_protocol:
            for player_id in active_protocol.group_members:
                self.__collect_peer(peers, player_id)
        return tuple(linked), active_protocol, foreign_app

    @final
    def __collect_can_group_with(
        self, peers: dict[str, PeerSnapshot], linked: tuple[ProtocolSnapshot, ...]
    ) -> set[str]:
        """
        Collect the players the player and its linked protocol players can group with.

        Returns the ids the player itself reports: a provider instance id among them
        stands for all (available) players of that provider.

        :param peers: The peers collected so far, by id.
        :param linked: What the linked protocol players report.
        """
        for protocol in linked:
            for player_id in peers[protocol.protocol_id].state.can_group_with:
                self.__collect_peer(peers, player_id)
        # a protocol player groups with protocol players as they are,
        # any other player with the visible players those belong to
        with_parent = self.type != PlayerType.PROTOCOL
        result: set[str] = set()
        for member_id in self.can_group_with:
            if self.__collect_peer(peers, member_id, with_parent):
                result.add(member_id)
                continue  # already a player ID
            # Check if member_id is a provider instance ID
            if provider := self.mass.get_provider(member_id):
                for player in self.mass.players.iter_players(
                    return_unavailable=False,  # Only include available players
                    provider_filter=provider.instance_id,
                    return_protocol_players=True,
                ):
                    self.__add_peer(peers, player.player_id, player, with_parent)
                    result.add(player.player_id)
        return result

    @final
    def __collect_groups(self, peers: dict[str, PeerSnapshot]) -> tuple[PeerSnapshot, ...]:
        """
        Collect the group players that can hold the player.

        :param peers: The peers collected so far, by id.
        """
        if self.type == PlayerType.PROTOCOL:
            # a protocol player follows the group state of its parent player
            return ()
        groups: list[PeerSnapshot] = []
        for group_player in self.mass.players.iter_players(
            return_unavailable=False, return_disabled=False
        ):
            if group_player.type != PlayerType.GROUP:
                continue
            self.__add_peer(peers, group_player.player_id, group_player)
            groups.append(peers[group_player.player_id])
        return tuple(groups)

    @final
    def __collect_standing_sources(self) -> tuple[AudioSource, ...]:
        """Collect the audio sources plugins bound to the player or report as its inputs."""
        if self.type == PlayerType.PROTOCOL:
            # a protocol player publishes its bare source list
            return ()
        # a device's own inputs may be reported on one of its linked protocol players
        device_player_ids = [
            self.player_id,
            *(link.output_protocol_id for link in self.__attr_linked_protocols),
        ]
        sources: list[AudioSource] = []
        for prov in self.mass.get_providers_supporting_feature(ProviderFeature.AUDIO_SOURCE):
            if not isinstance(prov, PluginProvider):
                continue
            sources += prov.get_player_audio_sources(self.player_id) or ()
            for device_player_id in device_player_ids:
                sources += prov.get_player_input_sources(device_player_id)
        return tuple(sources)

    @final
    def __collect_peer(
        self, peers: dict[str, PeerSnapshot], player_id: str, with_parent: bool = True
    ) -> Player | None:
        """
        Add the player with the given id to the peers, if it is registered.

        :param peers: The peers collected so far, by id.
        :param player_id: Id of the player to add.
        :param with_parent: Also add the player it is linked to as a protocol player.
        """
        if player := self.mass.players.get_player(player_id):
            self.__add_peer(peers, player_id, player, with_parent)
        return player

    @final
    def __add_peer(
        self,
        peers: dict[str, PeerSnapshot],
        player_id: str,
        player: Player,
        with_parent: bool = True,
    ) -> None:
        """
        Add a player to the peers.

        :param peers: The peers collected so far, by id.
        :param player_id: Id the player is registered under.
        :param player: The player to add.
        :param with_parent: Also add the player it is linked to as a protocol player.
        """
        if player_id not in peers:
            # positional, which is cheaper for the many players this runs for
            peers[player_id] = PeerSnapshot(
                player.player_id,
                player.type,
                player.available,
                player.available_for_playback,
                player.protocol_parent_id,
                player.grouping_locked,
                player.powered,
                player.is_active_session,
                player.state,
            )
        if with_parent and (parent_id := player.protocol_parent_id) and parent_id not in peers:
            self.__collect_peer(peers, parent_id)

    @final
    def __is_mass_source(self, source_id: str, queues: dict[str, QueueSnapshot]) -> bool:
        """
        Return whether Music Assistant itself plays the given source.

        :param source_id: Id of the source.
        :param queues: The queues collected so far, by id.
        """
        return (
            source_id in queues
            # A live AudioSource (e.g. Spotify Connect) is audio MA produces itself, unlike
            # the device's own streaming endpoint or a line-in it switched to
            or self.mass.players.is_live_audio_source(source_id)
            # If it's a known queue ID it's MA-managed; anything else is external
            # (line-in, TV input, etc.)
            or self.mass.player_queues.get(source_id) is not None
        )

    @final
    def __queue_snapshot(self, queue: PlayerQueue) -> QueueSnapshot:
        """Return the snapshot of a queue, with the artwork of its current item resolved."""
        item_image_url: str | None = None
        media_item_image_url: str | None = None
        if current_item := queue.current_item:
            # the image format needs to be 512x512 jpeg for maximum compatibility with players
            if current_item.image:
                item_image_url = self.mass.metadata.get_image_url(current_item.image, size=512)
            if current_item.media_item and current_item.media_item.image:
                media_item_image_url = self.mass.metadata.get_image_url(
                    current_item.media_item.image, size=512
                )
        return QueueSnapshot(
            queue=queue,
            item_image_url=item_image_url,
            media_item_image_url=media_item_image_url,
        )

    @final
    def __power_control_state(self) -> bool | None:
        """Return the power state based on the playercontrol which may have been set-up."""
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

    @final
    def __volume_control_level(self) -> int | None:
        """Return the volume level based on the playercontrol which may have been set-up."""
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
            if control.volume_level is None:
                return None
            return self.mass.players.scale_volume_from_device(self.player_id, control.volume_level)
        # handle player control for volume if set
        if player_control := self.mass.players.get_player_control(volume_control):
            return self.mass.players.scale_volume_from_device(
                self.player_id, player_control.volume_level
            )
        return None

    @final
    def __mute_control_state(self) -> bool | None:
        """Return the mute state based on any playercontrol which may have been set-up."""
        mute_control = self.mute_control
        if mute_control == PLAYER_CONTROL_FAKE:
            return bool(self.extra_data.get(ATTR_FAKE_MUTE, False))
        if mute_control == PLAYER_CONTROL_NATIVE:
            return self.volume_muted
        if mute_control == PLAYER_CONTROL_NONE:
            return None
        # handle protocol player as mute control
        if control := self.mass.players.get_player(mute_control):
            return control.volume_muted
        # handle player control for mute if set
        if player_control := self.mass.players.get_player_control(mute_control):
            return player_control.volume_muted
        return None

    @final
    def __external_source_active(self) -> bool:
        """Return whether the source the player reports for itself is an external one."""
        # deliberately the raw source rather than the active source of the state:
        # what has to expire is what the device itself keeps
        # reporting, not what the group or protocol it belongs to resolves that to
        source = self._attr_active_source
        if source is None or source == self.player_id:
            return False
        return self.mass.player_queues.get(source) is None

    @final
    def __expire_stale_external_pause(self) -> None:
        """Report an external source that has been paused for a while as no longer active."""
        if (timeout := self._attr_external_pause_idle_timeout) is None:
            return
        if (
            self._attr_playback_state != PlaybackState.PAUSED
            # while an output protocol renders the audio, MA owns playback and the
            # source the device reports for itself is overruled anyway
            or self.active_output_protocol not in (None, "native")
            or not self.__external_source_active()
        ):
            self.__external_pause_since = None
            if self._attr_playback_state == PlaybackState.PLAYING:
                # the device plays again, so the source we gave up on is live once more
                self.__ended_external_source = None
            return
        if self._attr_active_source == self.__ended_external_source:
            # the device keeps handing us the source we already gave up on
            self.mark_external_source_ended()
            return
        # any other source is a session of its own and gets the full grace period
        self.__ended_external_source = None
        if self.__external_pause_since is None:
            self.__external_pause_since = time.time()
        elif (time.time() - self.__external_pause_since) >= timeout:
            self.mark_external_source_ended()
            return
        # nothing changes on the device side when the session goes stale, so there is no
        # event to react to. Keep the check armed rather than relying on a single timer:
        # an update that bails out early would otherwise consume it and leave the source
        # paused for good.
        self.mass.call_later(
            timeout + 1,
            self.update_state,
            task_id=f"external_pause_{self.player_id}",
        )


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
