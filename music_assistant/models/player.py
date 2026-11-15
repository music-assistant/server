"""
Model of a Player within Music Assistant.

A Player is what users, clients and the queue see: one per device (or group), owned by
the players controller. It holds the identity, the config, the transient state and the
protocols that are attached to it; the providers own those protocols (PlayerProtocol)
and report through them what the device says about itself.

The state a player publishes is derived from what its protocols report, its config,
(sync)group memberships and other factors. It is calculated and snapshotted in the
PlayerState dataclass, which is also what is sent over the API. A player gives each value
of that state under the name of its field (player.volume_level, player.available, ...), as
it was published last; its 'state' property is the snapshot as a whole. What the state is
derived from is private to the player, or has a name of its own where other code has to
know what is true of the device right now (attached_protocols, available_for_playback,
reported_type, ...).
"""

from __future__ import annotations

import asyncio
import builtins
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar, final, overload

from music_assistant_models.config_entries import ConfigValueType
from music_assistant_models.constants import (
    EXTRA_ATTRIBUTES_TYPES,
    PLAYER_CONTROL_FAKE,
    PLAYER_CONTROL_NATIVE,
    PLAYER_CONTROL_NONE,
)
from music_assistant_models.enums import (
    PlaybackState,
    PlayerFeature,
    PlayerType,
    ProtocolRole,
    ProviderFeature,
    SleepTimerMode,
)
from music_assistant_models.player import (
    DeviceInfo,
    OutputProtocol,
    PlayerMedia,
    PlayerProtocolInfo,
    PlayerSource,
)
from music_assistant_models.player import Player as PlayerState
from music_assistant_models.unique_list import UniqueList
from propcache import under_cached_property as cached_property

from music_assistant.constants import (
    ATTR_FAKE_MUTE,
    ATTR_FAKE_POWER,
    ATTR_FAKE_VOLUME,
    ATTR_REACHABLE,
    CONF_DEVICE_INFO,
    CONF_ENTRY_PLAYER_ICON,
    CONF_EXPOSE_PLAYER_TO_HA,
    CONF_HIDE_IN_UI,
    CONF_LAST_SEEN,
    CONF_LEGACY_IDS,
    CONF_MUTE_CONTROL,
    CONF_PLAYERS,
    CONF_POWER_CONTROL,
    CONF_PREFERRED_OUTPUT_PROTOCOL,
    CONF_PROTOCOL_DEFAULTS,
    CONF_UNDERLYING_PLAYER_ID,
    CONF_VOLUME_CONTROL,
    PLAYER_CONTROL_PROTOCOL,
    PROTOCOL_PRIORITY,
    SETUP_REASON_PROVIDER_NOT_INSTALLED,
)
from music_assistant.helpers.player import get_default_player_icon
from music_assistant.models.player_protocol import PlayerProtocol
from music_assistant.models.player_state import (
    GroupSnapshot,
    PeerSnapshot,
    ProtocolSnapshot,
    QueueSnapshot,
    StateInputs,
    derive_player_state,
    reconcile_position_anchor,
    resolve_media_queue,
    resolve_synced_to,
)
from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from music_assistant_models.audio_processing import ActiveSourceAudioDetails
    from music_assistant_models.config_entries import PlayerConfig
    from music_assistant_models.media_items import AudioSource, MediaItemPalette
    from music_assistant_models.player import PlayerOption, PlayerSoundMode

    from music_assistant import MusicAssistant

# TypeVar for config value type inference
_ConfigValueT = TypeVar("_ConfigValueT", bound=ConfigValueType)

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

# How long the media updated callback waits, so a burst of state updates of one media change
# fires it once.
MEDIA_UPDATED_DEBOUNCE = 1

# How long a player that went idle keeps naming the Music Assistant source it played: a device
# that stops for a moment while its playback is replaced must not lose the queue it plays.
MASS_SOURCE_IDLE_MEMORY = 5

# config-derived cached properties (propcache keys in Player._cache); these are
# only invalidated by set_config, all other cached properties are invalidated on
# every update_state call
_CONFIG_CACHED_PROPS = frozenset({"_hide_in_ui", "_expose_to_ha"})

# The roles a protocol can stand for the device itself in: one protocol of a player does
# so, a protocol another one of these ranks above is a control protocol of the player, and
# the others are its audio protocols.
OWN_PROTOCOL_ROLES = frozenset({ProtocolRole.NATIVE, ProtocolRole.CONTROL, ProtocolRole.VIRTUAL})

# Audio protocols that take volume and mute commands without a stream running, in the
# order they are preferred for it (airplay and sendspin only do while streaming).
_CONTROL_PRIORITY = {"chromecast": 0, "dlna": 1, "airplay": 2, "sendspin": 3}

# Audio protocols in the order their name is preferred in (higher priority = better names
# typically). Chromecast and DLNA usually have good user-configured names, AirPlay also
# provides sensible names, Squeezelite and Sendspin may use MAC addresses or technical ids.
_NAME_PRIORITY = {"chromecast": 1, "airplay": 2, "dlna": 3, "squeezelite": 4, "sendspin": 5}
_TECHNICAL_NAME_PREFIXES = ("ap_", "cc_", "dlna_", "sq_", "sendspin_", "universal_")


@dataclass(frozen=True, slots=True)
class LinkedOutputProtocol:
    """
    An audio protocol attached to a player.

    Records the attachment itself (which protocol, on which domain, how preferred)
    and nothing about its current state: whether the protocol can actually be reached
    right now is answered by Player.get_linked_protocol() and Player.playback_domains,
    which ask the protocol itself.

    :param output_protocol_id: id of the attached audio protocol.
    :param protocol_domain: Domain of the protocol, e.g. "airplay" or "dlna".
    :param priority: Selection preference, lower is more preferred.
    :param derived_from: output_protocol_id of the base output this transport runs on
        top of ("native" when it rides on the native protocol of the player), if any.
    """

    output_protocol_id: str
    protocol_domain: str
    priority: int = 100
    derived_from: str | None = None


def get_device_name(own: PlayerProtocol | None, audio: Iterable[PlayerProtocol]) -> str | None:
    """
    Return the name the protocols of a device report for it, None without a usable one.

    That is the name its own protocol reports, else the first name one of its audio
    protocols reports that is no MAC address or technical id, asking the protocols
    known for user-friendly names first.

    :param own: The protocol that stands for the device itself, if any.
    :param audio: The audio protocols of the device.
    """
    if own is not None and (name := own.name):
        return name
    for protocol in sorted(audio, key=lambda p: _NAME_PRIORITY.get(p.provider.domain, 10)):
        if _is_user_friendly_name(name := protocol.name):
            return name
    return None


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
        "legacy_ids": tuple(state.legacy_ids),
        "last_seen": state.last_seen,
        "unavailable_since": state.unavailable_since,
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
        # never changed in place: a change comes as a new list of new objects
        "protocols": tuple(state.protocols),
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


def _is_user_friendly_name(name: str | None) -> bool:
    """Return whether a name looks like a real user-friendly name, not a technical id."""
    if not name or len(name) < 2:
        return False
    name_lower = name.lower().replace(":", "").replace("-", "").replace("_", "")
    # Filter out names that look like MAC addresses (12 hex chars)
    if len(name_lower) == 12 and all(c in "0123456789abcdef" for c in name_lower):
        return False
    # Filter out names that look like UUIDs
    if len(name_lower) >= 32 and all(c in "0123456789abcdef" for c in name_lower[:32]):
        return False
    # Filter out names that start with common player ID prefixes
    return not name.lower().startswith(_TECHNICAL_NAME_PREFIXES)


def _protocol_snapshot(
    player_id: str, protocol: PlayerProtocol, synced_to: str | None
) -> ProtocolSnapshot:
    """
    Return what a protocol reports for its player.

    :param player_id: Id of the player the protocol is attached to.
    :param protocol: The protocol.
    :param synced_to: Id of the protocol it reports to be synced to.
    """
    active_source = protocol.active_source
    return ProtocolSnapshot(
        protocol_id=protocol.protocol_id,
        role=protocol.role,
        available=protocol.available,
        available_for_playback=protocol.available_for_playback,
        needs_setup=protocol.needs_setup,
        setup_reason=protocol.setup_reason,
        supported_features=protocol.supported_features,
        playback_state=protocol.playback_state,
        elapsed_time=protocol.elapsed_time,
        elapsed_time_last_updated=protocol.elapsed_time_last_updated,
        # a protocol that plays the queue of its player reports itself as the source
        active_source=player_id if active_source == protocol.protocol_id else active_source,
        source_list=protocol.source_list,
        current_media=protocol.current_media,
        group_members=protocol.group_members,
        synced_to=synced_to,
    )


def _absent_protocol_snapshot(player_id: str, setup_reason: str | None) -> ProtocolSnapshot:
    """
    Return what stands in for the own protocol of a player that has none attached.

    :param player_id: Id of the player.
    :param setup_reason: Why the player has no protocol at all, when that is known.
    """
    return ProtocolSnapshot(
        protocol_id=player_id,
        role=None,
        available=False,
        available_for_playback=False,
        needs_setup=False,
        setup_reason=setup_reason,
        supported_features=set(),
        playback_state=PlaybackState.IDLE,
        elapsed_time=None,
        elapsed_time_last_updated=None,
        active_source=None,
        source_list=[],
        current_media=None,
        group_members=[],
        synced_to=None,
    )


def _protocol_inputs(protocol: PlayerProtocol, probe_synced_to: bool) -> tuple[Any, ...]:
    """
    Return the values a protocol reports that the state of its player is derived from.

    The playback position anchors are deliberately excluded: they are tracked
    separately with jump detection.

    :param protocol: The protocol.
    :param probe_synced_to: Read which protocol it reports to be synced to.
    """
    current_media = protocol.current_media
    device_info = protocol.device_info
    return (
        protocol.type,
        protocol.private,
        protocol.available,
        protocol.enabled,
        protocol.name,
        protocol.needs_setup,
        protocol.setup_reason,
        protocol.playback_state,
        protocol.powered,
        protocol.volume_level,
        protocol.volume_muted,
        protocol.active_source,
        protocol.active_sound_mode,
        protocol.is_active_session,
        protocol.synced_to if probe_synced_to else None,
        frozenset(protocol.supported_features),
        tuple(protocol.group_members),
        tuple(protocol.static_group_members),
        frozenset(protocol.can_group_with),
        (
            device_info.model,
            device_info.manufacturer,
            device_info.software_version,
            device_info.model_id,
            device_info.manufacturer_id,
            tuple(sorted(device_info.identifiers.items())),
        ),
        tuple(
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
            for s in protocol.source_list
        ),
        # the reported lists themselves: their properties build a new list on each update
        tuple((m.id, m.name, m.passive) for m in protocol._attr_sound_mode_list),
        tuple((o.key, o.value, o.read_only) for o in protocol._attr_options),
        (
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
        tuple(
            sorted(
                (key, _freeze(value))
                for key, value in protocol.extra_attributes.items()
                if key not in ("seq_no", "last_poll")
            )
        ),
    )


def _quieter_level(level: int | None, other: int | None) -> int | None:
    """Return the lower of two volume levels, either of which may be unknown."""
    known = [value for value in (level, other) if value is not None]
    return min(known) if known else None


def _quieter_mute(muted: bool | None, other: bool | None) -> bool | None:
    """Return muted when either of two mute states is, either of which may be unknown."""
    if True in (muted, other):
        return True
    return muted if muted is not None else other


def _position_anchor(
    protocol: PlayerProtocol,
) -> tuple[tuple[Any, Any], tuple[Any, Any] | None, bool]:
    """Return the playback position anchors a protocol reports, and whether it plays."""
    media = protocol.current_media
    return (
        (protocol.elapsed_time, protocol.elapsed_time_last_updated),
        (media.elapsed_time, media.elapsed_time_last_updated) if media is not None else None,
        protocol.playback_state == PlaybackState.PLAYING,
    )


@final
class Player:
    """
    A player within the Music Assistant Server, as users and clients see it.

    Created and owned by the players controller, which attaches the protocols of the
    player providers to it.
    """

    def __init__(
        self,
        mass: MusicAssistant,
        player_id: str,
        config: PlayerConfig,
        *,
        session_only: bool = False,
    ) -> None:
        """
        Initialize the Player.

        :param mass: The server the player belongs to.
        :param player_id: Id of the player.
        :param config: The config of the player.
        :param session_only: Whether the player only exists while its device is connected.
        """
        self.mass = mass
        self._session_only = session_only
        self.logger = mass.players.logger
        self._cache: dict[str, Any] = {}  # storage dict for cached properties
        self._player_id = player_id
        self._config = config
        self._protocols: dict[str, PlayerProtocol] = {}
        self._roles: dict[str, ProtocolRole] = {}
        self._own_protocol: PlayerProtocol | None = None
        self._audio_protocols: list[PlayerProtocol] = []
        self._linked_protocols: list[LinkedOutputProtocol] = []
        # only probe synced_to when a provider implementation overrides it with its
        # own (cheap) state; the base implementation derives it by scanning sibling
        # protocols, which is cross-player state covered by mark_state_dirty instead
        self._probe_synced_to: dict[str, bool] = {}
        # the ids of all protocols that belong to the player, attached or not
        self._protocol_ids: set[str] = set()
        self._active_output_protocol: str | None = None
        self._extra_data: dict[str, Any] = {}
        self._active_mass_source: str | None = None
        # when a player that went idle forgets its active mass source (monotonic)
        self._active_mass_source_expires_at: float | None = None
        self._sleep_timer_expires_at: float | None = None
        self._sleep_timer_mode: SleepTimerMode | None = None
        # a power on through the power control that waits for the device to come online
        self._power_on_in_progress: bool = False
        # when (monotonic) Music Assistant issued a power off the player has not reported yet
        self._power_off_issued_at: float | None = None
        self._stop_called: bool = False
        self._external_pause_since: float | None = None
        self._ended_external_source: str | None = None
        self._foreign_protocol_id: str | None = None
        # the protocols the state lists, with what they were built from
        self._protocol_infos_cache: tuple[Any, list[PlayerProtocolInfo]] | None = None
        # the level and mute the player had, by feature, when its own protocol became
        # unreachable; None once a command or an output it was given took over
        self._held_volume: dict[PlayerFeature, Any] = {}
        # Palette for the image currently shown, resolved asynchronously from the
        # cache controller and carried here so the (synchronous) state serialization
        # can read it back without blocking. See set_resolved_palette.
        self._current_palette: MediaItemPalette | None = None
        self._current_palette_url: str | None = None
        last_seen = mass.config.get_player_value(player_id, CONF_LAST_SEEN)
        self._last_seen: float | None = last_seen if isinstance(last_seen, float | int) else None
        # a player starts out without protocols, so unavailable since it was last seen
        self._unavailable_since: float | None = self._last_seen
        self._was_available: bool = False
        self._initialized = asyncio.Event()
        # Change-tracking internals for update_state:
        # - state_dirty forces a recalculation (state derived from other
        #   sources changed) - starts True so the first update always calculates
        # - input_snapshot/input_anchors hold the player's own inputs at the last
        #   calculation, so a no-change update_state call can return immediately
        # - state_fingerprint holds the flat leaf values of the last calculated
        #   PlayerState, used to determine the changed values without deepcopy
        self._state_dirty: bool = True
        self._input_snapshot: tuple[Any, ...] | None = None
        self._input_anchors: (
            dict[str, tuple[tuple[Any, Any], tuple[Any, Any] | None, bool]] | None
        ) = None
        self._state_fingerprint: dict[str, Any] | None = None
        # The PlayerState is the (snapshotted) final state of the player
        # after applying any config overrides and other transformations,
        # such as the display name and player controls.
        # the state is updated when calling 'update_state' and is what is sent over the API.
        self._state = PlayerState(
            player_id=player_id,
            provider=self.provider_id,
            type=self.reported_type,
            name=self._display_name,
            available=False,
            device_info=self._device_info,
            enabled=self._config.enabled,
            unavailable_since=self._unavailable_since,
            last_seen=self._last_seen,
        )

    # The final values: every field of the state the player published last (PlayerState),
    # in its order.
    @property
    def player_id(self) -> str:
        """Return the id of the player."""
        return self._player_id

    @property
    def provider(self) -> str:
        """Return the id of the provider instance of the player, as published."""
        return self._state.provider

    @property
    def type(self) -> PlayerType:
        """Return the type of the player, as published."""
        return self._state.type

    @property
    def name(self) -> str:
        """Return the name of the player."""
        return self._state.name

    @property
    def available(self) -> bool:
        """Return if clients can use the player, also one switched off at its power control."""
        return self._state.available

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info of the player."""
        return self._state.device_info

    @property
    def supported_features(self) -> set[PlayerFeature]:
        """Return the features the player supports."""
        return self._state.supported_features

    @property
    def playback_state(self) -> PlaybackState:
        """Return the playback state of the player."""
        return self._state.playback_state

    @property
    def elapsed_time(self) -> float | None:
        """Return the elapsed time of the current media, as of elapsed_time_last_updated."""
        return self._state.elapsed_time

    @property
    def elapsed_time_last_updated(self) -> float | None:
        """Return the unix (utc) timestamp at which elapsed_time was taken."""
        return self._state.elapsed_time_last_updated

    @property
    def powered(self) -> bool | None:
        """Return if the player is powered on, None without a power control."""
        return self._state.powered

    @property
    def volume_level(self) -> int | None:
        """Return the volume level of the player (0..100), None without a volume control."""
        return self._state.volume_level

    @property
    def volume_muted(self) -> bool | None:
        """Return if the player is muted, None without a mute control."""
        return self._state.volume_muted

    @property
    def group_members(self) -> UniqueList[str]:
        """Return the ids of the members of the group, or of the players synced to the player."""
        return self._state.group_members

    @property
    def static_group_members(self) -> UniqueList[str]:
        """Return the ids of the group members that can not be ungrouped."""
        return self._state.static_group_members

    @property
    def can_group_with(self) -> set[str]:
        """Return the ids of the players the player can group with."""
        return self._state.can_group_with

    @property
    def synced_to(self) -> str | None:
        """Return the id of the player the player is synced to, if any."""
        return self._state.synced_to

    @property
    def active_sound_mode(self) -> str | None:
        """Return the id of the active sound mode, if any."""
        return self._state.active_sound_mode

    @property
    def sound_mode_list(self) -> UniqueList[PlayerSoundMode]:
        """Return the sound modes the player offers."""
        return self._state.sound_mode_list

    @property
    def options(self) -> UniqueList[PlayerOption]:
        """Return the options the player offers."""
        return self._state.options

    @property
    def active_source(self) -> str | None:
        """Return the id of the active source: a source of the player or a Music Assistant queue."""
        return self._state.active_source

    @property
    def source_list(self) -> UniqueList[PlayerSource]:
        """Return the sources the player offers."""
        return self._state.source_list

    @property
    def active_group(self) -> str | None:
        """Return the id of the group player that holds the player, if any."""
        return self._state.active_group

    @property
    def current_media(self) -> PlayerMedia | None:
        """Return the media the player has loaded, if any."""
        return self._state.current_media

    @property
    def enabled(self) -> bool:
        """Return if the player is enabled."""
        return self._state.enabled

    @property
    def hide_in_ui(self) -> bool:
        """Return if the player is hidden in the UI."""
        return self._state.hide_in_ui

    @property
    def private(self) -> bool:
        """Return if the player is not offered to other clients as a playback or grouping target."""
        return self._state.private

    @property
    def expose_to_ha(self) -> bool:
        """Return if the player is exposed to Home Assistant."""
        return self._state.expose_to_ha

    @property
    def icon(self) -> str:
        """Return the icon of the player: an id of the shared Music Assistant icon set."""
        return self._state.icon

    @property
    def group_volume(self) -> int | None:
        """Return the level of the loudest powered member of the player's group, else its own."""
        return self._state.group_volume

    @property
    def group_volume_muted(self) -> bool | None:
        """Return if all powered members of the player's group are muted, else its own mute."""
        return self._state.group_volume_muted

    @property
    def extra_attributes(self) -> dict[str, EXTRA_ATTRIBUTES_TYPES]:
        """Return the extra attributes of the player, for the UI and other API clients."""
        return self._state.extra_attributes

    @property
    def power_control(self) -> str:
        """Return the control the power of the player goes through."""
        return self._state.power_control

    @property
    def volume_control(self) -> str:
        """Return the control the volume of the player goes through."""
        return self._state.volume_control

    @property
    def mute_control(self) -> str:
        """Return the control the mute of the player goes through."""
        return self._state.mute_control

    @property
    def output_protocols(self) -> list[OutputProtocol]:
        """Return all outputs of the player, each with whether it can be played to."""
        return self._state.output_protocols

    @property
    def active_output_protocol(self) -> str | None:
        """Return the id of the output that renders the audio: "native" or a protocol id."""
        return self._state.active_output_protocol

    @property
    def needs_setup(self) -> bool:
        """Return if the player has to be set up before it can be used."""
        return self._state.needs_setup

    @property
    def setup_reason(self) -> str | None:
        """Return a short slug that tells why the player needs setup."""
        return self._state.setup_reason

    @property
    def has_setup_flow(self) -> bool:
        """Return if an interactive setup flow can be started for the player."""
        return self._state.has_setup_flow

    @property
    def sleep_timer_expires_at(self) -> float | None:
        """Return the unix (utc) timestamp at which the active sleep timer stops playback."""
        return self._state.sleep_timer_expires_at

    @property
    def sleep_timer_mode(self) -> SleepTimerMode | None:
        """Return the point at which the active sleep timer stops playback."""
        return self._state.sleep_timer_mode

    @property
    def active_source_audio(self) -> ActiveSourceAudioDetails | None:
        """Return the audio details of the active external source, if known."""
        return self._state.active_source_audio

    @property
    def legacy_ids(self) -> list[str]:
        """Return the ids the player was known under before, which it keeps answering to."""
        return self._state.legacy_ids

    @property
    def last_seen(self) -> float | None:
        """Return the unix (utc) timestamp at which the player was last seen available."""
        return self._state.last_seen

    @property
    def unavailable_since(self) -> float | None:
        """Return the unix (utc) timestamp since when the player is unavailable, if it is."""
        return self._state.unavailable_since

    @property
    def protocols(self) -> list[PlayerProtocolInfo]:
        """Return all protocols that belong to the player, attached or not, as published."""
        return self._state.protocols

    @property
    def corrected_elapsed_time(self) -> float | None:
        """Return the elapsed time of the current media as of now."""
        return self._state.corrected_elapsed_time

    @property
    def current_item_id(self) -> str | None:
        """Return the id of the queue item the player has loaded, else the uri of its media."""
        return self._state.current_item_id

    @property
    def config(self) -> PlayerConfig:
        """Return the config of the player itself (not that of its protocols)."""
        return self._config

    @property
    def state(self) -> PlayerState:
        """Return the state the player published last: the snapshot for the API and the events."""
        return self._state

    @property
    def session_only(self) -> bool:
        """
        Return if the player only exists while its device is connected.

        True for something that is no device to keep track of once it is gone, such as
        a browser tab, an app or a virtual player.
        """
        return self._session_only

    @property
    def initialized(self) -> asyncio.Event:
        """
        Return if the player is initialized.

        Used by player controller to indicate that the player was published.
        """
        return self._initialized

    @property
    def attached_protocols(self) -> Mapping[str, PlayerProtocol]:
        """Return the protocol objects that are attached to the player right now, by their id."""
        return self._protocols

    @property
    def protocol_ids(self) -> set[str]:
        """Return the ids of all protocols that belong to the player, attached or not."""
        return self._protocol_ids

    @property
    def own_protocol(self) -> PlayerProtocol | None:
        """
        Return the attached protocol that stands for the device itself, if any.

        That is its native protocol, or a protocol that controls it without an audio
        path, or the virtual protocol of a group. Its settings are the unprefixed ones
        in the config of the player.
        """
        return self._own_protocol

    @property
    def audio_protocols(self) -> list[PlayerProtocol]:
        """Return the attached audio protocols of the player, most preferred first."""
        return self._audio_protocols

    @property
    def device_protocol(self) -> PlayerProtocol | None:
        """
        Return the attached protocol that reports for the device, if any.

        That is its own protocol, else its most preferred audio protocol. Ask it for
        what the device itself reports, such as its source, source list or current media.
        """
        return self._own_protocol or (self._audio_protocols[0] if self._audio_protocols else None)

    def get_role(self, protocol_id: str) -> ProtocolRole | None:
        """
        Return the role a protocol is attached to the player in, None when it is not attached.

        :param protocol_id: Id of the protocol.
        """
        return self._roles.get(protocol_id)

    @property
    def reported_type(self) -> PlayerType:
        """Return the type the own protocol of the player reports now, else the stored type."""
        own = self._own_protocol
        player_type = own.type if own is not None else self._config.player_type
        # a player is never a protocol, whatever one of its protocols reports
        return PlayerType.PLAYER if player_type == PlayerType.PROTOCOL else player_type

    @property
    def provider_id(self) -> str:
        """
        Return the id of the provider instance the protocols of the player give it right now.

        That of its own protocol, else of its most preferred attached audio protocol,
        else the one it had when a protocol was last attached.
        """
        if (protocol := self.device_protocol) is not None:
            return protocol.provider_id
        return self._config.provider

    @property
    def translation_owner(self) -> str:
        """Return the translation owner namespace ("provider.<domain>") of the player."""
        return f"provider.{self.provider_id.split('--', maxsplit=1)[0]}"

    @property
    def available_for_playback(self) -> bool:
        """
        Return if the player can be played to right now: enabled, reachable and set up.

        That takes a protocol that can render audio: a control protocol alone does not
        count. Neither a device that still needs setup - an unpaired AirPlay receiver, for
        example - nor a disabled player can accept a stream.

        For the players controller and the models, where a stream or a command goes to the
        device at this instant. Everyone else reads available: a player that is switched off
        at its power control is available too (see offline_by_power_control), and a command
        that starts playback on it powers it on first.
        """
        if not self._config.enabled:
            return False
        for protocol_id, protocol in self._protocols.items():
            if protocol.available_for_playback and self._roles[protocol_id] != ProtocolRole.CONTROL:
                return True
        return False

    @property
    def offline_by_power_control(self) -> bool:
        """
        Return if the device is offline because it is switched off at its external power control.

        Also True while Music Assistant powers it on through that control and waits for it
        to come online. Such a player is reported to clients as available and powered off,
        so it can be powered on (or played to) from there, while the device itself is not
        reachable.
        """
        return self._offline_by_power_control(self._reachable)

    @property
    def extra_data(self) -> dict[str, Any]:
        """
        Return the extra data of the player.

        This is a dict that can be used to store any extra data
        that is not part of the player state or config.
        This is not persisted and not exposed on the API.
        """
        return self._extra_data

    @cached_property
    def default_icon(self) -> str:
        """Return the default player icon."""
        device_info = self._device_info
        return get_default_player_icon(
            self.reported_type,
            self._own_protocol_domain(),
            device_info.manufacturer,
            device_info.model,
        )

    @property
    def hidden_by_default(self) -> bool:
        """Return if the player is hidden in the UI unless the user chose otherwise."""
        return self._protocol_default("hidden_by_default", False)

    @property
    def expose_to_ha_by_default(self) -> bool:
        """Return if the player is exposed to Home Assistant unless the user chose otherwise."""
        return self._protocol_default("expose_to_ha_by_default", True)

    @property
    def flow_mode(self) -> bool:
        """Return if the protocol that renders the audio of the player needs flow mode."""
        output_protocol = self.resolve_output_protocol()
        return output_protocol.flow_mode if output_protocol else True

    @property
    def supports_enqueue(self) -> bool:
        """
        Return if the player supports enqueueing tracks.

        This considers the active output protocol's capabilities if one is active.
        If an audio protocol is active, checks that protocol's ENQUEUE feature.
        Otherwise checks the native protocol's ENQUEUE feature.
        """
        return self._check_feature_with_active_protocol(PlayerFeature.ENQUEUE)

    @property
    def supports_gapless(self) -> bool:
        """
        Return if the player supports gapless playback.

        This considers the active output protocol's capabilities if one is active.
        If an audio protocol is active, checks that protocol's GAPLESS_PLAYBACK feature.
        Otherwise checks the native protocol's GAPLESS_PLAYBACK feature.
        """
        return self._check_feature_with_active_protocol(PlayerFeature.GAPLESS_PLAYBACK)

    # Protocol-related properties and helpers

    @cached_property
    def playback_domains(self) -> set[str]:
        """
        Return the protocol domains this player can be reached on right now.

        Only outputs that are available at this moment are included, so a protocol
        whose device went offline is left out.
        """
        return {output.protocol_domain for output in self._output_protocols if output.available}

    @property
    def linked_output_protocols(self) -> list[LinkedOutputProtocol]:
        """Return the attached audio protocols as output options, most preferred first."""
        return self._linked_protocols

    def set_active_output_protocol(self, protocol_id: str | None) -> None:
        """
        Set the currently active output protocol ID.

        :param protocol_id: The id of the audio protocol to set as active, "native" for
            native playback, or None to clear the active protocol.
        """
        # cancel any pending scheduled protocol clear,
        # as we're explicitly setting it now
        self.mass.cancel_task(f"clear_active_protocol_{self.player_id}")
        if self._own_protocol is not None and protocol_id == self._own_protocol.protocol_id:
            protocol_id = "native"  # Normalize to "native" for the native protocol
        if self._active_output_protocol == protocol_id:
            return  # No change
        if protocol_id:
            protocol_name = protocol_id
            if protocol_id == "native":
                protocol_name = "Native"
            elif protocol := self._protocols.get(protocol_id):
                protocol_name = protocol.provider.name
            self.logger.info(
                "Setting active output protocol on %s to %s",
                self.name,
                protocol_name,
            )
        else:
            self.logger.info(
                "Clearing active output protocol on %s",
                self.name,
            )
        self._active_output_protocol = protocol_id
        self.update_state()

    @property
    def foreign_protocol(self) -> PlayerProtocol | None:
        """Return the audio protocol whose app to show and control on this player."""
        if self._foreign_protocol_id is None:
            return None
        return self._protocols.get(self._foreign_protocol_id)

    def linked_foreign_protocol(self) -> PlayerProtocol | None:
        """Return the audio protocol that plays an app Music Assistant did not start."""
        for protocol in self._audio_protocols:
            if (
                protocol.available
                and protocol.playback_state in (PlaybackState.PLAYING, PlaybackState.PAUSED)
                # Music Assistant's own playback through a protocol reports no source, and
                # following a group is no source of its own
                and any(x.id == protocol.active_source for x in protocol.source_list)
            ):
                return protocol
        return None

    def get_linked_protocol(self, output_protocol_id: str) -> OutputProtocol | None:
        """
        Get an attached audio protocol by its id, with its name and availability resolved.

        :param output_protocol_id: id of the attached audio protocol.
        """
        for linked in self._linked_protocols:
            if linked.output_protocol_id == output_protocol_id:
                protocol = self._protocols[output_protocol_id]
                return OutputProtocol(
                    output_protocol_id=linked.output_protocol_id,
                    name=protocol.provider.name,
                    protocol_domain=linked.protocol_domain,
                    priority=linked.priority,
                    available=protocol.available_for_playback,
                    derived_from=linked.derived_from,
                )
        return None

    def get_output_protocol_by_domain(self, protocol_domain: str) -> OutputProtocol | None:
        """
        Get an output protocol by domain, including native protocol.

        Unlike get_linked_protocol, this also covers the player's own native output.

        :param protocol_domain: The protocol domain to search for (e.g., "airplay", "sonos").
        """
        for output_protocol in self._output_protocols:
            if output_protocol.protocol_domain == protocol_domain:
                return output_protocol
        return None

    def get_output_protocol(self, output_protocol_id: str) -> PlayerProtocol | None:
        """
        Return the attached protocol behind an output of the player.

        :param output_protocol_id: Id of the output: "native" or the id of a protocol.
        """
        if output_protocol_id == "native":
            own = self._own_protocol
            return own if own and PlayerFeature.PLAY_MEDIA in own.supported_features else None
        return self._protocols.get(output_protocol_id)

    def resolve_output_protocol(self) -> PlayerProtocol | None:
        """
        Return the protocol that actually renders this player's audio output.

        For a player playing via one of its audio protocols this is the active
        protocol; in all other cases (native output, group players serving their own
        stream) it is its own protocol. None for a player without any of them attached.
        """
        active_protocol = self._active_output_protocol
        if (
            active_protocol
            and active_protocol != "native"
            and (protocol := self._protocols.get(active_protocol))
        ):
            return protocol
        return self._own_protocol

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
        Return a single config value from the active configuration of the player itself.

        Entry defaults are already applied to the active configuration, so the
        default is only returned when the key itself is not present.

        :param key: The config key to retrieve.
        :param default: Value to return when the key is not present in the config.
        :param return_type: Optional type hint for type inference (e.g., str, int, bool).
            Note: This parameter is used purely for static type checking and does not
            perform runtime type validation. Callers are responsible for ensuring the
            specified type matches the actual config value type.
        """
        return self._config.get_value(key, default)

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
        Return a config value resolved on the protocol that renders the audio output.

        Audio/output related settings (output codec, fixed content length, output channels,
        sample rates) live on the protocol that actually renders the audio: the
        active audio protocol when outputting via one, otherwise the player's own
        protocol. The output protocol's value (or its provider's entry default) takes
        precedence; the value of the own protocol is the fallback for keys the output
        protocol has no entry for.

        :param key: The config key to retrieve.
        :param default: Value to return when the key is not present in any config.
        :param return_type: Optional type hint for type inference (e.g., str, int, bool).
            Note: This parameter is used purely for static type checking and does not
            perform runtime type validation. Callers are responsible for ensuring the
            specified type matches the actual config value type.
        """
        output_protocol = self.resolve_output_protocol()
        if output_protocol is not None and key in output_protocol.config.values:
            return output_protocol.config.get_value(key, default)
        own = self._own_protocol
        if own is not None and own is not output_protocol:
            return own.config.get_value(key, default)
        return default

    def mark_state_dirty(self) -> None:
        """
        Mark the player's (final) state as dirty.

        Forces the next update_state call to recalculate the full PlayerState.
        Must be called when state the player derives from changed outside the
        values its protocols report (e.g. group topology, the active queue) - the
        player controller does this automatically for all its notification paths
        (trigger_player_update and the state fan-out).
        """
        self._state_dirty = True

    def refresh_state(self, signal_event: bool = True) -> None:
        """
        Recalculate the player state unconditionally.

        Convenience shorthand for mark_state_dirty() + update_state(), for core
        code reacting to changes outside the values the protocols report.

        :param signal_event: If True, signal the state update event to the PlayerController.
        """
        self.mark_state_dirty()
        self.update_state(signal_event=signal_event)

    def update_state(self, force_update: bool = False, signal_event: bool = True) -> None:
        """
        Update the PlayerState from what the protocols of the player report.

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
        for protocol in self._protocols.values():
            protocol.invalidate_cache()
        self._expire_stale_external_pause()
        self._expire_idle_mass_source()
        new_snapshot = self._collect_input_snapshot()
        if (
            not force_update
            and not self._state_dirty
            and self._input_snapshot is not None
            and new_snapshot == self._input_snapshot
            and not self._position_anchors_moved()
        ):
            # None of the player's own inputs changed since the last calculation:
            # nothing to do. Changes the player derives from other sources
            # (players/queues/config) always come with a mark_state_dirty call.
            return
        self._state_dirty = False
        self._input_snapshot = new_snapshot
        self._input_anchors = {
            protocol_id: _position_anchor(protocol)
            for protocol_id, protocol in self._protocols.items()
        }
        # read once: every protocol is asked for it
        reachable = self._reachable
        self._track_availability(reachable)
        # calculate the new state
        prev_state = self._state
        self._track_held_volume()
        self._state, effects = derive_player_state(self._collect_state_inputs(reachable))
        self._foreign_protocol_id = effects.foreign_protocol_id
        if effects.clear_active_output:
            self.logger.info("Clearing active output protocol on %s", self.name)
            self._active_output_protocol = None
        # track stop called state
        if effects.left_idle:
            self._stop_called = False
            self._active_mass_source_expires_at = None
            self.mass.cancel_timer(f"set_mass_source_{self.player_id}")
        elif effects.went_idle:
            self._stop_called = True
            # when we're going to idle, the active mass source is forgotten after a short
            # delay, unless the player starts playing again before that
            if self._active_mass_source is not None:
                self._active_mass_source_expires_at = time.monotonic() + MASS_SOURCE_IDLE_MEMORY
                self.mass.call_later(
                    MASS_SOURCE_IDLE_MEMORY,
                    self.update_state,
                    task_id=f"set_mass_source_{self.player_id}",
                )
        changed_values = self._diff_state(prev_state, reachable)
        if not MEDIA_IDENTITY_KEYS.isdisjoint(changed_values.keys()):
            # current media changed, call the media updated callback
            # debounce the callback to avoid multiple calls when multiple
            # state updates happen in a short time
            self.mass.call_later(
                MEDIA_UPDATED_DEBOUNCE,
                self._on_media_updated,
                task_id=f"player_media_updated_{self.player_id}",
            )
        # persist the default name if it changed
        if effects.default_name:
            self._config.default_name = effects.default_name
            self.mass.config.set_player_default_name(self.player_id, effects.default_name)
        if "device_info.model" in changed_values or "device_info.manufacturer" in changed_values:
            self.store_protocol_defaults()
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

    def store_protocol_defaults(self) -> None:
        """
        Store what the protocols of the player give it, for when none of them is attached.

        That is its device info, and whether its own protocol hides it in the UI, exposes it to
        Home Assistant and keeps it private by default. Nothing is stored for a player without
        a stored config.
        """
        if not self._protocols:
            return
        conf_key = f"{CONF_PLAYERS}/{self.player_id}"
        if self.mass.config.get(conf_key) is None:
            # a player that only exists while its device is connected has no config to keep it in
            return
        device_info = self._device_info
        stored_info = {"model": device_info.model, "manufacturer": device_info.manufacturer}
        for key in ("software_version", "model_id", "manufacturer_id"):
            if value := getattr(device_info, key):
                stored_info[key] = value
        values: dict[str, Any] = {CONF_DEVICE_INFO: stored_info}
        if (own := self._own_protocol) is not None:
            values[CONF_PROTOCOL_DEFAULTS] = {
                "hidden_by_default": own.hidden_by_default,
                "expose_to_ha_by_default": own.expose_to_ha_by_default,
                "private": own.private,
            }
        for key, value in values.items():
            if self.mass.config.get(f"{conf_key}/values/{key}") != value:
                self.mass.config.set(f"{conf_key}/values/{key}", value)

    def mark_external_source_ended(self) -> None:
        """
        Stop presenting the external source the device has loaded as something to resume.

        Call this when the device makes clear that the session is gone, for example when
        it refuses to resume playback. Normal queue handling takes over from there.
        It acts on the own protocol of the player: the source an audio protocol reports
        stays as it is. Call :meth:`update_state` afterwards to publish the change.
        """
        if (own := self._own_protocol) is None:
            return
        if (active_source := own.active_source) is not None:
            # the updates that follow rebuild the state from the device, which keeps
            # reporting the very same source as paused, so remembering which source we
            # gave up on is what keeps this applied
            self._ended_external_source = active_source
        self._external_pause_since = None
        own.on_external_source_ended()

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
        self._current_palette_url = image_url
        self._current_palette = palette

    def set_config(self, config: PlayerConfig) -> None:
        """
        Set/update the player config.

        May only be called by the PlayerController.
        """
        self._config = config
        # config feeds several (cached) state values, so invalidate all cached
        # properties (including the config-derived ones) and force a recalculation
        self._cache.clear()
        self.mark_state_dirty()

    def set_initialized(self) -> None:
        """Set the player as initialized."""
        self._initialized.set()

    def add_protocol(self, protocol: PlayerProtocol, role: ProtocolRole) -> None:
        """
        Attach a protocol to the player.

        May only be called by the PlayerController.

        :param protocol: The protocol to attach.
        :param role: The role it attaches in.
        """
        protocol_id = protocol.protocol_id
        self._protocols[protocol_id] = protocol
        self._roles[protocol_id] = role
        self._protocol_ids.add(protocol_id)
        self._probe_synced_to[protocol_id] = (
            type(protocol).synced_to is not PlayerProtocol.synced_to
        )
        self._index_protocols()

    def remove_protocol(self, protocol_id: str, *, forget: bool = False) -> None:
        """
        Detach a protocol from the player.

        May only be called by the PlayerController.

        :param protocol_id: Id of the protocol to detach.
        :param forget: The protocol no longer belongs to the player at all.
        """
        self._protocols.pop(protocol_id, None)
        self._roles.pop(protocol_id, None)
        self._probe_synced_to.pop(protocol_id, None)
        if forget:
            self._protocol_ids.discard(protocol_id)
        if self._active_output_protocol == protocol_id or (
            self._active_output_protocol == "native"
            and self._own_protocol is not None
            and self._own_protocol.protocol_id == protocol_id
        ):
            self._active_output_protocol = None
        own = self._own_protocol
        if own is not None and own.protocol_id == protocol_id:
            if own.external_pause_idle_timeout is not None:
                self.mass.cancel_timer(f"external_pause_{self.player_id}")
            self._external_pause_since = None
            self._ended_external_source = None
        self._index_protocols()

    def to_dict(self) -> dict[str, Any]:
        """Return the (serializable) dict representation of the Player."""
        return self.state.to_dict()

    def volume_control_for_output(self, output_protocol_id: str) -> str:
        """
        Return the volume control that owns audio rendered over the given output.

        Unlike :attr:`volume_control`, which answers where a volume command should go
        right now, this answers who owns the volume of one specific output. Callers that
        know which interface is about to carry the audio must use this, so the answer does
        not depend on whether that output has been marked active yet.

        :param output_protocol_id: Id of the output protocol rendering the audio.
        :return: A control id, or NATIVE/FAKE/NONE. NONE means nothing in the signal path
            of that output owns the volume; no sibling interface is offered as a fallback.
        """
        return self._control_for_output(
            PlayerFeature.VOLUME_SET, CONF_VOLUME_CONTROL, output_protocol_id
        )

    def mute_control_for_output(self, output_protocol_id: str) -> str:
        """
        Return the mute control that owns audio rendered over the given output.

        The mute counterpart of :meth:`volume_control_for_output`.

        :param output_protocol_id: Id of the output protocol rendering the audio.
        """
        control = self._control_for_output(
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

    def get_control_protocol(self, control: str) -> PlayerProtocol | None:
        """
        Return the attached protocol a resolved power, volume or mute control stands for.

        :param control: A resolved control: NATIVE, or the id of an attached protocol.
            Any other control (fake, none, a player control) is no protocol.
        """
        if control == PLAYER_CONTROL_NATIVE:
            return self._own_protocol
        return self._protocols.get(control)

    def holds_volume(self, feature: PlayerFeature) -> bool:
        """
        Return whether the player keeps the level or mute it had when its own protocol dropped.

        While its own protocol is attached but can not be reached, the player keeps the level
        and mute it had, whichever protocol has the automatic control meanwhile: it is never
        shown louder than that protocol reports, and the output activated for playback is
        given them. That ends when a command goes to the control, when an output was given
        them, or when the own protocol can be reached again.

        :param feature: VOLUME_SET for the level, VOLUME_MUTE for the mute.
        """
        return self._held_volume.get(feature) is not None

    def release_held_volume(self, *features: PlayerFeature) -> None:
        """
        Stop keeping the level or mute of the player: what its control reports counts again.

        :param features: VOLUME_SET for the level, VOLUME_MUTE for the mute; both when none
            is given.
        """
        for feature in features or (PlayerFeature.VOLUME_SET, PlayerFeature.VOLUME_MUTE):
            if self._held_volume.get(feature) is not None:
                self._held_volume[feature] = None
                self.mark_state_dirty()

    def set_active_mass_source(self, value: str | None) -> None:
        """
        Set the id of the (last) active mass source.

        This is to keep track of the last active MA source for the player,
        so we can restore it when needed (e.g. after switching to a plugin source).
        """
        self.mass.cancel_timer(f"set_mass_source_{self.player_id}")
        self._active_mass_source_expires_at = None
        self._active_mass_source = value
        self.update_state()

    def set_sleep_timer(self, mode: SleepTimerMode | None, expires_at: float | None) -> None:
        """
        Set the sleep timer of the player and publish it.

        :param mode: The point at which the sleep timer stops playback, None to clear it.
        :param expires_at: The unix (utc) timestamp at which it stops playback, None when
            that is not known or the timer is cleared.
        """
        self._sleep_timer_mode = mode
        self._sleep_timer_expires_at = expires_at
        self.update_state()

    @property
    def power_on_in_progress(self) -> bool:
        """Return if a power on through the power control waits for the device to come online."""
        return self._power_on_in_progress

    def set_power_on_in_progress(self, value: bool) -> None:
        """
        Set whether a power on through the power control waits for the device to come online.

        May only be called by the PlayerController.

        :param value: True while the power on waits for the device, False once it ended or
            was called off.
        """
        self._power_on_in_progress = value
        self.mark_state_dirty()

    @property
    def power_off_issued_at(self) -> float | None:
        """Return when (monotonic) Music Assistant issued a power off that is not reported yet."""
        return self._power_off_issued_at

    def set_power_off_issued_at(self, value: float | None) -> None:
        """
        Set when (monotonic) Music Assistant issued a power off of the player.

        May only be called by the PlayerController.

        :param value: The time the power off was issued, None once the player reported it.
        """
        self._power_off_issued_at = value

    def mark_stop_called(self) -> None:
        """Mark that the STOP command was called on the player."""
        self._stop_called = True

    @property
    def stop_called(self) -> bool:
        """
        Return True if the STOP command was called on the player.

        This is used to differentiate between a user-initiated stop
        and a natural end of playback (e.g. end of track/queue).
        mainly for debugging/logging purposes by the streams controller.
        """
        return self._stop_called

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

    @property
    def _reachable(self) -> bool:
        """
        Return if a protocol of the player that can render audio reaches its device.

        A control protocol alone does not count: there is nothing to play on.
        """
        # a plain loop: this is read for every player the state of another one refers to
        for protocol_id, protocol in self._protocols.items():
            if protocol.available and self._roles[protocol_id] != ProtocolRole.CONTROL:
                return True
        return False

    @cached_property
    def _display_name(self) -> str:
        """Return the name the player is published with."""
        if custom_name := self._config.name:
            # always prefer the custom name over the default name
            return custom_name
        if name := self._followed_name() or self._config.default_name:
            return name
        # a new device that reports nothing better is shown by the name it reports,
        # also while it is away
        return (
            next((p.name for p in self._protocols.values() if p.name), None)
            or self._stored_protocol_name()
            or self.player_id
        )

    @property
    def _device_info(self) -> DeviceInfo:
        """
        Return the device info of the player.

        That of its own protocol, else of its most preferred attached audio protocol,
        with the identifiers the other protocols add; else what was last stored.
        """
        if (primary := self.device_protocol) is None:
            return self._stored_device_info()
        device_info = primary.device_info
        manufacturer = device_info.manufacturer or primary.provider.name
        if len(self._protocols) == 1 and manufacturer == device_info.manufacturer:
            return device_info
        merged = DeviceInfo(
            model=device_info.model,
            manufacturer=manufacturer,
            software_version=device_info.software_version,
            model_id=device_info.model_id,
            manufacturer_id=device_info.manufacturer_id,
        )
        for protocol in (primary, *self._protocols.values()):
            for identifier_type, value in protocol.device_info.identifiers.items():
                if identifier_type not in merged.identifiers:
                    merged.add_identifier(identifier_type, value)
        return merged

    @property
    def _extra_attributes(self) -> dict[str, EXTRA_ATTRIBUTES_TYPES]:
        """
        Return the extra attributes of the player.

        This is a dict that can be used to pass any extra (serializable)
        attributes over the API, to be consumed by the UI (or another APi client, such as HA).
        This is not persisted and not used or validated by the core logic.
        """
        if (protocol := self.device_protocol) is not None:
            return protocol.extra_attributes
        return {}

    @property
    def _has_setup_flow(self) -> bool:
        """
        Return if an interactive setup flow can be started for this player.

        True when one of its protocols has a setup flow that currently has something
        to offer. Unlike ``needs_setup`` this stays True once setup completed, so the UI
        can offer to re-run the flow on demand (e.g. to redo a pairing step that was skipped).
        """
        return any(protocol.has_setup_flow for protocol in self._protocols.values())

    @cached_property
    def _hide_in_ui(self) -> bool:
        """
        Return the hide player in UI options.

        This is a convenience property based on the config entry.
        """
        return bool(self._config.get_value(CONF_HIDE_IN_UI, self.hidden_by_default))

    @cached_property
    def _expose_to_ha(self) -> bool:
        """
        Return if the player should be exposed to Home Assistant.

        This is a convenience property that returns True if the player is set to be exposed
        to Home Assistant, based on the config entry.
        """
        return bool(self._config.get_value(CONF_EXPOSE_PLAYER_TO_HA, self.expose_to_ha_by_default))

    @cached_property
    def _icon(self) -> str:
        """Return the player icon."""
        icon = self.mass.config.get_player_value(self.player_id, CONF_ENTRY_PLAYER_ICON.key)
        return icon if isinstance(icon, str) and icon else self.default_icon

    @cached_property
    def _legacy_ids(self) -> list[str]:
        """Return the ids the player was known under before, which it keeps answering to."""
        legacy_ids = self.mass.config.get_player_value(self.player_id, CONF_LEGACY_IDS)
        if not isinstance(legacy_ids, list):
            return []
        return [legacy_id for legacy_id in legacy_ids if isinstance(legacy_id, str)]

    @cached_property
    def _power_control(self) -> str:
        """Return the power control type."""
        conf = self._stored_control_conf(CONF_POWER_CONTROL, PlayerFeature.POWER)
        if conf and conf in (PLAYER_CONTROL_NATIVE, PLAYER_CONTROL_FAKE, PLAYER_CONTROL_NONE):
            # the control type is explicitly set in the config, use that
            return str(conf)
        if conf and (_control := self.mass.players.get_player_control(str(conf))):
            # the control type is explicitly set to a player control,
            return _control.id
        # handle auto-select logic if not explicitly set in config
        if self._own_supports(PlayerFeature.POWER):
            # player supports native power control, always prefer that
            return PLAYER_CONTROL_NATIVE
        return PLAYER_CONTROL_NONE

    @cached_property
    def _volume_control(self) -> str:
        """Return the volume control type."""
        return self._resolve_control(PlayerFeature.VOLUME_SET, CONF_VOLUME_CONTROL)

    @cached_property
    def _mute_control(self) -> str:
        """Return the mute control type."""
        # this resolves independently from volume_control, so a device whose interfaces
        # advertise volume and mute separately (DLNA derives both from its own
        # RenderingControl actions) can end up with the two controls on different
        # protocols.
        control = self._resolve_control(PlayerFeature.VOLUME_MUTE, CONF_MUTE_CONTROL)
        if control == PLAYER_CONTROL_FAKE and self._volume_control == PLAYER_CONTROL_NONE:
            # fake mute is simulated by setting the volume to zero, so without a volume
            # control to drive there is no way to mute this player at all
            return PLAYER_CONTROL_NONE
        return control

    @cached_property
    def _output_protocols(self) -> list[OutputProtocol]:
        """
        Return all output options for this player.

        Includes:
        - Native playback (if the native protocol of the player supports PLAY_MEDIA)
        - The attached audio protocols
        - The protocols that belong to the player but are not attached, such as one the
          user switched off

        Each entry has an available flag indicating current availability.
        """
        result: list[OutputProtocol] = []
        listed_ids: set[str] = set()

        # Add native playback option if applicable
        if (own := self._own_protocol) is not None:
            listed_ids.add(own.protocol_id)
            if PlayerFeature.PLAY_MEDIA in own.supported_features:
                result.append(
                    OutputProtocol(
                        output_protocol_id="native",
                        name=own.provider.name,
                        protocol_domain=own.provider.domain,
                        priority=0,  # Native is always highest priority
                        available=own.available_for_playback,
                        is_native=True,
                    )
                )
            elif (
                own.provider.domain in PROTOCOL_PRIORITY
                and PlayerFeature.SET_MEMBERS in own.supported_features
            ):
                # The own protocol is itself of a known protocol domain.
                result.append(
                    OutputProtocol(
                        output_protocol_id=own.protocol_id,
                        name=own.provider.name,
                        protocol_domain=own.provider.domain,
                        priority=PROTOCOL_PRIORITY[own.provider.domain],
                        available=own.available_for_playback,
                        is_native=True,
                    )
                )

        # Add the attached audio protocols
        for linked in self._linked_protocols:
            listed_ids.add(linked.output_protocol_id)
            protocol = self._protocols[linked.output_protocol_id]
            result.append(
                OutputProtocol(
                    output_protocol_id=linked.output_protocol_id,
                    name=protocol.provider.name,
                    protocol_domain=linked.protocol_domain,
                    priority=linked.priority,
                    available=protocol.available_for_playback,
                    derived_from=linked.derived_from,
                )
            )

        # Add the protocols that are not attached
        for protocol_id in self._protocol_ids - listed_ids:
            if not (stored := self.mass.config.get_protocol_record(protocol_id)) or stored.get(
                "role"
            ) not in (ProtocolRole.NATIVE, ProtocolRole.AUDIO):
                continue
            provider_id = stored.get("provider") or ""
            protocol_domain = provider_id.split("--")[0] if provider_id else "unknown"
            is_native = stored.get("role") == ProtocolRole.NATIVE
            # resolve the persisted derived-transport edge (if any) so derived
            # outputs keep their base reference even while not attached
            derived_from = (stored.get("values") or {}).get(CONF_UNDERLYING_PLAYER_ID)
            if derived_from and own is not None and derived_from == own.protocol_id:
                derived_from = "native"
            result.append(
                OutputProtocol(
                    output_protocol_id="native" if is_native else protocol_id,
                    name=protocol_domain.title(),
                    protocol_domain=protocol_domain,
                    priority=0 if is_native else PROTOCOL_PRIORITY.get(protocol_domain, 100),
                    available=False,  # Protocols that are not attached are not available
                    is_native=is_native,
                    derived_from=None if is_native else derived_from,
                )
            )

        # Sort by priority (lower = more preferred)
        result.sort(key=lambda o: o.priority)
        return result

    def _index_protocols(self) -> None:
        """Rebuild what is kept per role of the attached protocols."""
        self._own_protocol = next(
            (
                protocol
                for protocol_id, protocol in self._protocols.items()
                # one that reports another role was attached as control protocol under
                # a protocol that ranks above it
                if self._roles[protocol_id] in OWN_PROTOCOL_ROLES
                and self._roles[protocol_id] == protocol.role
            ),
            None,
        )
        self._audio_protocols = sorted(
            (
                protocol
                for protocol_id, protocol in self._protocols.items()
                if self._roles[protocol_id] not in OWN_PROTOCOL_ROLES
            ),
            key=lambda protocol: PROTOCOL_PRIORITY.get(protocol.provider.domain, 100),
        )
        own_id = self._own_protocol.protocol_id if self._own_protocol else None
        self._linked_protocols = [
            LinkedOutputProtocol(
                output_protocol_id=protocol.protocol_id,
                protocol_domain=protocol.provider.domain,
                priority=PROTOCOL_PRIORITY.get(protocol.provider.domain, 100),
                derived_from=(
                    "native"
                    if (underlying_id := protocol.underlying_protocol_id)
                    and underlying_id == own_id
                    else underlying_id
                ),
            )
            for protocol in self._audio_protocols
        ]
        self._protocol_infos_cache = None
        self._cache.clear()
        self.mark_state_dirty()

    def _on_media_updated(self) -> None:
        """Let the protocols know that the current media of the player was updated."""
        for protocol in list(self._protocols.values()):
            protocol.on_player_media_updated()

    def _own_supports(self, feature: PlayerFeature) -> bool:
        """Return whether the own protocol of the player is attached and has a feature."""
        own = self._own_protocol
        return own is not None and feature in own.supported_features

    def _own_controls(self, feature: PlayerFeature) -> bool:
        """Return whether the own protocol of the player has a feature and can be reached."""
        own = self._own_protocol
        return own is not None and own.available and feature in own.supported_features

    def _track_held_volume(self) -> None:
        """
        Keep the level and mute the player had when its own protocol became unreachable.

        What the own protocol reported last may be stale then, and so may what the other
        protocols report: the player is shown as the quieter of what it had and what the
        protocol in control reports, until it is reachable again (see holds_volume).
        """
        if (own := self._own_protocol) is None:
            # it may come back reachable, or as it went
            return
        if own.available:
            self._held_volume.clear()
            return
        # the state is still the one published last
        previous = self._state
        self._held_volume.setdefault(PlayerFeature.VOLUME_SET, previous.volume_level)
        self._held_volume.setdefault(PlayerFeature.VOLUME_MUTE, previous.volume_muted)

    def _applies_held_volume(self, feature: PlayerFeature, control: str) -> bool:
        """Return whether a held level or mute applies to the control resolved for it."""
        # what a protocol reports can be stale; a fake control is the player's own level
        return self._held_volume.get(feature) is not None and control in self._protocols

    def _followed_name(self) -> str | None:
        """
        Return the name the stored default name of the player follows, None to keep it.

        That is the name its own protocol reports. An audio protocol only names a player
        that has no protocol of its own, or no stored name yet.
        """
        if (own := self._own_protocol) is not None and own.name:
            return own.name
        if self._config.default_name and self._own_protocol_domain() is not None:
            return None
        return get_device_name(None, self._audio_protocols)

    def _own_protocol_domain(self) -> str | None:
        """Return the provider domain of the own protocol of the player, also while it is away."""
        if (own := self._own_protocol) is not None:
            return own.provider.domain
        if (protocol_id := self.mass.config.get_primary_protocol_id(self.player_id)) is None:
            return None
        stored = self.mass.config.get_protocol_record(protocol_id) or {}
        return str(stored.get("provider") or "").split("--", maxsplit=1)[0]

    def _stored_protocol_name(self) -> str | None:
        """Return the name a protocol of the player was last seen with, its own protocol first."""
        names: list[tuple[bool, int, str, str]] = []
        for protocol_id in self._protocol_ids:
            stored = self.mass.config.get_protocol_record(protocol_id) or {}
            if name := stored.get("default_name"):
                domain = str(stored.get("provider") or "").split("--", maxsplit=1)[0]
                is_own = stored.get("role") in OWN_PROTOCOL_ROLES
                names.append((not is_own, _NAME_PRIORITY.get(domain, 10), protocol_id, str(name)))
        return min(names)[3] if names else None

    def _stored_device_info(self) -> DeviceInfo:
        """Return the device info of a player without protocols, from what was stored."""
        stored = self.mass.config.get(f"{CONF_PLAYERS}/{self.player_id}/values/{CONF_DEVICE_INFO}")
        stored = stored if isinstance(stored, dict) else {}
        manufacturer = stored.get("manufacturer")
        if not manufacturer:
            # clients fall back to the provider for a name, which may not be loaded
            domain = self._config.provider.split("--", maxsplit=1)[0]
            try:
                manufacturer = self.mass.get_provider_manifest(domain).name
            except KeyError:
                manufacturer = domain
        device_info = DeviceInfo()
        if manufacturer:
            device_info.manufacturer = str(manufacturer)
        if model := stored.get("model"):
            device_info.model = str(model)
        for key in ("software_version", "model_id", "manufacturer_id"):
            if isinstance(value := stored.get(key), str):
                setattr(device_info, key, value)
        return device_info

    def _protocol_default(self, key: str, default: bool) -> bool:
        """
        Return a default the own protocol gives the player, else the one it gave last.

        :param key: Name of the property of the protocol.
        :param default: The value for a player whose own protocol never gave one.
        """
        if (own := self._own_protocol) is not None:
            return bool(getattr(own, key))
        stored = self.mass.config.get(
            f"{CONF_PLAYERS}/{self.player_id}/values/{CONF_PROTOCOL_DEFAULTS}"
        )
        return bool(stored.get(key, default)) if isinstance(stored, dict) else default

    def _offline_by_power_control(self, reachable: bool) -> bool:
        """
        Return if the device is offline because it is switched off at its external power control.

        :param reachable: Whether a protocol that renders audio reaches the device.
        """
        if reachable or self.reported_type == PlayerType.GROUP:
            return False
        control = self.mass.players.get_player_control(self._power_control)
        if control is None or not control.supports_power:
            return False
        return not control.power_state or self._power_on_in_progress

    def _missing_provider_reason(self) -> str | None:
        """Return why the player has no protocol at all, when its provider is not installed."""
        # a player that never had a provider waits for a device that matches it
        if self._protocols or not (provider := self._config.provider):
            return None
        try:
            self.mass.get_provider_manifest(provider.split("--", maxsplit=1)[0])
        except KeyError:
            return SETUP_REASON_PROVIDER_NOT_INSTALLED
        return None

    def _track_availability(self, reachable: bool) -> None:
        """
        Keep when the player was last seen and since when it is unavailable.

        :param reachable: Whether a protocol that renders audio reaches the device.
        """
        if reachable == self._was_available:
            return
        self._was_available = reachable
        self._last_seen = now = time.time()
        self._unavailable_since = None if reachable else now
        # a player that only exists while its device is connected has no config to keep it in
        with suppress(KeyError):
            self.mass.config.set_player_value(self.player_id, CONF_LAST_SEEN, now)

    def _check_feature_with_active_protocol(self, feature: PlayerFeature) -> bool:
        """
        Check if a feature is supported considering the active output protocol.

        If an active output protocol is set (and not native), checks that audio
        protocol's features. Otherwise checks the native protocol's features.

        :param feature: The PlayerFeature to check.
        :return: True if the feature is supported by the active or the native protocol.
        """
        # If active output protocol is set and not native, check that protocol's features
        if (
            self._active_output_protocol
            and self._active_output_protocol != "native"
            and (protocol := self._protocols.get(self._active_output_protocol))
        ):
            return feature in protocol.supported_features
        # Otherwise check the native protocol's features
        return self._own_supports(feature)

    def _stored_control_conf(self, conf_key: str, feature: PlayerFeature) -> ConfigValueType:
        """
        Return the stored control selection, dropping a NATIVE the player can no longer back.

        A NATIVE selection is only meaningful while the player advertises the matching feature.
        Dropping a stale one makes the caller fall back to its auto-select logic instead of
        re-exposing a control the provider can no longer drive - the resolved control is what
        the final feature set is derived from, so an unchecked value would put the feature back.

        :param conf_key: Config key holding the control selection.
        :param feature: Feature a NATIVE selection requires the player to advertise.
        """
        conf = self.mass.config.get_player_value(self.player_id, conf_key)
        if conf == PLAYER_CONTROL_NATIVE and not self._own_supports(feature):
            return None
        return conf

    def _resolve_control(self, feature: PlayerFeature, conf_key: str) -> str:
        """
        Resolve the control a volume or mute command goes to right now.

        Without a selection in the config that is the protocol rendering the audio,
        else the native protocol while it can be reached, else any available protocol
        with the feature.

        :param feature: The feature the control has to support.
        :param conf_key: Config key holding the control selection.
        """
        conf = self._stored_control_conf(conf_key, feature)
        if conf and conf in (PLAYER_CONTROL_NATIVE, PLAYER_CONTROL_FAKE, PLAYER_CONTROL_NONE):
            # the control type is explicitly set in the config, use that
            return str(conf)
        if conf and conf not in (PLAYER_CONTROL_PROTOCOL, "auto"):
            # the control type is explicitly set to a protocol or player control,
            # check if it exists and is (currently) available
            if (protocol := self._commandable_protocol(str(conf))) and protocol.available:
                return protocol.protocol_id
            if _control := self.mass.players.get_player_control(str(conf)):
                return _control.id
        # handle auto-select logic if not explicitly set in config
        active_protocol = self._active_output_protocol
        if (
            active_protocol
            and active_protocol != "native"
            and (protocol := self._protocols.get(active_protocol))
            and protocol.available_for_playback
            and feature in protocol.supported_features
        ):
            # the protocol that renders the audio owns its level
            return protocol.protocol_id
        if self._own_controls(feature):
            return PLAYER_CONTROL_NATIVE
        if protocol := self._get_audio_protocol_for_feature(feature):
            return protocol.protocol_id
        return PLAYER_CONTROL_NONE

    def _get_audio_protocol_for_feature(self, feature: PlayerFeature) -> PlayerProtocol | None:
        """
        Return the idle audio protocol to send a control-plane command to.

        :param feature: The feature the protocol has to support.
        """
        # prefer the output the user picked. the stored value survives a protocol
        # that moved to another player, so it only counts while it still names one of
        # this player's own protocols - otherwise the command would land on another speaker.
        preferred_conf = self.mass.config.get_player_value(
            self.player_id, CONF_PREFERRED_OUTPUT_PROTOCOL
        )
        if preferred_conf and preferred_conf not in ("auto", "native"):
            preferred = self._commandable_protocol(str(preferred_conf))
            if (
                preferred is not None
                and preferred is not self._own_protocol
                and preferred.available_for_playback
                and feature in preferred.supported_features
            ):
                return preferred

        # Otherwise, use the first available audio protocol.
        # Prefer protocols that can process commands without active streaming
        # (cast/dlna can always handle volume, airplay/sendspin only while streaming).
        for protocol in sorted(
            self._audio_protocols,
            key=lambda protocol: _CONTROL_PRIORITY.get(protocol.provider.domain, 10),
        ):
            if protocol.available_for_playback and feature in protocol.supported_features:
                return protocol
        return None

    def _control_for_output(
        self, feature: PlayerFeature, conf_key: str, output_protocol_id: str
    ) -> str:
        """Resolve the control owning the given feature for one specific output."""
        conf = self._stored_control_conf(conf_key, feature)
        if conf and conf in (PLAYER_CONTROL_NATIVE, PLAYER_CONTROL_FAKE, PLAYER_CONTROL_NONE):
            return str(conf)
        if conf and conf not in (PLAYER_CONTROL_PROTOCOL, "auto"):
            # An explicitly configured control is a statement about the device as a whole,
            # so it stays in charge no matter which of its interfaces renders the audio.
            if (protocol := self._commandable_protocol(str(conf))) and protocol.available:
                return protocol.protocol_id
            if _control := self.mass.players.get_player_control(str(conf)):
                return _control.id
        # Availability is not checked - the named output is the one about to render.
        if (
            output_protocol_id != "native"
            and (protocol := self._protocols.get(output_protocol_id))
            and protocol is not self._own_protocol
            and feature in protocol.supported_features
        ):
            return protocol.protocol_id
        # Deliberately no fallback to a sibling audio protocol: the caller named the output
        # that carries the audio, so anything but the device's own control is by definition
        # not in that signal path.
        if self._own_controls(feature):
            return PLAYER_CONTROL_NATIVE
        return PLAYER_CONTROL_NONE

    def _commandable_protocol(self, protocol_id: str) -> PlayerProtocol | None:
        """
        Return an attached protocol that takes commands: the own one or an audio one.

        A control protocol another one ranks above takes none, also when it was picked.

        :param protocol_id: Id of the protocol.
        """
        protocol = self._protocols.get(protocol_id)
        if protocol is self._own_protocol or protocol in self._audio_protocols:
            return protocol
        return None

    def _collect_input_snapshot(self) -> tuple[Any, ...]:
        """
        Collect a snapshot of the player's own state-calculation inputs.

        Only covers inputs owned by the player itself (what its protocols report and
        its transient state); state the player derives from other sources
        (players/queues/config) is covered by mark_state_dirty instead.
        The playback position anchors are deliberately excluded: they are
        tracked separately with jump detection (_position_anchors_moved).
        """
        extra_data = self._extra_data
        probe = self._probe_synced_to
        return (
            tuple(
                (protocol_id, _protocol_inputs(protocol, probe[protocol_id]))
                for protocol_id, protocol in self._protocols.items()
            ),
            extra_data.get(ATTR_FAKE_POWER),
            extra_data.get(ATTR_FAKE_VOLUME),
            extra_data.get(ATTR_FAKE_MUTE),
            self._active_output_protocol,
            self._active_mass_source,
            self._sleep_timer_expires_at,
            self._sleep_timer_mode,
        )

    def _position_anchors_moved(self) -> bool:
        """Return whether a position anchor one of the protocols reports moved significantly."""
        if (prev_anchors := self._input_anchors) is None:
            return True
        for protocol_id, protocol in self._protocols.items():
            if (prev := prev_anchors.get(protocol_id)) is None:
                return True
            prev_player_anchor, prev_media_anchor, prev_playing = prev
            new_player_anchor, new_media_anchor, playing = _position_anchor(protocol)
            if _anchor_moved(
                prev_player_anchor, new_player_anchor, prev_playing, playing
            ) or _anchor_moved(prev_media_anchor, new_media_anchor, prev_playing, playing):
                return True
        return False

    def _diff_state(self, prev_state: PlayerState, reachable: bool) -> dict[str, tuple[Any, Any]]:
        """
        Return the values of the state that changed since the previous calculation.

        The player's own elapsed_time values are not part of the changed values: they
        refresh on every calculation but only current_media - which holds the final
        calculated position - is event-relevant.

        :param prev_state: The state the player had before the current one.
        :param reachable: Whether a protocol that renders audio reaches the device.
        """
        prev_fingerprint = self._state_fingerprint or _state_fingerprint(prev_state)
        new_fingerprint = _state_fingerprint(self._state)
        # not part of the state: whether the device can be reached, which its availability
        # does not tell for a player that is switched off at its power control
        new_fingerprint[ATTR_REACHABLE] = reachable
        self._state_fingerprint = new_fingerprint
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

    def _collect_state_inputs(self, reachable: bool) -> StateInputs:
        """
        Collect everything the state of the player is derived from.

        :param reachable: Whether a protocol that renders audio reaches the device.
        """
        own_protocol = self._own_protocol
        own = (
            _protocol_snapshot(self.player_id, own_protocol, own_protocol.synced_to)
            if own_protocol is not None
            else _absent_protocol_snapshot(self.player_id, self._missing_provider_reason())
        )
        peers: dict[str, PeerSnapshot] = {}
        linked, active_protocol, foreign_app = self._collect_protocols(
            peers, probe_synced_to=not own.synced_to
        )
        # a group protocol names its members by player id, a device protocol by protocol id
        by_player_id = own_protocol is not None and own_protocol.role == ProtocolRole.VIRTUAL
        for member_id in own.group_members:
            self._collect_peer(peers, member_id, as_member=True, by_player_id=by_player_id)
        static_group_members = own_protocol.static_group_members if own_protocol else []
        for member_id in static_group_members:
            self._collect_peer(peers, member_id, by_player_id=by_player_id)
        if own.synced_to:
            self._collect_peer(peers, own.synced_to, by_player_id=by_player_id)
        can_group_with: set[str] = set()
        protocol_can_group_with: set[str] = set()
        grouping_locked = own_protocol.grouping_locked if own_protocol else False
        # a player that is synced or keeps its grouping locked has nothing to group with,
        # which saves looking up all protocols of the providers it can group through
        if not grouping_locked and resolve_synced_to(own, linked, peers) is None:
            if own_protocol is not None:
                can_group_with = self._collect_can_group_with(peers, own_protocol)
            for protocol in self._audio_protocols:
                protocol_can_group_with |= self._collect_can_group_with(peers, protocol)
        groups = self._collect_groups(peers)
        session = self.mass.players.get_audio_source_session(self.player_id)
        source_ids = {self._active_mass_source, own.active_source}
        queues = {
            queue_id: QueueSnapshot(queue=queue)
            for queue_id in {self.player_id, *source_ids}
            if queue_id is not None and (queue := self.mass.player_queues.get(queue_id))
        }
        source_ids.update(group.state.active_source for group in groups)
        if session is not None:
            source_ids.add(session.active_source)
        if foreign_app:
            source_ids.add(foreign_app.active_source)
        inputs = StateInputs(
            player_id=self.player_id,
            provider=self.provider_id,
            player_type=self.reported_type,
            now=time.time(),
            previous_state=self._state,
            enabled=self._config.enabled,
            name=self._display_name,
            device_name=self._followed_name(),
            default_name=self._config.default_name,
            hide_in_ui=self._hide_in_ui,
            expose_to_ha=self._expose_to_ha,
            icon=self._icon,
            legacy_ids=self._legacy_ids,
            last_seen=self._last_seen,
            unavailable_since=self._unavailable_since,
            power_control=self._power_control,
            volume_control=self._volume_control,
            mute_control=self._mute_control,
            powered=self._power_control_state(),
            volume_level=self._volume_control_level(),
            volume_muted=self._mute_control_state(),
            offline_by_power_control=self._offline_by_power_control(reachable),
            own=own,
            device_info=self._device_info,
            private=self._protocol_default("private", False),
            trusts_reported_source=(own_protocol.trusts_reported_source if own_protocol else False),
            grouping_locked=grouping_locked,
            active_sound_mode=own_protocol.active_sound_mode if own_protocol else None,
            sound_mode_list=own_protocol.sound_mode_list if own_protocol else UniqueList(),
            options=own_protocol.options if own_protocol else UniqueList(),
            static_group_members=static_group_members,
            can_group_with=can_group_with,
            protocol_can_group_with=protocol_can_group_with,
            has_setup_flow=self._has_setup_flow,
            linked=linked,
            active_protocol=active_protocol,
            foreign_app=foreign_app,
            output_protocols=self._output_protocols,
            protocols=self._protocol_infos(),
            active_output_protocol=self._active_output_protocol,
            active_mass_source=self._active_mass_source,
            sleep_timer_expires_at=self._sleep_timer_expires_at,
            sleep_timer_mode=self._sleep_timer_mode,
            extra_attributes=self._extra_attributes,
            palette_url=self._current_palette_url,
            palette=self._current_palette,
            peers=peers,
            groups=groups,
            queues=queues,
            mass_sources=frozenset(
                source_id
                for source_id in source_ids
                if source_id is not None and self._is_mass_source(source_id, queues)
            ),
            session=session,
            session_image_url=(
                self.mass.metadata.get_image_url(session.source.image, size=512)
                if session is not None
                and session.source.image
                # what the source is playing may come with an image of its own
                and not (session.stream_metadata and session.stream_metadata.image_url)
                else None
            ),
            standing_sources=self._collect_standing_sources(),
        )
        # artwork is only looked up for the queue the player takes its media from
        if queues and (media_queue := resolve_media_queue(inputs)):
            self._resolve_artwork(media_queue)
        return inputs

    def _collect_protocols(
        self, peers: dict[str, PeerSnapshot], probe_synced_to: bool
    ) -> tuple[tuple[ProtocolSnapshot, ...], ProtocolSnapshot | None, ProtocolSnapshot | None]:
        """
        Collect what the audio protocols report, and the players they refer to.

        Returns the audio protocols, the one of the active output and the one
        that plays an app Music Assistant did not start.

        :param peers: The peers collected so far, by id.
        :param probe_synced_to: Read which protocol each audio protocol is synced
            to. That only counts for a player whose own protocol reports no sync leader,
            and finding it takes a pass over the protocols of the protocol's provider.
        """
        foreign_protocol = self.linked_foreign_protocol()
        foreign_app: ProtocolSnapshot | None = None
        active_output_protocol = self._active_output_protocol
        active_protocol: ProtocolSnapshot | None = None
        linked: list[ProtocolSnapshot] = []
        for protocol in self._audio_protocols:
            snapshot = _protocol_snapshot(
                self.player_id, protocol, protocol.synced_to if probe_synced_to else None
            )
            linked.append(snapshot)
            if protocol is foreign_protocol:
                foreign_app = snapshot
            if protocol.protocol_id == active_output_protocol:
                active_protocol = snapshot
            if snapshot.synced_to:
                self._collect_peer(peers, snapshot.synced_to)
        if active_protocol:
            for member_id in active_protocol.group_members:
                self._collect_peer(peers, member_id, as_member=True)
        return tuple(linked), active_protocol, foreign_app

    def _collect_can_group_with(
        self, peers: dict[str, PeerSnapshot], protocol: PlayerProtocol
    ) -> set[str]:
        """
        Collect the players a protocol of the player can group with.

        Returns the ids the protocol reports that stand for a player: a provider
        instance id among them stands for all (available) protocols of that provider.

        :param peers: The peers collected so far, by id.
        :param protocol: The protocol to collect the grouping candidates of.
        """
        result: set[str] = set()
        by_player_id = protocol.role == ProtocolRole.VIRTUAL
        for member_id in protocol.can_group_with:
            if self._collect_peer(peers, member_id, by_player_id=by_player_id):
                result.add(member_id)
                continue  # already the id of a protocol or player
            # Check if member_id is a provider instance ID
            if provider := self.mass.get_provider(member_id):
                for candidate in self.mass.players.iter_protocols(provider.instance_id):
                    if not candidate.available_for_playback or candidate.player is None:
                        continue  # Only include available protocols
                    self._add_peer(peers, candidate.protocol_id, candidate.player)
                    result.add(candidate.protocol_id)
        return result

    def _collect_groups(self, peers: dict[str, PeerSnapshot]) -> tuple[GroupSnapshot, ...]:
        """
        Collect the group players that can hold the player.

        :param peers: The peers collected so far, by id.
        """
        groups: list[GroupSnapshot] = []
        for group_player in self.mass.players.iter_players(
            return_unavailable=False, return_disabled=False
        ):
            if group_player.reported_type != PlayerType.GROUP:
                continue
            self._add_peer(peers, group_player.player_id, group_player)
            group_protocol = group_player.own_protocol
            groups.append(
                GroupSnapshot(
                    group_player.player_id,
                    group_protocol.powered if group_protocol else None,
                    group_protocol.is_active_session if group_protocol else False,
                    group_player.state,
                )
            )
        return tuple(groups)

    def _collect_standing_sources(self) -> tuple[AudioSource, ...]:
        """Collect the audio sources plugins bound to the player or report as its inputs."""
        sources: list[AudioSource] = []
        for prov in self.mass.get_providers_supporting_feature(ProviderFeature.AUDIO_SOURCE):
            if not isinstance(prov, PluginProvider):
                continue
            sources += prov.get_player_audio_sources(self.player_id) or ()
            # a device's own inputs may be reported for one of its protocols
            for device_id in {self.player_id, *self._protocols}:
                sources += prov.get_player_input_sources(device_id)
        return tuple(sources)

    def _collect_peer(
        self,
        peers: dict[str, PeerSnapshot],
        peer_id: str,
        as_member: bool = False,
        by_player_id: bool = False,
    ) -> Player | None:
        """
        Add the player that the given id names to the peers, if there is one.

        :param peers: The peers collected so far, by id.
        :param peer_id: Id of an attached protocol or of a player, or one of its legacy ids.
        :param as_member: The player is grouped to this one, which makes it count whether
            it is available for playback.
        :param by_player_id: The id names a player first, and the player of a protocol only
            when no player has it.
        """
        players = self.mass.players
        player = (
            players.get_player_or_protocol_player(peer_id)
            if by_player_id
            else players.get_player_by_protocol(peer_id)
        )
        if not player:
            return None
        self._add_peer(peers, peer_id, player)
        if as_member:
            # whether a player is available for playback only counts for a member
            peers[peer_id].available_for_playback = player.available_for_playback
        return player

    @staticmethod
    def _add_peer(peers: dict[str, PeerSnapshot], peer_id: str, player: Player) -> None:
        """
        Add a player to the peers, by its own id and the id that names it.

        :param peers: The peers collected so far, by id.
        :param peer_id: Id that names the player: one of its protocols, or its own.
        :param player: The player to add.
        """
        player_id = player.player_id
        if (peer := peers.get(player_id)) is None:
            own = player.own_protocol
            # positional, which is cheaper for the many players this runs for
            peer = peers[player_id] = PeerSnapshot(
                player_id,
                player.reported_type,
                player._reachable,
                own.grouping_locked if own else False,
                player.state,
            )
        if peer_id != player_id:
            peers[peer_id] = peer

    def _protocol_infos(self) -> list[PlayerProtocolInfo]:
        """Return the protocols the player is made up of, attached or not."""
        key = (
            frozenset(self._protocol_ids),
            tuple(protocol.available_for_playback for protocol in self._protocols.values()),
        )
        if (cached := self._protocol_infos_cache) is not None and cached[0] == key:
            # the same list as long as nothing changed, which keeps comparing it cheap
            return cached[1]
        infos: list[PlayerProtocolInfo] = []
        for protocol_id in sorted(self._protocol_ids):
            if protocol := self._protocols.get(protocol_id):
                infos.append(
                    PlayerProtocolInfo(
                        protocol_id=protocol_id,
                        domain=protocol.provider.domain,
                        provider=protocol.provider_id,
                        role=self._roles[protocol_id],
                        available=protocol.available_for_playback,
                    )
                )
            elif stored := self.mass.config.get_protocol_record(protocol_id):
                provider_id = str(stored.get("provider") or "")
                infos.append(
                    PlayerProtocolInfo(
                        protocol_id=protocol_id,
                        domain=provider_id.split("--", maxsplit=1)[0],
                        provider=provider_id,
                        role=ProtocolRole(stored.get("role") or ProtocolRole.UNKNOWN),
                        available=False,
                    )
                )
        self._protocol_infos_cache = (key, infos)
        return infos

    def _is_mass_source(self, source_id: str, queues: dict[str, QueueSnapshot]) -> bool:
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

    def _resolve_artwork(self, snapshot: QueueSnapshot) -> None:
        """Resolve the artwork of the current item of a queue into its snapshot."""
        if not (current_item := snapshot.queue.current_item):
            return
        # a stream that reports metadata shows its own image, or else that of the item
        stream_metadata = (
            current_item.streamdetails.stream_metadata if current_item.streamdetails else None
        )
        # the image format needs to be 512x512 jpeg for maximum compatibility with players
        if current_item.image and not (stream_metadata and stream_metadata.image_url):
            snapshot.item_image_url = self.mass.metadata.get_image_url(current_item.image, size=512)
        if not stream_metadata and current_item.media_item and current_item.media_item.image:
            snapshot.media_item_image_url = self.mass.metadata.get_image_url(
                current_item.media_item.image, size=512
            )

    def _power_control_state(self) -> bool | None:
        """Return the power state based on the playercontrol which may have been set-up."""
        power_control = self._power_control
        if power_control == PLAYER_CONTROL_FAKE:
            return bool(self._extra_data.get(ATTR_FAKE_POWER, False))
        if power_control == PLAYER_CONTROL_NONE:
            return None
        if protocol := self.get_control_protocol(power_control):
            return protocol.powered
        # handle player control for power if set
        if ext_ctrl := self.mass.players.get_player_control(power_control):
            return ext_ctrl.power_state
        return None

    def _volume_control_level(self) -> int | None:
        """Return the volume level based on the playercontrol which may have been set-up."""
        volume_control = self._volume_control
        level = self._reported_volume_level(volume_control)
        if self._applies_held_volume(PlayerFeature.VOLUME_SET, volume_control):
            return _quieter_level(self._held_volume[PlayerFeature.VOLUME_SET], level)
        return level

    def _reported_volume_level(self, volume_control: str) -> int | None:
        """Return the (logical) volume level a volume control reports."""
        if volume_control == PLAYER_CONTROL_FAKE:
            # Fake volume is already stored as logical (0-100)
            return int(self._extra_data.get(ATTR_FAKE_VOLUME, 0))
        if volume_control == PLAYER_CONTROL_NONE:
            return None
        if protocol := self.get_control_protocol(volume_control):
            # Scale device volume back to logical (0-100)
            if (volume_level := protocol.volume_level) is None:
                return None
            return self.mass.players.scale_volume_from_device(self.player_id, volume_level)
        # handle player control for volume if set
        if player_control := self.mass.players.get_player_control(volume_control):
            return self.mass.players.scale_volume_from_device(
                self.player_id, player_control.volume_level
            )
        return None

    def _mute_control_state(self) -> bool | None:
        """Return the mute state based on any playercontrol which may have been set-up."""
        mute_control = self._mute_control
        muted = self._reported_mute_state(mute_control)
        if self._applies_held_volume(PlayerFeature.VOLUME_MUTE, mute_control):
            return _quieter_mute(self._held_volume[PlayerFeature.VOLUME_MUTE], muted)
        return muted

    def _reported_mute_state(self, mute_control: str) -> bool | None:
        """Return the mute state a mute control reports."""
        if mute_control == PLAYER_CONTROL_FAKE:
            return bool(self._extra_data.get(ATTR_FAKE_MUTE, False))
        if mute_control == PLAYER_CONTROL_NONE:
            return None
        if protocol := self.get_control_protocol(mute_control):
            return protocol.volume_muted
        # handle player control for mute if set
        if player_control := self.mass.players.get_player_control(mute_control):
            return player_control.volume_muted
        return None

    def _external_source_active(self, own: PlayerProtocol) -> bool:
        """Return whether the source the own protocol reports is an external one."""
        # deliberately the reported source rather than the active source of the state:
        # what has to expire is what the device itself keeps
        # reporting, not what the group or protocol it belongs to resolves that to
        source = own.active_source
        if source is None or source in (self.player_id, own.protocol_id):
            return False
        return self.mass.player_queues.get(source) is None

    def _expire_idle_mass_source(self) -> None:
        """Forget the active mass source of a player that has been idle for long enough."""
        if (expires_at := self._active_mass_source_expires_at) is None:
            return
        if (remaining := expires_at - time.monotonic()) > 0:
            # a wake-up that runs a hair early would otherwise leave the source remembered
            self.mass.call_later(
                remaining, self.update_state, task_id=f"set_mass_source_{self.player_id}"
            )
            return
        self._active_mass_source = None
        self._active_mass_source_expires_at = None

    def _expire_stale_external_pause(self) -> None:
        """Report an external source that has been paused for a while as no longer active."""
        own = self._own_protocol
        if own is None or (timeout := own.external_pause_idle_timeout) is None:
            return
        playback_state = own.playback_state
        if (
            playback_state != PlaybackState.PAUSED
            # while an output protocol renders the audio, MA owns playback and the
            # source the device reports for itself is overruled anyway
            or self._active_output_protocol not in (None, "native")
            or not self._external_source_active(own)
        ):
            self._external_pause_since = None
            if playback_state == PlaybackState.PLAYING:
                # the device plays again, so the source we gave up on is live once more
                self._ended_external_source = None
            return
        if own.active_source == self._ended_external_source:
            # the device keeps handing us the source we already gave up on
            self.mark_external_source_ended()
            return
        # any other source is a session of its own and gets the full grace period
        self._ended_external_source = None
        if self._external_pause_since is None:
            self._external_pause_since = time.time()
        elif (time.time() - self._external_pause_since) >= timeout:
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
