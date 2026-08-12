"""
Native AirPlay announcement orchestration.

The cliairplay binary mixes a raw-PCM clip over the outgoing music with the music
ducked underneath - no flush, no re-anchor, the group timeline stays untouched. This
module renders the shared announcement clip once per member stdin format, arms every
member of the live session at one shared audible instant and tracks the per-member
outcome. Whenever there is no live playback to mix into (idle or parked player), the
announcement runs as a dedicated stream session instead, leaving the player idle.

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

from music_assistant_models.enums import ContentType, PlaybackState
from music_assistant_models.errors import PlayerCommandFailed

from .constants import (
    AIRPLAY_ANNOUNCE_AT_MARGIN_MS,
    AIRPLAY_ANNOUNCE_DONE_TIMEOUT_MS,
    AIRPLAY_ANNOUNCE_DUCK_DB,
    AIRPLAY_ANNOUNCE_DUCK_TAIL_S,
    AIRPLAY_ANNOUNCE_FALLBACK_SPAN_MS,
    AIRPLAY_ANNOUNCE_SESSION_DRAIN_S,
    AIRPLAY_ANNOUNCE_STARTED_TIMEOUT_MS,
    AIRPLAY_ANNOUNCE_VOLUME_BUMP_DELAY_MS,
    AIRPLAY_ANNOUNCE_VOLUME_RESTORE_PAD_MS,
    AIRPLAY_VOLUME_DB_PER_POINT,
)
from .stream_session import AirPlayStreamSession

if TYPE_CHECKING:
    from collections.abc import Iterable

    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.player import PlayerMedia

    from music_assistant.controllers.players.helpers import AnnounceData
    from music_assistant.controllers.streams.announcements import AnnouncementRender

    from .player import AirPlayPlayer
    from .provider import AirPlayProvider
    from .stream import AirPlayStream

# Scheduled announcement-volume changes of one member: the previous level and
# the timer task ids that apply/restore the announcement volume around the
# clip (mass.call_later tracks its timers by task id, and only cancel_timer
# releases a tracked entry that never fired).
_VolumeSchedule = tuple["AirPlayPlayer", int, list[str]]


async def play_announcement(
    player: AirPlayPlayer, announcement: PlayerMedia, volume_level: int | None
) -> None:
    """
    Play an announcement on the player (and, for an ad-hoc leader, its members).

    A live playing session mixes the clip over the music without interrupting
    it; an idle player plays the announcement as a dedicated stream session and
    ends idle. A group-entity announcement is forwarded per member by the
    controller; the members share one audible instant through the provider's
    announce-plan registry so every room renders the clip in sync.

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
        await _run_announcement(player, announcement, render, volume_level)
    finally:
        await renderer.release(render)


async def _run_announcement(
    player: AirPlayPlayer,
    announcement: PlayerMedia,
    render: AnnouncementRender,
    volume_level: int | None,
) -> None:
    """
    Render the clip, then mix it over live playback or play it as its own session.

    :param player: The player the announcement targets.
    :param announcement: The announcement to play.
    :param render: The announcement render to play.
    :param volume_level: Optional volume level for the announcement.
    """
    # The whole clip is rendered up front: the live path hands the binary a
    # complete file, and the exact duration bounds every wait below.
    duration = await render.wait_finished()
    if duration is None:
        duration = render.duration
    if duration <= 0:
        player.logger.warning(
            "Announcement for %s produced no audio; nothing to play", player.display_name
        )
        return
    if await _announce_over_live_session(player, render, duration, volume_level):
        return
    await _announce_with_session(player, announcement, render, duration, volume_level)


async def _announce_over_live_session(
    player: AirPlayPlayer,
    render: AnnouncementRender,
    duration: float,
    volume_level: int | None,
) -> bool:
    """
    Mix the clip over the live playing session.

    :param player: The player the announcement targets.
    :param render: The (finished) announcement render to play.
    :param duration: Exact clip duration in seconds.
    :param volume_level: Optional volume level for the announcement.
    :return: True when at least one member played the clip; False when there is
        no live playback to mix into, so the caller runs the dedicated
        announcement session instead.
    :raises PlayerCommandFailed: If there was live playback but no member armed
        the clip (tearing the playing session down for a fallback would trade
        the user's music for the announcement).
    """
    clip_files: dict[str, str] = {}
    volume_schedules: list[_VolumeSchedule] = []
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
                return False
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
            # Music keeps playing on every member, so falling back to a
            # dedicated announcement session would stop the user's playback
            # over a clip that could not be mixed anyway. Silently ignoring
            # the unknown arm command is exactly what an outdated cliairplay
            # build does.
            raise PlayerCommandFailed(
                f"No member of {player.display_name} armed the announcement; "
                "the running cliairplay binary may not support announcements yet "
                "(version mismatch)"
            )
        if volume_level is not None:
            for member in members:
                if (ack := started.get(member.player_id)) is None:
                    continue
                ack_at_unix_ms, ack_duration_ms = ack
                # The binary-reported duration includes the ducked silence
                # tail appended to the clip file; the restore must land INSIDE
                # that cushion, so it is timed on the content length alone.
                content_seconds = (
                    max(0.0, ack_duration_ms / 1000 - AIRPLAY_ANNOUNCE_DUCK_TAIL_S)
                    if ack_duration_ms
                    else duration
                )
                if schedule := _schedule_member_volume(
                    member,
                    volume_level,
                    ack_at_unix_ms or at_unix_ms,
                    content_seconds,
                ):
                    volume_schedules.append(schedule)
        padded_duration = duration + AIRPLAY_ANNOUNCE_DUCK_TAIL_S
        done_results = await asyncio.gather(
            *[
                streams[member_id].wait_announce_done(
                    max(0.0, (ack_at or at_unix_ms) / 1000 - time.time())
                    + (ack_duration / 1000 if ack_duration else padded_duration)
                    + AIRPLAY_ANNOUNCE_DONE_TIMEOUT_MS / 1000
                )
                for member_id, (ack_at, ack_duration) in started.items()
            ]
        )
        for member_id, done in zip(started, done_results, strict=True):
            if not done:
                player.logger.debug(
                    "Announcement on member %s was cut short or its completion went unreported",
                    member_id,
                )
        if failed := [m.display_name for m in members if m.player_id not in started]:
            player.logger.warning(
                "Announcement was not played on %d member(s) of %s: %s",
                len(failed),
                player.display_name,
                ", ".join(failed),
            )
        # announce_done fires when the clip is fully MIXED at the delivery
        # head - up to a member's span BEFORE it is audible. Returning then
        # would let the caller restore mutes (and arm a follow-up
        # announcement) over the audible tail, so hold the return until the
        # latest audible end across the started members.
        latest_end_unix_ms = max(
            (ack_at or at_unix_ms) + (ack_duration or int(padded_duration * 1000))
            for ack_at, ack_duration in started.values()
        )
        await asyncio.sleep(
            max(0.0, latest_end_unix_ms / 1000 - time.time())
            + AIRPLAY_ANNOUNCE_VOLUME_RESTORE_PAD_MS / 1000
        )
        return True
    except BaseException:
        # The scheduled volume changes belong to an announcement that is no
        # longer being tracked: cancel them and restore the previous levels
        # right away (a restore of an unchanged level is a harmless re-send).
        for member, prev_volume, task_ids in volume_schedules:
            for task_id in task_ids:
                member.mass.cancel_timer(task_id)
            player.mass.create_task(member.volume_set(prev_volume))
        raise
    finally:
        for path in clip_files.values():
            with suppress(OSError):
                Path(path).unlink()


async def _announce_with_session(
    player: AirPlayPlayer,
    announcement: PlayerMedia,
    render: AnnouncementRender,
    duration: float,
    volume_level: int | None,
) -> None:
    """
    Play the announcement as a dedicated stream session.

    Any existing (parked) session led by this player is stopped first. The
    player ends idle - the same end state the generic announcement flow leaves
    a non-playing player in; the queue keeps its resume position server-side.

    Known limitation: a synced member of a group without live playback is
    refused here - its stream belongs to the leader's shared (parked) session,
    which must not be torn down for a single-member announcement, so that
    announcement simply does not play. Announcing over a parked session
    without stopping it needs binary-side support (announce while in
    STANDBY), which is the planned proper fix.

    :param player: The player the announcement targets.
    :param announcement: The announcement media, owning the session's metadata.
    :param render: The (finished) announcement render to play.
    :param duration: Exact clip duration in seconds.
    :param volume_level: Optional volume level for the announcement.
    :raises PlayerCommandFailed: If the player is a synced group member or is
        streamed to by its Sendspin bridge - both own no session this path may
        replace.
    """
    provider = cast("AirPlayProvider", player.provider)
    if player.synced_to:
        # The controller's group-forward runs the member calls in a
        # TaskManager, which does not cancel siblings on a failure, so raising
        # here cannot take the leader's in-flight announcement down with it.
        raise PlayerCommandFailed(
            f"Cannot announce on {player.display_name}: a grouped AirPlay player "
            "without live playback cannot announce natively"
        )
    bridge = provider.bridge_manager.get_bridge(player.player_id)
    if bridge is not None and bridge.owns_airplay_stream:
        # The bridge is actively streaming to the device; a dedicated
        # announcement session would seize it from the Sendspin side with
        # nothing restoring that playback. Only reachable in a race (the live
        # mix path handles a playing bridge stream). A bridge that is merely
        # configured but idle is a bystander: the fallback then behaves
        # exactly as it does for an unbridged player.
        raise PlayerCommandFailed(
            f"Cannot announce on {player.display_name}: the player is being streamed "
            "to by its Sendspin bridge"
        )
    prev_volumes: dict[AirPlayPlayer, int] = {}
    session: AirPlayStreamSession | None = None
    try:
        # Session teardown and setup mirror play_media's cold path, under the
        # same player lock; the clip wait below runs outside it.
        async with player._lock:
            if player.stream and player.stream.running and player.stream.session:
                player._transitioning = True
                await player.stream.session.stop()
                player.stream = None
            sync_clients = player._get_sync_clients()
            session_pcm_format = await player._get_session_pcm_format(sync_clients, announcement)
            if volume_level is not None:
                for member in sync_clients:
                    prev_volume = member.volume_level
                    if prev_volume is not None and prev_volume != volume_level:
                        prev_volumes[member] = prev_volume
                        await member.volume_set(volume_level)
            session = AirPlayStreamSession(provider, sync_clients, session_pcm_format, announcement)
            await session.start(render.get_stream(session_pcm_format))
            player._transitioning = False
        # The clip is anchored: wait out the start lead plus the clip and the
        # receiver drain. The source ends after the clip, so the session
        # usually ends cleanly on its own and the stop below is cleanup only.
        await asyncio.sleep(
            max(0.0, session.start_time - time.time()) + duration + AIRPLAY_ANNOUNCE_SESSION_DRAIN_S
        )
    finally:
        player._transitioning = False
        if session is not None:
            await session.stop()
            # like player.stop(): an idle player must not keep showing media
            player._attr_current_media = None
            player.update_state()
        for member, prev_volume in prev_volumes.items():
            await member.volume_set(prev_volume)


def _live_members(player: AirPlayPlayer) -> list[AirPlayPlayer]:
    """
    Return the members a live announcement targets, or [] without live playback.

    A synced member announced individually is just itself; a session leader
    covers every member of its session. Only a PLAYING session can mix a clip -
    a parked (paused) or idle player has no live timeline to mix into.
    """
    if player.playback_state != PlaybackState.PLAYING:
        return []
    stream = player.stream
    if stream is None or not stream.running or not stream.connected:
        return []
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
    if player.state.active_group:
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
    plan_key = (player.state.active_group or player.synced_to or player.player_id, render_key)
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


def _member_span_ms(stream: AirPlayStream) -> int:
    """Return how far a member's delivery head runs ahead of its audible position (ms)."""
    if stream.warm_lead_ms > 0:
        return stream.warm_lead_ms
    if stream.latency_lead_ms > 0:
        return stream.latency_lead_ms
    return AIRPLAY_ANNOUNCE_FALLBACK_SPAN_MS


def _member_duck_db(member: AirPlayPlayer, volume_level: int | None) -> float:
    """
    Return the music duck (dB) for one member, compensated for its volume bump.

    The announcement volume is applied as DEVICE volume, which raises the music
    bed together with the clip. The AirPlay volume scale is linear dB (see
    AIRPLAY_VOLUME_DB_PER_POINT), so that rise is exactly known and the duck is
    deepened by the same amount - the music keeps its configured perceived duck
    depth while the clip plays at the configured announcement loudness. A bump
    DOWN (a night-mode announcement quieter than the music) symmetrically
    shallows the duck, and the result never leaves the binary's usable range.
    """
    duck_db = float(AIRPLAY_ANNOUNCE_DUCK_DB)
    if volume_level is None or member.volume_level is None:
        return duck_db
    bump_db = (volume_level - member.volume_level) * AIRPLAY_VOLUME_DB_PER_POINT
    return min(0.0, max(-60.0, duck_db - bump_db))


def _schedule_member_volume(
    member: AirPlayPlayer, volume_level: int, at_unix_ms: int, clip_seconds: float
) -> _VolumeSchedule | None:
    """
    Schedule the announcement volume around one member's audible clip window.

    Both changes are wall-clock timers on the acked instant: the done report
    arrives when the clip is fully MIXED (at the delivery head), which is ahead
    of it being heard, so neither change can key off it.

    :param member: The member whose volume is bumped and restored.
    :param volume_level: The announcement volume level.
    :param at_unix_ms: The acked audible instant of the clip on this member.
    :param clip_seconds: The clip duration in seconds.
    :return: The schedule to track (None when there is nothing to change).
    """
    prev_volume = member.volume_level
    if prev_volume is None or prev_volume == volume_level:
        return None
    delay = max(0.0, at_unix_ms / 1000 - time.time())
    # The bump is biased INTO the clip: a receiver that plays out later than
    # the reported instant would otherwise get louder while the old music is
    # still sounding. The duck ramp (and the pre-announce chime) masks the
    # late bump; short clips cap the bias at their midpoint.
    bump_delay = delay + min(AIRPLAY_ANNOUNCE_VOLUME_BUMP_DELAY_MS / 1000, clip_seconds / 2)
    # Deterministic task ids: cancel_timer is the only way to release a tracked
    # timer that never fires, and reusing the ids also cancels stale timers of
    # a replaced announcement on the same member.
    task_ids = [
        f"airplay_announce_volume_bump_{member.player_id}",
        f"airplay_announce_volume_restore_{member.player_id}",
    ]
    member.mass.call_later(bump_delay, member.volume_set, volume_level, task_id=task_ids[0])
    member.mass.call_later(
        delay + clip_seconds + AIRPLAY_ANNOUNCE_VOLUME_RESTORE_PAD_MS / 1000,
        member.volume_set,
        prev_volume,
        task_id=task_ids[1],
    )
    return (member, prev_volume, task_ids)


async def _render_clip_file(render: AnnouncementRender, pcm_format: AudioFormat) -> str:
    """
    Render the announcement clip into a temp file of raw PCM in the given format.

    A ducked-silence tail is appended: the binary holds the music duck for the
    whole file, so the music stays ducked briefly past the announcement and the
    volume restore has a safe window to land in.

    The caller owns the file and removes it once every member is done with it.

    :param render: The (finished) announcement render to read.
    :param pcm_format: The raw PCM format the file must carry.
    """
    clip = bytearray()
    async for chunk in render.get_stream(pcm_format):
        clip.extend(chunk)
    # Wire sizes come from the content type: at 24-bit the stdin carrier is
    # s32le while bit_depth stays 24, so bit_depth-derived sizes are wrong.
    bytes_per_sample = {
        ContentType.PCM_S16LE: 2,
        ContentType.PCM_S24LE: 3,
        ContentType.PCM_S32LE: 4,
        ContentType.PCM_F32LE: 4,
    }.get(pcm_format.content_type, pcm_format.bit_depth // 8)
    trail_frames = int(pcm_format.sample_rate * AIRPLAY_ANNOUNCE_DUCK_TAIL_S)
    clip.extend(bytes(trail_frames * bytes_per_sample * pcm_format.channels))
    return await asyncio.to_thread(_write_clip_file, clip)


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
