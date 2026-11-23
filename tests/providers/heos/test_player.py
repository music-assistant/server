"""Tests for the HEOS protocol."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.config_entries import PlayerConfig
from music_assistant_models.enums import MediaType, PlaybackState, ProtocolRole
from music_assistant_models.player import PlayerSource
from pyheos import PlayState as HeosPlayState
from pyheos import const as heos_const

from music_assistant.models.player import Player, PlayerMedia
from music_assistant.providers.heos.player import HeosPlayer
from tests.common import MockProvider, attach_mock, mock_player_mass

HIRES_SAMPLE_RATES = [
    (sr, bd) for sr in (44100, 48000, 88200, 96000, 176400, 192000) for bd in (16, 24)
]


def _url_stream_now_playing() -> MagicMock:
    """HEOS now-playing for a generic URL stream it cannot parse (MA's own source)."""
    now_playing = MagicMock()
    now_playing.source_id = heos_const.MUSIC_SOURCE_LOCAL_MUSIC
    now_playing.type = "song"
    now_playing.song = "Url Stream"
    now_playing.album = "Url Stream"
    now_playing.artist = "Url Stream"
    now_playing.image_url = ""
    now_playing.media_id = "1"
    now_playing.album_id = "1"
    now_playing.current_position = None
    now_playing.current_position_updated = None
    now_playing.duration = None
    return now_playing


def _external_now_playing(current_position: int | None, duration: int | None = 245000) -> MagicMock:
    """HEOS now-playing for an external source, with millisecond progress values."""
    now_playing = _url_stream_now_playing()
    now_playing.source_id = 9999
    now_playing.song = "External Track"
    now_playing.current_position = current_position
    now_playing.current_position_updated = datetime(2026, 1, 1, tzinfo=UTC)
    now_playing.duration = duration
    return now_playing


def _make_player(now_playing: MagicMock) -> HeosPlayer:
    """Build a HeosPlayer backed by a mocked device/controller."""
    provider = MagicMock()
    provider._heos_queue = MagicMock()
    device = MagicMock()
    device.player_id = "1"
    device.name = "Kitchen"
    device.heos = MagicMock()
    device.state = HeosPlayState.PLAY
    device.now_playing_media = now_playing
    return HeosPlayer(provider, device)


def test_url_stream_now_playing_preserves_ma_media_while_ma_controls() -> None:
    """
    Preserve MA's current_media against a HEOS "Url Stream" report.

    HEOS cannot parse metadata from the generic URL stream MA serves, so it
    reports ``Url Stream``. When MA controls playback that report must be
    ignored (even if ``active_source`` is momentarily stale from the
    ``play_url`` race) so MA's own, correct metadata is preserved.

    See https://github.com/music-assistant/support/issues/5614
    """
    protocol = _make_player(_url_stream_now_playing())
    correct_media = PlayerMedia(
        uri="http://ma/stream/foo",
        media_type=MediaType.TRACK,
        title="Real Track",
        artist="Real Artist",
        album="Real Album",
    )
    protocol._attr_current_media = correct_media
    protocol._ma_controls_playback = True
    # Stale: a previous external source is still the recorded active source.
    protocol._attr_active_source = "external_radio"

    protocol._update_player_current_media()

    # MA's correct media is preserved; the bogus "Url Stream" did not clobber it.
    assert protocol._attr_current_media is correct_media
    assert protocol._attr_current_media.title == "Real Track"
    # Playback control was not latched away to the phantom external source.
    assert protocol._ma_controls_playback is True


def test_external_source_now_playing_still_updates_media() -> None:
    """A genuinely external (non-local) source must still update current_media."""
    now_playing = _url_stream_now_playing()
    now_playing.source_id = 9999  # an external source, not MA's local stream
    now_playing.song = "External Track"
    now_playing.artist = "External Artist"
    protocol = _make_player(now_playing)
    protocol._attr_current_media = PlayerMedia(
        uri="http://ma/stream/foo", media_type=MediaType.TRACK, title="MA Track"
    )
    protocol._ma_controls_playback = True
    protocol._attr_active_source = protocol.protocol_id

    protocol._update_player_current_media()

    assert protocol._attr_current_media.title == "External Track"


def test_external_source_is_ignored_during_ma_playback_transition() -> None:
    """Suppress external-source media updates while MA playback is starting."""
    protocol = _make_player(_external_now_playing(None))
    protocol._ma_playback_starting = True
    protocol._ma_controls_playback = True
    current_media = PlayerMedia(
        uri="http://ma/stream/foo", media_type=MediaType.TRACK, title="MA Track"
    )
    protocol._attr_current_media = current_media

    protocol._update_player_current_media()

    assert protocol._ma_playback_starting is True
    assert protocol._ma_controls_playback is True
    assert protocol._attr_current_media.title == "MA Track"

    protocol._finish_ma_playback_transition()

    assert protocol._attr_current_media.title == "External Track"
    assert protocol._ma_controls_playback is False


async def test_new_ma_playback_cancels_previous_transition() -> None:
    """Keep a new MA playback transition protected from the previous timer."""
    protocol = _make_player(_url_stream_now_playing())
    mass = cast("MagicMock", protocol.mass)
    device = cast("MagicMock", protocol._device)
    transition_pending = True

    def cancel_timer(task_id: str) -> None:
        nonlocal transition_pending
        assert task_id == f"heos_playback_transition_{protocol.protocol_id}"
        transition_pending = False

    async def play_url(_: str) -> None:
        if transition_pending:
            protocol._finish_ma_playback_transition()

    mass.cancel_timer.side_effect = cancel_timer
    mass.streams.resolve_stream_url = AsyncMock(return_value="http://ma/stream/new")
    device.play_url = AsyncMock(side_effect=play_url)
    protocol._ma_playback_starting = True

    await protocol.play_media(
        PlayerMedia(
            uri="library://track/2",
            media_type=MediaType.TRACK,
            title="New MA Track",
        )
    )

    assert protocol._ma_playback_starting is True
    mass.cancel_timer.assert_called_once_with(f"heos_playback_transition_{protocol.protocol_id}")


async def test_select_source_ends_ma_playback_transition() -> None:
    """Allow an explicit source change to update HEOS media immediately."""
    protocol = _make_player(_url_stream_now_playing())
    mass = cast("MagicMock", protocol.mass)
    device = cast("MagicMock", protocol._device)
    protocol._ma_playback_starting = True

    async def play_input_source(_: str) -> None:
        assert protocol._ma_playback_starting is False

    device.play_input_source = AsyncMock(side_effect=play_input_source)

    await protocol.select_source("aux_in_1")

    mass.cancel_timer.assert_called_once_with(f"heos_playback_transition_{protocol.protocol_id}")


async def test_detach_cancels_playback_transition() -> None:
    """Cancel the playback transition when the protocol is detached."""
    protocol = _make_player(_url_stream_now_playing())
    mass = cast("MagicMock", protocol.mass)
    protocol._ma_playback_starting = True

    await protocol.on_attach(MagicMock())
    await protocol.on_detach()

    mass.cancel_timer.assert_any_call(f"heos_playback_transition_{protocol.protocol_id}")
    assert protocol._ma_playback_starting is False


async def _start_ma_playback(
    protocol: HeosPlayer, state: HeosPlayState = HeosPlayState.PLAY
) -> dict[str, Callable[[], None]]:
    """Start MA playback on a player in the given state and return its pending timers."""
    mass = cast("MagicMock", protocol.mass)
    device = cast("MagicMock", protocol._device)
    device.state = state
    timers: dict[str, Callable[[], None]] = {}

    def call_later(
        _delay: float, target: Callable[..., None], *args: Any, task_id: str, **kwargs: Any
    ) -> None:
        timers[task_id] = partial(target, *args, **kwargs)

    mass.call_later.side_effect = call_later
    mass.cancel_timer.side_effect = lambda task_id: timers.pop(task_id, None)
    mass.streams.resolve_stream_url = AsyncMock(return_value="http://ma/stream/next")
    device.play_url = AsyncMock()
    protocol.get_config_value = MagicMock(return_value=5)  # type: ignore[method-assign]
    protocol.set_dynamic_attributes()
    if state == HeosPlayState.PLAY:
        assert protocol.playback_state == PlaybackState.PLAYING

    await protocol.play_media(
        PlayerMedia(uri="library://track/2", media_type=MediaType.TRACK, title="Next")
    )
    return timers


async def _set_device_state(protocol: HeosPlayer, state: HeosPlayState) -> None:
    """Feed a HEOS state change event into the protocol."""
    cast("MagicMock", protocol._device).state = state
    await protocol._player_event_received(heos_const.EVENT_PLAYER_STATE_CHANGED)


async def test_transient_stop_while_restarting_ma_playback_keeps_playing() -> None:
    """
    Keep reporting playing while HEOS restarts on a new MA stream.

    HEOS reports stop for several seconds when switching to the next track's stream.
    Publishing that as idle drops the group's queue and can dissolve a sync group.

    See https://github.com/music-assistant/support/issues/6373
    """
    protocol = _make_player(_url_stream_now_playing())
    timers = await _start_ma_playback(protocol)
    transition_timer = f"heos_playback_transition_{protocol.protocol_id}"

    await _set_device_state(protocol, HeosPlayState.STOP)
    assert protocol.playback_state == PlaybackState.PLAYING

    # transition timeout passes while the device still restarts
    timers.pop(transition_timer)()
    assert protocol.playback_state == PlaybackState.PLAYING
    assert transition_timer in timers

    await _set_device_state(protocol, HeosPlayState.PLAY)
    timers.pop(transition_timer)()
    assert protocol.playback_state == PlaybackState.PLAYING
    assert protocol._ma_playback_starting is False


async def test_queue_cleanup_waits_for_device_to_play_during_restart() -> None:
    """Schedule the HEOS queue cleanup only once the device plays the new stream."""
    protocol = _make_player(_url_stream_now_playing())
    timers = await _start_ma_playback(protocol)
    cleanup_timer = f"heos_queue_cleanup_timer_{protocol.protocol_id}"

    await _set_device_state(protocol, HeosPlayState.STOP)
    await protocol._player_event_received(heos_const.EVENT_PLAYER_QUEUE_CHANGED)
    assert cleanup_timer not in timers

    await _set_device_state(protocol, HeosPlayState.PLAY)
    assert cleanup_timer in timers


async def test_ma_playback_that_never_starts_becomes_idle() -> None:
    """Report idle once HEOS keeps reporting stop past the playback transition."""
    protocol = _make_player(_url_stream_now_playing())
    timers = await _start_ma_playback(protocol)
    transition_timer = f"heos_playback_transition_{protocol.protocol_id}"

    await _set_device_state(protocol, HeosPlayState.STOP)
    timers.pop(transition_timer)()
    timers.pop(transition_timer)()

    assert protocol.playback_state == PlaybackState.IDLE
    assert protocol._ma_playback_starting is False


async def test_stop_during_ma_playback_transition_reports_idle() -> None:
    """A stop requested during the playback transition is reported right away."""
    protocol = _make_player(_url_stream_now_playing())
    await _start_ma_playback(protocol)
    cast("MagicMock", protocol._device).stop = AsyncMock()

    await protocol.stop()
    await _set_device_state(protocol, HeosPlayState.STOP)

    assert protocol.playback_state == PlaybackState.IDLE


def test_media_position_and_duration_reported_in_seconds() -> None:
    """HEOS reports milliseconds; the media must carry seconds."""
    protocol = _make_player(_external_now_playing(113000))
    protocol._attr_active_source = "9999"

    protocol._update_player_current_media()

    assert protocol._attr_current_media is not None
    assert protocol._attr_current_media.elapsed_time == 113
    assert protocol._attr_current_media.duration == 245


def test_media_and_player_position_agree() -> None:
    """The media-level and player-level positions must use the same unit."""
    protocol = _make_player(_external_now_playing(113000))
    protocol._attr_active_source = "9999"

    protocol.set_dynamic_attributes(update_media=True)

    assert protocol._attr_current_media is not None
    assert protocol._attr_current_media.elapsed_time == protocol._attr_elapsed_time == 113


def test_position_zero_is_reported_not_dropped() -> None:
    """Position 0 is a real position at the start of a track, not a missing one."""
    protocol = _make_player(_external_now_playing(0))
    protocol._attr_active_source = "9999"

    protocol.set_dynamic_attributes(update_media=True)

    assert protocol._attr_current_media is not None
    assert protocol._attr_current_media.elapsed_time == 0
    assert protocol._attr_elapsed_time == 0


def test_missing_progress_stays_none() -> None:
    """Without progress info, no position or duration is reported."""
    protocol = _make_player(_external_now_playing(None, duration=None))
    protocol._attr_active_source = "9999"

    protocol.set_dynamic_attributes(update_media=True)

    assert protocol._attr_current_media is not None
    assert protocol._attr_current_media.elapsed_time is None
    assert protocol._attr_current_media.duration is None
    assert protocol._attr_elapsed_time is None


async def _report_progress(protocol: HeosPlayer, position: int) -> None:
    """Feed a HEOS progress event (milliseconds) into the protocol."""
    now_playing = cast("MagicMock", protocol._device).now_playing_media
    now_playing.current_position = position
    now_playing.current_position_updated = datetime(2026, 1, 1, tzinfo=UTC)
    await protocol._player_event_received(heos_const.EVENT_PLAYER_NOW_PLAYING_PROGRESS)


async def test_previous_stream_progress_is_ignored_while_restarting() -> None:
    """
    Ignore progress until HEOS plays the new MA stream.

    Right after play_url, HEOS can still send a progress event of the previous
    stream. Reporting it as the position of the new item makes a later resume
    (e.g. after an announcement) seek past the end of a shorter track, which is
    then skipped with "No audio was received".

    See https://github.com/music-assistant/support/issues/7014
    """
    protocol = _make_player(_url_stream_now_playing())
    await _start_ma_playback(protocol)

    await _report_progress(protocol, 244000)
    assert protocol._attr_elapsed_time is None

    await _set_device_state(protocol, HeosPlayState.STOP)
    await _report_progress(protocol, 244000)
    assert protocol._attr_elapsed_time is None

    await _set_device_state(protocol, HeosPlayState.PLAY)
    await _report_progress(protocol, 2000)
    assert protocol._attr_elapsed_time == 2


async def test_previous_stream_position_is_cleared_when_new_playback_starts() -> None:
    """Do not publish the previous stream's cached position with the new media."""
    protocol = _make_player(_external_now_playing(244000))
    protocol._attr_active_source = protocol.protocol_id

    await _start_ma_playback(protocol)

    assert protocol._attr_elapsed_time is None
    assert protocol._attr_elapsed_time_last_updated is None


async def test_play_before_restart_stop_does_not_end_progress_wait() -> None:
    """Keep ignoring progress when HEOS still reports the old stream as playing."""
    protocol = _make_player(_url_stream_now_playing())
    await _start_ma_playback(protocol)

    await _set_device_state(protocol, HeosPlayState.PLAY)
    await _report_progress(protocol, 244000)

    assert protocol._attr_elapsed_time is None


async def test_new_playback_during_transition_ignores_previous_progress() -> None:
    """Wait for the next restart again when MA starts another stream."""
    protocol = _make_player(_url_stream_now_playing())
    await _start_ma_playback(protocol)
    await _set_device_state(protocol, HeosPlayState.STOP)
    await _set_device_state(protocol, HeosPlayState.PLAY)

    await protocol.play_media(
        PlayerMedia(uri="library://track/3", media_type=MediaType.TRACK, title="Other")
    )
    await _report_progress(protocol, 5000)

    assert protocol._attr_elapsed_time is None


@pytest.mark.parametrize("state", [HeosPlayState.STOP, HeosPlayState.PAUSE])
async def test_progress_is_reported_when_starting_from_stop_or_pause(
    state: HeosPlayState,
) -> None:
    """Report progress on the first play when there is no restart stop to wait for."""
    protocol = _make_player(_url_stream_now_playing())
    await _start_ma_playback(protocol, state)

    await _set_device_state(protocol, HeosPlayState.PLAY)
    await _report_progress(protocol, 1000)

    assert protocol._ma_playback_starting is True
    assert protocol._attr_elapsed_time == 1


async def test_progress_is_reported_after_transition_ends_without_play() -> None:
    """Report progress again once the playback transition has ended."""
    protocol = _make_player(_url_stream_now_playing())
    timers = await _start_ma_playback(protocol)
    transition_timer = f"heos_playback_transition_{protocol.protocol_id}"

    timers.pop(transition_timer)()
    await _report_progress(protocol, 3000)

    assert protocol._ma_playback_starting is False
    assert protocol._attr_elapsed_time == 3


def test_group_members_need_the_stream_of_their_leader() -> None:
    """Members are moved along when their leader renders through another protocol."""
    player = HeosPlayer.__new__(HeosPlayer)

    assert player.native_grouping_requires_own_stream is True


@pytest.mark.parametrize(
    ("device_model", "expected_rates"),
    [
        ("HEOS 3", [(44100, 16), (48000, 16)]),
        ("HEOS 3 HS2", HIRES_SAMPLE_RATES),
    ],
)
def test_sample_rates_follow_heos_generation(
    device_model: str, expected_rates: list[tuple[int, int]]
) -> None:
    """Gen 1 HEOS speakers are capped at 48kHz/16-bit, HS2 models get hi-res."""
    player = _make_player(_url_stream_now_playing())
    player._device.model = device_model

    player.set_device_info()

    assert player._attr_supported_sample_rates == expected_rates


async def test_inputs_of_the_device_are_marked_as_inputs() -> None:
    """The inputs of the device are inputs; its music services and radio tuner are not."""
    protocol = _make_player(_url_stream_now_playing())
    provider = cast("Any", protocol.provider)
    provider.music_source_list = [PlayerSource(id="1", name="Pandora", can_play_pause=True)]
    provider.input_source_list = [
        SimpleNamespace(source_id=int(protocol.protocol_id), media_id=media_id, name=name)
        for media_id, name in (
            (heos_const.INPUT_LINE_IN_1, "Line In 1"),
            (heos_const.INPUT_OPTICAL_IN_1, "Optical In 1"),
            (heos_const.INPUT_TUNER, "Tuner"),
        )
    ]

    await protocol.build_source_list()

    assert {x.name: x.is_input for x in protocol._attr_source_list} == {
        "Pandora": False,
        "Line In 1": True,
        "Optical In 1": True,
        "Tuner": False,
    }


DEN_ID = 2
KITCHEN_LINE_IN = f"{heos_const.INPUT_LINE_IN_1}@1"
DEN_LINE_IN = f"{heos_const.INPUT_LINE_IN_1}@2"


def _heos_system(protocol: HeosPlayer, device_id: int) -> HeosPlayer:
    """
    Let a HEOS device be one of Kitchen (1), Den (2) and the attic (3).

    HEOS names each input after its device: "Den - Line In 1".
    """
    protocol._attr_source_list = []
    cast("Any", protocol)._protocol_id = str(device_id)
    provider = cast("Any", protocol.provider)
    provider.music_source_list = []
    provider.input_source_list = [
        SimpleNamespace(source_id=source_id, media_id=media_id, name=name)
        for source_id, media_id, name in (
            (1, heos_const.INPUT_LINE_IN_1, "Kitchen - Line In 1"),
            (DEN_ID, heos_const.INPUT_LINE_IN_1, "Den - Line In 1"),
            (DEN_ID, heos_const.INPUT_OPTICAL_IN_1, "Den - Optical In 1"),
            (DEN_ID, heos_const.INPUT_TUNER, "Den - Tuner"),
            # a device whose name HEOS did not put in front of its input
            (3, heos_const.INPUT_AUX_IN_1, "Phono"),
        )
    ]
    provider.input_device_names = {1: "Kitchen", DEN_ID: "Den", 3: "Attic"}
    cast("Any", protocol._heos).players = {
        1: SimpleNamespace(name="Kitchen"),
        DEN_ID: SimpleNamespace(name="Den"),
        3: SimpleNamespace(name="Attic"),
    }
    return protocol


def _kitchen_with_den(now_playing: MagicMock | None = None) -> HeosPlayer:
    """Return Kitchen (device 1), in a HEOS system with Den (device 2) and the attic (3)."""
    return _heos_system(_make_player(now_playing or _url_stream_now_playing()), 1)


def _aux_input_now_playing(station: str) -> MagicMock:
    """HEOS now-playing for its line-in 1, reported with the given station."""
    now_playing = _url_stream_now_playing()
    now_playing.source_id = heos_const.MUSIC_SOURCE_AUX_INPUT
    now_playing.media_id = heos_const.INPUT_LINE_IN_1
    now_playing.station = station
    return now_playing


async def test_the_inputs_of_the_other_devices_are_listed_as_their_inputs() -> None:
    """
    Kitchen lists its own input, then the inputs of Den and the attic, each naming its device.

    Den's tuner is a radio, not an input, and is left out. Every name drops the device, which
    the naming rule adds back where the input is shown on another device.
    """
    protocol = _kitchen_with_den()

    await protocol.build_source_list()

    assert [(x.id, x.name, x.is_input, x.owner_player_id) for x in protocol._attr_source_list] == [
        (KITCHEN_LINE_IN, "Line In 1", True, None),
        (DEN_LINE_IN, "Line In 1", True, "2"),
        (f"{heos_const.INPUT_OPTICAL_IN_1}@2", "Optical In 1", True, "2"),
        (f"{heos_const.INPUT_AUX_IN_1}@3", "Phono", True, "3"),
    ]


async def test_an_input_has_the_same_id_on_its_own_device() -> None:
    """Den lists its own line-in by the id Kitchen lists it by, without Den as its owner."""
    den = _heos_system(_make_player(_url_stream_now_playing()), DEN_ID)

    await den.build_source_list()

    assert [(x.id, x.owner_player_id) for x in den._attr_source_list if x.id == DEN_LINE_IN] == [
        (DEN_LINE_IN, None)
    ]


@pytest.mark.parametrize(
    ("source", "expected_args"),
    [
        (DEN_LINE_IN, (heos_const.INPUT_LINE_IN_1, DEN_ID)),
        (KITCHEN_LINE_IN, (heos_const.INPUT_LINE_IN_1,)),
    ],
    ids=["other_device", "own"],
)
async def test_an_input_plays_from_the_device_it_belongs_to(
    source: str, expected_args: tuple[str | int, ...]
) -> None:
    """The input of Den is played from Den, Kitchen's own input from Kitchen."""
    protocol = _kitchen_with_den()
    device = cast("MagicMock", protocol._device)
    device.play_input_source = AsyncMock()

    await protocol.select_source(source)

    device.play_input_source.assert_awaited_once_with(*expected_args)


@pytest.mark.parametrize(
    ("station", "expected"),
    [("Den - Line In 1", DEN_LINE_IN), ("Kitchen - Line In 1", KITCHEN_LINE_IN)],
    ids=["other_device", "own"],
)
def test_the_input_that_plays_is_found_by_the_station(station: str, expected: str) -> None:
    """HEOS reports the input of another device by the name of that device and the input."""
    protocol = _kitchen_with_den(_aux_input_now_playing(station))

    protocol._update_player_current_media()

    assert protocol._attr_active_source == expected


async def test_an_input_of_a_renamed_device_is_still_found() -> None:
    """
    Den is renamed Study after the inputs were read: its input keeps its name and is found.

    HEOS then reports it as "Study - Line In 1".
    """
    protocol = _kitchen_with_den(_aux_input_now_playing("Study - Line In 1"))
    cast("Any", protocol._heos).players[DEN_ID] = SimpleNamespace(name="Study")

    await protocol.build_source_list()
    protocol._update_player_current_media()

    assert protocol._attr_active_source == DEN_LINE_IN
    assert [x.name for x in protocol._attr_source_list if x.id == DEN_LINE_IN] == ["Line In 1"]


def _attached_to_a_player(protocol: HeosPlayer) -> Player:
    """
    Attach Kitchen to a player of its own, which played its Music Assistant queue last.

    Den has a player too, so its inputs keep their owner on Kitchen.
    """
    mass = mock_player_mass()
    protocol.mass = mass
    attach_mock(MockProvider("heos", mass=mass), str(DEN_ID), "Den")
    player = Player(
        mass,
        "pl_kitchen",
        PlayerConfig(values={}, provider="heos", player_id="pl_kitchen", default_name="Kitchen"),
    )
    mass.players._players[player.player_id] = player
    mass.players._protocols[protocol.protocol_id] = protocol
    player.set_initialized()
    player.add_protocol(protocol, ProtocolRole.NATIVE)
    protocol.set_player(player)
    player.set_active_mass_source(player.player_id)
    return player


@pytest.mark.parametrize(
    ("station", "expected"),
    [("Kitchen - Line In 1", KITCHEN_LINE_IN), ("Den - Line In 1", DEN_LINE_IN)],
    ids=["own_input", "input_of_another_device"],
)
async def test_an_input_started_from_the_heos_app_takes_over_the_ma_queue(
    station: str, expected: str
) -> None:
    """After Kitchen played its Music Assistant queue, the HEOS app starts an input on it."""
    protocol = _kitchen_with_den()
    await protocol.build_source_list()
    player = _attached_to_a_player(protocol)
    protocol._attr_active_source = protocol.protocol_id
    player.update_state(signal_event=False)
    while_queue_played = player.active_source

    cast("MagicMock", protocol._device).now_playing_media = _aux_input_now_playing(station)
    protocol._update_player_state()
    protocol._update_player_current_media()
    player.update_state(signal_event=False)

    assert while_queue_played == player.player_id
    assert player.active_source == expected


async def test_a_local_music_report_does_not_take_over_the_ma_queue() -> None:
    """A local music source stays distrusted: Music Assistant's own stream is reported as one."""
    protocol = _kitchen_with_den()
    local_music = str(heos_const.MUSIC_SOURCE_LOCAL_MUSIC)
    cast("Any", protocol.provider).music_source_list = [
        PlayerSource(id=local_music, name="Local Music", can_play_pause=True)
    ]
    await protocol.build_source_list()
    player = _attached_to_a_player(protocol)

    protocol._attr_active_source = local_music
    protocol._attr_playback_state = PlaybackState.PLAYING
    player.update_state(signal_event=False)

    assert player.active_source == player.player_id


def _kitchen_with_a_namesake(
    namesake_inputs: list[tuple[str, str]], own_input_name: str = "Line In 1"
) -> HeosPlayer:
    """Return Kitchen (device 1) and its line-in, in a HEOS system with a second Kitchen (2)."""
    protocol = _kitchen_with_den()
    provider = cast("Any", protocol.provider)
    provider.input_source_list = [
        SimpleNamespace(
            source_id=1, media_id=heos_const.INPUT_LINE_IN_1, name=f"Kitchen - {own_input_name}"
        ),
        *(
            SimpleNamespace(source_id=2, media_id=media_id, name=f"Kitchen - {name}")
            for media_id, name in namesake_inputs
        ),
    ]
    provider.input_device_names = {1: "Kitchen", 2: "Kitchen"}
    cast("Any", protocol._heos).players = {
        1: SimpleNamespace(name="Kitchen"),
        2: SimpleNamespace(name="Kitchen"),
    }
    return protocol


@pytest.mark.parametrize(
    ("namesake_inputs", "own_input_name", "media_id", "station", "expected"),
    [
        (
            [(heos_const.INPUT_LINE_IN_1, "Line In 1")],
            "Line In 1",
            heos_const.INPUT_LINE_IN_1,
            "Kitchen - Line In 1",
            KITCHEN_LINE_IN,
        ),
        (
            [(heos_const.INPUT_OPTICAL_IN_1, "Optical In 1")],
            "Line In 1",
            heos_const.INPUT_OPTICAL_IN_1,
            "Kitchen - Optical In 1",
            f"{heos_const.INPUT_OPTICAL_IN_1}@2",
        ),
        (
            [(heos_const.INPUT_AUX_IN_1, "Phono")],
            "Phono",
            heos_const.INPUT_AUX_IN_1,
            "Kitchen - Phono",
            f"{heos_const.INPUT_AUX_IN_1}@2",
        ),
    ],
    ids=["same_input_on_both", "input_of_the_namesake_only", "same_name_other_input"],
)
def test_an_input_of_devices_with_the_same_name_is_found_by_the_input_too(
    namesake_inputs: list[tuple[str, str]],
    own_input_name: str,
    media_id: str,
    station: str,
    expected: str,
) -> None:
    """
    Two devices are named Kitchen, so the station alone does not tell whose input plays.

    An input only one of them has is still found, also when an input of the other has the same
    name; one both have is reported as the device's own, the owner left out.
    """
    now_playing = _aux_input_now_playing(station)
    now_playing.media_id = media_id
    protocol = _kitchen_with_a_namesake(namesake_inputs, own_input_name)
    cast("MagicMock", protocol._device).now_playing_media = now_playing

    protocol._update_player_current_media()

    assert protocol._attr_active_source == expected
