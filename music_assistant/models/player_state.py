"""
Derivation of the state a player publishes.

derive_player_state calculates the PlayerState of a player from a snapshot of
everything that state depends on. It reads nothing but that snapshot and performs
nothing itself: what has to happen besides publishing the new state is returned
as effects, for the caller to carry out.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from music_assistant_models.constants import PLAYER_CONTROL_NONE
from music_assistant_models.enums import (
    MediaType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
    ProtocolRole,
)
from music_assistant_models.player import Player as PlayerState
from music_assistant_models.player import PlayerMedia, PlayerSource
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import (
    ACTIVE_PROTOCOL_FEATURES,
    EXTERNAL_SOURCES,
    FOREIGN_PROTOCOL_FEATURES,
    PROTOCOL_FEATURES,
)
from music_assistant.helpers.util import html_to_markdown

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping

    from music_assistant_models.audio_processing import ActiveSourceAudioDetails
    from music_assistant_models.constants import EXTRA_ATTRIBUTES_TYPES
    from music_assistant_models.enums import SleepTimerMode
    from music_assistant_models.media_items import AudioSource, MediaItemPalette
    from music_assistant_models.player import (
        DeviceInfo,
        OutputProtocol,
        PlayerOption,
        PlayerProtocolInfo,
        PlayerSoundMode,
    )
    from music_assistant_models.player_queue import PlayerQueue

    from music_assistant.controllers.players.audio_sources import AudioSourceSession

# corrected-position jumps larger than this (in seconds) are treated as a discrete
# position change (seek/buffer correction) instead of regular playback progression
POSITION_JUMP_THRESHOLD = 1.0


@dataclass(slots=True, kw_only=True)
class ProtocolSnapshot:
    """
    What one protocol reports for the player it is attached to.

    Taken from the player's own protocol and from each of its audio protocols, through
    their properties. Other protocols are named by their protocol id.

    :param protocol_id: Id of the protocol. The id of the player where it stands in for
        an own protocol that is not attached.
    :param role: Role the protocol is attached in, None where none is attached.
    :param available: Whether the device can be reached.
    :param available_for_playback: Whether it is enabled, reachable and set up.
    :param needs_setup: Whether it has to be set up before it can be used.
    :param setup_reason: Why it needs setup, if it gave a reason.
    :param supported_features: Features of the provider object itself.
    :param playback_state: Playback state of the device.
    :param elapsed_time: Playback position of the device, in seconds.
    :param elapsed_time_last_updated: Timestamp the playback position was taken at.
    :param active_source: Source the device has selected, the id of the player for its
        own queue.
    :param source_list: Sources of the device.
    :param current_media: Media the device has loaded.
    :param group_members: Ids of the protocols grouped to the device, itself included.
    :param synced_to: Id of the protocol the device is synced to. Only given where it
        counts: for the player's own protocol, and for the audio protocols of a player
        whose own protocol reports no sync leader.
    """

    protocol_id: str
    role: ProtocolRole | None
    available: bool
    available_for_playback: bool
    needs_setup: bool
    setup_reason: str | None
    supported_features: set[PlayerFeature]
    playback_state: PlaybackState
    elapsed_time: float | None
    elapsed_time_last_updated: float | None
    active_source: str | None
    source_list: list[PlayerSource]
    current_media: PlayerMedia | None
    group_members: list[str]
    synced_to: str | None


@dataclass(slots=True)
class PeerSnapshot:
    """
    Another player, as far as the state of a player depends on it.

    Found under its own id and under every id that names it: a legacy id, or the id
    of one of its protocols.

    :param player_id: Id of the player.
    :param type: Type of the player.
    :param available: Whether its device can be reached.
    :param grouping_locked: Whether its provider keeps its grouping read-only.
    :param state: The state it publishes.
    :param available_for_playback: Whether it is enabled, reachable and set up. Only
        given where it counts: for the players grouped to the player.
    """

    player_id: str
    type: PlayerType
    available: bool
    grouping_locked: bool
    state: PlayerState
    available_for_playback: bool | None = None


@dataclass(slots=True)
class GroupSnapshot:
    """
    A group player that can hold a player.

    :param player_id: Id of the group player.
    :param powered: Power state its own protocol reports.
    :param is_active_session: Whether it is holding its members.
    :param state: The state it publishes.
    """

    player_id: str
    powered: bool | None
    is_active_session: bool
    state: PlayerState


@dataclass(slots=True, kw_only=True)
class QueueSnapshot:
    """
    A queue, with the artwork of its current item resolved to urls.

    The urls are only given where they count: for the queue the player takes its
    current media from (see resolve_media_queue).

    :param queue: The queue.
    :param item_image_url: Url of the image of the current queue item.
    :param media_item_image_url: Url of the image of the media item it holds.
    """

    queue: PlayerQueue
    item_image_url: str | None = None
    media_item_image_url: str | None = None


@dataclass(slots=True, kw_only=True)
class StateInputs:
    """
    Everything the state of a player is derived from.

    Other players, queues and sources are included as far as the values below refer
    to them by id. Volume levels are logical (0..100), the player's volume limits
    are applied where the levels are read.

    :param player_id: Id of the player.
    :param provider: Instance id of the provider of the player.
    :param player_type: Type of the player.
    :param now: Current time, as a timestamp.
    :param previous_state: The state published before this one.
    :param enabled: Whether the player is enabled in its config.
    :param name: Display name of the player.
    :param device_name: Name the protocols of the player report for its device that the
        stored name follows, None to keep the stored name.
    :param default_name: Device name stored in the config.
    :param hide_in_ui: Whether the player is hidden in the UI.
    :param expose_to_ha: Whether the player is exposed to Home Assistant.
    :param icon: Icon of the player.
    :param legacy_ids: Ids the player was known under before, which it keeps answering to.
    :param last_seen: Timestamp the availability of the player last changed at.
    :param unavailable_since: Timestamp since when none of its protocols is available.
    :param power_control: The control the power of the player is resolved to.
    :param volume_control: The control the volume of the player is resolved to.
    :param mute_control: The control the mute of the player is resolved to.
    :param powered: Power state the power control reports.
    :param volume_level: Volume level the volume control reports.
    :param volume_muted: Mute state the mute control reports.
    :param offline_by_power_control: Whether the device is offline because it is
        switched off at its power control.
    :param own: What the player's own protocol reports: its native protocol, one that
        controls it without an audio path, or the virtual one of a group.
    :param device_info: Device info of the player.
    :param private: Whether the player may not be offered to other clients.
    :param trusts_reported_source: Whether a source on the player's own source list
        counts as a takeover of the Music Assistant queue.
    :param grouping_locked: Whether the provider keeps the grouping read-only.
    :param active_sound_mode: Sound mode the device has selected.
    :param sound_mode_list: Sound modes of the device.
    :param options: Options of the device.
    :param static_group_members: Ids naming the group members that can not be removed.
    :param can_group_with: Ids naming the players the own protocol can group with.
    :param protocol_can_group_with: Ids naming the players the audio protocols can
        group with.
    :param has_setup_flow: Whether a setup flow can be started for the player.
    :param linked: What the audio protocols report, most preferred first.
    :param active_protocol: What the audio protocol of the active output reports.
    :param foreign_app: The audio protocol that plays an app Music Assistant did
        not start.
    :param output_protocols: Output options of the player.
    :param protocols: The protocols the player is made up of.
    :param active_output_protocol: Id of the active output: an audio protocol,
        "native" or None.
    :param active_mass_source: Id of the last active Music Assistant source.
    :param sleep_timer_expires_at: Timestamp the sleep timer stops playback at.
    :param sleep_timer_mode: Point at which the sleep timer stops playback.
    :param extra_attributes: Extra attributes of the player.
    :param palette_url: Url of the image the resolved palette belongs to.
    :param palette: Resolved color palette of that image.
    :param peers: The other players referred to, by id.
    :param groups: The group players that can hold the player.
    :param queues: The queues the player can be playing from, by id.
    :param mass_sources: The sources referred to that Music Assistant plays itself:
        a queue or a live audio source.
    :param session: Live audio source playing on the player.
    :param session_image_url: Url of the image of that audio source. Only given where
        it counts: for a source that reports no image of what it is playing.
    :param standing_sources: Audio sources that are selectable on the player without
        a session being active.
    """

    player_id: str
    provider: str
    player_type: PlayerType
    now: float
    previous_state: PlayerState
    enabled: bool
    name: str
    device_name: str | None
    default_name: str | None
    hide_in_ui: bool
    expose_to_ha: bool
    icon: str
    legacy_ids: list[str]
    last_seen: float | None
    unavailable_since: float | None
    power_control: str
    volume_control: str
    mute_control: str
    powered: bool | None
    volume_level: int | None
    volume_muted: bool | None
    offline_by_power_control: bool
    own: ProtocolSnapshot
    device_info: DeviceInfo
    private: bool
    trusts_reported_source: bool
    grouping_locked: bool
    active_sound_mode: str | None
    sound_mode_list: UniqueList[PlayerSoundMode]
    options: UniqueList[PlayerOption]
    static_group_members: list[str]
    can_group_with: set[str]
    protocol_can_group_with: set[str]
    has_setup_flow: bool
    linked: tuple[ProtocolSnapshot, ...]
    active_protocol: ProtocolSnapshot | None
    foreign_app: ProtocolSnapshot | None
    output_protocols: list[OutputProtocol]
    protocols: list[PlayerProtocolInfo]
    active_output_protocol: str | None
    active_mass_source: str | None
    sleep_timer_expires_at: float | None
    sleep_timer_mode: SleepTimerMode | None
    extra_attributes: dict[str, EXTRA_ATTRIBUTES_TYPES]
    palette_url: str | None
    palette: MediaItemPalette | None
    peers: Mapping[str, PeerSnapshot]
    groups: tuple[GroupSnapshot, ...]
    queues: Mapping[str, QueueSnapshot]
    mass_sources: frozenset[str]
    session: AudioSourceSession | None
    session_image_url: str | None
    standing_sources: tuple[AudioSource, ...]


@dataclass(slots=True, kw_only=True)
class StateEffects:
    """
    What has to happen besides publishing a derived state.

    :param position_jumped: The corrected playback position jumped (seek or buffer
        correction), so the queue timing has to re-base on it.
    :param media_position_jumped: The corrected position of the current media jumped.
    :param default_name: Device name to store as the default name of the player.
    :param left_idle: Playback started on an idle player.
    :param went_idle: Playback ended, the player is idle now.
    :param foreign_protocol_id: Id of the audio protocol whose app the state shows,
        which is where its transport commands have to go.
    :param clear_active_output: The active output is no longer the playback path: the
        device follows a native group leader now.
    """

    position_jumped: bool
    media_position_jumped: bool
    default_name: str | None
    left_idle: bool
    went_idle: bool
    foreign_protocol_id: str | None
    clear_active_output: bool


def derive_player_state(inputs: StateInputs) -> tuple[PlayerState, StateEffects]:
    """
    Calculate the state a player publishes and what has to happen besides publishing it.

    :param inputs: Everything the state is derived from.
    """
    own = inputs.own
    prev_state = inputs.previous_state
    # a device that follows a native group leader gets its audio from that leader, so
    # an output that was active before is not its playback path anymore
    follows_native_leader = (
        own.role == ProtocolRole.CONTROL
        and own.synced_to is not None
        and inputs.active_output_protocol not in (None, "native")
    )
    if follows_native_leader:
        inputs = replace(inputs, active_protocol=None, active_output_protocol=None)
    synced_to = resolve_synced_to(own, inputs.linked, inputs.peers)
    active_group = _active_group(inputs)
    setup_protocol = _protocol_needing_setup(inputs)
    foreign_app = _shown_foreign_app(inputs, synced_to, active_group)
    playback_state, elapsed_time, elapsed_time_last_updated = _playback_position(
        inputs, synced_to, active_group, foreign_app
    )
    active_source = _active_source(inputs, synced_to, active_group, foreign_app)
    source_list = _source_list(inputs, foreign_app)
    state = PlayerState(
        player_id=inputs.player_id,
        provider=inputs.provider,
        type=inputs.player_type,
        # the client view: it includes a player switched off at its power control, so
        # internal code that needs a reachable device checks available_for_playback
        available=inputs.enabled
        and (_reachable(inputs) or inputs.offline_by_power_control)
        and setup_protocol is None,
        device_info=inputs.device_info,
        supported_features=_supported_features(inputs, foreign_app, source_list),
        playback_state=playback_state,
        elapsed_time=elapsed_time,
        elapsed_time_last_updated=elapsed_time_last_updated,
        powered=inputs.powered,
        volume_level=inputs.volume_level,
        volume_muted=inputs.volume_muted,
        group_members=UniqueList(_group_members(inputs, synced_to)),
        static_group_members=UniqueList(_static_group_members(inputs)),
        can_group_with=_can_group_with(inputs, synced_to, active_source),
        synced_to=synced_to,
        active_source=active_source,
        active_source_audio=_active_source_audio(inputs, synced_to, active_group),
        source_list=source_list,
        active_group=active_group,
        current_media=_current_media(
            inputs,
            synced_to,
            active_group,
            foreign_app,
            active_source,
            elapsed_time,
            elapsed_time_last_updated,
        ),
        active_sound_mode=inputs.active_sound_mode,
        sound_mode_list=inputs.sound_mode_list,
        options=inputs.options,
        name=inputs.name,
        enabled=inputs.enabled,
        hide_in_ui=inputs.hide_in_ui,
        private=inputs.private,
        expose_to_ha=inputs.expose_to_ha,
        icon=inputs.icon,
        extra_attributes=inputs.extra_attributes,
        power_control=inputs.power_control,
        volume_control=inputs.volume_control,
        mute_control=inputs.mute_control,
        output_protocols=inputs.output_protocols,
        active_output_protocol=inputs.active_output_protocol,
        needs_setup=setup_protocol is not None,
        setup_reason=(
            setup_protocol.setup_reason
            if setup_protocol
            # without an own protocol, the reason is why the player has no protocol at all
            else (inputs.own.setup_reason if inputs.own.role is None else None)
        ),
        has_setup_flow=inputs.has_setup_flow,
        sleep_timer_expires_at=inputs.sleep_timer_expires_at,
        sleep_timer_mode=inputs.sleep_timer_mode,
        legacy_ids=inputs.legacy_ids,
        last_seen=inputs.last_seen,
        unavailable_since=inputs.unavailable_since,
        protocols=_protocols(inputs, foreign_app),
    )
    volume_members = _volume_members(inputs, state)
    state.group_volume = _group_volume(state, volume_members)
    state.group_volume_muted = _group_volume_muted(state, volume_members)

    prev_playing = prev_state.playback_state == PlaybackState.PLAYING
    new_playing = playback_state == PlaybackState.PLAYING
    # detect a discrete jump of the corrected position (seek/buffer correction);
    # the fresh anchor is always adopted into the state
    _, _, position_jumped = reconcile_position_anchor(
        prev_state.elapsed_time,
        prev_state.elapsed_time_last_updated,
        elapsed_time,
        elapsed_time_last_updated,
        prev_playing,
        new_playing,
        inputs.now,
        force_adopt=True,
    )
    # Players that mirror another player's media (grouped/synced members) share
    # the owner's PlayerMedia object, which the owner already reconciled - only
    # report the jump, never mutate the shared object.
    mirrors_owner = bool(active_group or synced_to or foreign_app)
    media_position_jumped = _reconcile_media_anchor(
        prev_state.current_media,
        state.current_media,
        mirrors_owner,
        prev_playing,
        new_playing,
        inputs.now,
    )

    was_idle = prev_state.playback_state == PlaybackState.IDLE
    is_idle = playback_state == PlaybackState.IDLE
    device_name = inputs.device_name
    return state, StateEffects(
        position_jumped=position_jumped,
        media_position_jumped=media_position_jumped,
        default_name=device_name if device_name and inputs.default_name != device_name else None,
        left_idle=was_idle and not is_idle,
        went_idle=is_idle and not was_idle,
        foreign_protocol_id=foreign_app.protocol_id if foreign_app else None,
        clear_active_output=follows_native_leader,
    )


def resolve_synced_to(
    own: ProtocolSnapshot,
    linked: Iterable[ProtocolSnapshot],
    peers: Mapping[str, PeerSnapshot],
) -> str | None:
    """
    Return the id of the player a player is synced to.

    This checks both the sync state of the player's own protocol and that of its
    audio protocols, translating the id of the leading protocol to its player.

    :param own: What the player's own protocol reports.
    :param linked: What the audio protocols report, most preferred first.
    :param peers: The other players referred to, by id.
    """
    if own_synced_to := own.synced_to:
        if sync_parent := peers.get(own_synced_to):
            return sync_parent.player_id
        return own_synced_to
    # check if any of the audio protocols are synced,
    # and if so, return the player they are synced to
    for protocol in linked:
        if protocol.synced_to and (sync_parent := peers.get(protocol.synced_to)):
            return sync_parent.player_id
    return None


def resolve_media_queue(inputs: StateInputs) -> QueueSnapshot | None:
    """
    Return the queue a player takes its current media from, if any.

    :param inputs: Everything the state is derived from.
    """
    synced_to = resolve_synced_to(inputs.own, inputs.linked, inputs.peers)
    active_group = _active_group(inputs)
    foreign_app = _shown_foreign_app(inputs, synced_to, active_group)
    if synced_to or active_group or foreign_app or inputs.session is not None:
        # the media is that of another player, an app or a live external source
        return None
    return _active_queue(inputs, _active_source(inputs, synced_to, active_group, foreign_app))


def reconcile_position_anchor(
    prev_position: float | None,
    prev_timestamp: float | None,
    new_position: float | None,
    new_timestamp: float | None,
    prev_playing: bool,
    new_playing: bool,
    now: float,
    force_adopt: bool = False,
) -> tuple[float | None, float | None, bool]:
    """
    Reconcile a playback position anchor (position + timestamp pair) with its predecessor.

    The anchor is a value that only changes on discrete events: regular playback
    progression extrapolates to (nearly) the same corrected position as the previous
    anchor and therefore keeps the previous anchor, so steady playback yields no
    state change at all. The anchor is only adopted when the corrected position
    jumped more than POSITION_JUMP_THRESHOLD (seek/buffer correction), when the
    playing state changed or when force_adopt is set.

    :param prev_position: Position of the previous anchor.
    :param prev_timestamp: Timestamp of the previous anchor.
    :param new_position: Position of the candidate anchor.
    :param new_timestamp: Timestamp of the candidate anchor.
    :param prev_playing: Whether the player was playing at the previous anchor.
    :param new_playing: Whether the player is playing at the candidate anchor.
    :param now: Current time, as a timestamp.
    :param force_adopt: Always adopt the candidate anchor (still reports jumps).

    Returns a (position, timestamp, jumped) tuple where jumped indicates a
    corrected-position discontinuity larger than the threshold, or an
    incomplete anchor becoming complete.
    """
    if (
        not isinstance(prev_position, int | float)
        or not isinstance(prev_timestamp, int | float)
        or not isinstance(new_position, int | float)
        or not isinstance(new_timestamp, int | float)
    ):
        # incomplete (or non-numeric) anchor data: adopt the candidate as-is;
        # a candidate that just became complete is a jump (the position appeared)
        jumped = isinstance(new_position, int | float) and isinstance(new_timestamp, int | float)
        return new_position, new_timestamp, jumped
    # a position anchor only advances (extrapolates) while playing
    prev_corrected = prev_position + (now - prev_timestamp) if prev_playing else prev_position
    new_corrected = new_position + (now - new_timestamp) if new_playing else new_position
    jumped = abs(prev_corrected - new_corrected) > POSITION_JUMP_THRESHOLD
    if force_adopt or jumped or prev_playing != new_playing:
        return new_position, new_timestamp, jumped
    return prev_position, prev_timestamp, False


def _reachable(inputs: StateInputs) -> bool:
    """Return whether a protocol of the player that can render audio reaches its device."""
    own = inputs.own
    return (own.available and own.role != ProtocolRole.CONTROL) or any(
        protocol.available for protocol in inputs.linked
    )


def _playable(inputs: StateInputs) -> bool:
    """Return whether a protocol of the player that can render audio is reachable and set up."""
    own = inputs.own
    return (own.available_for_playback and own.role != ProtocolRole.CONTROL) or any(
        protocol.available_for_playback for protocol in inputs.linked
    )


def _protocol_needing_setup(inputs: StateInputs) -> ProtocolSnapshot | None:
    """Return the protocol that has to be set up before the player can be used, if any."""
    own = inputs.own
    if _playable(inputs):
        # a protocol that is reachable and set up makes the player usable as it is
        return None
    if own.needs_setup:
        return own
    return next((p for p in inputs.linked if p.available and p.needs_setup), None)


def _protocols(
    inputs: StateInputs, foreign_app: ProtocolSnapshot | None
) -> list[PlayerProtocolInfo]:
    """
    Return the protocols the player is made up of, the one of its playback path marked.

    That is the protocol whose app the player shows, else the one of the active output.
    """
    active_id = foreign_app.protocol_id if foreign_app else inputs.active_output_protocol
    if active_id == "native":
        active_id = inputs.own.protocol_id
    if active_id is None:
        return inputs.protocols
    return [
        replace(protocol, active=True) if protocol.protocol_id == active_id else protocol
        for protocol in inputs.protocols
    ]


def _active_group(inputs: StateInputs) -> str | None:
    """Return the id of the group player that currently holds the player, if any."""
    for group in inputs.groups:
        if group.player_id == inputs.player_id:
            continue
        # Use the power state the group's own protocol reports (not `state.powered`)
        # so the check reflects what the group player itself believes — for
        # native/fake control the group's `power()` method sets it directly.
        # `state.powered` is resolved through the power control, which may be None
        # for power_control == NONE even though the group is actively capturing members.
        powered = group.powered
        if powered is False:
            # explicit power-off (fake or native) - never captures members
            continue
        if powered is not True and not group.is_active_session:
            # no explicit power-on and no captured session - group is dormant,
            # configured members are free to be controlled individually
            continue
        if inputs.player_id in group.state.group_members:
            return group.player_id
    return None


def _shown_foreign_app(
    inputs: StateInputs, synced_to: str | None, active_group: str | None
) -> ProtocolSnapshot | None:
    """Return the audio protocol whose app is shown on the player, if any."""
    own = inputs.own
    if (
        (foreign_app := inputs.foreign_app)
        and inputs.active_output_protocol in (None, "native")
        and own.playback_state != PlaybackState.PLAYING
        # a paused player keeps its place, until an app actually plays over it
        and (
            own.playback_state != PlaybackState.PAUSED
            or foreign_app.playback_state == PlaybackState.PLAYING
        )
        and not synced_to
        and not active_group
        and inputs.session is None
    ):
        return foreign_app
    return None


def _playback_position(
    inputs: StateInputs,
    synced_to: str | None,
    active_group: str | None,
    foreign_app: ProtocolSnapshot | None,
) -> tuple[PlaybackState, float | None, float | None]:
    """Return the playback state, the playback position and the timestamp of that position."""
    own = inputs.own
    playback_state: PlaybackState
    elapsed_time: float | None
    elapsed_time_last_updated: float | None
    # If an output protocol is active (and not native),
    # use what that protocol reports as the source of truth
    if active_protocol := inputs.active_protocol:
        playback_state = active_protocol.playback_state
        elapsed_time = active_protocol.elapsed_time
        elapsed_time_last_updated = active_protocol.elapsed_time_last_updated
    elif foreign_app:
        playback_state = foreign_app.playback_state
        elapsed_time = foreign_app.elapsed_time
        elapsed_time_last_updated = foreign_app.elapsed_time_last_updated
    # If we're synced to another player, mirror the leader's state so that
    # synced clients report the same playback info as their leader.
    elif synced_to and (sync_parent := inputs.peers.get(synced_to)):
        playback_state = sync_parent.state.playback_state
        elapsed_time = sync_parent.state.elapsed_time
        elapsed_time_last_updated = sync_parent.state.elapsed_time_last_updated
    else:
        playback_state = own.playback_state
        elapsed_time = own.elapsed_time
        elapsed_time_last_updated = own.elapsed_time_last_updated

    # A live external source reports its own logical position (Spotify Connect,
    # AirPlay, Yandex Ynison). Prefer it over the protocol / own elapsed_time,
    # which tracks bytes consumed — the wrong clock for a live source, losing
    # upstream seeks and pause-resume on corrected_elapsed_time, which the
    # player_queues controller and several player providers consume.
    # Only for a player playing the source itself: one that is hearing another
    # player's audio already took that player's position above, and its own
    # position would contradict the media it is reporting.
    if (
        not synced_to
        and not active_group
        and (session := inputs.session) is not None
        and session.stream_metadata is not None
        and session.stream_metadata.elapsed_time is not None
    ):
        elapsed_time = session.stream_metadata.elapsed_time
        elapsed_time_last_updated = session.stream_metadata.elapsed_time_last_updated or inputs.now

    if inputs.offline_by_power_control:
        # a device that is switched off plays nothing, while one cut off mid-song may
        # still report the state it had
        playback_state = PlaybackState.IDLE

    return playback_state, elapsed_time, elapsed_time_last_updated


def _active_source(
    inputs: StateInputs,
    synced_to: str | None,
    active_group: str | None,
    foreign_app: ProtocolSnapshot | None,
) -> str | None:
    """
    Return the active source, based on any group memberships, source plugins etc.

    This is rather complicated as we need to account for various scenarios like:
        - player is grouped/synced: use the active source of the group/parent player
        - plugin source active: return the active plugin source
        - an audio protocol that has a 3rd party source active
        - the device may report an active source that is actually from an
            active output protocol (e.g. AirPlay)
    """
    own = inputs.own
    # if the player is grouped/synced, use the active source of the group/parent player
    if parent_id := (synced_to or active_group):
        if parent := inputs.peers.get(parent_id):
            return parent.state.active_source
        return None  # should not happen but just in case

    # a live external source playing on this player is what it is playing, and MA
    # put it there, so it outranks whatever the device reports about itself
    if inputs.session is not None:
        return inputs.session.active_source

    # an app on an audio protocol plays past the device's own protocol, which plays nothing
    if foreign_app:
        return foreign_app.active_source

    # always prefer active MA source but add a guard to detect if player is really playing
    # something different, such as a line-in or TV input. Many players do not accurately
    # report the active_source, so only the obvious cases count as a source "takeover"
    if inputs.active_mass_source and not _reports_source_takeover(inputs):
        return inputs.active_mass_source

    # active source as reported by the player itself
    if (
        own.active_source
        and own.active_source != inputs.player_id
        and own.playback_state != PlaybackState.IDLE
        # If an output protocol is active, we simply overrule the active source of the player.
        # Trying to handle this differently is a hot mess and leads to all kinds of edge cases,
        # because many players do not report the active source correctly, especially not when
        # an output protocol is active.
        and inputs.active_output_protocol in (None, "native")
    ):
        return own.active_source

    # return the (last) known MA source - fallback to player's own queue source if none
    return inputs.active_mass_source or inputs.player_id


def _reports_source_takeover(inputs: StateInputs) -> bool:
    """Return whether the source the player reports for itself took it over."""
    if (reported_source := inputs.own.active_source) is None:
        return False
    if reported_source.lower() in EXTERNAL_SOURCES:
        return True
    return inputs.trusts_reported_source and any(
        x.id == reported_source for x in inputs.own.source_list
    )


def _active_source_audio(
    inputs: StateInputs, synced_to: str | None, active_group: str | None
) -> ActiveSourceAudioDetails | None:
    """Return the audio details of the active external source."""
    if parent_id := (active_group or synced_to):
        if parent_id != inputs.player_id and (parent := inputs.peers.get(parent_id)):
            return parent.state.active_source_audio
        return None
    if inputs.session is not None:
        return inputs.session.active_source_audio
    return None


def _source_list(
    inputs: StateInputs, foreign_app: ProtocolSnapshot | None
) -> UniqueList[PlayerSource]:
    """Return the sources of the player."""
    player_id = inputs.player_id
    sources = UniqueList(inputs.own.source_list)
    # always ensure the Music Assistant Queue is in the source list
    mass_source = next((x for x in sources if x.id == player_id), None)
    if mass_source is None:
        # if the MA queue is not in the source list, add it.
        # The capability flags reflect what the queue can actually do right now, so clients can
        # grey out controls instead of issuing commands that can only fail: an empty queue has
        # nothing to play, seek or skip through, and a queue that played to its end can only be
        # started over, with nothing left to seek within or skip to.
        queue = own_queue.queue if (own_queue := inputs.queues.get(player_id)) else None
        queue_has_items = bool(queue and queue.items)
        queue_running = queue_has_items and not (queue and queue.ended)
        mass_source = PlayerSource(
            id=player_id,
            name="Music Assistant Queue",
            passive=False,
            can_play_pause=queue_has_items,
            can_seek=queue_running,
            can_next_previous=queue_running,
        )
        sources.append(mass_source)
    # publish the live external source playing on this player, so clients can name it
    # and offer only the transport it actually supports
    if (session := inputs.session) is not None and (source_uri := session.source_uri):
        sources.append(
            PlayerSource(
                id=source_uri,
                name=session.source.name,
                passive=not session.source.can_initiate,
                can_play_pause=session.source.can_play_pause,
                can_seek=session.source.can_seek,
                can_next_previous=session.source.can_next_previous,
                can_shuffle=session.source.can_shuffle,
                can_repeat=session.source.can_repeat,
                # the ordering the session reports, so a client can render it
                # without a queue to read it from
                shuffle_enabled=session.shuffle_enabled,
                repeat_mode=session.repeat_mode,
            )
        )
    # an app on an audio protocol, with what that protocol offers for it; the same
    # service among the player's own sources is another way to play it
    if foreign_app:
        app_source = next(x for x in foreign_app.source_list if x.id == foreign_app.active_source)
        sources = UniqueList(x for x in sources if x.id != app_source.id)
        sources.append(app_source)
    # standing entries for the audio sources plugins bound to this player or report
    # as inputs of its own device, so they are selectable from the source menu
    # without a session being active first;
    # an already listed uri is skipped: the live session entry above carries the
    # live shuffle/repeat state and must win
    present_ids = {x.id for x in sources}
    for source in inputs.standing_sources:
        if not (uri := source.uri) or uri in present_ids:
            continue
        present_ids.add(uri)
        sources.append(
            PlayerSource(
                id=uri,
                name=source.name,
                passive=not source.can_initiate,
                can_play_pause=source.can_play_pause,
                can_seek=source.can_seek,
                can_next_previous=source.can_next_previous,
                can_shuffle=source.can_shuffle,
                can_repeat=source.can_repeat,
            )
        )
    return sources


def _supported_features(
    inputs: StateInputs,
    foreign_app: ProtocolSnapshot | None,
    source_list: UniqueList[PlayerSource],
) -> set[PlayerFeature]:
    """Return the supported features, based on the supported output protocol(s)."""
    features = inputs.own.supported_features.copy()
    if inputs.active_protocol:
        # Active audio protocol: add from that specific protocol
        for feature in inputs.active_protocol.supported_features:
            if feature in ACTIVE_PROTOCOL_FEATURES:
                features.add(feature)
    if foreign_app:
        features |= foreign_app.supported_features & FOREIGN_PROTOCOL_FEATURES
    # Append (allowed features) from all audio protocols
    for protocol in inputs.linked:
        for feature in protocol.supported_features:
            if feature in PROTOCOL_FEATURES:
                features.add(feature)
    if inputs.power_control != PLAYER_CONTROL_NONE:
        features.add(PlayerFeature.POWER)
    else:
        features.discard(PlayerFeature.POWER)
    # Group providers derive volume and mute capabilities from their members;
    # the group itself intentionally has no native control to resolve here.
    if inputs.player_type != PlayerType.GROUP:
        if inputs.volume_control != PLAYER_CONTROL_NONE:
            features.add(PlayerFeature.VOLUME_SET)
        else:
            features.discard(PlayerFeature.VOLUME_SET)
        if inputs.mute_control != PLAYER_CONTROL_NONE:
            features.add(PlayerFeature.VOLUME_MUTE)
        else:
            features.discard(PlayerFeature.VOLUME_MUTE)
    if sum(1 for s in source_list if not s.passive) >= 2:
        features.add(PlayerFeature.SELECT_SOURCE)
    if inputs.grouping_locked:
        # A provider keeps this group read-only (e.g. an externally-created mixed group);
        # withdraw grouping even if an audio protocol would otherwise supply it.
        features.discard(PlayerFeature.SET_MEMBERS)
    return features


def _group_members(inputs: StateInputs, synced_to: str | None) -> list[str]:
    """Return the ids of the players grouped to the player."""
    if synced_to:
        # If player is synced to another player, it has no group members itself
        return []

    # Start by translating the reported group members (protocols) to player IDs
    members = _player_ids(inputs, inputs.own.group_members)

    # If there's an active audio protocol, include its group members (translated)
    if inputs.active_protocol:
        for member_id in _player_ids(inputs, inputs.active_protocol.group_members):
            if member_id not in members:
                members.append(member_id)

    if inputs.player_type != PlayerType.GROUP:
        # Ensure the player_id is first in the group_members list
        if len(members) > 0 and members[0] != inputs.player_id:
            members = [inputs.player_id, *[m for m in members if m != inputs.player_id]]
        # If the only member is self, return empty list
        if members == [inputs.player_id]:
            return []
    return members


def _static_group_members(inputs: StateInputs) -> list[str]:
    """Return the ids of the players that can not be removed from the group."""
    # an id that names no player stays: a group of players also lists its members that
    # are not there, by their player id
    return [
        peer.player_id if (peer := inputs.peers.get(member_id)) else member_id
        for member_id in inputs.static_group_members
    ]


def _players(inputs: StateInputs, ids: Iterable[str]) -> Iterator[PeerSnapshot]:
    """
    Translate the ids protocols report to the players they stand for.

    :param ids: Ids of protocols and players.
    """
    for peer_id in ids:
        if peer := inputs.peers.get(peer_id):
            yield peer


def _player_ids(inputs: StateInputs, ids: Iterable[str]) -> list[str]:
    """Translate the ids protocols report to the ids of the players they stand for, in order."""
    return list(dict.fromkeys(player.player_id for player in _players(inputs, ids)))


def _can_group_with(
    inputs: StateInputs, synced_to: str | None, active_source: str | None
) -> set[str]:
    """
    Return the ids of the players the player can group with.

    If player is synced: return empty set (already grouped).
    Otherwise what its own protocol can group with, plus what its audio protocols can
    while no external source plays.

    All protocol IDs are translated to the IDs of the players they are attached to.
    """

    def _should_include_player(player: PeerSnapshot) -> bool:
        """Check if a player should be included in the can-group-with set."""
        if not player.available:
            return False
        if player.player_id == inputs.player_id:
            return False  # Don't include self
        if player.grouping_locked:
            # The candidate keeps its own group read-only (e.g. an externally-created
            # mixed group); never offer it as a target, including via an audio protocol
            # that would otherwise reintroduce it.
            return False
        # Don't include (playing) players that have group members (they are group leaders)
        # Use the normalized members: a solo player that reports itself as its only
        # member (e.g. a detached Sendspin client) is not a group leader.
        if (  # noqa: SIM103
            player.state.playback_state in (PlaybackState.PLAYING, PlaybackState.PAUSED)
            and player.state.group_members
        ):
            return False
        return True

    if synced_to:
        # player is already synced/grouped, cannot group with others
        return set()

    if inputs.grouping_locked:
        # A provider keeps this group read-only; offer no grouping targets, including
        # any an audio protocol would otherwise contribute.
        return set()

    # always start with the options of the player's own protocol. These name protocols,
    # e.g. the AirPlay protocols of other players a native AirPlay player can group with
    result = {
        candidate.player_id
        for candidate in _players(inputs, inputs.can_group_with)
        if candidate.type not in (PlayerType.UNKNOWN, PlayerType.SOURCE)
        and _should_include_player(candidate)
    }

    # External source is active - don't include protocol-based grouping
    # When the device plays something MA does not produce (a TV input, line-in, its own
    # streaming endpoint), grouping via protocols (AirPlay, Sendspin, etc.) wouldn't
    # work - only native grouping is available.
    if _plays_external_source(inputs, active_source):
        return result

    # Add the options of the audio protocol(s)
    for candidate in _players(inputs, inputs.protocol_can_group_with):
        if _should_include_player(candidate):
            result.add(candidate.player_id)
    return result


def _plays_external_source(inputs: StateInputs, active_source: str | None) -> bool:
    """
    Return whether the active source is one Music Assistant does not produce itself.

    External sources are the ones MA does not produce itself, such as a TV input,
    line-in, or the device's own streaming endpoint. When one is active,
    protocol-based grouping is not available.
    """
    if active_source is None:
        return False

    # Player's own ID means MA queue is (or was) active
    if active_source == inputs.player_id:
        return False

    return active_source not in inputs.mass_sources


def _current_media(
    inputs: StateInputs,
    synced_to: str | None,
    active_group: str | None,
    foreign_app: ProtocolSnapshot | None,
    active_source: str | None,
    elapsed_time: float | None,
    elapsed_time_last_updated: float | None,
) -> PlayerMedia | None:
    """Return the current media of the player."""
    own = inputs.own
    # if the player is grouped/synced, use the current_media of the group/parent player
    if parent_id := (active_group or synced_to):
        if parent_id != inputs.player_id and (parent := inputs.peers.get(parent_id)):
            return parent.state.current_media
        return None  # if parent player not found, return None for current media
    # a live external source reports what it plays itself
    if inputs.session is not None:
        return _session_media(inputs, inputs.session, elapsed_time, elapsed_time_last_updated)
    if foreign_app:
        return foreign_app.current_media
    # if MA queue is active, return those details
    if active_queue := _active_queue(inputs, active_source):
        return _queue_media(inputs, active_queue)
    # return the media the device reports itself if no group/queue is active: on its
    # own protocol, else on the protocol that renders the audio
    reporting = own if own.current_media or not inputs.active_protocol else inputs.active_protocol
    if media := reporting.current_media:
        image_url = media.image_url
        media_elapsed_time, media_elapsed_time_last_updated = _resolve_position(
            media.elapsed_time,
            media.elapsed_time_last_updated,
            reporting.elapsed_time,
            reporting.elapsed_time_last_updated,
        )
        return PlayerMedia(
            uri=media.uri,
            media_type=media.media_type,
            title=media.title,
            artist=media.artist,
            album=media.album,
            image_url=image_url,
            palette=_resolved_palette(inputs, image_url),
            duration=media.duration,
            source_id=media.source_id or active_source,
            queue_item_id=media.queue_item_id,
            elapsed_time=media_elapsed_time,
            elapsed_time_last_updated=media_elapsed_time_last_updated,
        )
    return None


def _active_queue(inputs: StateInputs, active_source: str | None) -> QueueSnapshot | None:
    """Return the queue of the active source, or the own queue of a player that reports none."""
    active_queue: QueueSnapshot | None = None
    if active_source:
        active_queue = inputs.queues.get(active_source)
    if not active_queue and inputs.own.active_source is None:
        active_queue = inputs.queues.get(inputs.player_id)
    return active_queue


def _queue_media(inputs: StateInputs, active_queue: QueueSnapshot) -> PlayerMedia | None:
    """Return the current item of the active queue as media, None when it has none."""
    queue = active_queue.queue
    if not (current_item := queue.current_item):
        # queue is active but no current item
        return None
    item_image_url = active_queue.item_image_url
    if current_item.streamdetails and (
        stream_metadata := current_item.streamdetails.stream_metadata
    ):
        # handle stream metadata in streamdetails (e.g. for radio stream)
        image_url = stream_metadata.image_url or item_image_url
        elapsed_time, elapsed_time_last_updated = _resolve_position(
            stream_metadata.elapsed_time,
            stream_metadata.elapsed_time_last_updated,
            # a duration is the song's, and how long the stream has played is no
            # position in that song
            None if stream_metadata.duration else queue.elapsed_time,
            queue.elapsed_time_last_updated,
        )
        return PlayerMedia(
            uri=current_item.uri,
            media_type=current_item.media_type,
            title=stream_metadata.title or current_item.name,
            artist=stream_metadata.artist,
            album=stream_metadata.album or stream_metadata.description or current_item.name,
            image_url=image_url,
            palette=_resolved_palette(inputs, image_url),
            duration=stream_metadata.duration or current_item.duration,
            source_id=queue.queue_id,
            queue_item_id=current_item.queue_item_id,
            elapsed_time=elapsed_time,
            elapsed_time_last_updated=elapsed_time_last_updated,
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
        image_url = active_queue.media_item_image_url or item_image_url
        return PlayerMedia(
            uri=str(media_item.uri),
            media_type=media_item.media_type,
            title=f"{media_item.name} ({version})" if version else media_item.name,
            # a podcast episode has no artists, the podcast name takes the artist line
            artist=getattr(media_item, "artist_str", None) or (podcast.name if podcast else None),
            album=album.name if album else None if podcast else description,
            album_artist=getattr(album, "artist_str", None),
            image_url=image_url,
            palette=_resolved_palette(inputs, image_url),
            duration=media_item.duration,
            source_id=queue.queue_id,
            queue_item_id=current_item.queue_item_id,
            elapsed_time=int(queue.elapsed_time),
            elapsed_time_last_updated=queue.elapsed_time_last_updated,
        )

    # fallback to basic current item details
    return PlayerMedia(
        uri=current_item.uri,
        media_type=current_item.media_type,
        title=current_item.name,
        image_url=item_image_url,
        palette=_resolved_palette(inputs, item_image_url),
        duration=current_item.duration,
        source_id=queue.queue_id,
        queue_item_id=current_item.queue_item_id,
        elapsed_time=int(queue.elapsed_time),
        elapsed_time_last_updated=queue.elapsed_time_last_updated,
    )


def _session_media(
    inputs: StateInputs,
    session: AudioSourceSession,
    elapsed_time: float | None,
    elapsed_time_last_updated: float | None,
) -> PlayerMedia:
    """
    Describe what a live external source is playing on the player.

    Falls back to the source's own name and artwork for the parts it has not
    reported, so a source that reports nothing still shows as itself rather
    than as an empty player.

    :param session: The live source session on the player.
    :param elapsed_time: Playback position the player publishes.
    :param elapsed_time_last_updated: Timestamp of that playback position.
    """
    metadata = session.stream_metadata
    image_url = (metadata.image_url if metadata else None) or inputs.session_image_url
    return PlayerMedia(
        uri=session.source_uri or session.source_id,
        media_type=MediaType.AUDIO_SOURCE,
        title=(metadata.title if metadata else None) or session.source.name,
        artist=metadata.artist if metadata else None,
        album=(metadata.album or metadata.description) if metadata else None,
        image_url=image_url,
        palette=_resolved_palette(inputs, image_url),
        duration=metadata.duration if metadata else None,
        # the owner of the session, which is what its stream url is keyed on
        source_id=session.player_id,
        # carried so this object can be handed back to the player and still
        # resolve, as the announcement restore does
        queue_session_id=session.playback_session_id,
        # the published playback position already resolves the source's own position
        # against the clock this player reports (an audio protocol's, or its own) - taking it
        # from there is what keeps current_media and PlayerState.elapsed_time in agreement
        elapsed_time=int(elapsed_time) if elapsed_time is not None else None,
        elapsed_time_last_updated=elapsed_time_last_updated,
    )


def _resolved_palette(inputs: StateInputs, image_url: str | None) -> MediaItemPalette | None:
    """Return the resolved palette if it belongs to image_url, else None."""
    if image_url and image_url == inputs.palette_url:
        return inputs.palette
    return None


def _resolve_position(
    primary: int | None,
    primary_last_updated: float | None,
    fallback: float | None,
    fallback_last_updated: float | None,
) -> tuple[int | None, float | None]:
    """
    Return the (elapsed_time, elapsed_time_last_updated) pair to report for a media item.

    :param primary: Position reported by the media itself, preferred when set.
    :param primary_last_updated: Timestamp belonging to the primary position.
    :param fallback: Position to report when the media has none of its own.
    :param fallback_last_updated: Timestamp belonging to the fallback position.
    """
    # a position is only meaningful together with the timestamp it was taken at,
    # so both values always come from the same source - never a mix of the two
    if primary is not None:
        return primary, primary_last_updated
    if fallback is not None:
        return int(fallback), fallback_last_updated
    return None, None


def _reconcile_media_anchor(
    prev_media: PlayerMedia | None,
    new_media: PlayerMedia | None,
    mirrors_owner: bool,
    prev_playing: bool,
    new_playing: bool,
    now: float,
) -> bool:
    """
    Reconcile the position anchor on the freshly calculated current media.

    Keeps the previous anchor while it extrapolates to the same corrected
    position (steady playback), so regular ticks don't change the state.

    Returns True when the corrected current media position jumped (seek or
    buffer correction reached the current media).

    :param prev_media: Current media of the previous state.
    :param new_media: Current media of the new state.
    :param mirrors_owner: Whether the new media is the object of another player.
    :param prev_playing: Whether the player was playing in the previous state.
    :param new_playing: Whether the player is playing in the new state.
    :param now: Current time, as a timestamp.
    """
    if new_media is None or prev_media is None:
        return False
    if (new_media.queue_item_id or new_media.uri) != (prev_media.queue_item_id or prev_media.uri):
        # different item loaded - adopt the new anchor as-is
        return False
    position, timestamp, jumped = reconcile_position_anchor(
        prev_media.elapsed_time,
        prev_media.elapsed_time_last_updated,
        new_media.elapsed_time,
        new_media.elapsed_time_last_updated,
        prev_playing,
        new_playing,
        now,
        force_adopt=mirrors_owner,
    )
    if not mirrors_owner:
        # steady playback resolves to the previous anchor, so nothing changed;
        # a jump, a playing-state change (or a previous anchor that was still
        # incomplete) adopts the new one
        new_media.elapsed_time = int(position) if position is not None else None
        new_media.elapsed_time_last_updated = timestamp
    return jumped


def _volume_members(inputs: StateInputs, state: PlayerState) -> list[PlayerState]:
    """
    Return the states of the group members that count for the group volume and mute.

    :param state: The new state of the player, which stands in for the player itself.
    """
    members: list[PlayerState] = []
    for member_id in state.group_members:
        peer = inputs.peers.get(member_id)
        # the player itself may be named by one of its legacy ids
        if (peer.player_id if peer else member_id) == inputs.player_id:
            if inputs.player_type != PlayerType.PLAYER:
                continue
            available_for_playback: bool | None = _playable(inputs)
            member = state
        elif peer:
            available_for_playback = peer.available_for_playback
            member = peer.state
        else:
            continue
        if not available_for_playback or not member.enabled:
            continue
        if member.powered is False:
            continue
        members.append(member)
    return members


def _group_volume(state: PlayerState, members: list[PlayerState]) -> int | None:
    """
    Return the group volume level.

    For group players or syncgroups, returns the maximum volume level of all
    powered-on child players, or None if no children support volume control.

    For non-group players, returns the player's own volume level.
    """
    if len(state.group_members) == 0:
        # player is not a group or syncgroup
        if state.volume_control == PLAYER_CONTROL_NONE:
            return None
        return state.volume_level
    # return the maximum volume of all (turned on) child players
    group_volume: int | None = None
    for member in members:
        if member.volume_control == PLAYER_CONTROL_NONE:
            continue
        if (member_volume := member.volume_level) is None:
            continue
        if group_volume is None or member_volume > group_volume:
            group_volume = member_volume
    return group_volume


def _group_volume_muted(state: PlayerState, members: list[PlayerState]) -> bool | None:
    """
    Return the group mute state.

    If this player is a group player or syncgroup, this will return True if all (powered on)
    child players in the group are muted, False if at least one is not muted, or None if
    none of the players within the group support mute control.

    If the player is not a group player or syncgroup, this will return the mute state of the
    player itself (if set), or None if not supported.
    """
    if len(state.group_members) == 0:
        # player is not a group or syncgroup
        if state.mute_control == PLAYER_CONTROL_NONE:
            return None
        return state.volume_muted
    # calculate group mute state from all (turned on) players
    any_unmuted = False
    any_muted = False
    for member in members:
        if member.mute_control == PLAYER_CONTROL_NONE:
            continue
        if (member_muted := member.volume_muted) is None:
            continue
        if member_muted:
            any_muted = True
        else:
            any_unmuted = True
    if any_unmuted and not any_muted:
        return False
    if any_muted and not any_unmuted:
        return True
    return None
