"""Tests for WebKioskProvider lifecycle helpers."""

from __future__ import annotations

import asyncio
import time
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import MediaType, PlaybackState
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import ItemMapping
from music_assistant_models.player import PlayerMedia
from music_assistant_models.player_queue import PlayerQueue

import music_assistant.providers.web_kiosk.provider as provider_module
from music_assistant.controllers.streams import StreamsController
from music_assistant.providers.web_kiosk.player import WebKioskPlayer
from music_assistant.providers.web_kiosk.provider import WebKioskProvider


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

    await provider._advance_after_track(
        "wk_test", cast("WebKioskPlayer", player).playback_generation
    )

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

    await provider._advance_after_track(
        "wk_test", cast("WebKioskPlayer", player).playback_generation
    )

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

    await provider._advance_after_track(
        "wk_test", cast("WebKioskPlayer", player).playback_generation
    )

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

    await provider._advance_after_track(
        "wk_test", cast("WebKioskPlayer", player).playback_generation
    )
    await provider._advance_after_track(
        "wk_test", cast("WebKioskPlayer", player).playback_generation
    )

    mass_mock.player_queues.play_index.assert_awaited_once()


async def test_concurrent_tabs_share_one_registered_player(
    provider: WebKioskProvider, mass_mock: Mock
) -> None:
    """Two simultaneous connections reuse one player even while registration awaits."""
    registry = {}
    entered = asyncio.Event()
    release = asyncio.Event()

    async def register(player: object) -> None:
        entered.set()
        await release.wait()
        if player.player_id in registry:  # type: ignore[attr-defined]
            raise RuntimeError("Duplicate registration")
        registry[player.player_id] = player  # type: ignore[attr-defined]

    mass_mock.players.get_player.side_effect = lambda player_id, **_: registry.get(player_id)
    mass_mock.players.register.side_effect = register
    first = asyncio.create_task(provider.get_or_register_player("wk_shared"))
    await entered.wait()
    second = asyncio.create_task(provider.get_or_register_player("wk_shared"))
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)

    assert results[0] is results[1]
    mass_mock.players.register.assert_awaited_once()


async def test_unload_unregisters_disabled_players(
    provider: WebKioskProvider, mass_mock: Mock, player: object
) -> None:
    """Disabled kiosks do not remain attached to an unloaded provider."""

    def iter_players(**kwargs: object) -> list[object]:
        return [player] if kwargs.get("return_disabled") else []

    mass_mock.players.iter_players.side_effect = iter_players
    await provider.unload()

    mass_mock.players.unregister.assert_awaited_once_with("wk_test")


async def test_idle_timeout_keeps_a_connected_browser(
    provider: WebKioskProvider, mass_mock: Mock, player: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An open kiosk never disappears from MA just because playback is idle."""
    _run_tasks(mass_mock)
    mass_mock.players.iter_players.return_value = [player]
    provider.http_server = Mock()
    provider.http_server.client_count.return_value = 1
    provider.http_server.stop = AsyncMock()
    provider._player_last_activity["wk_test"] = 1
    monkeypatch.setattr(time, "monotonic", lambda: 10000)
    tick = asyncio.Event()
    original_sleep = asyncio.sleep

    async def sleep(_: float) -> None:
        if tick.is_set():
            mass_mock.closing = True
        tick.set()
        await original_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    await provider.loaded_in_mass()
    assert provider._timeout_task is not None
    await provider._timeout_task
    await original_sleep(0)

    mass_mock.players.unregister.assert_not_awaited()
    await provider.unload()


async def test_unload_waits_for_an_inflight_disconnect_removal(
    provider: WebKioskProvider, mass_mock: Mock, player: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An old disconnect removal cannot continue after provider unload returns."""
    monkeypatch.setattr(provider_module, "PLAYER_DISCONNECT_GRACE_SECONDS", 0)
    _run_tasks(mass_mock)
    mass_mock.players.get_player.return_value = player
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def unregister(_: str) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    mass_mock.players.unregister.side_effect = unregister
    provider.schedule_player_close("wk_test")
    removal = provider._disconnect_tasks["wk_test"]
    await entered.wait()
    await provider.unload()
    completed = cancelled.is_set()
    # Cleanup even on the pre-fix failure so no dangling task survives the test.
    removal.cancel()
    await asyncio.gather(removal, return_exceptions=True)
    assert completed


async def test_stream_resolution_does_not_hide_programming_errors(
    provider: WebKioskProvider, mass_mock: Mock
) -> None:
    """Expected media failures are recoverable; implementation defects still surface."""
    mass_mock.streams.resolve_stream_url.side_effect = InvalidDataError("Invalid media")
    assert await provider.get_ma_stream_url("wk_test", Mock()) is None
    mass_mock.streams.resolve_stream_url.side_effect = RuntimeError("Broken implementation")
    with pytest.raises(RuntimeError, match="Broken implementation"):
        await provider.get_ma_stream_url("wk_test", Mock())


@pytest.mark.parametrize("operation", ["wave", "lyrics", "ended"])
async def test_background_media_work_reports_unexpected_errors(
    provider: WebKioskProvider, mass_mock: Mock, player: object, operation: str
) -> None:
    """A programming defect is reported by the task rather than hidden as missing media."""
    tasks: list[asyncio.Task[None]] = []

    def spawn(coro: object) -> asyncio.Task[None]:
        task: asyncio.Task[None] = asyncio.create_task(coro)  # type: ignore[arg-type]
        tasks.append(task)
        return task

    mass_mock.create_task.side_effect = spawn
    mapping = Mock(item_id="track", provider_instance="library")
    media = Mock(provider_mappings=[mapping])
    mass_mock.player_queues.get_active_queue.return_value = Mock(
        current_item=Mock(media_item=media)
    )
    provider.http_server = Mock()
    error = RuntimeError("Broken implementation")
    if operation == "wave":
        mass_mock.streams.audio_analysis.get_wave_form = AsyncMock(side_effect=error)
        provider.spawn_wave("wk_test", 1)
    elif operation == "lyrics":
        mass_mock.metadata.get_track_lyrics = AsyncMock(side_effect=error)
        provider.spawn_lyrics("wk_test", 1)
    else:
        _playing_at_end(player)
        mass_mock.players.get_player.return_value = player
        mass_mock.player_queues.get_active_queue.side_effect = error
        provider.track_ended("wk_test", cast("WebKioskPlayer", player).playback_generation)

    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert results == [error]


@pytest.mark.parametrize("flow_reason", ["crossfade", "overlay", "plugin"])
async def test_html5_stream_stays_per_item_when_ma_requires_flow(
    provider: WebKioskProvider, mass_mock: Mock, player: object, flow_reason: str
) -> None:
    """The real MA resolver cannot turn this browser's per-track playback into queue flow."""
    mass_mock.config.get_raw_core_config_value.return_value = "INFO"
    streams = StreamsController(mass_mock)
    streams._base_url = "http://ma.local:8095"
    mass_mock.streams = streams
    mass_mock.players.get_player.return_value = player
    player.config.get_value.side_effect = lambda key, default=None: (  # type: ignore[attr-defined]
        "mp3" if key == "output_codec" else default
    )
    queue = PlayerQueue(
        queue_id="wk_test",
        active=True,
        display_name="Kiosk",
        available=True,
        items=2,
        crossfade_enabled=flow_reason == "crossfade",
        overlay_enabled=flow_reason == "overlay",
        overlay_source=ItemMapping(
            item_id="overlay", provider="builtin", name="Overlay", media_type=MediaType.SOUND_EFFECT
        )
        if flow_reason == "overlay"
        else None,
    )
    mass_mock.player_queues.get.return_value = queue
    if flow_reason == "plugin":
        streams.set_flow_mode_required("wk_test", "test-owner", True)
    media = PlayerMedia(
        uri="unused",
        media_type=MediaType.TRACK,
        source_id="wk_test",
        queue_item_id="item-1",
        queue_session_id="session-1",
    )
    # Establish that this is a genuine flow-triggering MA configuration.
    assert await streams.resolve_stream_url("wk_test", media) == (
        "http://ma.local:8095/flow/session-1/wk_test/item-1/wk_test.mp3"
    )

    assert await provider.get_ma_stream_url("wk_test", media) == (
        "http://ma.local:8095/single/session-1/wk_test/item-1/wk_test.mp3"
    )


async def test_queued_end_task_rechecks_generation_before_advancing(
    provider: WebKioskProvider, mass_mock: Mock, player: WebKioskPlayer
) -> None:
    """A valid end becomes stale if playback changes before its background task runs."""
    tasks: list[asyncio.Task[None]] = []

    def spawn(coro: object) -> asyncio.Task[None]:
        task: asyncio.Task[None] = asyncio.create_task(coro)  # type: ignore[arg-type]
        tasks.append(task)
        return task

    mass_mock.create_task.side_effect = spawn
    mass_mock.players.get_player.return_value = player
    mass_mock.player_queues.get_active_queue.return_value = Mock(
        queue_id="wk_test", current_index=0
    )
    mass_mock.player_queues.get_next_item.return_value = Mock(queue_item_id="next")
    mass_mock.player_queues.index_by_id.return_value = 1
    mass_mock.player_queues.play_index = AsyncMock()
    await player.play_media(PlayerMedia(uri="old", duration=5, media_type=MediaType.TRACK))
    provider.track_ended("wk_test", player.playback_generation)
    await player.play_media(PlayerMedia(uri="new", duration=5, media_type=MediaType.TRACK))
    await asyncio.gather(*tasks)
    mass_mock.player_queues.play_index.assert_not_awaited()
