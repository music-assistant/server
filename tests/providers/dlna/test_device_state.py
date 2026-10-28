"""Tests for the state the DLNA player reads from its device."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from async_upnp_client.profiles.dlna import TransportState
from music_assistant_models.enums import PlaybackState, PlayerFeature

from music_assistant.providers.dlna.player import DLNAPlayer
from tests.common import MockProvider

REPORTED_AT = datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC)
STALE_POSITION = 120.0
STALE_REPORTED_AT = 1000.0


def _mock_device(**device_state: Any) -> MagicMock:
    """
    Return a mocked device that fully reports its state.

    :param device_state: Attributes to override on the device.
    """
    device = MagicMock()
    device.profile_device.available = True
    device.name = "Living Room Renderer"
    device.volume_level = 0.5
    device.is_volume_muted = False
    device.transport_state = TransportState.PLAYING
    device.current_track_uri = "http://192.168.1.10/stream.mp3"
    device.media_title = "Test Title"
    device.media_artist = "Test Artist"
    device.media_album_name = "Test Album"
    device.media_image_url = "http://192.168.1.10/cover.jpg"
    device.media_duration = 240
    device.media_position = 42
    device.media_position_updated_at = REPORTED_AT
    for name, value in device_state.items():
        setattr(device, name, value)
    return device


def _player(device: MagicMock) -> DLNAPlayer:
    """
    Return a player for the given device that already knows a position.

    :param device: The mocked device to attach to the player.
    """
    provider = MockProvider("dlna", instance_id="dlna_test")
    provider.mass.streams.base_url = "http://192.168.1.2:8097"

    player = DLNAPlayer(
        provider,  # type: ignore[arg-type]
        "uuid:dlna-player",
        "http://192.168.1.10/description.xml",
        device=device,
    )
    player._attr_elapsed_time = STALE_POSITION
    player._attr_elapsed_time_last_updated = STALE_REPORTED_AT
    return player


async def _updated_player(**device_state: Any) -> DLNAPlayer:
    """
    Run a state update on a player that already knows a position, and return it.

    :param device_state: Attributes to override on the fully reporting mocked device.
    """
    player = _player(_mock_device(**device_state))
    await player.set_dynamic_attributes()
    return player


async def test_zero_position_replaces_the_previous_one() -> None:
    """A device restarting a track reports 0, which must not be read as 'unknown'."""
    player = await _updated_player(media_position=0)

    assert player.elapsed_time == 0.0
    assert player.elapsed_time_last_updated == REPORTED_AT.timestamp()


async def test_known_position_is_applied() -> None:
    """A position reported by the device is adopted together with its timestamp."""
    player = await _updated_player(media_position=42)

    assert player.elapsed_time == 42.0
    assert player.elapsed_time_last_updated == REPORTED_AT.timestamp()


async def test_missing_position_keeps_the_previous_one() -> None:
    """A device that reports no position at all leaves the known position alone."""
    player = await _updated_player(media_position=None)

    assert player.elapsed_time == STALE_POSITION
    assert player.elapsed_time_last_updated == STALE_REPORTED_AT


async def test_position_from_before_a_resume_is_not_extrapolated() -> None:
    """
    A position reported from before a resume is anchored at the resume.

    A device does not re-stamp a position that did not change, so the timestamp it
    reports right after a resume still dates from before the pause.
    """
    paused_at = datetime.now(UTC) - timedelta(minutes=10)
    device = _mock_device(
        transport_state=TransportState.PAUSED_PLAYBACK,
        media_position=200,
        media_position_updated_at=paused_at,
    )
    player = _player(device)
    await player.set_dynamic_attributes()

    device.transport_state = TransportState.PLAYING
    resumed_at = time.time()
    await player.set_dynamic_attributes()

    assert player.elapsed_time == 200.0
    assert player.elapsed_time_last_updated is not None
    assert player.elapsed_time_last_updated >= resumed_at


class _Clock:
    """Stand-in for the time module that the test moves forward by hand."""

    def __init__(self, now: float) -> None:
        self.now = now

    def time(self) -> float:
        return self.now


def _mock_clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    """
    Replace the clock of the player and of its base class by one moved forward by hand.

    :param monkeypatch: The pytest monkeypatch fixture.
    """
    clock = _Clock(REPORTED_AT.timestamp() + 600)
    monkeypatch.setattr("music_assistant.providers.dlna.player.time", clock)
    monkeypatch.setattr("music_assistant.models.player.time", clock)
    return clock


def _stamp(clock: _Clock) -> datetime:
    """
    Return the current time of the mocked clock as a device position timestamp.

    :param clock: The mocked clock.
    """
    return datetime.fromtimestamp(clock.now, UTC)


async def _start(
    monkeypatch: pytest.MonkeyPatch, media_position: float | None
) -> tuple[DLNAPlayer, MagicMock, _Clock]:
    """
    Start playback on a mocked clock the way play_media does, and return the player.

    :param monkeypatch: The pytest monkeypatch fixture.
    :param media_position: The position the device reports.
    """
    clock = _mock_clock(monkeypatch)
    device = _mock_device(
        transport_state=TransportState.STOPPED,
        media_position=media_position,
        media_position_updated_at=REPORTED_AT,
        can_pause=True,
        async_pause=AsyncMock(),
        async_seek_rel_time=AsyncMock(),
        async_next=AsyncMock(),
        async_play=AsyncMock(),
        async_update=AsyncMock(),
    )
    player = _player(device)
    player.get_config_value = MagicMock(return_value=False)  # type: ignore[method-assign]
    player.mass.call_later = MagicMock()  # type: ignore[method-assign]
    await player.set_dynamic_attributes()
    # what play_media assumes until the device reports otherwise
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_elapsed_time = 0
    player._attr_elapsed_time_last_updated = clock.now
    device.transport_state = TransportState.PLAYING
    if media_position is not None:
        device.media_position_updated_at = _stamp(clock)
    await player.set_dynamic_attributes()
    return player, device, clock


async def _set_state(player: DLNAPlayer, device: MagicMock, state: TransportState) -> None:
    """
    Let the device report a new transport state, which polls it first like the event does.

    :param player: The player of the device.
    :param device: The mocked device.
    :param state: The transport state the device reports.
    """
    device.transport_state = state
    await player.poll()
    await player.set_dynamic_attributes()


@pytest.mark.parametrize("media_position", [0, None])
async def test_resume_continues_on_a_device_that_does_not_advance_its_position(
    monkeypatch: pytest.MonkeyPatch, media_position: int | None
) -> None:
    """
    A resume continues from the pause on a device whose position does not move.

    Some LinkPlay devices report 00:00:00 for as long as they play, others no position at
    all, so what the device reports after a resume is no better than before the pause.
    """
    player, device, clock = await _start(monkeypatch, media_position)
    clock.now += 12
    await _set_state(player, device, TransportState.PAUSED_PLAYBACK)
    clock.now += 90
    await _set_state(player, device, TransportState.PLAYING)

    assert player.elapsed_time == 12.0
    assert player.elapsed_time_last_updated == clock.now

    clock.now += 16
    await _set_state(player, device, TransportState.PAUSED_PLAYBACK)
    clock.now += 30
    await _set_state(player, device, TransportState.PLAYING)
    clock.now += 2

    assert player.corrected_elapsed_time == 30.0


async def test_placeholder_position_restamped_during_the_pause_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An event that repeats 00:00:00 during the pause re-stamps it, which changes nothing."""
    player, device, clock = await _start(monkeypatch, 0)
    clock.now += 12
    await _set_state(player, device, TransportState.PAUSED_PLAYBACK)
    clock.now += 40
    device.media_position_updated_at = _stamp(clock)
    await player.set_dynamic_attributes()
    clock.now += 50
    await _set_state(player, device, TransportState.PLAYING)

    assert player.elapsed_time == 12.0
    assert player.elapsed_time_last_updated == clock.now


async def test_placeholder_position_first_reported_during_the_pause_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device paused before it reported any position, which then reports 00:00:00."""
    player, device, clock = await _start(monkeypatch, None)
    clock.now += 12
    await _set_state(player, device, TransportState.PAUSED_PLAYBACK)
    clock.now += 40
    device.media_position = 0
    device.media_position_updated_at = _stamp(clock)
    await player.set_dynamic_attributes()
    clock.now += 50
    await _set_state(player, device, TransportState.PLAYING)

    assert player.elapsed_time == 12.0
    assert player.elapsed_time_last_updated == clock.now


async def test_placeholder_position_of_a_player_found_while_playing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device found playing is not taken to have played since its placeholder's timestamp."""
    clock = _mock_clock(monkeypatch)
    device = _mock_device(
        media_position=0,
        media_position_updated_at=REPORTED_AT - timedelta(hours=1),
        async_update=AsyncMock(),
    )
    player = _player(device)
    await player.set_dynamic_attributes()
    clock.now += 12
    await _set_state(player, device, TransportState.PAUSED_PLAYBACK)

    assert player.elapsed_time == 12.0

    clock.now += 60
    await _set_state(player, device, TransportState.PLAYING)

    assert player.elapsed_time == 12.0
    assert player.elapsed_time_last_updated == clock.now


async def test_pause_noticed_late_keeps_the_position_of_the_pause_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pause that is only noticed at the next poll still holds where it was paused."""
    player, device, clock = await _start(monkeypatch, 0)
    clock.now += 12
    await player.pause()
    clock.now += 25
    await _set_state(player, device, TransportState.PAUSED_PLAYBACK)

    device.async_pause.assert_awaited_once()
    assert player.elapsed_time == 12.0


async def test_pause_seen_before_the_position_is_read_keeps_the_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A pause that is processed before the position is read still keeps MA's clock.

    The pause can reach the player while the poll is still running, before the device's
    00:00:00 is re-read, so only a later report of the position shows it does not advance.
    """
    player, device, clock = await _start(monkeypatch, 0)
    clock.now += 12
    device.transport_state = TransportState.PAUSED_PLAYBACK
    await player.set_dynamic_attributes()
    clock.now += 1
    await _set_state(player, device, TransportState.PAUSED_PLAYBACK)

    assert player.elapsed_time == 12.0

    clock.now += 30
    await _set_state(player, device, TransportState.PLAYING)
    clock.now += 2

    assert player.corrected_elapsed_time == 14.0


@pytest.mark.parametrize(
    ("command", "args", "expected"), [("seek", (83,), 83.0), ("next_track", (), 0.0)]
)
async def test_command_while_paused_moves_the_position(
    monkeypatch: pytest.MonkeyPatch, command: str, args: tuple[int, ...], expected: float
) -> None:
    """A seek or skip while paused on a device without a position resumes from there."""
    player, device, clock = await _start(monkeypatch, 0)
    clock.now += 12
    await _set_state(player, device, TransportState.PAUSED_PLAYBACK)
    await getattr(player, command)(*args)
    await player.set_dynamic_attributes()
    clock.now += 30
    await _set_state(player, device, TransportState.PLAYING)

    assert player.elapsed_time == expected
    assert player.elapsed_time_last_updated == clock.now


async def test_track_changed_on_the_device_while_paused_starts_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another track started from outside MA (e.g. a vendor app) starts from 0."""
    player, device, clock = await _start(monkeypatch, 0)
    clock.now += 12
    await _set_state(player, device, TransportState.PAUSED_PLAYBACK)
    clock.now += 30
    device.current_track_uri = "http://192.168.1.10/other.mp3"
    await player.set_dynamic_attributes()

    assert player.elapsed_time == 0.0

    clock.now += 30
    await _set_state(player, device, TransportState.PLAYING)

    assert player.elapsed_time == 0.0
    assert player.elapsed_time_last_updated == clock.now


async def test_resume_continues_from_the_position_the_device_reported_at_the_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device that reports its position at the pause resumes from exactly that position."""
    player, device, clock = await _start(monkeypatch, 0)
    clock.now += 30
    device.media_position = 29
    device.media_position_updated_at = _stamp(clock)
    await player.set_dynamic_attributes()
    clock.now += 15
    device.media_position = 44
    device.media_position_updated_at = _stamp(clock)
    await _set_state(player, device, TransportState.PAUSED_PLAYBACK)
    clock.now += 90
    await _set_state(player, device, TransportState.PLAYING)

    assert player.elapsed_time == 44.0
    assert player.elapsed_time_last_updated == clock.now


async def test_resume_adopts_a_position_the_device_reported_since_the_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device that reports a new position while paused (e.g. after a seek) is followed."""
    player, device, clock = await _start(monkeypatch, 0)
    clock.now += 2
    device.media_position = 2
    device.media_position_updated_at = _stamp(clock)
    await _set_state(player, device, TransportState.PAUSED_PLAYBACK)
    device.media_position = 83
    device.media_position_updated_at = _stamp(clock)
    await player.set_dynamic_attributes()
    clock.now += 90
    await _set_state(player, device, TransportState.PLAYING)

    assert player.elapsed_time == 83.0
    assert player.elapsed_time_last_updated == clock.now


async def test_device_that_falls_behind_once_is_followed_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device whose position stalled for a while (e.g. buffering) is followed once it moves."""
    player, device, clock = await _start(monkeypatch, 0)
    clock.now += 10
    device.media_position_updated_at = _stamp(clock)
    await player.set_dynamic_attributes()
    clock.now += 20
    device.media_position = 21
    device.media_position_updated_at = _stamp(clock)
    await player.set_dynamic_attributes()

    assert player.elapsed_time == 21.0
    assert player.elapsed_time_last_updated == clock.now


async def test_update_between_polls_does_not_flag_a_normal_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An update for anything but the position (e.g. the volume) shows the last poll's position."""
    player, device, clock = await _start(monkeypatch, 0)
    clock.now += 30
    device.media_position = 30
    device.media_position_updated_at = _stamp(clock)
    await player.set_dynamic_attributes()
    clock.now += 20
    device.volume_level = 0.3
    await player.set_dynamic_attributes()
    clock.now += 10
    device.media_position = 60
    device.media_position_updated_at = _stamp(clock)
    await _set_state(player, device, TransportState.PAUSED_PLAYBACK)
    clock.now += 30
    await _set_state(player, device, TransportState.PLAYING)

    assert player.elapsed_time == 60.0
    assert player.elapsed_time_last_updated == clock.now


async def test_normal_device_found_while_playing_keeps_its_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device found playing at 300 s keeps that position through an update between polls."""
    clock = _mock_clock(monkeypatch)
    # read by the poll a moment before the player first takes it in
    device = _mock_device(media_position=300, media_position_updated_at=_stamp(clock))
    clock.now += 2
    player = _player(device)
    await player.set_dynamic_attributes()
    clock.now += 12
    device.volume_level = 0.3
    await player.set_dynamic_attributes()

    assert player.corrected_elapsed_time == 314.0


async def test_pause_command_followed_by_play_is_not_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pause the device never reported, undone by a play, does not set a later pause."""
    player, device, clock = await _start(monkeypatch, 0)
    clock.now += 12
    await player.pause()
    await player.play()
    clock.now += 20
    await _set_state(player, device, TransportState.PAUSED_PLAYBACK)

    assert player.elapsed_time == 32.0


async def test_normal_device_that_reports_late_after_play_media_is_followed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device that was slow to start is not taken to be stuck at its first position."""
    player, device, clock = await _start(monkeypatch, None)
    clock.now += 9
    device.media_position = 1
    device.media_position_updated_at = _stamp(clock)
    await _set_state(player, device, TransportState.PLAYING)

    assert player.corrected_elapsed_time == 1.0

    clock.now += 10
    device.volume_level = 0.3
    await player.set_dynamic_attributes()

    assert player.corrected_elapsed_time == 11.0


async def test_pause_command_the_device_ignored_is_not_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pause command after which the device still plays does not set a later pause."""
    player, device, clock = await _start(monkeypatch, 0)
    clock.now += 12
    await player.pause()
    clock.now += 1
    await _set_state(player, device, TransportState.PLAYING)
    clock.now += 27
    await _set_state(player, device, TransportState.PAUSED_PLAYBACK)

    assert player.elapsed_time == 40.0


async def test_seek_while_paused_is_kept_until_a_normal_device_resumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device that only reports the position of a seek while paused when it resumes."""
    player, device, clock = await _start(monkeypatch, 0)
    clock.now += 12
    device.media_position = 12
    device.media_position_updated_at = _stamp(clock)
    await _set_state(player, device, TransportState.PAUSED_PLAYBACK)
    await player.seek(83)
    await player.set_dynamic_attributes()
    clock.now += 5
    await _set_state(player, device, TransportState.PAUSED_PLAYBACK)

    assert player.elapsed_time == 83.0
    assert player._stuck_position is None

    clock.now += 30
    device.media_position = 83
    device.media_position_updated_at = _stamp(clock)
    await _set_state(player, device, TransportState.PLAYING)
    clock.now += 2

    assert player.corrected_elapsed_time == 85.0


@pytest.mark.parametrize("media_position", [0, None])
async def test_stop_starts_the_position_over(
    monkeypatch: pytest.MonkeyPatch, media_position: int | None
) -> None:
    """Playback that starts over after a stop starts from 0, or the device's position, again."""
    player, device, clock = await _start(monkeypatch, media_position)
    clock.now += 12
    await _set_state(player, device, TransportState.PAUSED_PLAYBACK)
    await _set_state(player, device, TransportState.STOPPED)
    clock.now += 5
    await _set_state(player, device, TransportState.PLAYING)

    assert player.elapsed_time == 0.0
    assert player.elapsed_time_last_updated == clock.now


async def test_pause_noticed_late_follows_the_position_the_device_paused_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A device that reports where it paused keeps that position until it resumes.

    MA's clock ran on until the pause was noticed, which must not make the device's
    position look stuck once it is reported again.
    """
    player, device, clock = await _start(monkeypatch, 0)
    clock.now += 70
    device.media_position = 70
    device.media_position_updated_at = _stamp(clock)
    await player.set_dynamic_attributes()
    # paused at 100 s, noticed 30 s later
    clock.now += 60
    device.media_position = 100
    device.media_position_updated_at = _stamp(clock)
    await _set_state(player, device, TransportState.PAUSED_PLAYBACK)

    assert player.elapsed_time == 100.0

    clock.now += 10
    await _set_state(player, device, TransportState.PAUSED_PLAYBACK)

    assert player.elapsed_time == 100.0
    assert player._stuck_position is None

    clock.now += 30
    await _set_state(player, device, TransportState.PLAYING)

    assert player.elapsed_time == 100.0
    assert player.elapsed_time_last_updated == clock.now


async def test_placeholder_position_restamped_while_playing_is_recognised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device that repeats 00:00:00 every few seconds while playing still pauses in place."""
    player, device, clock = await _start(monkeypatch, 0)
    for _ in range(3):
        clock.now += 4
        device.media_position_updated_at = _stamp(clock)
        await player.set_dynamic_attributes()
    await _set_state(player, device, TransportState.PAUSED_PLAYBACK)

    assert player._stuck_position == 0
    assert player.elapsed_time == 12.0


@pytest.mark.parametrize("pause", [False, True])
async def test_normal_device_that_stalls_while_restamping_is_followed(
    monkeypatch: pytest.MonkeyPatch, pause: bool
) -> None:
    """A device whose position advanced and then stalls (e.g. buffering) stays where it is."""
    player, device, clock = await _start(monkeypatch, 0)
    for position in (10, 20, 30):
        clock.now += 10
        device.media_position = position
        device.media_position_updated_at = _stamp(clock)
        await player.set_dynamic_attributes()
    # the device re-sends 30 with a fresh timestamp while it buffers
    for _ in range(3):
        clock.now += 5
        device.media_position_updated_at = _stamp(clock)
        await _set_state(player, device, TransportState.PLAYING)

    assert player.corrected_elapsed_time == 30.0
    assert player._stuck_position is None

    if pause:
        await _set_state(player, device, TransportState.PAUSED_PLAYBACK)

        assert player.elapsed_time == 30.0

        clock.now += 20
        await _set_state(player, device, TransportState.PLAYING)
    clock.now += 3
    device.media_position = 33
    device.media_position_updated_at = _stamp(clock)
    await _set_state(player, device, TransportState.PLAYING)

    assert player.corrected_elapsed_time == 33.0


async def test_placeholder_position_restamped_after_a_track_change_is_recognised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device that advanced on its last track and re-sends 00:00:00 on the next one."""
    player, device, clock = await _start(monkeypatch, 0)
    clock.now += 10
    device.media_position = 10
    device.media_position_updated_at = _stamp(clock)
    await player.set_dynamic_attributes()
    device.current_track_uri = "http://192.168.1.10/other.mp3"
    device.media_position = 0
    await player.set_dynamic_attributes()
    for _ in range(3):
        clock.now += 4
        device.media_position_updated_at = _stamp(clock)
        await player.set_dynamic_attributes()
    await _set_state(player, device, TransportState.PAUSED_PLAYBACK)

    assert player._stuck_position == 0
    assert player.elapsed_time == 12.0


async def test_position_of_a_player_found_while_playing_keeps_its_own_anchor() -> None:
    """Without having seen playback start, the timestamp the device reports is all there is."""
    player = await _updated_player(media_position=200, media_position_updated_at=REPORTED_AT)

    assert player.elapsed_time == 200.0
    assert player.elapsed_time_last_updated == REPORTED_AT.timestamp()


async def test_optimistic_playing_state_does_not_hide_the_start_of_playback() -> None:
    """
    Playback starting is tracked from what the device reports, not from what MA assumes.

    A play command marks the player as playing before the device confirms it, which must
    not be mistaken for the player having been playing all along.
    """
    device = _mock_device(
        transport_state=TransportState.STOPPED,
        media_position=200,
        media_position_updated_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    player = _player(device)
    await player.set_dynamic_attributes()

    player._attr_playback_state = PlaybackState.PLAYING
    device.transport_state = TransportState.PLAYING
    started_at = time.time()
    await player.set_dynamic_attributes()

    assert player.elapsed_time_last_updated is not None
    assert player.elapsed_time_last_updated >= started_at


async def test_transport_state_event_polls_before_reading_the_position() -> None:
    """A player that starts playing reports the position of the track it just started."""
    device = _mock_device(
        transport_state=TransportState.STOPPED,
        media_position=200,
        media_position_updated_at=datetime.now(UTC) - timedelta(minutes=10),
    )

    async def _async_update(**_kwargs: Any) -> None:
        """Answer the poll a round trip later with the position of the new track."""
        await asyncio.sleep(0)
        device.transport_state = TransportState.PLAYING
        device.media_position = 0
        device.media_position_updated_at = datetime.now(UTC)

    device.async_update = _async_update

    player = _player(device)
    tasks: list[asyncio.Task[Any]] = []

    def _create_task(target: Coroutine[Any, Any, Any], **_kwargs: Any) -> asyncio.Task[Any]:
        # eager, like the real helper: the stale read happened because the update task
        # ran up to its first suspension before the poll task got its answer
        task: asyncio.Task[Any] = asyncio.Task(
            target, loop=asyncio.get_running_loop(), eager_start=True
        )
        tasks.append(task)
        return task

    player.mass.create_task = _create_task  # type: ignore[assignment]

    service = MagicMock()
    service.service_id = "urn:upnp-org:serviceId:AVTransport"
    state_variable = MagicMock()
    state_variable.name = "TransportState"
    state_variable.value = TransportState.PLAYING

    started_at = time.time()
    player._handle_event(service, [state_variable])
    await asyncio.gather(*tasks)

    assert player.elapsed_time == 0.0
    assert player.elapsed_time_last_updated is not None
    assert player.elapsed_time_last_updated >= started_at


@pytest.mark.parametrize(("reported", "expected"), [(0.5, 50), (0.0, 0), (1.0, 100)])
async def test_volume_level_is_scaled_to_percent(reported: float, expected: int) -> None:
    """The device reports volume as a 0..1 fraction, the player as a percentage."""
    player = await _updated_player(volume_level=reported)

    assert player.volume_level == expected


async def test_unknown_volume_level_stays_unknown() -> None:
    """A device that does not report its volume must not read as volume 0."""
    player = await _updated_player(volume_level=None)

    assert player.volume_level is None


@pytest.mark.parametrize("reported", [True, False])
async def test_mute_state_is_applied(reported: bool) -> None:
    """A mute state reported by the device is adopted as-is."""
    player = await _updated_player(is_volume_muted=reported)

    assert player.volume_muted is reported


async def test_unknown_mute_state_stays_unknown() -> None:
    """A device that does not report its mute state must not read as unmuted."""
    player = await _updated_player(is_volume_muted=None)

    assert player.volume_muted is None


@pytest.mark.parametrize("capable", [True, False])
async def test_spotify_connect_is_exposed_as_controllable_source(capable: bool) -> None:
    """A Spotify Connect session on the device offers the transport controls the device has."""
    player = await _updated_player(
        current_track_uri="spotify:track:4uLU6hMCjMI75M1A2tKUQC",
        has_pause=True,
        has_next=capable,
        has_previous=capable,
        has_seek_rel_time=capable,
        can_seek_rel_time=capable,
    )

    assert player.active_source == "spotify"
    assert len(player.source_list) == 1
    source = player.source_list[0]
    assert source.id == "spotify"
    assert source.passive
    assert source.can_play_pause
    assert source.can_next_previous is capable
    assert source.can_seek is capable


async def test_spotify_seek_follows_live_transport_actions() -> None:
    """Seek isn't offered while the device leaves Seek out of its current transport actions."""
    player = await _updated_player(
        current_track_uri="spotify:track:4uLU6hMCjMI75M1A2tKUQC",
        has_seek_rel_time=True,
        can_seek_rel_time=False,
    )

    assert player.source_list[0].can_seek is False


async def test_spotify_source_is_dropped_when_the_session_ends() -> None:
    """Once the device plays something else, the Spotify source is no longer listed."""
    device = _mock_device(current_track_uri="spotify:track:4uLU6hMCjMI75M1A2tKUQC")
    player = _player(device)
    await player.set_dynamic_attributes()
    device.current_track_uri = "http://192.168.1.2:8097/flow/stream.flac"
    await player.set_dynamic_attributes()

    assert player.active_source is None
    assert player.source_list == []


async def test_transport_features_follow_device_capabilities() -> None:
    """Next/previous and seek are advertised only when the device has those actions."""
    capable = _player(_mock_device(has_next=True, has_previous=True, has_seek_rel_time=True))
    capable.set_static_attributes()
    limited = _player(_mock_device(has_next=True, has_previous=False, has_seek_rel_time=False))
    limited.set_static_attributes()

    assert {PlayerFeature.NEXT_PREVIOUS, PlayerFeature.SEEK} <= capable.supported_features
    assert PlayerFeature.NEXT_PREVIOUS not in limited.supported_features
    assert PlayerFeature.SEEK not in limited.supported_features


async def test_seek_sends_relative_time() -> None:
    """Seeking sends the position to the device as a relative time."""
    device = _mock_device(async_seek_rel_time=AsyncMock())
    player = _player(device)

    await player.seek(83)

    device.async_seek_rel_time.assert_awaited_once_with(timedelta(seconds=83))


@pytest.mark.parametrize(
    ("command", "action_name"), [("next_track", "Next"), ("previous_track", "Previous")]
)
async def test_skip_is_sent_despite_stale_transport_actions(command: str, action_name: str) -> None:
    """A device whose CurrentTransportActions omits Next/Previous still gets the raw action."""
    action = MagicMock(async_call=AsyncMock())
    device = _mock_device(
        can_next=False, can_previous=False, async_next=AsyncMock(), async_previous=AsyncMock()
    )
    device._action = MagicMock(
        side_effect=lambda _service, name: action if name == action_name else None
    )
    player = _player(device)

    await getattr(player, command)()

    device._action.assert_any_call("AVT", action_name)
    action.async_call.assert_awaited_once_with(InstanceID=0)


@pytest.mark.parametrize(
    ("command", "args"), [("seek", (83,)), ("next_track", ()), ("previous_track", ())]
)
async def test_transport_command_reads_the_new_state_back(
    command: str, args: tuple[int, ...]
) -> None:
    """A device that sends no event for a skip or seek still reports its new track and position."""
    device = _mock_device(
        can_next=True,
        can_previous=True,
        async_seek_rel_time=AsyncMock(),
        async_next=AsyncMock(),
        async_previous=AsyncMock(),
    )

    async def _async_update(**_kwargs: Any) -> None:
        """Answer the poll with the state the command left the device in."""
        device.media_title = "New Title"
        device.media_position = 83
        device.media_position_updated_at = datetime.now(UTC)

    device.async_update = _async_update
    player = _player(device)
    await player.set_dynamic_attributes()
    scheduled: list[tuple[float, Any]] = []

    def _call_later(delay: float, target: Any, *_args: Any, **_kwargs: Any) -> None:
        # the base player also schedules synchronous media callbacks, which are not polls
        if inspect.iscoroutinefunction(target):
            scheduled.append((delay, target))

    player.mass.call_later = _call_later  # type: ignore[method-assign,assignment]

    await getattr(player, command)(*args)
    assert scheduled
    for delay, target in scheduled:
        assert delay < player.poll_interval
        await target()

    assert player.current_media is not None
    assert player.current_media.title == "New Title"
    assert player.elapsed_time == 83.0


async def test_pending_refresh_does_not_reconnect_an_unloaded_player() -> None:
    """A refresh timer that fires after the device was disconnected leaves it disconnected."""
    player = _player(_mock_device())
    player.device = None
    player._device_connect = AsyncMock()  # type: ignore[method-assign]

    await player._refresh_state()

    player._device_connect.assert_not_awaited()
    assert player.device is None
