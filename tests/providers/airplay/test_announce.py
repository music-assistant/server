"""Unit tests for the native AirPlay announcement orchestration."""

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, Iterator
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


@pytest.fixture(autouse=True)
def _zero_restore_pad() -> Iterator[None]:
    """
    Zero the audible-end pad for every test.

    The live path holds its return (and the volume restore) until the clip's
    audible end plus this pad; the streams below ack a long-past instant, so
    only the pad would otherwise add real wall-clock time to every test. The
    audible-end test overrides it with its own value.
    """
    with patch.object(announce, "AIRPLAY_ANNOUNCE_VOLUME_RESTORE_PAD_MS", 0):
        yield


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
    ack: tuple[int, int] | None = (1, 1),
) -> MagicMock:
    """
    Build a mock member stream whose announce arm resolves with the given ack.

    The default ack reports a long-past audible instant, so the audible-end
    hold at the end of the live path resolves without waiting.
    """
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
    # the leader path wraps its orchestration in a mass-created task
    player.mass.create_task = MagicMock(
        side_effect=lambda coro, *_args, **_kwargs: asyncio.get_running_loop().create_task(coro)
    )
    player.provider._announce_tasks = {}
    player.provider.bridge_manager.get_bridge = MagicMock(return_value=None)
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
async def test_no_member_arming_fails_without_killing_the_music() -> None:
    """
    Live members that never arm the clip fail the announcement, not the music.

    An outdated cliairplay silently ignores the arm command; a fallback session
    would stop the user's playing session over a clip that could not be mixed.
    """
    streams = [_make_stream(ack=None), _make_stream(ack=None)]
    members = _make_playing_group(*streams)
    live_session = streams[0].session
    live_session.stop = AsyncMock()

    with (
        patch.object(announce, "_announce_with_session", new_callable=AsyncMock) as session_path,
        pytest.raises(PlayerCommandFailed, match="may not support announcements"),
    ):
        await announce.play_announcement(members[0], _make_announcement(), None)

    for stream in streams:
        stream.announce.assert_awaited_once()
        stream.wait_announce_done.assert_not_awaited()
    session_path.assert_not_awaited()
    live_session.stop.assert_not_awaited()
    members[0].mass.streams.announcement_renderer.release.assert_awaited_once()


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
    ack_at_unix_ms = int(time.time() * 1000) + 200
    # the acked duration includes the 1s ducked-silence tail: 2s of content
    stream = _make_stream(ack=(ack_at_unix_ms, 3000))
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
    # the bump derives from the acked instant (~0.2s out) plus the 0.3s
    # into-the-clip bias; the restore follows the CONTENT length (the acked
    # duration minus the silence tail) plus the (zeroed) pad, so it lands
    # inside the ducked cushion - 1.7s after the bump here
    assert 0.2 < bump.args[0] <= 0.5
    assert restore.args[0] - bump.args[0] == pytest.approx(1.7)


def test_member_duck_compensates_the_volume_bump() -> None:
    """
    The duck deepens by exactly the device-volume bump so the music never rises.

    38 -> 61 volume points is +6.9 dB on the AirPlay dB scale; the -18 dB duck
    becomes -24.9 dB so the music's perceived level stays at the configured
    duck depth (the regression heard as "the music was not ducked"). A bump
    down shallows it symmetrically, and without a bump the base duck applies.
    """
    member = _make_player("m")
    member.volume_level = 38
    assert announce._member_duck_db(member, 61) == pytest.approx(-24.9)
    assert announce._member_duck_db(member, 18) == pytest.approx(-12.0)
    assert announce._member_duck_db(member, None) == pytest.approx(-18.0)
    assert announce._member_duck_db(member, 38) == pytest.approx(-18.0)
    # extreme bumps clamp to the binary's usable range (never boost the music)
    member.volume_level = 0
    assert announce._member_duck_db(member, 100) == pytest.approx(-48.0)
    assert announce._member_duck_db(member, 0) == pytest.approx(-18.0)
    member.volume_level = 100
    assert announce._member_duck_db(member, 0) == pytest.approx(0.0)


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
    # the player ends idle without still showing media (like player.stop())
    assert player._attr_current_media is None
    player.update_state.assert_called()


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
async def test_group_forwarded_member_calls_coalesce_onto_the_leader() -> None:
    """The controller's group-forward arms every member exactly once, via the leader."""
    streams = [_make_stream(), _make_stream(), _make_stream()]
    members = _make_playing_group(*streams)
    leader, member_1, member_2 = members
    provider = leader.provider
    render = _make_render()
    for member in members:
        member.provider = provider
        if member is not leader:
            member.synced_to = leader.player_id
        member.mass.streams.announcement_renderer.acquire = MagicMock(return_value=render)
    announcement = _make_announcement()

    await asyncio.gather(
        announce.play_announcement(leader, announcement, None),
        announce.play_announcement(member_1, announcement, None),
        announce.play_announcement(member_2, announcement, None),
    )

    # one orchestration armed every member; the member calls awaited it
    # instead of arming their own stream a second time
    for stream in streams:
        stream.announce.assert_awaited_once()
        stream.wait_announce_done.assert_awaited_once()
    assert provider._announce_tasks == {}
    for member in members:
        member.mass.streams.announcement_renderer.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_member_without_group_announcement_arms_itself() -> None:
    """A direct announcement to one synced member still mixes on just that member."""
    stream = _make_stream()
    members = _make_playing_group(stream)
    member = members[0]
    member.synced_to = "some_leader"

    with patch.object(announce, "_LEADER_TASK_POLL_INTERVAL_S", 0):
        await announce.play_announcement(member, _make_announcement(), None)

    stream.announce.assert_awaited_once()
    # a member's own run is never published for others to join
    assert member.provider._announce_tasks == {}


@pytest.mark.asyncio
async def test_synced_member_without_live_playback_is_refused() -> None:
    """
    A parked group member must not announce by tearing down the shared session.

    Its stream belongs to the leader's parked session; stopping that for a
    single-member announcement would silence every room in the group.
    """
    parked_stream = _make_stream()
    members = _make_playing_group(parked_stream)
    member = members[0]
    member.synced_to = "some_leader"
    member.playback_state = PlaybackState.PAUSED
    parked_session = parked_stream.session
    parked_session.stop = AsyncMock()

    with (
        patch.object(announce, "_LEADER_TASK_POLL_INTERVAL_S", 0),
        pytest.raises(PlayerCommandFailed, match="without live playback"),
    ):
        await announce.play_announcement(member, _make_announcement(), None)

    parked_session.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_return_holds_until_the_audible_end() -> None:
    """
    The call returns only once the clip is audibly over, not when it is mixed.

    announce_done reports MIX completion at the delivery head - ahead of
    audibility - and the caller re-mutes muted players the moment this returns.
    """
    now_unix_ms = int(time.time() * 1000)
    stream = _make_stream(ack=(now_unix_ms + 250, 100))
    members = _make_playing_group(stream)

    with patch.object(announce, "AIRPLAY_ANNOUNCE_VOLUME_RESTORE_PAD_MS", 100):
        started = time.monotonic()
        await announce.play_announcement(members[0], _make_announcement(), None)
        elapsed = time.monotonic() - started

    # audible end = acked instant + clip duration (0.35s out) + the 0.1s pad
    assert elapsed >= 0.4


@pytest.mark.asyncio
async def test_bridged_player_with_live_stream_arms_itself() -> None:
    """A Sendspin-bridged player mixes the clip into the bridge-owned stream."""
    stream = _make_stream()
    stream.session = None
    player = _make_player("bridged", stream=stream)
    bridge = MagicMock(owns_airplay_stream=True)
    player.provider.bridge_manager.get_bridge = MagicMock(return_value=bridge)

    await announce.play_announcement(player, _make_announcement(), None)

    stream.announce.assert_awaited_once()
    stream.wait_announce_done.assert_awaited_once()


@pytest.mark.asyncio
async def test_bridge_configured_player_mixes_over_its_own_session() -> None:
    """
    A bridge that is merely configured never blocks the live mix.

    The regression this pins: a player with a Sendspin bridge set up but
    playing its own (session-backed) AirPlay stream mixes the clip like any
    unbridged player - the idle bridge is a bystander.
    """
    (member,) = _make_playing_group(_make_stream())
    bridge = MagicMock(owns_airplay_stream=False)
    member.provider.bridge_manager.get_bridge = MagicMock(return_value=bridge)

    await announce.play_announcement(member, _make_announcement(), None)

    member.stream.announce.assert_awaited_once()
    member.stream.wait_announce_done.assert_awaited_once()


@pytest.mark.asyncio
async def test_bridge_streaming_player_never_runs_a_session_fallback() -> None:
    """A player whose bridge owns the live stream fails instead of seizing the device."""
    player = _make_player("bridged")
    player.playback_state = PlaybackState.IDLE
    bridge = MagicMock(owns_airplay_stream=True)
    player.provider.bridge_manager.get_bridge = MagicMock(return_value=bridge)

    with (
        patch.object(announce, "AirPlayStreamSession") as session_cls,
        pytest.raises(PlayerCommandFailed, match="Sendspin bridge"),
    ):
        await announce.play_announcement(player, _make_announcement(), None)

    session_cls.assert_not_called()


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
