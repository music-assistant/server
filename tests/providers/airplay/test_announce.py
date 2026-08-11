"""Unit tests for the native AirPlay announcement orchestration."""

import asyncio
import logging
import time
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import ContentType, PlaybackState
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.media_items import AudioFormat

from music_assistant.providers.airplay import announce
from music_assistant.providers.airplay.constants import (
    AIRPLAY_ANNOUNCE_AT_MARGIN_MS,
    AIRPLAY_ANNOUNCE_DUCK_DB,
    AIRPLAY_ANNOUNCE_FALLBACK_SPAN_MS,
    AIRPLAY_PCM_FORMAT,
)

ANNOUNCE_DATA = {
    "announcement_url": "http://ma.local/tts.mp3",
    "pre_announce": True,
    "pre_announce_url": "http://ma.local/chime.mp3",
    "announce_player_id": None,
}
HIRES_PCM_FORMAT = AudioFormat(content_type=ContentType.PCM_S32LE, sample_rate=48000, bit_depth=24)


def _make_render(duration: float = 1.5) -> MagicMock:
    """Build a mock announcement render that finished with the given duration."""
    render = MagicMock()
    render.duration = duration
    render.wait_finished = AsyncMock(return_value=duration)

    async def get_stream(_output_format: Any) -> AsyncGenerator[bytes]:
        yield b"\x00" * 64

    render.get_stream = get_stream
    return render


def _make_stream(
    pcm_format: AudioFormat = AIRPLAY_PCM_FORMAT,
    ack: tuple[int, int] | None = (0, 0),
) -> MagicMock:
    """Build a mock member stream whose announce arm resolves with the given ack."""
    stream = MagicMock()
    stream.running = True
    stream.connected = True
    stream.pcm_format = pcm_format
    stream.warm_lead_ms = 0
    stream.latency_lead_ms = 0
    stream.announce = AsyncMock(return_value=True)
    stream.wait_announce_started = AsyncMock(return_value=ack)
    stream.wait_announce_done = AsyncMock(return_value=True)
    return stream


def _make_player(player_id: str, stream: MagicMock | None = None) -> MagicMock:
    """Build a mock AirPlay player for announcement orchestration tests."""
    player = MagicMock()
    player.player_id = player_id
    player.display_name = player_id
    player.synced_to = None
    player.playback_state = PlaybackState.PLAYING
    player.volume_level = 30
    player.volume_set = AsyncMock()
    player.stream = stream
    player._lock = asyncio.Lock()
    player.logger = logging.getLogger("test.airplay.announce")
    renderer = player.mass.streams.announcement_renderer
    renderer.acquire = MagicMock(return_value=_make_render())
    renderer.release = AsyncMock()
    return player


def _make_playing_group(*streams: MagicMock) -> list[MagicMock]:
    """Build a playing session of one member per given stream; first one leads."""
    members = [_make_player(f"member_{i}", stream=stream) for i, stream in enumerate(streams)]
    session = MagicMock()
    session.sync_clients = members
    for member in members:
        member.stream.session = session
    return members


def _make_announcement() -> MagicMock:
    """Build the announcement PlayerMedia handed down by the player controller."""
    return MagicMock(custom_data=dict(ANNOUNCE_DATA))


def test_member_span_prefers_the_warm_lead() -> None:
    """A splice-timeline member's span is its warm lead, not the device lead."""
    stream = _make_stream()
    stream.warm_lead_ms = 600
    stream.latency_lead_ms = 1900

    assert announce._member_span_ms(stream) == 600


def test_member_span_uses_the_device_lead_without_a_warm_lead() -> None:
    """Without a warm lead, the reported device lead bounds the delivery head."""
    stream = _make_stream()
    stream.latency_lead_ms = 1900

    assert announce._member_span_ms(stream) == 1900


def test_member_span_falls_back_when_nothing_was_reported() -> None:
    """Both leads at 0 mean unreported: assume the binary's default playback lead."""
    assert announce._member_span_ms(_make_stream()) == AIRPLAY_ANNOUNCE_FALLBACK_SPAN_MS


def test_shared_instant_covers_the_slowest_member() -> None:
    """The shared instant sits past the LARGEST member span plus the fan-out margin."""
    fast = _make_stream()
    fast.warm_lead_ms = 600
    slow = _make_stream()
    slow.latency_lead_ms = 1900
    unreported = _make_stream()

    before_ms = int(time.time() * 1000)
    at_unix_ms = announce._shared_announce_instant([fast, slow, unreported])
    after_ms = time.time() * 1000

    # the unreported member's fallback span (2000) is the largest of the three
    expected_lead = AIRPLAY_ANNOUNCE_FALLBACK_SPAN_MS + AIRPLAY_ANNOUNCE_AT_MARGIN_MS
    assert before_ms + expected_lead <= at_unix_ms <= after_ms + expected_lead


@pytest.mark.asyncio
async def test_live_announcement_arms_every_member_at_one_shared_instant() -> None:
    """A playing session arms every member with the same instant, duck and clip file."""
    streams = [_make_stream(), _make_stream()]
    members = _make_playing_group(*streams)
    leader = members[0]

    with patch.object(announce, "_announce_with_session", new_callable=AsyncMock) as session_path:
        await announce.play_announcement(leader, _make_announcement(), None)

    session_path.assert_not_awaited()
    arms = [stream.announce.call_args for stream in streams]
    for arm in arms:
        assert arm is not None
    # one shared instant and the default duck for every member
    assert len({arm.args[1] for arm in arms}) == 1
    assert {arm.args[2] for arm in arms} == {AIRPLAY_ANNOUNCE_DUCK_DB}
    # both members share one stdin format, so they share one clip file
    assert len({arm.args[0] for arm in arms}) == 1
    assert arms[0].args[0].endswith(".pcm")
    for stream in streams:
        stream.wait_announce_done.assert_awaited_once()
    leader.mass.streams.announcement_renderer.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_live_announcement_renders_one_clip_per_distinct_format() -> None:
    """Members on different stdin formats each get a clip in exactly their format."""
    streams = [_make_stream(), _make_stream(pcm_format=HIRES_PCM_FORMAT)]
    members = _make_playing_group(*streams)

    with patch.object(announce, "_announce_with_session", new_callable=AsyncMock):
        await announce.play_announcement(members[0], _make_announcement(), None)

    clip_paths = {stream.announce.call_args.args[0] for stream in streams}
    assert len(clip_paths) == 2


@pytest.mark.asyncio
async def test_idle_player_takes_the_session_path() -> None:
    """Without live playback the announcement runs as its own stream session."""
    player = _make_player("solo")
    player.playback_state = PlaybackState.IDLE
    announcement = _make_announcement()

    with patch.object(announce, "_announce_with_session", new_callable=AsyncMock) as session_path:
        await announce.play_announcement(player, announcement, 40)

    session_path.assert_awaited_once()
    args = session_path.call_args.args
    assert args[0] is player
    assert args[1] is announcement
    assert args[3] == 1.5  # the render's exact duration bounds the session waits
    assert args[4] == 40


@pytest.mark.asyncio
async def test_live_path_falls_back_when_no_member_started() -> None:
    """When no member arms the clip (e.g. an outdated binary), the session path runs."""
    streams = [_make_stream(ack=None), _make_stream(ack=None)]
    members = _make_playing_group(*streams)

    with patch.object(announce, "_announce_with_session", new_callable=AsyncMock) as session_path:
        await announce.play_announcement(members[0], _make_announcement(), None)

    for stream in streams:
        stream.announce.assert_awaited_once()
        stream.wait_announce_done.assert_not_awaited()
    session_path.assert_awaited_once()


@pytest.mark.asyncio
async def test_partial_success_warns_and_does_not_fall_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One member playing the clip is a success; the members that did not are named."""
    streams = [_make_stream(), _make_stream(ack=None)]
    members = _make_playing_group(*streams)

    with (
        patch.object(announce, "_announce_with_session", new_callable=AsyncMock) as session_path,
        caplog.at_level(logging.WARNING),
    ):
        await announce.play_announcement(members[0], _make_announcement(), None)

    session_path.assert_not_awaited()
    assert "member_1" in caplog.text
    streams[0].wait_announce_done.assert_awaited_once()
    streams[1].wait_announce_done.assert_not_awaited()


@pytest.mark.asyncio
async def test_volume_is_scheduled_on_the_acked_instant() -> None:
    """The volume bump lands at the acked audible instant, the restore after the clip."""
    ack_at_unix_ms = int(time.time() * 1000) + 1000
    stream = _make_stream(ack=(ack_at_unix_ms, 800))
    members = _make_playing_group(stream)
    member = members[0]
    member.volume_level = 30

    with patch.object(announce, "_announce_with_session", new_callable=AsyncMock):
        await announce.play_announcement(member, _make_announcement(), 55)

    scheduled = member.mass.call_later.call_args_list
    assert len(scheduled) == 2
    bump, restore = scheduled
    assert bump.args[1:] == (member.volume_set, 55)
    assert restore.args[1:] == (member.volume_set, 30)
    # the bump delay derives from the acked instant (~1s out), the restore
    # follows it by the acked clip duration plus the fixed pad
    assert 0.5 <= bump.args[0] <= 1.05
    assert restore.args[0] - bump.args[0] == pytest.approx(0.8 + 0.5)


@pytest.mark.asyncio
async def test_no_volume_level_leaves_the_volume_alone() -> None:
    """Without an announcement volume nothing is scheduled on any member."""
    members = _make_playing_group(_make_stream())

    with patch.object(announce, "_announce_with_session", new_callable=AsyncMock):
        await announce.play_announcement(members[0], _make_announcement(), None)

    members[0].mass.call_later.assert_not_called()


@pytest.mark.asyncio
async def test_session_path_plays_the_clip_and_restores_the_volume() -> None:
    """The dedicated session serves the clip to the configured group at the given volume."""
    player = _make_player("solo")
    player.playback_state = PlaybackState.IDLE
    player.volume_level = 25
    player._get_sync_clients = MagicMock(return_value=[player])
    player._get_session_pcm_format = AsyncMock(return_value=AIRPLAY_PCM_FORMAT)
    announcement = _make_announcement()
    render = _make_render(duration=0.01)
    player.mass.streams.announcement_renderer.acquire = MagicMock(return_value=render)

    with (
        patch.object(announce, "AirPlayStreamSession") as session_cls,
        patch.object(announce, "AIRPLAY_ANNOUNCE_SESSION_DRAIN_S", 0.0),
    ):
        session = session_cls.return_value
        session.start = AsyncMock()
        session.stop = AsyncMock()
        session.start_time = 0.0
        await announce.play_announcement(player, announcement, 60)

    assert session_cls.call_args.args == (
        player.provider,
        [player],
        AIRPLAY_PCM_FORMAT,
        announcement,
    )
    session.start.assert_awaited_once()
    session.stop.assert_awaited_once()
    # announcement volume before the session, previous level restored after
    assert [call.args for call in player.volume_set.await_args_list] == [(60,), (25,)]


@pytest.mark.asyncio
async def test_session_path_stops_a_parked_session_first() -> None:
    """A parked (paused) session is stopped before the dedicated announcement session."""
    parked_stream = _make_stream()
    player = _make_player("solo", stream=parked_stream)
    player.playback_state = PlaybackState.PAUSED
    player._get_sync_clients = MagicMock(return_value=[player])
    player._get_session_pcm_format = AsyncMock(return_value=AIRPLAY_PCM_FORMAT)
    parked_session = parked_stream.session
    parked_session.stop = AsyncMock()
    render = _make_render(duration=0.01)
    player.mass.streams.announcement_renderer.acquire = MagicMock(return_value=render)

    with (
        patch.object(announce, "AirPlayStreamSession") as session_cls,
        patch.object(announce, "AIRPLAY_ANNOUNCE_SESSION_DRAIN_S", 0.0),
    ):
        session = session_cls.return_value
        session.start = AsyncMock()
        session.stop = AsyncMock()
        session.start_time = 0.0
        await announce.play_announcement(player, _make_announcement(), None)

    parked_session.stop.assert_awaited_once()
    assert player.stream is None
    session.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_announcement_without_data_is_refused() -> None:
    """An announcement without its announce data cannot be rendered."""
    player = _make_player("solo")

    with pytest.raises(PlayerCommandFailed, match="carries no announcement data"):
        await announce.play_announcement(player, MagicMock(custom_data=None), None)


@pytest.mark.asyncio
async def test_announcement_without_audio_plays_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A render that produced no audio is skipped, releasing the render regardless."""
    stream = _make_stream()
    members = _make_playing_group(stream)
    player = members[0]
    render = _make_render(duration=0.0)
    player.mass.streams.announcement_renderer.acquire = MagicMock(return_value=render)

    with caplog.at_level(logging.WARNING):
        await announce.play_announcement(player, _make_announcement(), None)

    stream.announce.assert_not_awaited()
    assert "produced no audio" in caplog.text
    player.mass.streams.announcement_renderer.release.assert_awaited_once()
