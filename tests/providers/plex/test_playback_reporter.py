"""Tests for reporting the playback of Plex tracks to the Plex server."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import requests
from music_assistant_models.enums import MediaType, PlaybackState
from music_assistant_models.playback_progress_report import MediaItemPlaybackProgressReport

from music_assistant.providers.plex import PlexProvider, playback_reporter
from music_assistant.providers.plex.helpers import SUPPORTED_FEATURES, compute_client_id
from music_assistant.providers.plex.playback_reporter import PlexPlaybackReporter

INSTANCE_ID = "plex--1"
TRACK_1 = "/library/metadata/101"
TRACK_2 = "/library/metadata/102"


def _report(
    seconds_played: int = 30,
    player_id: str | None = "player1",
    media_type: MediaType = MediaType.TRACK,
) -> MediaItemPlaybackProgressReport:
    """Return a playback progress report of a track."""
    return MediaItemPlaybackProgressReport(
        uri=f"{INSTANCE_ID}://track/101",
        media_type=media_type,
        name="Track",
        duration=180,
        seconds_played=seconds_played,
        fully_played=False,
        is_playing=True,
        player_id=player_id,
    )


def _reporter() -> Any:
    """Return a playback reporter for a mocked Plex provider."""
    provider = MagicMock()
    provider.instance_id = INSTANCE_ID
    provider.mass.players.get_player.return_value.name = "Living Room"
    return PlexPlaybackReporter(provider)


def _sent(reporter: Any) -> list[tuple[dict[str, str], dict[str, str]]]:
    """Return the params and headers of the timeline updates sent to the Plex server."""
    return [
        (call.kwargs["params"], call.kwargs["headers"])
        for call in reporter.provider._plex_server.query.call_args_list
    ]


def _states(reporter: Any) -> list[tuple[str, str, str]]:
    """Return the key, state and position of each timeline update sent."""
    return [(params["key"], params["state"], params["time"]) for params, _ in _sent(reporter)]


async def _run_retry(reporter: Any) -> None:
    """Run the stop retry the reporter scheduled last."""
    retry = reporter.mass.call_later.call_args.args[1]
    await retry()


async def test_reports_playing_track_as_the_player() -> None:
    """A playing track is reported under the identity of its player."""
    reporter = _reporter()

    await reporter.report(_report(), TRACK_1, PlaybackState.PLAYING)

    [(params, headers)] = _sent(reporter)
    assert params == {
        "ratingKey": "101",
        "key": TRACK_1,
        "state": "playing",
        "time": "30000",
        "duration": "180000",
    }
    assert headers["X-Plex-Client-Identifier"] == compute_client_id(INSTANCE_ID, "player1")
    assert headers["X-Plex-Session-Identifier"] == compute_client_id(INSTANCE_ID, "player1")
    assert headers["X-Plex-Device-Name"] == "Living Room"
    reporter.mass.players.get_player.assert_called_with("player1")


async def test_player_name_outside_latin_1_can_be_sent() -> None:
    """A player name in any script reaches the Plex server as UTF-8."""
    reporter = _reporter()
    reporter.mass.players.get_player.return_value.name = "客厅"

    await reporter.report(_report(), TRACK_1, PlaybackState.PLAYING)

    [(_params, headers)] = _sent(reporter)
    assert headers["X-Plex-Device-Name"].encode("latin-1") == "客厅".encode()


async def test_report_without_player_is_ignored() -> None:
    """A report that names no player has no device to report under."""
    reporter = _reporter()

    await reporter.report(_report(player_id=None), TRACK_1, PlaybackState.PLAYING)

    assert _sent(reporter) == []


async def test_pause_resume_and_stop_are_reported() -> None:
    """Every change of state is reported, and the same report only once."""
    reporter = _reporter()

    await reporter.report(_report(30), TRACK_1, PlaybackState.PLAYING)
    await reporter.report(_report(30), TRACK_1, PlaybackState.PLAYING)
    await reporter.report(_report(60), TRACK_1, PlaybackState.PLAYING)
    await reporter.report(_report(62), TRACK_1, PlaybackState.PAUSED)
    await reporter.report(_report(62), TRACK_1, PlaybackState.PAUSED)
    await reporter.report(_report(62), TRACK_1, PlaybackState.PLAYING)
    await reporter.report(_report(70), TRACK_1, PlaybackState.IDLE)

    assert _states(reporter) == [
        (TRACK_1, "playing", "30000"),
        (TRACK_1, "playing", "60000"),
        (TRACK_1, "paused", "62000"),
        (TRACK_1, "playing", "62000"),
        (TRACK_1, "stopped", "70000"),
    ]


async def test_play_queue_of_a_mirrored_player() -> None:
    """A player that Plex Connect mirrors is reported within its Plex play queue until removed."""
    reporter = _reporter()
    play_queue_item_id = {"id": "901"}
    remove = reporter.add_play_queue(
        "player1",
        lambda: {"containerKey": "/playQueues/77", "playQueueItemID": play_queue_item_id["id"]},
    )

    await reporter.report(_report(170), TRACK_1, PlaybackState.PLAYING)
    # the play queue already moved on to the next track when the previous one is stopped
    play_queue_item_id["id"] = "902"
    await reporter.report(_report(178), TRACK_1, PlaybackState.IDLE)
    await reporter.report(_report(1), TRACK_2, PlaybackState.PLAYING)
    await reporter.report(_report(1, player_id="player2"), TRACK_2, PlaybackState.PLAYING)
    remove()
    await reporter.report(_report(31), TRACK_2, PlaybackState.PLAYING)

    players = {compute_client_id(INSTANCE_ID, p): p for p in ("player1", "player2")}
    assert [
        (
            players[headers["X-Plex-Client-Identifier"]],
            params["state"],
            params.get("playQueueItemID"),
        )
        for params, headers in _sent(reporter)
    ] == [
        ("player1", "playing", "901"),
        # the previous track is stopped within its own play queue item
        ("player1", "stopped", "901"),
        ("player1", "playing", "902"),
        ("player2", "playing", None),
        ("player1", "playing", None),
    ]


async def test_stop_of_a_track_not_reported_before() -> None:
    """A track that stops without an earlier report is still reported stopped."""
    reporter = _reporter()

    await reporter.report(_report(30), TRACK_1, PlaybackState.PLAYING)
    await reporter.report(_report(4), TRACK_2, PlaybackState.IDLE)
    # the track that plays is still known, so the same report of it is not sent again
    await reporter.report(_report(30), TRACK_1, PlaybackState.PLAYING)

    assert _states(reporter) == [(TRACK_1, "playing", "30000"), (TRACK_2, "stopped", "4000")]


async def test_failed_stop_is_sent_again() -> None:
    """A stop the Plex server did not take is sent again later, until it takes it."""
    reporter = _reporter()
    query = reporter.provider._plex_server.query
    await reporter.report(_report(30), TRACK_1, PlaybackState.PLAYING)

    query.side_effect = requests.exceptions.ConnectionError
    await reporter.report(_report(34), TRACK_1, PlaybackState.IDLE)
    assert reporter.mass.call_later.call_args.args[0] == playback_reporter.STOP_RETRY_DELAY
    query.side_effect = None
    await _run_retry(reporter)

    assert _states(reporter) == [
        (TRACK_1, "playing", "30000"),
        (TRACK_1, "stopped", "34000"),
        (TRACK_1, "stopped", "34000"),
    ]
    assert reporter.mass.call_later.call_count == 1


async def test_failed_stop_is_given_up() -> None:
    """A stop the Plex server keeps refusing is sent a limited number of times."""
    reporter = _reporter()
    reporter.provider._plex_server.query.side_effect = requests.exceptions.ConnectionError

    await reporter.report(_report(34), TRACK_1, PlaybackState.IDLE)
    for _ in range(playback_reporter.STOP_RETRIES):
        await _run_retry(reporter)

    assert len(_sent(reporter)) == playback_reporter.STOP_RETRIES + 1
    assert reporter.mass.call_later.call_count == playback_reporter.STOP_RETRIES
    assert reporter._retry_timers == set()


async def test_failed_play_is_not_retried() -> None:
    """A playing track the Plex server did not take is reported again with its next report."""
    reporter = _reporter()
    reporter.provider._plex_server.query.side_effect = requests.exceptions.ConnectionError

    await reporter.report(_report(30), TRACK_1, PlaybackState.PLAYING)

    reporter.mass.call_later.assert_not_called()


async def test_stop_cancels_pending_retries() -> None:
    """Stopping the reporter cancels the stops still waiting to be sent again."""
    reporter = _reporter()
    reporter.provider._plex_server.query.side_effect = requests.exceptions.ConnectionError
    await reporter.report(_report(34), TRACK_1, PlaybackState.IDLE)
    task_id = reporter.mass.call_later.call_args.kwargs["task_id"]

    reporter.stop()

    reporter.mass.cancel_timer.assert_called_once_with(task_id)


@pytest.mark.parametrize(
    ("media_type", "reported"), [(MediaType.TRACK, True), (MediaType.PODCAST_EPISODE, False)]
)
async def test_provider_reports_the_tracks_it_streamed(
    media_type: MediaType, reported: bool
) -> None:
    """The provider hands the progress of the tracks it streamed to its reporter."""
    mass = MagicMock()
    config = MagicMock()
    config.instance_id = INSTANCE_ID
    config.get_value = {"library_type": "music", "log_level": "INFO"}.get
    setup_data = {"library_type": "music", "token": "local_auth"}
    mass.config.get = lambda key, default=None: (
        setup_data if str(key).endswith("/setup_data") else default
    )
    mass.config.decrypt_string = lambda value: value
    provider = PlexProvider(mass, MagicMock(domain="plex"), config, SUPPORTED_FEATURES)
    assert provider.playback_reporter.provider is provider
    provider.playback_reporter = MagicMock(report=AsyncMock())
    report = _report(media_type=media_type)

    await provider.on_playback_progress(report, TRACK_1, PlaybackState.PLAYING)
    await provider.unload()

    if reported:
        provider.playback_reporter.report.assert_awaited_once_with(
            report, TRACK_1, PlaybackState.PLAYING
        )
    else:
        provider.playback_reporter.report.assert_not_awaited()
    provider.playback_reporter.stop.assert_called_once_with()
