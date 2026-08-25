"""
Native AirPlay announcement orchestration.

The cliairplay binary mixes a raw-PCM clip over the outgoing music with the music
ducked underneath - no flush, no re-anchor, the group timeline stays untouched. This
module renders the shared announcement clip once per member stdin format, arms every
member of the live session at one shared audible instant and tracks the per-member
outcome. Native announcements are only offered while there is live playback to mix
into; without it the player controller plays the announcement its own way.

The clip is wrapped in ducked silence: the binary holds the music duck for the whole
file, so the lead-in is a window in which the music is already quiet and nothing is
being said yet. That is where the announcement volume is raised, and the trailing
silence is where it is put back - neither change is ever heard on the music itself.

Targeting semantics: a player addressed individually announces alone - over its own
ducked copy of the group's music, while the other rooms play on untouched - whenever
a group ENTITY exists as the whole-group handle (a syncgroup member, even the one
leading the underlying session). Only an ad-hoc sync leader, which has no entity
above it, represents its whole group, exactly like playing media to it does.

A group-entity announcement is forwarded by the player controller to every member
concurrently; each call arms its own member and they share one audible instant via
the provider's announce-plan registry, so every room renders the clip in sync.
Members of a Sendspin GROUP of bridged players each compute their own instant, so a
group announcement there can be offset by tens of ms across rooms; cross-player
plan sharing for that case is future work.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import ContentType
from music_assistant_models.errors import PlayerCommandFailed

from .constants import (
    AIRPLAY_ANNOUNCE_AT_MARGIN_MS,
    AIRPLAY_ANNOUNCE_DONE_TIMEOUT_MS,
    AIRPLAY_ANNOUNCE_DUCK_DB,
    AIRPLAY_ANNOUNCE_DUCK_LEAD_S,
    AIRPLAY_ANNOUNCE_DUCK_TAIL_S,
    AIRPLAY_ANNOUNCE_FALLBACK_SPAN_MS,
    AIRPLAY_ANNOUNCE_STARTED_TIMEOUT_MS,
    AIRPLAY_ANNOUNCE_VOLUME_BUMP_DELAY_MS,
    AIRPLAY_ANNOUNCE_VOLUME_RESTORE_PAD_MS,
    AIRPLAY_VOLUME_DB_PER_POINT,
    AIRPLAY_VOLUME_ECHO_GRACE_S,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.player import PlayerMedia

    from music_assistant.controllers.players.helpers import AnnounceData
    from music_assistant.controllers.streams.announcements import AnnouncementRender
    from music_assistant.models.player import Player

    from .player import AirPlayPlayer
    from .provider import AirPlayProvider
    from .stream import AirPlayStream


async def play_announcement(
    player: AirPlayPlayer, announcement: PlayerMedia, volume_level: int | None
) -> None:
    """
    Play an announcement on the player (and, for an ad-hoc leader, its members).

    The clip is mixed over the live playing session without interrupting it. A
    group-entity announcement is forwarded per member by the controller; the
    members share one audible instant through the provider's announce-plan
    registry so every room renders the clip in sync.

    :param player: The player the announcement targets.
    :param announcement: The announcement to play.
    :param volume_level: Optional volume level for the announcement.
    """
    announce_data = cast("AnnounceData | None", announcement.custom_data)
    if not announce_data or "announcement_url" not in announce_data:
        raise PlayerCommandFailed(
            f"Announcement for {player.display_name} carries no announcement data"
        )
    renderer = player.mass.streams.announcement_renderer
    render = renderer.acquire(announce_data)
    try:
        await _run_announcement(player, render, volume_level)
    finally:
        await renderer.release(render)


async def _run_announcement(
    player: AirPlayPlayer,
    render: AnnouncementRender,
    volume_level: int | None,
) -> None:
    """
    Render the clip and mix it over the live playing session.

    :param player: The player the announcement targets.
    :param render: The announcement render to play.
    :param volume_level: Optional volume level for the announcement.
    """
    # The whole clip is rendered up front: the binary is handed a complete file,
    # and the exact duration bounds every wait below.
    duration = await render.wait_finished()
    if duration is None:
        duration = render.duration
    if duration <= 0:
        player.logger.warning(
            "Announcement for %s produced no audio; nothing to play", player.display_name
        )
        return
    await _announce_over_live_session(player, render, duration, volume_level)


async def _announce_over_live_session(
    player: AirPlayPlayer,
    render: AnnouncementRender,
    duration: float,
    volume_level: int | None,
) -> None:
    """
    Mix the clip over the live playing session.

    :param player: The player the announcement targets.
    :param render: The (finished) announcement render to play.
    :param duration: Exact clip duration in seconds.
    :param volume_level: Optional volume level for the announcement.
    :raises PlayerCommandFailed: If the player stopped playing before the clip
        could be armed, or if no member armed it.
    """
    clip_files: dict[str, str] = {}
    bumped: dict[str, int] = {}
    try:
        # The dispatch decision and the arming run under the player lock - the
        # same lock play_media holds to mutate the session - while the
        # multi-second clip waits below run outside it, so provider-internal
        # paths (DACP feedback, member removal on stream loss) are not blocked
        # for the clip's duration. The controller's per-player playback lock
        # serializes this whole announcement against cmd_stop, cmd_resume,
        # cmd_power, enqueue_next_media, play_media and other announcements,
        # but NOT against cmd_play/cmd_pause/cmd_seek - those can land inside
        # the waits, where the binary's own cancel semantics keep them safe: a
        # pause or flush cancels the clip cleanly (done cancelled=1) and the
        # announcement ends with a cancelled outcome instead of wedging.
        async with player._lock:
            members = _live_members(player)
            if not members:
                # the feature is only advertised while there is live audio to mix into,
                # so by now that playback ended or moved to a stream we do not own
                raise PlayerCommandFailed(
                    f"Cannot announce on {player.display_name}: "
                    "there is no live playback to mix the announcement into"
                )
            streams: dict[str, AirPlayStream] = {}
            for member in members:
                assert member.stream is not None  # guaranteed by _live_members
                streams[member.player_id] = member.stream
            # one clip file per distinct member stdin format, shared by all
            # members on that format
            for stream in streams.values():
                clip_key = _format_key(stream.pcm_format)
                if clip_key not in clip_files:
                    clip_files[clip_key] = await _render_clip_file(render, stream.pcm_format)
            # resolved only now: file rendering above must not eat into the
            # margin the shared instant carries
            at_unix_ms = _resolve_announce_instant(player, members, render.key)
            delivered = await asyncio.gather(
                *[
                    streams[member.player_id].announce(
                        clip_files[_format_key(streams[member.player_id].pcm_format)],
                        at_unix_ms,
                        _member_duck_db(member, volume_level),
                    )
                    for member in members
                ],
                return_exceptions=True,
            )
        for member, sent in zip(members, delivered, strict=True):
            if isinstance(sent, BaseException):
                player.logger.debug(
                    "Could not deliver the announcement arm to %s: %r",
                    member.display_name,
                    sent,
                )
        started_timeout = (
            max(0.0, at_unix_ms / 1000 - time.time()) + AIRPLAY_ANNOUNCE_STARTED_TIMEOUT_MS / 1000
        )
        acks = await asyncio.gather(
            *[
                stream.wait_announce_started(started_timeout)
                if sent is True
                else _no_announce_ack()
                for stream, sent in zip(streams.values(), delivered, strict=True)
            ]
        )
        started: dict[str, tuple[int, int]] = {
            member.player_id: ack
            for member, ack in zip(members, acks, strict=True)
            if ack is not None
        }
        if not started:
            # Music keeps playing on every member, so failing here leaves the
            # user's playback untouched. Silently ignoring the unknown arm
            # command is exactly what an outdated cliairplay build does.
            raise PlayerCommandFailed(
                f"No member of {player.display_name} armed the announcement; "
                "the running cliairplay binary may not support announcements yet "
                "(version mismatch)"
            )
        armed = [member for member in members if member.player_id in started]
        if failed := [m.display_name for m in members if m.player_id not in started]:
            player.logger.warning(
                "Announcement was not played on %d member(s) of %s: %s",
                len(failed),
                player.display_name,
                ", ".join(failed),
            )
        # Every instant below is derived from the acked start of the clip FILE,
        # which opens with the ducked lead-in: the music is already ducked there
        # while the announcement itself has not started yet.
        file_seconds = AIRPLAY_ANNOUNCE_DUCK_LEAD_S + duration + AIRPLAY_ANNOUNCE_DUCK_TAIL_S
        earliest_at_unix_ms = min(ack_at or at_unix_ms for ack_at, _ in started.values())
        content_end_unix_ms = (
            max(ack_at or at_unix_ms for ack_at, _ in started.values())
            + (AIRPLAY_ANNOUNCE_DUCK_LEAD_S + duration) * 1000
        )
        latest_end_unix_ms = max(
            (ack_at or at_unix_ms) + (ack_duration or int(file_seconds * 1000))
            for ack_at, ack_duration in started.values()
        )
        # the receiver echoes every level it is given, and those echoes must not be
        # read as the user reaching for the volume mid-announcement
        for member in armed:
            member.suppress_volume_reports(
                max(0.0, latest_end_unix_ms / 1000 - time.time()) + AIRPLAY_VOLUME_ECHO_GRACE_S
            )
        # the done reports arrive while the volume timeline below runs its course
        done_task = asyncio.gather(
            *[
                streams[member_id].wait_announce_done(
                    max(0.0, (ack_at or at_unix_ms) / 1000 - time.time())
                    + (ack_duration / 1000 if ack_duration else file_seconds)
                    + AIRPLAY_ANNOUNCE_DONE_TIMEOUT_MS / 1000
                )
                for member_id, (ack_at, ack_duration) in started.items()
            ]
        )
        try:
            if volume_level is not None:
                await _volume_around_clip(
                    player,
                    armed,
                    volume_level,
                    earliest_at_unix_ms,
                    content_end_unix_ms,
                    bumped,
                )
            done_results = await done_task
        except BaseException:
            done_task.cancel()
            raise
        for member_id, done in zip(started, done_results, strict=True):
            if not done:
                player.logger.debug(
                    "Announcement on member %s was cut short or its completion went unreported",
                    member_id,
                )
        # announce_done fires when the clip is fully MIXED at the delivery
        # head - up to a member's span BEFORE it is audible. Returning then
        # would let the caller restore mutes (and arm a follow-up
        # announcement) over the audible tail, so hold the return until the
        # latest audible end across the started members.
        await _hold_until(latest_end_unix_ms + AIRPLAY_ANNOUNCE_VOLUME_RESTORE_PAD_MS)
    finally:
        if bumped:
            # A failed or cancelled announcement leaves the music playing, so whatever
            # the timeline above did not put back still has to be restored - from its
            # own task, since an await here is cancelled along with this one.
            player.mass.create_task(_restore_announcement_volume(player, bumped))
        for path in clip_files.values():
            with suppress(OSError):
                Path(path).unlink()


def _live_members(player: AirPlayPlayer) -> list[AirPlayPlayer]:
    """
    Return the members a live announcement targets, or [] without live playback.

    A synced member announced individually is just itself; a session leader
    covers every member of its session. Only a PLAYING session can mix a clip -
    a parked (paused) or idle player has no live timeline to mix into.
    """
    if not player.has_live_audio:
        return []
    stream = player.stream
    assert stream is not None  # guaranteed by has_live_audio
    if player.synced_to:
        return [player]
    if stream.session is None:
        # A Sendspin-bridged player plays without a stream session, but its
        # stream is a regular AirPlayStream the clip mixes into (self-only).
        provider = cast("AirPlayProvider", player.provider)
        bridge = provider.bridge_manager.get_bridge(player.player_id)
        if bridge is not None and bridge.owns_airplay_stream:
            return [player]
        return []
    if _owning_group_entity(player):
        # A group ENTITY (e.g. a syncgroup) owns this session, and that entity
        # is the whole-group announcement handle: this player addressed
        # individually announces alone, even as the session's sync leader.
        return [player]
    # An ad-hoc leader has no entity above it, so it IS the group handle:
    # announcing to it covers every member of its session.
    return [
        member
        for member in stream.session.sync_clients
        if member.stream is not None and member.stream.running and member.stream.connected
    ]


def _resolve_announce_instant(
    player: AirPlayPlayer, members: list[AirPlayPlayer], render_key: str
) -> int:
    """
    Return the audible instant (unix ms) the announcement is armed for.

    When the targets are only a part of a multi-member session (a group-entity
    announcement is fanned out per member by the controller, each call arming
    its own member), the instant is shared through the provider's plan
    registry: the first call computes it from EVERY session member's span, the
    concurrent sibling calls reuse it, and every room renders the clip in
    sync. A call that arms its whole target set at once (ad-hoc leader, solo,
    bridged) needs no plan.

    :param player: The player this call targets.
    :param members: The members this call arms.
    :param render_key: Identity of the announcement audio; instants are only
        ever shared between arms of the SAME announcement.
    """
    session = player.stream.session if player.stream else None
    session_members = session.sync_clients if session else members
    if len(members) >= len(session_members):
        return _shared_announce_instant(
            member.stream for member in members if member.stream is not None
        )
    provider = cast("AirPlayProvider", player.provider)
    plans = provider._announce_plans
    now_ms = int(time.time() * 1000)
    # prune settled plans so the registry cannot grow with announcement history
    for key in [key for key, at_ms in plans.items() if at_ms <= now_ms]:
        del plans[key]
    plan_key = (
        _owning_group_entity(player) or player.synced_to or player.player_id,
        render_key,
    )
    if (at_unix_ms := plans.get(plan_key)) is not None:
        return at_unix_ms
    # the instant must clear EVERY session member's span: the sibling calls of
    # a group fan-out reuse it for their own members
    at_unix_ms = _shared_announce_instant(
        member.stream for member in session_members if member.stream is not None
    )
    plans[plan_key] = at_unix_ms
    return at_unix_ms


def _shared_announce_instant(streams: Iterable[AirPlayStream]) -> int:
    """
    Return the shared audible instant (unix ms) for arming an announcement.

    Every member must mix the clip into audio it has not delivered yet; its
    span is how far ahead of the audible position that delivery head runs, so
    the shared instant sits past the largest member span plus a fan-out margin.
    """
    max_span_ms = max(_member_span_ms(stream) for stream in streams)
    return int(time.time() * 1000) + max_span_ms + AIRPLAY_ANNOUNCE_AT_MARGIN_MS


def _owning_group_entity(player: AirPlayPlayer) -> str | None:
    """
    Return the id of the group ENTITY that owns this player's session, if any.

    ``active_group`` only ever names a real group player (e.g. a syncgroup) -
    but a protocol player never carries it itself: the model keeps the group
    state on the device player it renders for, so the ownership is read
    through the protocol parent when needed.
    """
    if player.state.active_group:
        return player.state.active_group
    if player.protocol_parent_id and (
        parent := player.mass.players.get_player(player.protocol_parent_id)
    ):
        return parent.state.active_group
    return None


def _member_span_ms(stream: AirPlayStream) -> int:
    """Return how far a member's delivery head runs ahead of its audible position (ms)."""
    if stream.warm_lead_ms > 0:
        return stream.warm_lead_ms
    if stream.latency_lead_ms > 0:
        return stream.latency_lead_ms
    return AIRPLAY_ANNOUNCE_FALLBACK_SPAN_MS


def _volume_target(member: AirPlayPlayer) -> Player:
    """
    Return the player whose volume control owns this member's output.

    An AirPlay volume writes the receiver's own level, so it may only be set when
    nothing else owns it; on a device that is also reachable through a native
    provider the announcement volume belongs on that parent instead.
    """
    if (parent_id := member.protocol_parent_id) and (
        parent := member.mass.players.get_player(parent_id)
    ):
        return parent
    return member


def _member_duck_db(member: AirPlayPlayer, volume_level: int | None) -> float:
    """
    Return the music duck (dB) for one member, compensated for its volume bump.

    The announcement volume raises the music bed together with the clip, so the duck
    is deepened by that same rise and the music keeps its configured perceived duck
    depth while the clip plays at the configured announcement loudness. A bump DOWN
    (a night-mode announcement quieter than the music) symmetrically shallows the
    duck, and the result never leaves the binary's usable range.

    The rise is read off the AirPlay volume scale, which is linear dB (see
    AIRPLAY_VOLUME_DB_PER_POINT). A level that lands on another control (the native
    volume of the device this output renders for) follows that control's own taper,
    so there the compensation is an approximation - still far closer than leaving the
    bed to ride up with the clip.
    """
    duck_db = float(AIRPLAY_ANNOUNCE_DUCK_DB)
    if volume_level is None:
        return duck_db
    target = _volume_target(member)
    if (prev_volume := target.state.volume_level) is None:
        return duck_db
    # the levels are logical, and the volume limits configured on the target decide
    # what they land on: the device levels are what the rise is actually made of
    scale = member.mass.players.scale_volume_to_device
    bump_db = (
        scale(target.player_id, volume_level) - scale(target.player_id, prev_volume)
    ) * AIRPLAY_VOLUME_DB_PER_POINT
    return min(0.0, max(-60.0, duck_db - bump_db))


async def _volume_around_clip(
    player: AirPlayPlayer,
    armed: list[AirPlayPlayer],
    volume_level: int,
    lead_in_unix_ms: float,
    content_end_unix_ms: float,
    bumped: dict[str, int],
) -> None:
    """
    Move the volume to the announcement level and back, inside the ducked silence.

    Both changes are timed on the acked instant of the clip file: the done report
    arrives when the clip is fully MIXED (at the delivery head), which is ahead of
    it being heard, so neither change can key off it.

    :param player: The player the announcement targets.
    :param armed: The members playing the clip.
    :param volume_level: The announcement volume level.
    :param lead_in_unix_ms: Start of the earliest member's ducked lead-in.
    :param content_end_unix_ms: End of the latest member's announcement audio.
    :param bumped: Mapping that tracks which players still need restoring.
    """
    await _hold_until(lead_in_unix_ms + AIRPLAY_ANNOUNCE_VOLUME_BUMP_DELAY_MS)
    await _apply_announcement_volume(armed, volume_level, bumped)
    await _hold_until(content_end_unix_ms + AIRPLAY_ANNOUNCE_VOLUME_RESTORE_PAD_MS)
    await _restore_announcement_volume(player, bumped)


async def _apply_announcement_volume(
    members: list[AirPlayPlayer], volume_level: int, bumped: dict[str, int]
) -> None:
    """
    Put every member on the announcement volume.

    :param members: The members playing the clip.
    :param volume_level: The announcement volume level.
    :param bumped: Mapping that is filled in-place with the previous level per player
        id, so the caller restores exactly what was changed even if this call fails.
    """
    targets: list[Player] = []
    for member in members:
        target = _volume_target(member)
        prev_volume = target.state.volume_level
        if prev_volume is None or prev_volume == volume_level or target.player_id in bumped:
            continue
        bumped[target.player_id] = prev_volume
        targets.append(target)
    # the command travels through the controller so it lands on the control that owns
    # the output, on that control's own scale
    results = await asyncio.gather(
        *[target.mass.players.cmd_volume_set(target.player_id, volume_level) for target in targets],
        return_exceptions=True,
    )
    for target, result in zip(targets, results, strict=True):
        if isinstance(result, BaseException):
            target.logger.warning(
                "Could not set the announcement volume on %s: %r", target.display_name, result
            )


async def _restore_announcement_volume(player: AirPlayPlayer, bumped: dict[str, int]) -> None:
    """
    Put every bumped player back on the level it had before the announcement.

    An entry is dropped only once its player is restored, so a later call covers
    exactly what this one did not reach.

    :param player: The player the announcement targets.
    :param bumped: The level each player carried before the announcement.
    """
    for player_id in list(bumped):
        try:
            await player.mass.players.cmd_volume_set(player_id, bumped[player_id])
        except Exception as err:
            player.logger.warning(
                "Could not restore the volume of %s after the announcement: %r", player_id, err
            )
            continue
        del bumped[player_id]


async def _hold_until(unix_ms: float) -> None:
    """Wait for the given wall-clock instant (unix ms) to arrive."""
    await asyncio.sleep(max(0.0, unix_ms / 1000 - time.time()))


async def _render_clip_file(render: AnnouncementRender, pcm_format: AudioFormat) -> str:
    """
    Render the announcement clip into a temp file of raw PCM in the given format.

    The clip is wrapped in ducked silence: the binary holds the music duck for the
    whole file, so the music is already ducked before the announcement starts and
    stays ducked briefly past it - the window in which the announcement volume is
    raised and put back.

    The caller owns the file and removes it once every member is done with it.

    :param render: The (finished) announcement render to read.
    :param pcm_format: The raw PCM format the file must carry.
    """
    clip = bytearray(_clip_silence(pcm_format, AIRPLAY_ANNOUNCE_DUCK_LEAD_S))
    async for chunk in render.get_stream(pcm_format):
        clip.extend(chunk)
    clip.extend(_clip_silence(pcm_format, AIRPLAY_ANNOUNCE_DUCK_TAIL_S))
    return await asyncio.to_thread(_write_clip_file, clip)


def _clip_silence(pcm_format: AudioFormat, seconds: float) -> bytes:
    """Return the silence an announcement clip is wrapped in, in the given PCM format."""
    # Wire sizes come from the content type: at 24-bit the stdin carrier is
    # s32le while bit_depth stays 24, so bit_depth-derived sizes are wrong.
    bytes_per_sample = {
        ContentType.PCM_S16LE: 2,
        ContentType.PCM_S24LE: 3,
        ContentType.PCM_S32LE: 4,
        ContentType.PCM_F32LE: 4,
    }.get(pcm_format.content_type, pcm_format.bit_depth // 8)
    frames = int(pcm_format.sample_rate * seconds)
    return bytes(frames * bytes_per_sample * pcm_format.channels)


def _write_clip_file(data: bytes | bytearray) -> str:
    """Write clip audio to a uniquely named temp file and return its path."""
    fd, path = tempfile.mkstemp(prefix="ma_airplay_announce_", suffix=".pcm")
    with os.fdopen(fd, "wb") as clip_file:
        clip_file.write(data)
    return path


async def _no_announce_ack() -> tuple[int, int] | None:
    """Stand in for the started-ack of a member whose arm was never delivered."""
    return None


def _format_key(pcm_format: AudioFormat) -> str:
    """Return the identity of a raw PCM stdin format for clip-file sharing."""
    return (
        f"{pcm_format.content_type.value}_{pcm_format.sample_rate}"
        f"_{pcm_format.bit_depth}_{pcm_format.channels}"
    )
