"""Tests for handing the next item to the Roku app and noticing when its stream ends."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.enums import PlaybackState

from music_assistant.providers.roku_media_assistant.player import MediaAssistantPlayer
from tests.providers.roku_media_assistant.test_track_change import _make_player, _media


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Skip the pause between enqueue attempts."""
    sleep = AsyncMock()
    monkeypatch.setattr(
        "music_assistant.providers.roku_media_assistant.player.asyncio.sleep", sleep
    )
    return sleep


async def _poll_frozen(player: MediaAssistantPlayer, seconds: float, frozen_for: float) -> None:
    """
    Poll twice at the same position, the second time as if it had not moved for a while.

    :param player: The player to poll.
    :param seconds: The playback position the Roku reports, in seconds.
    :param frozen_for: How long the position has not moved by the second poll, in seconds.
    """
    state: dict[str, Any] = {
        "@state": "play",
        "position": f"{int(seconds * 1000)} ms",
        "duration": "300000 ms",
    }
    player.roku._get_media_state.return_value = state  # type: ignore[attr-defined]
    await player.poll()
    player._position_last_moved = time.time() - frozen_for
    await player.poll()


async def test_failed_enqueue_is_retried() -> None:
    """A failed handover of the next item is tried again, so the app doesn't run dry."""
    player = _make_player()
    await player.play_media(_media("first"))
    player.roku_input.reset_mock()  # type: ignore[attr-defined]
    player.roku_input.side_effect = [OSError("timed out"), None]  # type: ignore[attr-defined]
    queued = _media("queued")

    await player.enqueue_next_media(queued)

    assert player.roku_input.await_count == 2  # type: ignore[attr-defined]
    assert player.queued is queued


async def test_enqueue_gives_up_after_three_attempts(
    no_retry_wait: AsyncMock, caplog: pytest.LogCaptureFixture
) -> None:
    """Three failed handovers leave nothing queued, with a wait only between them."""
    player = _make_player()
    await player.play_media(_media("first"))
    player.roku_input.reset_mock()  # type: ignore[attr-defined]
    player.roku_input.side_effect = OSError("timed out")  # type: ignore[attr-defined]

    await player.enqueue_next_media(_media("queued"))

    assert player.roku_input.await_count == 3  # type: ignore[attr-defined]
    assert player.queued is None
    # the handover holds the player's lock: no wait after the last attempt
    assert no_retry_wait.await_count == 2
    assert "Gave up enqueuing queued on Roku after 3 attempts" in caplog.text


async def test_enqueue_needs_the_app_in_front(caplog: pytest.LogCaptureFixture) -> None:
    """Nothing is handed over while another app is in front, and the log names the apps."""
    player = _make_player()
    await player.play_media(_media("first"))
    player.roku_input.reset_mock()  # type: ignore[attr-defined]
    player.roku.update.return_value.app.app_id = "12"  # type: ignore[attr-defined]

    await player.enqueue_next_media(_media("queued"))

    player.roku_input.assert_not_awaited()  # type: ignore[attr-defined]
    assert player.queued is None
    assert "none of the Roku apps dev is in front" in caplog.text


async def test_position_frozen_at_the_end_is_idle() -> None:
    """The app sits at the end of a drained stream in "play"; that is reported as idle."""
    player = _make_player()
    await player.play_media(_media("first"))

    await _poll_frozen(player, 299.5, frozen_for=11)

    assert player._attr_playback_state == PlaybackState.IDLE


async def test_stream_end_is_logged_once_per_stream(caplog: pytest.LogCaptureFixture) -> None:
    """Polls that keep finding the app at the end say so once; the next stream's end again."""
    player = _make_player()
    await player.play_media(_media("first"))

    await _poll_frozen(player, 299.5, frozen_for=11)
    await player.poll()
    await player.poll()
    await player.play_media(_media("second"))
    await _poll_frozen(player, 299.5, frozen_for=11)

    assert player._attr_playback_state == PlaybackState.IDLE
    assert caplog.text.count("stream ended") == 2


async def test_position_frozen_briefly_at_the_end_is_still_playing() -> None:
    """The last seconds of a track aren't taken for its end."""
    player = _make_player()
    await player.play_media(_media("first"))

    await _poll_frozen(player, 299.5, frozen_for=4)

    assert player._attr_playback_state == PlaybackState.PLAYING


async def test_position_frozen_mid_track_is_still_playing() -> None:
    """Only a position at the end of the stream counts as the stream having ended."""
    player = _make_player()
    await player.play_media(_media("first"))

    await _poll_frozen(player, 120, frozen_for=30)

    assert player._attr_playback_state == PlaybackState.PLAYING
