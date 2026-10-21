"""Tests for correcting the Alexa player's position from the Echo's own reports."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from music_assistant_models.enums import PlaybackState

from music_assistant.providers import alexa
from music_assistant.providers.alexa import AlexaPlayer, get_playback_state
from tests.common import MockProvider, create_mock_config

NOW = 1_000_000.0


def _setup_value(key: str) -> str | None:
    return {
        alexa.CONF_API_URL: "http://skill:5000/",
        alexa.CONF_API_BASIC_AUTH_USERNAME: "user",
        alexa.CONF_API_BASIC_AUTH_PASSWORD: "secret",
    }.get(key)


def _player(state: PlaybackState = PlaybackState.PLAYING) -> tuple[AlexaPlayer, Any]:
    """Return a player on an Echo, in the given state, and its provider."""
    provider = cast("Any", MockProvider("alexa", instance_id="alexa--1"))
    provider.mass.config.get_base_player_config.return_value = create_mock_config("Echo")
    provider.mass.loop.time.return_value = 100.0
    provider.get_setup_value = MagicMock(side_effect=_setup_value)
    provider.get_volumes = AsyncMock(return_value={"SERIAL": 20})
    provider._volumes_lock = asyncio.Lock()
    player = AlexaPlayer(provider, "Echo", MagicMock(device_serial_number="SERIAL"))
    player._attr_playback_state = state
    player._attr_elapsed_time = 0
    player._attr_elapsed_time_last_updated = NOW - 600
    return player, provider


def _report(**changes: Any) -> dict[str, Any]:
    report = {
        "current": True,
        "activity": "PLAYING",
        "offsetMs": 125_000,
        "ageSeconds": 2.0,
        "observedAt": 42.0,
    }
    report.update(changes)
    return report


async def _correct(player: AlexaPlayer, report: dict[str, Any] | None) -> MagicMock:
    with (
        patch.object(alexa, "get_playback_state", AsyncMock(return_value=report)),
        patch("music_assistant.providers.alexa.time.time", return_value=NOW),
        patch.object(player, "update_state") as update_state,
    ):
        await player._correct_position()
    return update_state


async def test_reported_offset_replaces_the_clock_estimate() -> None:
    """The Echo's stream offset becomes the elapsed time, anchored to its age."""
    player, _ = _player()
    update_state = await _correct(player, _report())
    assert player._attr_elapsed_time == 125
    assert player._attr_elapsed_time_last_updated == NOW - 2.0
    assert player.playback_state == PlaybackState.PLAYING
    # MA extrapolates from the anchor while playing: 125 s plus the 2 s since the report.
    with patch("music_assistant.models.player.time.time", return_value=NOW):
        assert player.corrected_elapsed_time == pytest.approx(127)
    update_state.assert_called_once()


async def test_pause_for_a_voice_request_pauses_the_player() -> None:
    """Alexa's STOPPED (paused for a voice request) holds the position."""
    player, _ = _player()
    await _correct(player, _report(activity="STOPPED", offsetMs=90_500))
    assert player.playback_state == PlaybackState.PAUSED
    assert player._attr_elapsed_time == 90.5


@pytest.mark.parametrize(
    "report",
    [
        None,
        {},
        _report(current=False),
        _report(activity="FINISHED"),
        _report(activity="IDLE"),
        _report(offsetMs=None),
        _report(ageSeconds="soon"),
    ],
)
async def test_unusable_reports_change_nothing(report: dict[str, Any] | None) -> None:
    """Old streams, finished or idle Echos and malformed reports are ignored."""
    player, _ = _player()
    update_state = await _correct(player, report)
    assert player._attr_elapsed_time == 0
    assert player.playback_state == PlaybackState.PLAYING
    update_state.assert_not_called()


async def test_same_report_is_applied_once() -> None:
    """A report already applied must not overwrite later local changes."""
    player, _ = _player()
    await _correct(player, _report())
    player._attr_playback_state = PlaybackState.PAUSED  # e.g. paused from the MA UI
    update_state = await _correct(player, _report())
    assert player.playback_state == PlaybackState.PAUSED
    update_state.assert_not_called()


async def test_stopped_player_is_not_revived() -> None:
    """After MA stops the player, the skill API is not asked and nothing changes."""
    player, _ = _player(PlaybackState.IDLE)
    lookup = AsyncMock(return_value=_report())
    with patch.object(alexa, "get_playback_state", lookup):
        await player._correct_position()
    lookup.assert_not_awaited()
    assert player.playback_state == PlaybackState.IDLE


@pytest.mark.parametrize(
    ("state", "interval"),
    [
        (PlaybackState.PLAYING, 5),
        # Alexa resumes on its own after a voice request, so a paused Echo is watched too.
        (PlaybackState.PAUSED, 5),
        (PlaybackState.IDLE, 30),
    ],
)
def test_poll_interval_follows_whether_the_echo_can_move(
    state: PlaybackState, interval: int
) -> None:
    """Every 5 s while playing or paused, every 30 s otherwise."""
    player, _ = _player(state)
    assert player.poll_interval == interval


async def test_paused_poll_corrects_the_position_but_reads_volume_at_the_idle_pace() -> None:
    """Paused polls check the skill API each time but Amazon only every 30 s."""
    player, provider = _player(PlaybackState.PAUSED)
    lookup = AsyncMock(return_value=None)
    with patch.object(alexa, "get_playback_state", lookup), patch.object(player, "update_state"):
        for now in (100.0, 105.0, 110.0, 131.0):
            provider.mass.loop.time.return_value = now
            await player.poll()
    assert lookup.await_count == 4
    assert provider.get_volumes.await_count == 2  # at 100 s and 131 s


async def test_playing_poll_corrects_the_position_and_reads_the_volume() -> None:
    """While playing, every poll does both, as before."""
    player, provider = _player(PlaybackState.PLAYING)
    lookup = AsyncMock(return_value=None)
    with patch.object(alexa, "get_playback_state", lookup), patch.object(player, "update_state"):
        for now in (100.0, 105.0):
            provider.mass.loop.time.return_value = now
            await player.poll()
    assert lookup.await_count == 2
    assert provider.get_volumes.await_count == 2


def _session(status: int = 200, body: Any = None, error: Exception | None = None) -> MagicMock:
    response = MagicMock()
    response.status = status
    response.json = AsyncMock(return_value=body)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(side_effect=error) if error else MagicMock(return_value=context)
    return session


async def test_playback_state_request_uses_the_skill_api() -> None:
    """The report is fetched for the player with the configured API credentials."""
    _, provider = _player()
    provider.mass.http_session = _session(body=_report())
    assert await get_playback_state(provider, "Echo") == _report()
    args, kwargs = provider.mass.http_session.get.call_args
    assert args == ("http://skill:5000/alexa/playback-state",)
    assert kwargs["params"] == {"player_id": "Echo"}
    assert kwargs["auth"] == aiohttp.BasicAuth("user", "secret")


@pytest.mark.parametrize(
    "session",
    [
        _session(status=404),
        _session(body=["not", "an", "object"]),
        _session(error=aiohttp.ClientConnectionError("down")),
        _session(error=TimeoutError()),
    ],
)
async def test_playback_state_failures_return_none(session: MagicMock) -> None:
    """Missing endpoints, bad bodies and network errors are quiet None results."""
    _, provider = _player()
    provider.mass.http_session = session
    assert await get_playback_state(provider, "Echo") is None
