"""Tests for WebKioskProvider lifecycle helpers."""

from __future__ import annotations

import asyncio
import builtins
import importlib.util
import time
from collections.abc import Coroutine
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.enums import MediaType, PlaybackState
from music_assistant_models.errors import InvalidDataError, PlayerUnavailableError
from music_assistant_models.media_items import ItemMapping
from music_assistant_models.player import PlayerMedia
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

import music_assistant.providers.web_kiosk.provider as provider_module
from music_assistant.controllers.streams import StreamsController
from music_assistant.providers.web_kiosk.http_server import WebKioskHTTPServer
from music_assistant.providers.web_kiosk.ownership import VerifiedSendspinClaim
from music_assistant.providers.web_kiosk.player import WebKioskPlayer
from music_assistant.providers.web_kiosk.provider import WebKioskProvider
from music_assistant.providers.web_kiosk.sendspin_bridge import WebKioskSendspinBridgeManager


async def test_sendspin_import_failure_prevents_provider_startup(
    mass_mock: Mock, manifest_mock: Mock, config_mock: Mock
) -> None:
    """A broken required Sendspin import fails setup instead of silently disabling it."""
    original_import = builtins.__import__

    def fail_sendspin_import(
        name: str,
        globals_dict: dict[str, Any] | None = None,
        locals_dict: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if name == "sendspin_bridge" and level == 1:
            raise ImportError("Sendspin API regression")
        return original_import(name, globals_dict, locals_dict, fromlist, level)

    spec = importlib.util.spec_from_file_location(
        provider_module.__name__, provider_module.__file__
    )
    assert spec is not None
    assert spec.loader is not None
    loader = spec.loader
    module = importlib.util.module_from_spec(spec)

    async def load_provider() -> None:
        loader.exec_module(module)
        instance = module.WebKioskProvider(mass_mock, manifest_mock, config_mock, set())
        await instance.handle_async_init()

    with (
        patch("builtins.__import__", side_effect=fail_sendspin_import),
        patch.object(WebKioskHTTPServer, "start", new_callable=AsyncMock) as start,
        pytest.raises(ImportError, match="Sendspin API regression"),
    ):
        await load_provider()
    start.assert_not_awaited()


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
    media = Mock(media_type=MediaType.TRACK)
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
    player._attr_current_media = Mock(  # type: ignore[attr-defined]
        duration=30,
        stream_duration=None,
        media_type=MediaType.TRACK,
        source_id="wk_test",
        queue_item_id="item-1",
    )
    player._attr_elapsed_time = 29  # type: ignore[attr-defined]
    player._track_end_handled = False  # type: ignore[attr-defined]


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
    provider: WebKioskProvider, mass_mock: Mock, player: WebKioskPlayer
) -> None:
    """Two end signals for the same play do not skip an extra track."""
    _playing_at_end(player)
    generation = player.playback_generation
    mass_mock.players.get_player.return_value = player
    second = PlayerMedia(uri="b", duration=200, source_id="wk_test", queue_item_id="item-2")
    third = PlayerMedia(uri="c", duration=200, source_id="wk_test", queue_item_id="item-3")
    await player.enqueue_next_media(second)

    await provider._advance_after_track("wk_test", generation)
    await player.enqueue_next_media(third)
    await provider._advance_after_track("wk_test", generation)

    assert player.current_media is second
    assert player.enqueued_media is third


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


@pytest.mark.parametrize(
    "error", [PlayerUnavailableError("Missing player"), RuntimeError("Broken teardown")]
)
async def test_unload_reports_unexpected_unregister_failures(
    provider: WebKioskProvider, player: WebKioskPlayer, mass_mock: Mock, error: Exception
) -> None:
    """Expected player failures are tolerated; programming failures reach the caller."""
    mass_mock.players.iter_players.return_value = [player]
    mass_mock.players.unregister.side_effect = error
    if isinstance(error, RuntimeError):
        with pytest.raises(RuntimeError, match="Broken teardown"):
            await provider.unload()
    else:
        await provider.unload()


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
    media = Mock(media_type=MediaType.TRACK, provider_mappings=[mapping])
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
        cast("Any", player).start_enqueued_media = AsyncMock(side_effect=error)
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


async def test_closing_kiosk_preserves_sendspin_settings(
    provider: WebKioskProvider, mass_mock: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closing a page detaches the bridge without deleting its saved settings."""
    monkeypatch.setattr(provider_module, "PLAYER_DISCONNECT_GRACE_SECONDS", 0)
    _run_tasks(mass_mock)
    manager = WebKioskSendspinBridgeManager(provider)
    provider.bridge_manager = manager
    player = await provider.get_or_register_player("wk_test")
    assert player is not None
    await provider.note_sendspin_client_id(
        player,
        "hSDwCYkwp1R0i33ctD73Wg2_Og0mOBr066SpjqqbTmo",
        sendspin_claim=VerifiedSendspinClaim(
            player.player_id, "hSDwCYkwp1R0i33ctD73Wg2_Og0mOBr066SpjqqbTmo"
        ),
    )
    mass_mock.players.get_player.side_effect = lambda pid, **_: (
        player if pid == "wk_test" else Mock()
    )
    provider.schedule_player_close(player.player_id)
    await asyncio.sleep(0.02)
    assert mass_mock.players.unregister.await_args_list == [(("wk_test",), {})]
    mass_mock.players.delete_player_config.assert_not_called()


async def test_sendspin_identity_has_one_kiosk_owner(
    provider: WebKioskProvider, mass_mock: Mock
) -> None:
    """Switching device IDs transfers one identity without leaving a destructive old owner."""
    provider.bridge_manager = WebKioskSendspinBridgeManager(provider)
    first = await provider.get_or_register_player("wk_first")
    second = await provider.get_or_register_player("wk_second")
    assert first is not None
    assert second is not None
    await asyncio.gather(
        provider.note_sendspin_client_id(
            first,
            "hSDwCYkwp1R0i33ctD73Wg2_Og0mOBr066SpjqqbTmo",
            sendspin_claim=VerifiedSendspinClaim(
                first.player_id, "hSDwCYkwp1R0i33ctD73Wg2_Og0mOBr066SpjqqbTmo"
            ),
        ),
        provider.note_sendspin_client_id(
            second,
            "hSDwCYkwp1R0i33ctD73Wg2_Og0mOBr066SpjqqbTmo",
            sendspin_claim=VerifiedSendspinClaim(
                second.player_id, "hSDwCYkwp1R0i33ctD73Wg2_Og0mOBr066SpjqqbTmo"
            ),
        ),
    )
    assert first.sendspin_client_id is None
    assert second.sendspin_client_id == "hSDwCYkwp1R0i33ctD73Wg2_Og0mOBr066SpjqqbTmo"
    mass_mock.players.get_player.return_value = Mock()
    await provider.remove_player(first.player_id)
    mass_mock.players.unregister.assert_awaited_once_with("wk_first", permanent=True)
    mass_mock.players.unregister.reset_mock()
    await provider.remove_player(second.player_id)
    assert mass_mock.players.unregister.await_args_list == [
        (("hSDwCYkwp1R0i33ctD73Wg2_Og0mOBr066SpjqqbTmo",), {"permanent": True}),
        (("wk_second",), {"permanent": True}),
    ]


@pytest.mark.parametrize("failure", [OSError("Address already in use"), asyncio.CancelledError()])
async def test_failed_initialization_releases_resources(
    provider: WebKioskProvider, failure: BaseException
) -> None:
    """Setup failure and cancellation leave no bridge subscriptions or HTTP runner."""
    with (
        patch.object(WebKioskHTTPServer, "start", new_callable=AsyncMock, side_effect=failure),
        patch.object(WebKioskHTTPServer, "stop", new_callable=AsyncMock) as stop,
        patch.object(WebKioskSendspinBridgeManager, "close", new_callable=AsyncMock) as close,
        pytest.raises(type(failure)),
    ):
        await provider.handle_async_init()
    close.assert_awaited_once()
    stop.assert_awaited_once()
    assert provider.bridge_manager is None
    assert provider.http_server is None


@pytest.mark.parametrize(
    ("queue_state", "resumed"),
    [
        (PlaybackState.PLAYING, True),
        (PlaybackState.PAUSED, False),
        (PlaybackState.IDLE, False),
    ],
)
async def test_sendspin_fallback_resumes_only_playing_queue(
    provider: WebKioskProvider,
    player: WebKioskPlayer,
    mass_mock: Mock,
    queue_state: PlaybackState,
    resumed: bool,
) -> None:
    """Falling back to HTML5 keeps a paused or stopped queue where it was."""
    client_id = "hSDwCYkwp1R0i33ctD73Wg2_Og0mOBr066SpjqqbTmo"
    player.sendspin_client_id = client_id
    player.sendspin_claim = VerifiedSendspinClaim(player.player_id, client_id)
    mass_mock.players.get_player = Mock(
        side_effect=lambda player_id, **_: player if player_id == player.player_id else None
    )
    queue = PlayerQueue(
        queue_id=player.player_id,
        active=True,
        display_name="Test Kiosk",
        available=True,
        items=1,
        state=queue_state,
    )
    mass_mock.player_queues.get_active_queue = Mock(return_value=queue)
    mass_mock.player_queues.resume = AsyncMock()

    await provider.fallback_sendspin(player.player_id, expected_client_id=client_id)

    assert player.sendspin_fallback
    assert player.active_output_protocol == "native"
    assert mass_mock.player_queues.resume.await_count == int(resumed)
    assert queue.state == queue_state


async def test_fast_track_end_between_position_reports_advances(
    provider: WebKioskProvider, mass_mock: Mock, player: WebKioskPlayer
) -> None:
    """A 2x episode ending between heartbeats completes from the final reported position."""
    mass_mock.player_queues.get_item.return_value = Mock(extra_attributes={"playback_speed": 2.0})
    await player.play_media(
        PlayerMedia(
            uri="podcast",
            duration=299,
            stream_duration=299,
            source_id="wk_test",
            queue_item_id="episode",
        )
    )
    player.note_seek(145)
    assert not player.playback_reached_end()
    mass_mock.players.get_player.return_value = player
    nxt = PlayerMedia(uri="next", duration=200, source_id="wk_test", queue_item_id="item-2")
    await player.enqueue_next_media(nxt)

    await provider._advance_after_track("wk_test", player.playback_generation, position=149.5)

    assert player.current_media is nxt


async def test_stale_track_end_position_does_not_move_the_clock(
    provider: WebKioskProvider, mass_mock: Mock, player: WebKioskPlayer
) -> None:
    """An end report from an older playback cannot change the current position."""
    await player.play_media(PlayerMedia(uri="track", duration=300, source_id="wk_test"))
    player.note_seek(20)
    mass_mock.players.get_player.return_value = player
    mass_mock.player_queues.play_index = AsyncMock()

    await provider._advance_after_track("wk_test", "older-generation", position=299)

    assert player.elapsed_time == 20
    mass_mock.player_queues.play_index.assert_not_awaited()


async def test_announcement_end_reports_idle_without_advancing_queue(
    provider: WebKioskProvider, mass_mock: Mock, player: WebKioskPlayer
) -> None:
    """An announcement ending reports IDLE even when music had handed over its next item."""
    await player.play_media(
        PlayerMedia(uri="music", duration=200, source_id="wk_test", queue_item_id="music")
    )
    await player.enqueue_next_media(
        PlayerMedia(uri="next", duration=200, source_id="wk_test", queue_item_id="next")
    )
    await player.play_media(
        PlayerMedia(
            uri="http://ma.local/announcement",
            media_type=MediaType.ANNOUNCEMENT,
            title="Announcement",
            duration=5,
        )
    )
    player.note_seek(5)
    mass_mock.players.get_player.return_value = player
    mass_mock.player_queues.play_index = AsyncMock()

    await provider._advance_after_track("wk_test", player.playback_generation)

    assert player.playback_state == PlaybackState.IDLE
    assert player.current_media is None
    mass_mock.player_queues.play_index.assert_not_awaited()
    mass_mock.players.cmd_stop.assert_not_awaited()


async def test_play_index_publishes_wave_and_lyrics_of_the_dispatched_item(
    provider: WebKioskProvider, mass_mock: Mock, player: WebKioskPlayer
) -> None:
    """The queue updates its current item only after dispatch, so lookups use the new item."""
    tasks: list[asyncio.Task[Any]] = []

    def spawn(coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coro)
        tasks.append(task)
        return task

    mass_mock.create_task = Mock(side_effect=spawn)
    previous = Mock(
        media_item=Mock(provider_mappings=[Mock(item_id="old", provider_instance="lib")])
    )
    dispatched = Mock(
        media_item=Mock(
            media_type=MediaType.TRACK,
            provider_mappings=[Mock(item_id="new", provider_instance="lib")],
        ),
        extra_attributes={},
    )
    mass_mock.player_queues.get_active_queue.return_value = Mock(current_item=previous)
    mass_mock.player_queues.get_item.side_effect = lambda queue_id, item_id: (
        dispatched if (queue_id, item_id) == ("wk_test", "new-item") else None
    )
    mass_mock.streams.audio_analysis.get_wave_form = AsyncMock(
        side_effect=lambda item_id, _provider: [1.0] if item_id == "new" else [0.5]
    )
    mass_mock.metadata.get_track_lyrics = AsyncMock(
        side_effect=lambda media: (
            ("new line", None) if media is dispatched.media_item else ("old", None)
        )
    )
    provider.http_server = Mock()

    await player.play_media(
        PlayerMedia(uri="new", duration=180, source_id="wk_test", queue_item_id="new-item")
    )
    await asyncio.gather(*tasks)

    assert provider.http_server.broadcast_wave.call_args.args[2] == [1.0]
    assert provider.http_server.broadcast_lyrics.call_args.args[2] == [
        {"t": None, "text": "new line"}
    ]


@pytest.mark.parametrize(
    "media_type", [MediaType.RADIO, MediaType.AUDIOBOOK, MediaType.PODCAST_EPISODE]
)
async def test_non_track_playback_never_requests_track_lyrics(
    provider: WebKioskProvider, media_type: MediaType
) -> None:
    """Only tracks are passed to the track lyrics lookup."""
    media = Mock(media_type=media_type)
    queue = Mock(current_item=Mock(media_item=media))
    cast("Mock", provider.mass.player_queues).get_active_queue.return_value = queue
    cast("Mock", provider.mass.metadata).get_track_lyrics = AsyncMock(return_value=("x", None))
    provider.http_server = Mock()

    await provider._publish_lyrics("wk_test", 3)

    cast("Mock", provider.mass.metadata).get_track_lyrics.assert_not_awaited()
    provider.http_server.broadcast_lyrics.assert_called_once_with("wk_test", 3, [])


async def test_failed_html5_stream_stops_current_playback(
    provider: WebKioskProvider, mass_mock: Mock, player: WebKioskPlayer
) -> None:
    """A stream the browser cannot load ends playback instead of staying PLAYING."""
    await player.play_media(PlayerMedia(uri="broken", duration=180, source_id="wk_test"))
    mass_mock.players.get_player.return_value = player

    def state() -> PlaybackState:
        return player.playback_state

    await provider._stop_failed_playback("wk_test", "stale-generation")
    assert state() == PlaybackState.PLAYING

    await provider._stop_failed_playback("wk_test", player.playback_generation)
    assert state() == PlaybackState.IDLE
    mass_mock.players.cmd_stop.assert_not_awaited()


async def test_unload_cancels_completion_work_in_flight(
    provider: WebKioskProvider, mass_mock: Mock, player: WebKioskPlayer
) -> None:
    """Background playback work cannot outlive the provider and reach a replacement kiosk."""
    tasks: list[asyncio.Task[Any]] = []

    def spawn(coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coro)
        tasks.append(task)
        return task

    mass_mock.create_task = Mock(side_effect=spawn)
    entered = asyncio.Event()
    finished = asyncio.Event()

    async def hold_transition() -> bool:
        entered.set()
        await asyncio.Event().wait()
        finished.set()
        return True

    _playing_at_end(player)
    mass_mock.players.get_player.return_value = player
    player.start_enqueued_media = hold_transition  # type: ignore[method-assign]
    mass_mock.players.iter_players = Mock(return_value=[])

    provider.track_ended("wk_test", player.playback_generation)
    await asyncio.wait_for(entered.wait(), 1)
    await provider.unload()

    assert all(task.done() for task in tasks)
    assert not finished.is_set()
    provider.track_ended("wk_test", player.playback_generation)
    assert len(tasks) == 1


@pytest.mark.parametrize("playing", [True, False])
async def test_sendspin_fallback_of_sync_follower_leaves_the_group_like_ungroup(
    provider: WebKioskProvider, player: WebKioskPlayer, mass_mock: Mock, playing: bool
) -> None:
    """A follower leaves the group as an ungroup does: the leader plays on, nothing moves."""
    client_id = "hSDwCYkwp1R0i33ctD73Wg2_Og0mOBr066SpjqqbTmo"
    player.sendspin_client_id = client_id
    player.sendspin_claim = VerifiedSendspinClaim(player.player_id, client_id)
    leader = Mock(player_id="speaker", set_members=AsyncMock())
    protocol = Mock(player_id=client_id, synced_to="speaker", group_members=[])
    players = {player.player_id: player, client_id: protocol, "speaker": leader}
    mass_mock.players.get_player = Mock(side_effect=lambda pid, **_: players.get(pid))
    leader_queue = PlayerQueue(
        queue_id="speaker",
        active=True,
        display_name="Speaker",
        available=True,
        items=3,
        state=PlaybackState.PLAYING if playing else PlaybackState.PAUSED,
        current_item=QueueItem(queue_id="speaker", queue_item_id="item", name="Song", duration=200),
    )
    mass_mock.player_queues.get_active_queue = Mock(return_value=leader_queue)
    mass_mock.player_queues.resume = AsyncMock()
    mass_mock.player_queues.play_media = AsyncMock()
    mass_mock.player_queues.load = AsyncMock()

    await provider.fallback_sendspin(player.player_id, expected_client_id=client_id)

    leader.set_members.assert_awaited_once_with(player_ids_to_remove=[client_id])
    mass_mock.players.unregister.assert_awaited_once_with(client_id)
    assert player.active_output_protocol == "native"
    mass_mock.player_queues.resume.assert_not_awaited()
    mass_mock.player_queues.play_media.assert_not_awaited()
    mass_mock.player_queues.load.assert_not_awaited()
    assert leader_queue.state == (PlaybackState.PLAYING if playing else PlaybackState.PAUSED)


async def test_unload_cancels_browser_fallback_in_flight(
    provider: WebKioskProvider, mass_mock: Mock
) -> None:
    """A browser-requested fallback is provider-owned work that unload cancels."""
    tasks: list[asyncio.Task[Any]] = []

    def spawn(coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coro)
        tasks.append(task)
        return task

    mass_mock.create_task = Mock(side_effect=spawn)
    mass_mock.players.iter_players = Mock(return_value=[])
    entered = asyncio.Event()
    finished = asyncio.Event()

    async def hold_fallback(_player_id: str, **_kwargs: object) -> None:
        entered.set()
        await asyncio.Event().wait()
        finished.set()

    provider.fallback_sendspin = AsyncMock(side_effect=hold_fallback)  # type: ignore[method-assign]

    provider.request_sendspin_fallback("wk_test", "client")
    await asyncio.wait_for(entered.wait(), 1)
    await provider.unload()

    assert all(task.done() for task in tasks)
    assert not finished.is_set()
    provider.request_sendspin_fallback("wk_test", "client")
    assert len(tasks) == 1


async def test_stop_during_fallback_teardown_is_not_undone(
    provider: WebKioskProvider, player: WebKioskPlayer, mass_mock: Mock
) -> None:
    """A stop issued while the Sendspin output is being detached wins over the resume."""
    client_id = "hSDwCYkwp1R0i33ctD73Wg2_Og0mOBr066SpjqqbTmo"
    player.sendspin_client_id = client_id
    player.sendspin_claim = VerifiedSendspinClaim(player.player_id, client_id)
    mass_mock.players.get_player = Mock(
        side_effect=lambda pid, **_: player if pid == player.player_id else None
    )
    queue = PlayerQueue(
        queue_id=player.player_id,
        active=True,
        display_name="Test Kiosk",
        available=True,
        items=1,
        state=PlaybackState.PLAYING,
    )
    mass_mock.player_queues.get_active_queue = Mock(return_value=queue)
    order: list[str] = []
    mass_mock.player_queues.resume = AsyncMock(side_effect=lambda _qid: order.append("resume"))
    detaching = asyncio.Event()
    release = asyncio.Event()

    async def slow_unregister(_player_id: str) -> None:
        detaching.set()
        await release.wait()

    mass_mock.players.unregister = AsyncMock(side_effect=slow_unregister)

    async def user_stop() -> None:
        # The player controller runs stop under the same playback lock.
        async with mass_mock.players.get_group_and_player_lock(player.player_id):
            queue.state = PlaybackState.IDLE
            order.append("stop")

    fallback = asyncio.create_task(
        provider.fallback_sendspin(player.player_id, expected_client_id=client_id)
    )
    await asyncio.wait_for(detaching.wait(), 1)
    stop = asyncio.create_task(user_stop())
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(fallback, stop)

    assert order[-1] == "stop"
    assert queue.state == PlaybackState.IDLE


async def test_lyrics_are_looked_up_only_for_kiosks_showing_them(
    provider: WebKioskProvider, mass_mock: Mock
) -> None:
    """No connected kiosk shows lyrics, so no lyrics lookup is scheduled."""
    provider.http_server = Mock()
    provider.http_server.lyrics_requested.return_value = False
    mass_mock.create_task = Mock()

    provider.spawn_lyrics("wk_test", 1)
    mass_mock.create_task.assert_not_called()

    provider.http_server.lyrics_requested.return_value = True
    mass_mock.create_task = Mock(side_effect=lambda coro: coro)
    provider.spawn_lyrics("wk_test", 2)
    mass_mock.create_task.assert_called_once()
    provider.http_server.lyrics_requested.assert_called_with("wk_test")


async def test_track_end_starts_the_enqueued_media(
    provider: WebKioskProvider, mass_mock: Mock, player: WebKioskPlayer
) -> None:
    """The handed-over item starts on the kiosk; the queue follows its current media."""
    _playing_at_end(player)
    nxt = PlayerMedia(uri="next", duration=200, source_id="wk_test", queue_item_id="item-2")
    await player.enqueue_next_media(nxt)
    generation = player.playback_generation
    mass_mock.players.get_player.return_value = player
    mass_mock.player_queues.play_index = AsyncMock()
    mass_mock.player_queues.get_active_queue = Mock()

    await provider._advance_after_track("wk_test", generation)

    assert player.current_media is nxt
    assert player.playback_state == PlaybackState.PLAYING
    assert player.playback_generation != generation
    assert player.enqueued_media is None
    mass_mock.player_queues.play_index.assert_not_awaited()
    mass_mock.player_queues.get_active_queue.assert_not_called()
    mass_mock.players.cmd_stop.assert_not_awaited()


async def test_track_end_without_enqueued_media_goes_idle(
    provider: WebKioskProvider, mass_mock: Mock, player: WebKioskPlayer
) -> None:
    """With nothing handed over, the kiosk reports IDLE and leaves the queue to MA."""
    _playing_at_end(player)
    mass_mock.players.get_player.return_value = player
    mass_mock.player_queues.play_index = AsyncMock()
    mass_mock.player_queues.get_active_queue = Mock()

    await provider._advance_after_track("wk_test", player.playback_generation)

    assert player.playback_state == PlaybackState.IDLE
    mass_mock.player_queues.play_index.assert_not_awaited()
    mass_mock.player_queues.get_active_queue.assert_not_called()
    mass_mock.players.cmd_stop.assert_not_awaited()


@pytest.mark.parametrize("report", ["ended", "failed"])
async def test_end_reports_do_not_wait_for_the_playback_lock(
    provider: WebKioskProvider, mass_mock: Mock, player: WebKioskPlayer, report: str
) -> None:
    """An announcement holds the playback lock until the player is IDLE; reports must not wait."""
    await player.play_media(
        PlayerMedia(uri="announce", media_type=MediaType.ANNOUNCEMENT, duration=4)
    )
    player.note_seek(4)
    mass_mock.players.get_player.return_value = player
    held = asyncio.Event()
    release = asyncio.Event()

    async def announcement_waits_for_idle() -> None:
        async with mass_mock.players.get_group_and_player_lock(player.player_id):
            held.set()
            await release.wait()

    holder = asyncio.create_task(announcement_waits_for_idle())
    await held.wait()
    handler = (
        provider._advance_after_track(player.player_id, player.playback_generation)
        if report == "ended"
        else provider._stop_failed_playback(player.player_id, player.playback_generation)
    )
    try:
        await asyncio.wait_for(handler, 1)
        assert player.playback_state == PlaybackState.IDLE
    finally:
        release.set()
        await holder
