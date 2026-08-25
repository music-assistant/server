"""Unit tests for the native AirPlay announcement orchestration."""

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from music_assistant_models.enums import ContentType
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.media_items import AudioFormat

from music_assistant.providers.airplay import announce
from music_assistant.providers.airplay.constants import (
    AIRPLAY_ANNOUNCE_AT_MARGIN_MS,
    AIRPLAY_ANNOUNCE_DUCK_DB,
    AIRPLAY_ANNOUNCE_DUCK_LEAD_S,
    AIRPLAY_ANNOUNCE_DUCK_TAIL_S,
    AIRPLAY_ANNOUNCE_FALLBACK_SPAN_MS,
    AIRPLAY_ANNOUNCE_VOLUME_BUMP_DELAY_MS,
    AIRPLAY_PCM_FORMAT,
    AIRPLAY_VOLUME_ECHO_GRACE_S,
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
        # non-silent, so a clip is distinguishable from the silence around it
        yield b"\xff" * 64

    render.get_stream = get_stream
    return render


def _make_stream(
    pcm_format: AudioFormat = AIRPLAY_PCM_FORMAT,
    ack: tuple[int, int] | None = (1, 1),
) -> MagicMock:
    """
    Build a mock member stream whose announce arm resolves with the given ack.

    The default ack reports a long-past audible instant, so every wait the
    announcement holds for resolves without spending wall-clock time.
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
    player.state.active_group = None
    player.state.volume_level = 30
    player.protocol_parent_id = None
    player.has_live_audio = stream is not None
    player.stream = stream
    player._lock = asyncio.Lock()
    player.logger = logging.getLogger("test.airplay.announce")
    player.mass.create_task = MagicMock(
        side_effect=lambda coro, *_args, **_kwargs: asyncio.get_running_loop().create_task(coro)
    )
    player.mass.players.cmd_volume_set = AsyncMock()
    player.provider._announce_plans = {}
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


@contextmanager
def _timeline(player: MagicMock) -> Iterator[list[tuple[str, float]]]:
    """
    Record the announcement's volume timeline without spending its waits.

    Yields the ordered log of the instants (unix ms) the announcement holds for and
    the volume levels it sets, so a test can assert the ORDER of both volume changes
    against the clip's own instants without reading the wall clock.

    :param player: The player the announcement targets; its controller is the one
        every volume command travels through.
    """
    events: list[tuple[str, float]] = []

    async def hold_until(unix_ms: float) -> None:
        events.append(("hold", unix_ms))

    player.mass.players.cmd_volume_set = AsyncMock(
        side_effect=lambda _player_id, level: events.append(("volume", level))
    )
    with patch.object(announce, "_hold_until", hold_until):
        yield events


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

    await announce.play_announcement(leader, _make_announcement(), None)

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

    await announce.play_announcement(members[0], _make_announcement(), None)

    clip_paths = {stream.announce.call_args.args[0] for stream in streams}
    assert len(clip_paths) == 2


@pytest.mark.asyncio
async def test_clip_file_wraps_the_audio_in_ducked_silence() -> None:
    """The clip file is lead-in silence, then the announcement audio, then tail silence."""
    clip_path = await announce._render_clip_file(_make_render(), HIRES_PCM_FORMAT)
    try:
        clip = Path(clip_path).read_bytes()
    finally:
        Path(clip_path).unlink()

    # the silence is sized on the content type: this format carries 24 bit over an
    # s32le wire, so a bit_depth-derived size would come out a quarter short
    frame_bytes = 4 * HIRES_PCM_FORMAT.channels
    lead_bytes = int(HIRES_PCM_FORMAT.sample_rate * AIRPLAY_ANNOUNCE_DUCK_LEAD_S) * frame_bytes
    tail_bytes = int(HIRES_PCM_FORMAT.sample_rate * AIRPLAY_ANNOUNCE_DUCK_TAIL_S) * frame_bytes
    assert clip == bytes(lead_bytes) + b"\xff" * 64 + bytes(tail_bytes)


@pytest.mark.asyncio
async def test_player_without_live_audio_is_refused() -> None:
    """Without audio to mix into the announcement is refused, releasing the render."""
    player = _make_player("solo")

    with pytest.raises(PlayerCommandFailed, match="stopped playing"):
        await announce.play_announcement(player, _make_announcement(), 40)

    player.mass.streams.announcement_renderer.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_member_arming_fails_without_killing_the_music() -> None:
    """
    Live members that never arm the clip fail the announcement, not the music.

    An outdated cliairplay silently ignores the arm command: nothing is said, but
    the session every member is playing from is left exactly as it was.
    """
    streams = [_make_stream(ack=None), _make_stream(ack=None)]
    members = _make_playing_group(*streams)
    live_session = streams[0].session
    live_session.stop = AsyncMock()

    with pytest.raises(PlayerCommandFailed, match="may not support announcements"):
        await announce.play_announcement(members[0], _make_announcement(), None)

    for stream in streams:
        stream.announce.assert_awaited_once()
        stream.wait_announce_done.assert_not_awaited()
    live_session.stop.assert_not_awaited()
    members[0].mass.streams.announcement_renderer.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_partial_success_warns_about_the_members_that_stayed_silent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One member playing the clip is a success; the members that did not are named."""
    streams = [_make_stream(), _make_stream(ack=None)]
    members = _make_playing_group(*streams)

    with caplog.at_level(logging.WARNING):
        await announce.play_announcement(members[0], _make_announcement(), None)

    assert "member_1" in caplog.text
    streams[0].wait_announce_done.assert_awaited_once()
    streams[1].wait_announce_done.assert_not_awaited()


@pytest.mark.asyncio
async def test_volume_moves_inside_the_ducked_silence() -> None:
    """
    The volume is raised in the ducked lead-in and restored in the ducked tail.

    Both changes are timed on the acked start of the clip FILE: the raise lands
    after that start but before the announcement audio at the end of the lead-in,
    and the restore after that audio but before the file ends - so neither is ever
    heard on the music, which is ducked for the whole file.
    """
    duration = 1.5
    file_ms = int((AIRPLAY_ANNOUNCE_DUCK_LEAD_S + duration + AIRPLAY_ANNOUNCE_DUCK_TAIL_S) * 1000)
    ack_at_unix_ms = 1_700_000_000_000
    (member,) = _make_playing_group(_make_stream(ack=(ack_at_unix_ms, file_ms)))

    with _timeline(member) as events:
        await announce.play_announcement(member, _make_announcement(), 55)

    assert [name for name, _ in events] == ["hold", "volume", "hold", "volume", "hold"]
    assert events[1][1] == 55
    assert events[3][1] == 30  # the level the volume target carried before
    raised_at, restored_at = events[0][1], events[2][1]
    assert raised_at == ack_at_unix_ms + AIRPLAY_ANNOUNCE_VOLUME_BUMP_DELAY_MS
    # inside the lead-in: the music is already ducked, nothing is being said yet
    assert ack_at_unix_ms < raised_at < ack_at_unix_ms + AIRPLAY_ANNOUNCE_DUCK_LEAD_S * 1000
    # inside the tail: the announcement is over, the clip file is not
    assert (
        ack_at_unix_ms + (AIRPLAY_ANNOUNCE_DUCK_LEAD_S + duration) * 1000
        <= restored_at
        < ack_at_unix_ms + file_ms
    )


@pytest.mark.asyncio
async def test_short_clip_is_fully_covered_by_the_announcement_volume() -> None:
    """
    A clip shorter than the ducked lead-in still plays at the announcement volume.

    The raise is timed on the lead-in, never on the clip's own length, so a short
    announcement is at the announcement level from its first word (the regression
    heard as a short announcement not playing at all).
    """
    duration = 0.4
    file_ms = int((AIRPLAY_ANNOUNCE_DUCK_LEAD_S + duration + AIRPLAY_ANNOUNCE_DUCK_TAIL_S) * 1000)
    ack_at_unix_ms = 1_700_000_000_000
    (member,) = _make_playing_group(_make_stream(ack=(ack_at_unix_ms, file_ms)))
    member.mass.streams.announcement_renderer.acquire = MagicMock(
        return_value=_make_render(duration)
    )

    with _timeline(member) as events:
        await announce.play_announcement(member, _make_announcement(), 55)

    assert [name for name, _ in events] == ["hold", "volume", "hold", "volume", "hold"]
    raised_at, restored_at = events[0][1], events[2][1]
    # the raise still lands before a word is said, the restore only after the last
    assert raised_at < ack_at_unix_ms + AIRPLAY_ANNOUNCE_DUCK_LEAD_S * 1000
    assert restored_at >= ack_at_unix_ms + (AIRPLAY_ANNOUNCE_DUCK_LEAD_S + duration) * 1000


@pytest.mark.asyncio
async def test_announcement_volume_lands_on_the_protocol_parent() -> None:
    """
    The announcement volume is set on the control that owns the member's output.

    An AirPlay child of a native player must not write the receiver's own level:
    the command travels through the controller to the parent, on the parent's scale.
    """
    (member,) = _make_playing_group(_make_stream())
    parent = _make_player("parent")
    parent.state.volume_level = 20
    parent.mass = member.mass
    member.protocol_parent_id = "parent"
    member.mass.players.get_player = MagicMock(return_value=parent)

    with _timeline(member):
        await announce.play_announcement(member, _make_announcement(), 55)

    # the parent's own level is bumped and restored; the child is never addressed
    assert member.mass.players.cmd_volume_set.await_args_list == [
        call("parent", 55),
        call("parent", 20),
    ]


@pytest.mark.asyncio
async def test_armed_members_ignore_their_own_volume_echoes() -> None:
    """
    Every armed member ignores the device's volume reports until its clip is over.

    The receiver echoes each level it is handed back over DACP; an echo read as the
    user reaching for the volume would be written straight back to the device.
    """
    # taken WITH the wall clock the ack is built from, so the window below is
    # bracketed exactly however slow the run itself is
    now_ms = int(time.time() * 1000)
    file_ms = 3000
    (member,) = _make_playing_group(_make_stream(ack=(now_ms + 300, file_ms)))

    with _timeline(member):
        await announce.play_announcement(member, _make_announcement(), 55)
    elapsed = time.time() - now_ms / 1000

    audible_end_s = (300 + file_ms) / 1000
    window = member.suppress_volume_reports.call_args.args[0]
    assert audible_end_s + AIRPLAY_VOLUME_ECHO_GRACE_S - elapsed <= window
    assert window <= audible_end_s + AIRPLAY_VOLUME_ECHO_GRACE_S


def test_member_duck_compensates_the_volume_bump() -> None:
    """
    The duck deepens by exactly the device-volume bump so the music never rises.

    38 -> 61 volume points is +6.9 dB on the AirPlay dB scale; the -18 dB duck
    becomes -24.9 dB so the music's perceived level stays at the configured
    duck depth (the regression heard as "the music was not ducked"). A bump
    down shallows it symmetrically, and without a bump the base duck applies.
    """
    member = _make_player("m")
    member.state.volume_level = 38
    assert announce._member_duck_db(member, 61) == pytest.approx(-24.9)
    assert announce._member_duck_db(member, 18) == pytest.approx(-12.0)
    assert announce._member_duck_db(member, None) == pytest.approx(-18.0)
    assert announce._member_duck_db(member, 38) == pytest.approx(-18.0)
    # extreme bumps clamp to the binary's usable range (never boost the music)
    member.state.volume_level = 0
    assert announce._member_duck_db(member, 100) == pytest.approx(-48.0)
    assert announce._member_duck_db(member, 0) == pytest.approx(-18.0)
    member.state.volume_level = 100
    assert announce._member_duck_db(member, 0) == pytest.approx(0.0)


def test_member_duck_compensates_the_volume_target_bump() -> None:
    """The compensation follows the level of the control the bump is applied to."""
    member = _make_player("child")
    member.state.volume_level = 100
    parent = _make_player("parent")
    parent.state.volume_level = 38
    member.protocol_parent_id = "parent"
    member.mass.players.get_player = MagicMock(return_value=parent)

    assert announce._member_duck_db(member, 61) == pytest.approx(-24.9)


def test_volume_target_is_the_protocol_parent() -> None:
    """The volume of a member with a protocol parent is owned by that parent."""
    member = _make_player("child")
    parent = _make_player("parent")
    member.protocol_parent_id = "parent"
    member.mass.players.get_player = MagicMock(return_value=parent)

    assert announce._volume_target(member) is parent
    # a member without one owns its own volume
    member.protocol_parent_id = None
    assert announce._volume_target(member) is member


@pytest.mark.asyncio
async def test_no_volume_level_leaves_the_volume_alone() -> None:
    """Without an announcement volume no level is touched on any member."""
    members = _make_playing_group(_make_stream())

    await announce.play_announcement(members[0], _make_announcement(), None)

    members[0].mass.players.cmd_volume_set.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_entity_fanout_arms_each_member_at_one_shared_instant() -> None:
    """
    A group-entity fan-out arms each member once, at one shared instant.

    The controller forwards a group-entity announcement per member; each call
    arms only its OWN member, and all of them share one audible instant through
    the provider's plan registry, so every room renders in sync.
    """
    streams = [_make_stream(), _make_stream(), _make_stream()]
    members = _make_playing_group(*streams)
    leader, member_1, member_2 = members
    provider = leader.provider
    render = _make_render()
    for member in members:
        member.provider = provider
        member.state.active_group = "syncgroup_1"
        if member is not leader:
            member.synced_to = leader.player_id
        member.mass.streams.announcement_renderer.acquire = MagicMock(return_value=render)
    # the second member reports a span past the fallback the others assume:
    # the shared instant must clear the LARGEST one for every sibling arm
    largest_span_ms = AIRPLAY_ANNOUNCE_FALLBACK_SPAN_MS + 1000
    streams[1].latency_lead_ms = largest_span_ms
    announcement = _make_announcement()

    before_ms = int(time.time() * 1000)
    await asyncio.gather(
        announce.play_announcement(leader, announcement, None),
        announce.play_announcement(member_1, announcement, None),
        announce.play_announcement(member_2, announcement, None),
    )
    after_ms = time.time() * 1000

    instants = set()
    for stream in streams:
        stream.announce.assert_awaited_once()
        stream.wait_announce_done.assert_awaited_once()
        instants.add(stream.announce.await_args.args[1])
    assert len(instants) == 1
    # the instant is computed inside the call, so the clock reads either side
    # of it bracket the lead however long the fan-out itself takes
    expected_lead = largest_span_ms + AIRPLAY_ANNOUNCE_AT_MARGIN_MS
    assert before_ms + expected_lead <= next(iter(instants)) <= after_ms + expected_lead


@pytest.mark.asyncio
async def test_group_entity_session_leader_announces_alone() -> None:
    """
    A group entity's session leader addressed individually announces alone.

    The entity itself is the whole-group handle, so leading the underlying
    session does not widen an individual announcement.
    """
    streams = [_make_stream(), _make_stream()]
    members = _make_playing_group(*streams)
    leader = members[0]
    leader.state.active_group = "syncgroup_1"
    members[1].synced_to = leader.player_id

    await announce.play_announcement(leader, _make_announcement(), None)

    streams[0].announce.assert_awaited_once()
    streams[1].announce.assert_not_awaited()


@pytest.mark.asyncio
async def test_protocol_child_reads_group_ownership_from_its_parent() -> None:
    """
    A protocol child leading a syncgroup's session still announces alone.

    Protocol players never carry active_group themselves - the model keeps the
    group state on the device player they render for - so the whole-group
    handle is found through the protocol parent (the regression heard as an
    individual announcement playing on the whole syncgroup).
    """
    streams = [_make_stream(), _make_stream()]
    members = _make_playing_group(*streams)
    leader = members[0]
    leader.protocol_parent_id = "milo_parent"
    parent = MagicMock()
    parent.state.active_group = "syncgroup_1"
    leader.mass.players.get_player = MagicMock(return_value=parent)
    members[1].synced_to = leader.player_id

    await announce.play_announcement(leader, _make_announcement(), None)

    streams[0].announce.assert_awaited_once()
    streams[1].announce.assert_not_awaited()


@pytest.mark.asyncio
async def test_member_announced_directly_arms_itself() -> None:
    """A direct announcement to one synced member mixes on just that member."""
    stream = _make_stream()
    members = _make_playing_group(stream)
    member = members[0]
    member.synced_to = "some_leader"

    await announce.play_announcement(member, _make_announcement(), None)

    stream.announce.assert_awaited_once()


@pytest.mark.asyncio
async def test_synced_member_without_live_playback_is_refused() -> None:
    """
    A parked group member has nothing to mix into, so its announcement is refused.

    Its stream belongs to the leader's parked session, which is left untouched:
    stopping it for a single-member announcement would silence the whole group.
    """
    parked_stream = _make_stream()
    members = _make_playing_group(parked_stream)
    member = members[0]
    member.synced_to = "some_leader"
    member.has_live_audio = False
    parked_session = parked_stream.session
    parked_session.stop = AsyncMock()

    with pytest.raises(PlayerCommandFailed, match="stopped playing"):
        await announce.play_announcement(member, _make_announcement(), None)

    parked_stream.announce.assert_not_awaited()
    parked_session.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_return_holds_until_the_audible_end() -> None:
    """
    The call returns only once the clip is audibly over, not when it is mixed.

    announce_done reports MIX completion at the delivery head - ahead of
    audibility - and the caller re-mutes muted players the moment this returns.
    """
    # taken WITH the wall clock the ack is built from, so the hold is measured
    # from that same instant however slow the setup below runs
    started = time.monotonic()
    now_unix_ms = int(time.time() * 1000)
    stream = _make_stream(ack=(now_unix_ms + 250, 100))
    members = _make_playing_group(stream)

    with patch.object(announce, "AIRPLAY_ANNOUNCE_VOLUME_RESTORE_PAD_MS", 100):
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
async def test_bridged_player_without_a_stream_to_mix_into_is_refused() -> None:
    """
    A bridged player Sendspin is not streaming through is refused, not seized.

    Neither its own session nor the bridge is rendering audio here, so there is
    nothing to mix the clip into and the device is left to whatever owns it.
    """
    stream = _make_stream()
    stream.session = None
    player = _make_player("bridged", stream=stream)
    bridge = MagicMock(owns_airplay_stream=False)
    player.provider.bridge_manager.get_bridge = MagicMock(return_value=bridge)

    with pytest.raises(PlayerCommandFailed, match="stopped playing"):
        await announce.play_announcement(player, _make_announcement(), None)

    stream.announce.assert_not_awaited()


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
