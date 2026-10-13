"""Tests for WebKioskProvider lifecycle helpers."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, Mock

from music_assistant_models.enums import PlaybackState

import music_assistant.providers.web_kiosk.provider as provider_module
from music_assistant.providers.web_kiosk.provider import WebKioskProvider

if TYPE_CHECKING:
    import pytest


def test_stream_token_is_deterministic_and_sized(provider: WebKioskProvider) -> None:
    """The per-player stream token is stable for a player and 32 chars long."""
    first = provider.get_stream_token("wk_test")
    second = provider.get_stream_token("wk_test")

    assert first == second
    assert len(first) == 32


def test_stream_token_differs_per_player(provider: WebKioskProvider) -> None:
    """Two players never share a stream token."""
    assert provider.get_stream_token("wk_a") != provider.get_stream_token("wk_b")


def test_player_display_name_ip_based(provider: WebKioskProvider) -> None:
    """IP-derived player ids render as a friendly address."""
    assert provider.player_display_name("wk_192_168_10_15") == "Web Kiosk (192.168.10.15)"


async def test_get_or_register_player_registers_once(
    provider: WebKioskProvider, mass_mock: Mock
) -> None:
    """A new player id registers; a repeat call reuses the existing player."""
    mass_mock.players.get_player.return_value = None
    mass_mock.players.register = AsyncMock()

    player = await provider.get_or_register_player("wk_test")

    assert player is not None
    assert player.player_id == "wk_test"
    mass_mock.players.register.assert_awaited_once()


def _run_tasks(mass_mock: Mock) -> None:
    """Make provider background work actually run on the test loop."""

    def spawn(coro: object) -> asyncio.Task[None]:
        return asyncio.get_running_loop().create_task(coro)  # type: ignore[arg-type]

    mass_mock.create_task.side_effect = spawn


async def test_closed_page_unregisters_player_after_grace(
    provider: WebKioskProvider, mass_mock: Mock, player: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last socket closing removes the player once the grace window passes."""
    monkeypatch.setattr(provider_module, "PLAYER_DISCONNECT_GRACE_SECONDS", 0.05)
    _run_tasks(mass_mock)
    mass_mock.players.get_player.return_value = player

    provider.schedule_player_close("wk_test")
    await asyncio.sleep(0.12)

    mass_mock.players.unregister.assert_awaited_once_with("wk_test")


async def test_reconnect_during_grace_keeps_player(
    provider: WebKioskProvider, mass_mock: Mock, player: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A socket that returns inside the window cancels removal. A reload does this."""
    monkeypatch.setattr(provider_module, "PLAYER_DISCONNECT_GRACE_SECONDS", 0.05)
    _run_tasks(mass_mock)
    mass_mock.players.get_player.return_value = player

    provider.schedule_player_close("wk_test")
    provider.cancel_player_close("wk_test")
    await asyncio.sleep(0.12)

    mass_mock.players.unregister.assert_not_awaited()


async def test_close_waits_while_another_browser_is_connected(
    provider: WebKioskProvider, mass_mock: Mock, player: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A grace timer that wakes while a socket is open leaves the player registered."""
    monkeypatch.setattr(provider_module, "PLAYER_DISCONNECT_GRACE_SECONDS", 0.05)
    _run_tasks(mass_mock)
    mass_mock.players.get_player.return_value = player
    provider.http_server = Mock()
    provider.http_server.client_count.return_value = 1

    provider.schedule_player_close("wk_test")
    await asyncio.sleep(0.12)

    mass_mock.players.unregister.assert_not_awaited()


async def test_publish_wave_sends_stored_bins(provider: WebKioskProvider) -> None:
    """The page receives the stored energy curve without a browser token."""
    mapping = Mock(item_id="track-1", provider_instance="yandex", provider_domain="yandex_music")
    media = Mock(provider_mappings=[mapping])
    queue = Mock(current_item=Mock(media_item=media))
    cast("Mock", provider.mass.player_queues).get_active_queue.return_value = queue
    cast("Mock", provider.mass.streams.audio_analysis).get_wave_form = AsyncMock(
        return_value=[0.2, 1.0]
    )
    provider.http_server = Mock()

    await provider._publish_wave("wk_test", 4)

    provider.http_server.broadcast_wave.assert_called_once_with("wk_test", 4, [0.2, 1.0])


async def test_publish_lyrics_sends_parsed_lines(provider: WebKioskProvider) -> None:
    """Lyric lines go out on the player socket without a browser token."""
    media = Mock()
    queue = Mock(current_item=Mock(media_item=media))
    cast("Mock", provider.mass.player_queues).get_active_queue.return_value = queue
    cast("Mock", provider.mass.metadata).get_track_lyrics = AsyncMock(
        return_value=("one\ntwo", None)
    )
    provider.http_server = Mock()

    await provider._publish_lyrics("wk_test", 2)

    provider.http_server.broadcast_lyrics.assert_called_once_with(
        "wk_test",
        2,
        [{"t": None, "text": "one"}, {"t": None, "text": "two"}],
    )


def _playing_at_end(player: object) -> None:
    """Put a player at the end of a 30 second track."""
    player._attr_playback_state = PlaybackState.PLAYING  # type: ignore[attr-defined]
    player._attr_current_media = Mock(duration=30, stream_duration=None)  # type: ignore[attr-defined]
    player._attr_elapsed_time = 29  # type: ignore[attr-defined]
    player._track_end_handled = False  # type: ignore[attr-defined]


async def test_track_end_plays_the_next_item(
    provider: WebKioskProvider, mass_mock: Mock, player: object
) -> None:
    """Finishing a track starts the following queue item without a browser token."""
    _playing_at_end(player)
    mass_mock.players.get_player.return_value = player
    nxt = Mock(queue_item_id="item-2")
    queue = Mock(queue_id="wk_test", current_index=0)
    mass_mock.player_queues.get_active_queue.return_value = queue
    mass_mock.player_queues.get_next_item.return_value = nxt
    mass_mock.player_queues.index_by_id.return_value = 1
    mass_mock.player_queues.play_index = AsyncMock()

    await provider._advance_after_track("wk_test")

    mass_mock.player_queues.play_index.assert_awaited_once_with("wk_test", 1)
    mass_mock.players.cmd_stop.assert_not_awaited()


async def test_track_end_stops_when_nothing_follows(
    provider: WebKioskProvider, mass_mock: Mock, player: object
) -> None:
    """The last track leaves the player idle instead of stuck on playing."""
    _playing_at_end(player)
    mass_mock.players.get_player.return_value = player
    queue = Mock(queue_id="wk_test", current_index=0)
    mass_mock.player_queues.get_active_queue.return_value = queue
    mass_mock.player_queues.get_next_item.return_value = None
    mass_mock.player_queues.play_index = AsyncMock()

    await provider._advance_after_track("wk_test")

    mass_mock.players.cmd_stop.assert_awaited_once_with("wk_test")
    mass_mock.player_queues.play_index.assert_not_awaited()


async def test_track_end_ignores_a_position_still_in_the_song(
    provider: WebKioskProvider, mass_mock: Mock, player: object
) -> None:
    """A stale end signal in the middle of a new track does not skip it."""
    player._attr_playback_state = PlaybackState.PLAYING  # type: ignore[attr-defined]
    player._attr_current_media = Mock(duration=180, stream_duration=None)  # type: ignore[attr-defined]
    player._attr_elapsed_time = 10  # type: ignore[attr-defined]
    player._track_end_handled = False  # type: ignore[attr-defined]
    mass_mock.players.get_player.return_value = player
    mass_mock.player_queues.play_index = AsyncMock()

    await provider._advance_after_track("wk_test")

    mass_mock.player_queues.play_index.assert_not_awaited()
    mass_mock.players.cmd_stop.assert_not_awaited()
    assert player._track_end_handled is False  # type: ignore[attr-defined]


async def test_track_end_runs_once_per_play(
    provider: WebKioskProvider, mass_mock: Mock, player: object
) -> None:
    """Two end signals for the same play do not skip an extra track."""
    _playing_at_end(player)
    mass_mock.players.get_player.return_value = player
    nxt = Mock(queue_item_id="item-2")
    queue = Mock(queue_id="wk_test", current_index=0)
    mass_mock.player_queues.get_active_queue.return_value = queue
    mass_mock.player_queues.get_next_item.return_value = nxt
    mass_mock.player_queues.index_by_id.return_value = 1
    mass_mock.player_queues.play_index = AsyncMock()

    await provider._advance_after_track("wk_test")
    await provider._advance_after_track("wk_test")

    mass_mock.player_queues.play_index.assert_awaited_once()
