"""
Native AirPlay announcement orchestration.

The cliairplay binary mixes a raw-PCM clip over the outgoing music with the music
ducked underneath - no flush, no re-anchor, the group timeline stays untouched. This
module renders the shared announcement clip once per member stdin format, arms every
member of the live session at one shared audible instant and tracks the per-member
outcome. Whenever there is no live playback to mix into (idle or parked player, or no
member armed the clip), the announcement runs as a dedicated stream session instead,
leaving the player idle.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import PlayerCommandFailed

from .constants import (
    AIRPLAY_ANNOUNCE_AT_MARGIN_MS,
    AIRPLAY_ANNOUNCE_DONE_TIMEOUT_MS,
    AIRPLAY_ANNOUNCE_DUCK_DB,
    AIRPLAY_ANNOUNCE_FALLBACK_SPAN_MS,
    AIRPLAY_ANNOUNCE_SESSION_DRAIN_S,
    AIRPLAY_ANNOUNCE_STARTED_TIMEOUT_MS,
    AIRPLAY_ANNOUNCE_VOLUME_RESTORE_PAD_MS,
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
# the timers that apply/restore the announcement volume around the clip.
_VolumeSchedule = tuple["AirPlayPlayer", int, list[asyncio.TimerHandle]]


async def play_announcement(
    player: AirPlayPlayer, announcement: PlayerMedia, volume_level: int | None
) -> None:
    """
    Play an announcement on the player (and the members it leads).

    A live playing session mixes the clip over the music without interrupting
    it; a player that is idle or parked - or whose members could not mix the
    clip - plays the announcement as a dedicated stream session and ends idle.

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
    finally:
        await renderer.release(render)


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
        no live playback or no member armed it, so the caller falls back to a
        dedicated announcement session.
    """
    clip_files: dict[str, str] = {}
    volume_schedules: list[_VolumeSchedule] = []
    try:
        # The dispatch decision and the arming run under the player lock - the
        # same lock play_media holds to mutate the session - while the
        # multi-second clip waits below run outside it, so provider-internal
        # paths (DACP feedback, member removal on stream loss) are not blocked
        # for the clip's duration. User-driven playback commands are already
        # serialized against the whole announcement by the controller's
        # per-player playback lock.
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
            at_unix_ms = _shared_announce_instant(streams.values())
            delivered = await asyncio.gather(
                *[
                    stream.announce(
                        clip_files[_format_key(stream.pcm_format)],
                        at_unix_ms,
                        AIRPLAY_ANNOUNCE_DUCK_DB,
                    )
                    for stream in streams.values()
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
            player.logger.info(
                "No member of %s armed the announcement; playing it as its own session",
                player.display_name,
            )
            return False
        if volume_level is not None:
            for member in members:
                if (ack := started.get(member.player_id)) is None:
                    continue
                ack_at_unix_ms, ack_duration_ms = ack
                if schedule := _schedule_member_volume(
                    member,
                    volume_level,
                    ack_at_unix_ms or at_unix_ms,
                    ack_duration_ms / 1000 if ack_duration_ms else duration,
                ):
                    volume_schedules.append(schedule)
        done_results = await asyncio.gather(
            *[
                streams[member_id].wait_announce_done(
                    max(0.0, (ack_at or at_unix_ms) / 1000 - time.time())
                    + (ack_duration / 1000 if ack_duration else duration)
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
        return True
    except BaseException:
        # The scheduled volume changes belong to an announcement that is no
        # longer being tracked: cancel them and restore the previous levels
        # right away (a restore of an unchanged level is a harmless re-send).
        for member, prev_volume, handles in volume_schedules:
            for handle in handles:
                handle.cancel()
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

    Any existing (parked) session is stopped first. The player ends idle - the
    same end state the generic announcement flow leaves a non-playing player in;
    the queue keeps its resume position server-side.

    :param player: The player the announcement targets.
    :param announcement: The announcement media, owning the session's metadata.
    :param render: The (finished) announcement render to play.
    :param duration: Exact clip duration in seconds.
    :param volume_level: Optional volume level for the announcement.
    """
    provider = cast("AirPlayProvider", player.provider)
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
        return []
    return [
        member
        for member in stream.session.sync_clients
        if member.stream is not None and member.stream.running and member.stream.connected
    ]


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
    handles = [
        member.mass.call_later(delay, member.volume_set, volume_level),
        member.mass.call_later(
            delay + clip_seconds + AIRPLAY_ANNOUNCE_VOLUME_RESTORE_PAD_MS / 1000,
            member.volume_set,
            prev_volume,
        ),
    ]
    return (member, prev_volume, handles)


async def _render_clip_file(render: AnnouncementRender, pcm_format: AudioFormat) -> str:
    """
    Render the announcement clip into a temp file of raw PCM in the given format.

    The caller owns the file and removes it once every member is done with it.

    :param render: The (finished) announcement render to read.
    :param pcm_format: The raw PCM format the file must carry.
    """
    chunks: list[bytes] = []
    async for chunk in render.get_stream(pcm_format):
        chunks.append(chunk)
    return await asyncio.to_thread(_write_clip_file, b"".join(chunks))


def _write_clip_file(data: bytes) -> str:
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
    return f"{pcm_format.content_type.value}_{pcm_format.sample_rate}_{pcm_format.bit_depth}"
