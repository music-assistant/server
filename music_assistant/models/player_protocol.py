"""
Base class for the protocol objects of a Player Provider.

A PlayerProtocol is the provider's handle on one device over one protocol: it reports what
the device says about itself and carries out the commands for it. Player Provider
implementations subclass it and look up their own objects through the provider
(`get_protocol`, `protocols`).

The player that users, clients and the queue see is the Player a protocol is attached to
(`self.player`), which is owned by the players controller. A protocol has no published
state of its own: the state of its player is derived from what all its protocols report.
"""

from __future__ import annotations

import builtins
import time
from abc import ABC
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from enum import Enum
from typing import TYPE_CHECKING, Any, TypeVar, cast, final, overload

from music_assistant_models.config_entries import MULTI_VALUE_SPLITTER, ConfigValueType
from music_assistant_models.constants import EXTRA_ATTRIBUTES_TYPES
from music_assistant_models.enums import (
    IdentifierType,
    MediaType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
    ProtocolRole,
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
from music_assistant_models.unique_list import UniqueList
from propcache import under_cached_property as cached_property

from music_assistant.constants import CONF_CACHED_ARP_MAC, CONF_FLOW_MODE, CONF_SAMPLE_RATES

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.config_entries import (
        ConfigActionResult,
        ConfigEntry,
        PlayerConfig,
    )
    from music_assistant_models.enums import RepeatMode
    from music_assistant_models.media_items import AudioFormat

    from .player import Player
    from .player_provider import PlayerProvider
    from .setup_flow import SetupSession

# TypeVar for config value type inference
_ConfigValueT = TypeVar("_ConfigValueT", bound=ConfigValueType)

# config-derived cached properties (propcache keys in PlayerProtocol._cache); these are
# only invalidated by set_config, all other cached properties (including those defined
# by provider implementations) are invalidated on every state update
_CONFIG_CACHED_PROPS = frozenset({"enabled", "flow_mode"})


def _clamp_elapsed_time(elapsed_time: float | None) -> float | None:
    """Return elapsed_time clamped to a non-negative value."""
    return max(0.0, elapsed_time) if elapsed_time is not None else None


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


class PlayerProtocol(ABC):
    """
    Base representation of one device on one protocol, as its provider handles it.

    Player Provider implementations should inherit from this base model.
    """

    # The role this protocol has for its device: the vendor's own control plane (native),
    # control without an audio path, an audio path next to another protocol (audio) or
    # no device at all (virtual). Read when the protocol attaches.
    _attr_role: ProtocolRole = ProtocolRole.NATIVE

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
    _attr_needs_poll: bool = False
    _attr_poll_interval: int = 30
    _attr_hidden_by_default: bool = False
    _attr_private: bool = False
    _attr_expose_to_ha_by_default: bool = True
    _attr_enabled_by_default: bool = True
    _attr_session_only: bool = False
    _attr_needs_setup: bool = False
    _attr_setup_reason: str | None = None
    _attr_supported_sample_rates: list[tuple[int, int]] | None = None
    _attr_underlying_protocol_id: str | None = None
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

    def __init__(self, provider: PlayerProvider, protocol_id: str) -> None:
        """Initialize the protocol object."""
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
        self._protocol_id = protocol_id
        self._provider = provider
        self._player: Player | None = None
        self._config = self.mass.config.get_base_protocol_config(protocol_id, self.provider_id)
        self._extra_attributes: dict[str, Any] = {}
        self._reported_mac: str | None = None
        # called once when the protocol is detached from its player
        self._on_detach_callbacks: list[Callable[[], None]] = []

    @property
    def available(self) -> bool:
        """
        Return if the player is available.

        A protocol that rides on another one is available only while that one is attached
        and can play: it reaches its device through it.
        """
        if (underlying_protocol_id := self.underlying_protocol_id) is not None:
            underlying = self.mass.players.get_protocol(underlying_protocol_id)
            if (
                underlying is None
                or underlying.player is None
                or not underlying.available_for_playback
            ):
                return False
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
    def needs_gapless_burst(self) -> bool:
        """
        Return if the player must hold a whole opening chunk of a track before it plays gapless.

        Such a player gets a large head start on every track it streams on its own. A player
        that struggles with the realtime pacing otherwise plays in flow mode.
        """
        return False

    @property
    def device_info(self) -> DeviceInfo:
        """
        Return the device info of the player.

        Software players that share a host report an id of their own instance among its
        identifiers as UUID or SERIAL_NUMBER, and the zones of one chassis their zone as ZONE
        (MAIN_ZONE for the zone the protocols of the chassis without a zone play on).
        """
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
    def session_only(self) -> bool:
        """
        Return if the player only exists while its device is connected.

        True for something that is no device to keep track of once it is gone, such as
        a browser tab, an app or a virtual player.
        """
        return self._attr_session_only

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
        Return if the protocol can currently be used to play (or control) audio.

        A device that is reachable but still needs setup - an unpaired AirPlay
        receiver, for example - can not accept a stream, so it must never be
        picked as an output protocol or command target. Neither can a protocol
        the user switched off.
        """
        return self.enabled and self.available and not self.needs_setup

    @property
    @final
    def implements_setup_flow(self) -> bool:
        """Return if this player implements its own interactive setup flow."""
        return type(self).run_setup_flow is not PlayerProtocol.run_setup_flow

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
        Return if an interactive setup flow can be started for this protocol.

        Unlike ``needs_setup`` this stays True once setup completed, so the UI can
        offer to re-run the flow on demand (e.g. to redo a pairing step that was skipped).
        """
        return self.implements_setup_flow and self.setup_flow_available

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
        the id of this protocol if it is currently playing a MA queue.
        """
        return self._attr_active_source

    @property
    def external_pause_idle_timeout(self) -> int | None:
        """
        Return the seconds an external source may sit paused before its session counts as ended.

        None for a device that reports a source it no longer plays as stopped by itself.
        """
        return self._attr_external_pause_idle_timeout

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
        this should return the protocol id's of this provider synced to this player,
        and this should include the protocol's own id (as first item in the list).

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

        This should return set of protocol id's this player can group/sync with
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
        """Return the id of the protocol this player is synced to (sync leader)."""
        # default implementation, feel free to override if your
        # provider has a more efficient way to determine this
        if self.type == PlayerType.GROUP:
            return None
        for protocol in self.mass.players.iter_protocols(self.provider_id):
            if protocol.type == PlayerType.GROUP or not protocol.available:
                continue
            if self.protocol_id in protocol.group_members and protocol is not self:
                return protocol.protocol_id
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

    @property
    def default_output_protocol_domain(self) -> str | None:
        """
        Return the protocol domain this player prefers as its default output, if any.

        A player that has no native audio path of its own (e.g. a control/grouping shell
        for a device whose playback runs over an audio protocol) can point the automatic
        output selection at a specific protocol domain (such as ``dlna``). The base player
        has no preference; an explicit user selection always overrides this default.
        """
        return None

    @property
    def grouping_locked(self) -> bool:
        """
        Return whether ALL grouping must be suppressed in this player's exposed state.

        This is the broad, final lock: while it holds, ``SET_MEMBERS`` is withdrawn and no
        group targets are offered in the final state, even ones an audio protocol of the
        player would otherwise supply. It must therefore be reserved for a genuinely read-only
        topology — for example a device in an externally-created cross-backend group that
        Music Assistant keeps read-only — and NOT used merely because a device's own native
        grouping capability is unavailable. A provider that only wants to disable its native
        grouping path (e.g. while its control API is unreachable) should instead withhold the
        native ``SET_MEMBERS`` from ``supported_features`` and return no native
        ``can_group_with`` candidates, leaving core free to still group via an audio protocol.
        """
        return False

    @property
    def prefer_native_grouping(self) -> bool:
        """
        Return whether this player should group natively before any audio protocol.

        A device that runs its own multiroom (e.g. a LinkPlay speaker exposed as a control
        shell) should keep grouping on its native path rather than route it through a linked
        AirPlay/DLNA protocol that merely happens to be its preferred playback output. When
        this is set, grouping selection tries native grouping first; the usual compatibility
        checks still decide whether native grouping is actually possible, and every other
        player keeps the default protocol-first ordering. Playback output selection is
        unaffected.
        """
        return False

    def is_native_group_compatible(self, other: PlayerProtocol) -> bool:
        """
        Return whether this player can natively group with the given player.

        Native grouping normally works between any two players of the same provider
        instance. A provider that hosts several incompatible device backends behind a
        single instance can narrow this so the grouping layer never routes a cross-backend
        pair onto a native group it cannot form.

        :param other: The player considered for a native group with this one.
        """
        return self.provider.instance_id == other.provider.instance_id

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

        Called by the Player Controller on the native protocol of a player after
        play_media is executed on one of its audio protocols. Allows the native
        implementation to perform special logic when protocol playback starts.

        Optional - providers can override to implement protocol-specific logic.

        :param output_protocol: The OutputProtocol object containing protocol details.
        """
        return  # Optional callback - no-op by default

    def get_shared_stream(
        self, media: PlayerMedia, pcm_format: AudioFormat
    ) -> AsyncGenerator[bytes] | None:
        """
        Return the audio of the one stream this group renders for all of its members.

        Override in the protocol of a group that hands its members media of a stream it
        produces itself. A member that takes raw audio asks for it here, under the media
        it was handed (whose ``source_id`` is the id of the group player).

        :param media: The media a member was asked to play.
        :param pcm_format: The PCM format the member wants the audio in.
        :return: The audio, or None when the media is not that of such a shared stream.
        """
        return None

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

        :param player_ids_to_add: List of protocol id's to add to the group.
        :param player_ids_to_remove: List of protocol id's to remove from the group.
        """
        raise NotImplementedError(
            "set_members needs to be implemented when PlayerFeature.SET_MEMBERS is set"
        )

    async def poll(self) -> None:
        """
        Poll player for state updates.

        This is called by the players controller
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
        setup_data = self._config.setup_data
        if key in setup_data:
            value = setup_data[key]
            return self.mass.config.decrypt_string(value) if isinstance(value, str) else value
        return default

    async def on_config_updated(self) -> None:
        """
        Handle logic when the protocol is attached or its config is updated.

        Override this method in your player implementation if you need
        to perform any additional setup logic after the protocol is attached and
        the self.config was loaded, and whenever the config changes.
        """
        return

    async def on_attach(self, player: Player) -> None:
        """
        Handle logic when the protocol is attached to its player.

        Called after the config was loaded and on_config_updated ran, also when the
        same object attaches again after it was detached.

        :param player: The player the protocol is attached to, also available as self.player.
        """
        return

    async def on_detach(self) -> None:
        """
        Handle logic when the protocol is detached from its player.

        self.player is already None when this runs: release what the protocol holds itself.
        """
        callbacks, self._on_detach_callbacks = self._on_detach_callbacks, []
        for callback in callbacks:
            try:
                callback()
            except Exception as err:
                self.logger.error(
                    "Error calling on_detach callback for protocol %s: %s",
                    self.protocol_id,
                    err,
                )

    async def on_remove(self) -> None:
        """
        Handle logic when the protocol is removed for good.

        Called after the protocol was detached and its config was deleted, also for an
        object the provider holds that is not attached. Override this method in your
        player implementation if the provider keeps something of its own about the
        device, such as an entry in its list of manually added devices. The players
        controller holds an object that is not attached only weakly, so a provider with
        such a list keeps a reference to the object of every device on it.
        """
        return

    def on_external_source_ended(self) -> None:
        """
        Stop reporting the external source the device has loaded.

        Called when the session of that source is gone while the device keeps reporting
        it, see Player.mark_external_source_ended. Override this when the reported
        values do not come from the attributes cleared here.
        """
        self._attr_playback_state = PlaybackState.IDLE
        self._attr_active_source = None
        self._attr_current_media = None

    async def group_with(self, target_protocol_id: str) -> None:
        """
        Handle GROUP_WITH command on the player.

        Group this player to the given syncleader/target.
        Will only be called if the PlayerFeature.SET_MEMBERS is supported.

        :param target_protocol_id: protocol id of the target player / sync leader.
        """
        # convenience helper method
        # no need to implement unless your player/provider has an optimized way to execute this
        # default implementation will simply call set_members
        # to add the target player to the group.
        target = self.provider.get_protocol(target_protocol_id, raise_unavailable=True)
        assert target  # for type checking
        await target.set_members(player_ids_to_add=[self.protocol_id])

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
            if leader := self.provider.get_protocol(self.synced_to):
                # if this player is synced to another player, remove self from that group
                await leader.set_members(player_ids_to_remove=[self.protocol_id])
        elif self.group_members:
            await self.set_members(player_ids_to_remove=self.group_members)

    def on_group_member_updated(
        self, member_player: Player, changed_values: dict[str, tuple[Any, Any]]
    ) -> None:
        """Handle callback when a group member of the group player is updated."""
        # optional callback
        # default implementation will simply trigger an update for the state of the player
        self._trigger_player_update()

    def on_group_updated(
        self, group_player: Player, changed_values: dict[str, tuple[Any, Any]]
    ) -> None:
        """Handle callback when a group player is updated this player is a member of."""
        # optional callback
        # default implementation will simply trigger an update for the state of the player
        self._trigger_player_update()

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
        """Handle callback when the sync leader of the player of this protocol is updated."""
        # optional callback
        # default implementation will simply trigger an update for the state of the player
        self._trigger_player_update()

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
    def protocol_id(self) -> str:
        """Return the id of the protocol object."""
        return self._protocol_id

    @property
    @final
    def provider(self) -> PlayerProvider:
        """Return the provider of the protocol."""
        return self._provider

    @property
    @final
    def provider_id(self) -> str:
        """Return the provider (instance) id of the protocol."""
        return self._provider.instance_id

    @property
    @final
    def translation_owner(self) -> str:
        """Return the translation owner namespace ("provider.<domain>") of the provider."""
        return self._provider.translation_owner

    @property
    @final
    def player(self) -> Player | None:
        """Return the player this protocol is attached to, None while it is not attached."""
        return self._player

    @property
    @final
    def role(self) -> ProtocolRole:
        """Return the role this protocol reports for its device."""
        return self._attr_role

    @property
    @final
    def config(self) -> PlayerConfig:
        """
        Return the config of the protocol.

        It has no name: the name the user gave the device is self.player.config.name.
        """
        return self._config

    @cached_property
    @final
    def enabled(self) -> bool:
        """Return if the user wants this protocol used (the output toggle in its config)."""
        return self._config.enabled

    @property
    @final
    def display_name(self) -> str:
        """Return the name to show for the device: that of its player, else its own."""
        if self._player is not None:
            return self._player.name
        return self.name or self._config.default_name or self.protocol_id

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
    def identifiers(self) -> frozenset[tuple[IdentifierType, str]]:
        """
        Return every identifier this protocol knows of its device as (type, value) pairs.

        A device with several MAC addresses (the one its provider reported and the one
        ARP resolved) holds a pair for each.
        """
        identifiers = set(self.device_info.identifiers.items())
        cached_arp_mac = self.mass.config.get_protocol_value(self.protocol_id, CONF_CACHED_ARP_MAC)
        for mac_address in (self._reported_mac, cached_arp_mac):
            if mac_address and isinstance(mac_address, str):
                identifiers.add((IdentifierType.MAC_ADDRESS, mac_address))
        return frozenset(identifiers)

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
                        self.protocol_id,
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
    def underlying_protocol_id(self) -> str | None:
        """
        Return the id of the protocol this (derived) protocol runs on top of, if any.

        Set by bridge implementations (e.g. a Sendspin bridge riding on an AirPlay
        protocol), so the derived protocol attaches to the player of the protocol it
        rides on instead of being matched by its device identifiers.
        """
        return self._attr_underlying_protocol_id

    @final
    def update_state(self, force_update: bool = False, signal_event: bool = True) -> None:
        """
        Publish what the protocol reports through the state of its player.

        This method should be called whenever a reported value changed. It does nothing
        while the protocol is not attached to a player.

        :param force_update: If True, always recalculate the state, even when no
        (known) input changed. An update event still only fires when the
        recalculated state actually differs.
        :param signal_event: If True, signal the state update event to the PlayerController.
        """
        if (player := self._player) is None:
            # a protocol that waits for its player is matched again on what it reports now
            self.mass.players.on_unattached_protocol_update(self)
            return
        if player.attached_protocols.get(self._protocol_id) is not self:
            # a debounced update of an object that was detached or replaced meanwhile
            return
        player.update_state(force_update=force_update, signal_event=signal_event)

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
    def set_config(self, config: PlayerConfig) -> None:
        """
        Set/update the config of the protocol.

        May only be called by the PlayerController.
        """
        self._config = config
        # config feeds several (cached) values, so invalidate all cached
        # properties (including the config-derived ones)
        self._cache.clear()
        if self._player is not None:
            self._player.mark_state_dirty()

    @final
    def invalidate_cache(self) -> None:
        """Drop the cached properties that do not come from the config."""
        for key in list(self._cache):
            if key not in _CONFIG_CACHED_PROPS:
                del self._cache[key]

    @final
    def set_player(self, player: Player | None) -> None:
        """
        Set the player this protocol is attached to.

        May only be called by the PlayerController.

        :param player: The player, or None when the protocol is detached.
        """
        self._player = player

    @final
    def set_reported_mac(self, mac_address: str | None) -> None:
        """
        Set the MAC address the provider reported where ARP resolved another one.

        May only be called by the PlayerController.

        :param mac_address: The reported MAC address, or None when it is the resolved one.
        """
        self._reported_mac = mac_address

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

    def __hash__(self) -> int:
        """Return a hash of the protocol object."""
        return hash(self.protocol_id)

    def __str__(self) -> str:
        """Return a string representation of the protocol object."""
        return f"Protocol {self.name} ({self.protocol_id})"

    def __repr__(self) -> str:
        """Return a string representation of the protocol object."""
        return f"<PlayerProtocol name={self.name} id={self.protocol_id} available={self.available}>"

    def __eq__(self, other: object) -> bool:
        """Check equality of two protocol objects."""
        if not isinstance(other, PlayerProtocol):
            return False
        return self.protocol_id == other.protocol_id

    def __ne__(self, other: object) -> bool:
        """Check inequality of two protocol objects."""
        return not self.__eq__(other)

    def _update_setup_data(self, key: str, value: ConfigValueType, immediate: bool = True) -> None:
        """
        Update a single setup_data value for this protocol (e.g. a rotated pairing credential).

        :param key: The setup data key to update.
        :param value: The new value; strings are encrypted at rest.
        :param immediate: Persist to disk right away (the default) instead of on the
            debounced save timer, so a critical value survives a crash.
        """
        stored_value = self.mass.config.encrypt_string(value) if isinstance(value, str) else value
        self.mass.config.set_protocol_setup_value(
            self.protocol_id, key, stored_value, immediate=immediate
        )

    @final
    def _trigger_player_update(self) -> None:
        """Trigger a (debounced) state update of the player this protocol is attached to."""
        if self._player is not None:
            self.mass.players.trigger_player_update(self._player.player_id)


__all__ = [
    # explicitly re-export the models we imported from the models package,
    # for convenience reasons
    "EXTRA_ATTRIBUTES_TYPES",
    "AnnouncementFeature",
    "DeviceInfo",
    "PlayerMedia",
    "PlayerProtocol",
    "PlayerSource",
]
