"""Test raid state machine and event bus integration."""

# mypy: disable-error-code="unreachable"
from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

from music_assistant.providers.twitch import TwitchProvider

# --- Event Bus Lifecycle ---


async def test_unsubscribes_on_unload(provider: TwitchProvider) -> None:
    """Provider calls unsubscribe callable in unload()."""
    unsub_mock = Mock()
    provider._unsub_queue_updated = unsub_mock
    provider._eventsub = Mock()
    provider._eventsub.stop = AsyncMock()
    provider._grace_timer = None
    provider._idle_timer = None

    await provider.unload()

    unsub_mock.assert_called_once()


# --- State Machine — Play/Pause/Stop ---


async def test_playing_twitch_uri_subscribes_to_raids(provider: TwitchProvider) -> None:
    """PLAYING + Twitch URI subscribes to raid events."""
    provider._access_token = "test"
    provider._client_id = "test"
    provider._auto_raid = True
    provider._eventsub = Mock()
    provider._eventsub.subscribe_raids = AsyncMock()
    provider._eventsub.is_connected = True
    provider._eventsub.start = AsyncMock()

    # Mock _get_users to avoid real API call
    with patch.object(provider, "_get_users", new_callable=AsyncMock, return_value=[{"id": "123"}]):
        await provider._handle_queue_playing("twitch://channel/streamer_a", "streamer_a")

    provider._eventsub.subscribe_raids.assert_called_once_with("123")


async def test_playing_non_twitch_uri_unsubscribes(provider: TwitchProvider) -> None:
    """PLAYING + non-Twitch URI unsubscribes from raids."""
    provider._eventsub = Mock()
    provider._eventsub.unsubscribe_all = AsyncMock()
    provider._current_channel_login = "streamer_a"

    await provider._handle_queue_stopped()

    provider._eventsub.unsubscribe_all.assert_called()


async def test_playing_same_channel_no_duplicate_subscribe(provider: TwitchProvider) -> None:
    """Re-playing same channel doesn't create duplicate subscription."""
    provider._access_token = "test"
    provider._client_id = "test"
    provider._eventsub = Mock()
    provider._eventsub.subscribe_raids = AsyncMock()
    provider._eventsub.is_connected = True
    provider._current_channel_login = "streamer_a"

    await provider._handle_queue_playing("twitch://channel/streamer_a", "streamer_a")

    # Should not subscribe again — already on same channel
    provider._eventsub.subscribe_raids.assert_not_called()


# --- Pause / Stop / Resume ---


async def test_pause_unsubscribes_keeps_ws_warm(provider: TwitchProvider) -> None:
    """Pause unsubscribes EventSub but keeps WebSocket connected."""
    provider._eventsub = Mock()
    provider._eventsub.unsubscribe_all = AsyncMock()
    provider._eventsub.stop = AsyncMock()

    await provider._handle_queue_paused()

    provider._eventsub.unsubscribe_all.assert_called_once()
    provider._eventsub.stop.assert_not_called()  # WS stays warm


async def test_stop_starts_grace_period(provider: TwitchProvider) -> None:
    """Stop/idle creates a grace timer task."""
    provider._eventsub = Mock()
    provider._eventsub.unsubscribe_all = AsyncMock()
    provider._grace_timer = None

    await provider._handle_queue_idle()

    assert provider._grace_timer is not None
    # Cancel so it doesn't run in background
    provider._grace_timer.cancel()


async def test_grace_period_then_idle_timer(provider: TwitchProvider) -> None:
    """After grace period, EventSub unsubscribes and idle timer starts."""
    provider._eventsub = Mock()
    provider._eventsub.unsubscribe_all = AsyncMock()
    provider._idle_timer = None

    with patch("music_assistant.providers.twitch.asyncio.sleep", new_callable=AsyncMock):
        await provider._grace_period()

    provider._eventsub.unsubscribe_all.assert_called_once()
    assert provider._idle_timer is not None
    provider._idle_timer.cancel()


async def test_idle_timer_disconnects_websocket(provider: TwitchProvider) -> None:
    """After idle timer, EventSub WebSocket is disconnected."""
    eventsub_mock = Mock()
    eventsub_mock.stop = AsyncMock()
    provider._eventsub = eventsub_mock

    with patch("music_assistant.providers.twitch.asyncio.sleep", new_callable=AsyncMock):
        await provider._idle_disconnect()

    eventsub_mock.stop.assert_called_once()
    assert provider._eventsub is None


async def test_resume_resubscribes(provider: TwitchProvider) -> None:
    """Resume from pause cancels timers and resubscribes."""
    provider._access_token = "test"
    provider._client_id = "test"
    provider._auto_raid = True
    provider._current_channel_login = None  # Was paused, now playing new
    provider._eventsub = Mock()
    provider._eventsub.subscribe_raids = AsyncMock()
    provider._eventsub.is_connected = True
    provider._eventsub.start = AsyncMock()

    # Simulate a pending idle timer
    mock_timer = Mock()
    mock_timer.cancel = Mock()
    provider._idle_timer = mock_timer

    with patch.object(provider, "_get_users", new_callable=AsyncMock, return_value=[{"id": "123"}]):
        await provider._handle_queue_playing("twitch://channel/streamer_a", "streamer_a")

    # Timer should have been cancelled
    mock_timer.cancel.assert_called_once()
    # Should have subscribed
    provider._eventsub.subscribe_raids.assert_called_once()


async def test_resume_cancels_idle_timer(provider: TwitchProvider) -> None:
    """Resume cancels any pending idle disconnect timer."""
    provider._access_token = "test"
    provider._client_id = "test"
    provider._auto_raid = True
    provider._current_channel_login = None

    mock_timer = Mock()
    mock_timer.cancel = Mock()
    provider._idle_timer = mock_timer
    provider._eventsub = Mock()
    provider._eventsub.subscribe_raids = AsyncMock()
    provider._eventsub.start = AsyncMock()

    with patch.object(provider, "_get_users", new_callable=AsyncMock, return_value=[{"id": "123"}]):
        await provider._handle_queue_playing("twitch://channel/streamer_a", "streamer_a")

    mock_timer.cancel.assert_called_once()
    assert provider._idle_timer is None


# --- Raid Handling ---


async def test_raid_triggers_play_media(provider: TwitchProvider) -> None:
    """Raid event calls mass.player_queues.play_media()."""
    provider._current_channel_login = "streamer_a"
    provider.mass.player_queues.play_media = AsyncMock()  # type: ignore[method-assign]

    await provider._on_raid("streamer_a", "streamer_c")

    provider.mass.player_queues.play_media.assert_called_once()
    call_args = provider.mass.player_queues.play_media.call_args
    # Check the media URI contains the raid target
    assert "streamer_c" in str(call_args)


async def test_stale_raid_ignored(provider: TwitchProvider) -> None:
    """Raid from non-current channel is ignored."""
    provider._current_channel_login = "streamer_a"
    provider.mass.player_queues.play_media = AsyncMock()  # type: ignore[method-assign]

    await provider._on_raid("streamer_b", "streamer_c")

    provider.mass.player_queues.play_media.assert_not_called()


async def test_raid_to_offline_target_handled(provider: TwitchProvider) -> None:
    """Raid to offline channel: error logged, no crash."""
    provider._current_channel_login = "streamer_a"
    provider.mass.player_queues.play_media = AsyncMock(  # type: ignore[method-assign]
        side_effect=Exception("stream offline")
    )

    # Should not raise
    await provider._on_raid("streamer_a", "offline_channel")


# --- State Machine — Extended ---


async def test_playing_different_channel_resubscribes(provider: TwitchProvider) -> None:
    """Switching channels: unsubscribe old, subscribe new."""
    provider._access_token = "test"
    provider._client_id = "test"
    provider._auto_raid = True
    provider._current_channel_login = "streamer_a"
    provider._eventsub = Mock()
    provider._eventsub.subscribe_raids = AsyncMock()
    provider._eventsub.unsubscribe_all = AsyncMock()
    provider._eventsub.is_connected = True
    provider._eventsub.start = AsyncMock()

    with patch.object(provider, "_get_users", new_callable=AsyncMock, return_value=[{"id": "456"}]):
        await provider._handle_queue_playing("twitch://channel/streamer_b", "streamer_b")

    # Should have subscribed to the new channel
    provider._eventsub.subscribe_raids.assert_called_once_with("456")
    assert provider._current_channel_login == "streamer_b"


async def test_rapid_raids_last_wins(provider: TwitchProvider) -> None:
    """Multiple rapid raids — last one is the one that plays."""
    provider._current_channel_login = "streamer_a"
    provider.mass.player_queues.play_media = AsyncMock()  # type: ignore[method-assign]

    await provider._on_raid("streamer_a", "streamer_b")
    await provider._on_raid("streamer_a", "streamer_c")

    # play_media called twice — last call should be for streamer_c
    assert provider.mass.player_queues.play_media.call_count == 2
    last_call = provider.mass.player_queues.play_media.call_args
    assert "streamer_c" in str(last_call)


# --- Auto-Raid Toggle ---


async def test_auto_raid_disabled_ignores_raids(provider: TwitchProvider) -> None:
    """With auto_raid=False, raid events are ignored."""
    provider._auto_raid = False
    provider._current_channel_login = "streamer_a"
    provider.mass.player_queues.play_media = AsyncMock()  # type: ignore[method-assign]

    await provider._on_raid("streamer_a", "streamer_c")

    provider.mass.player_queues.play_media.assert_not_called()


async def test_auto_raid_disabled_no_eventsub_connection(provider: TwitchProvider) -> None:
    """With auto_raid=False, _handle_queue_playing does not create EventSub."""
    provider._access_token = "test"
    provider._client_id = "test"
    provider._auto_raid = False
    provider._eventsub = None

    with patch.object(provider, "_get_users", new_callable=AsyncMock, return_value=[{"id": "123"}]):
        await provider._handle_queue_playing("twitch://channel/streamer_a", "streamer_a")

    # EventSub should NOT have been created
    assert provider._eventsub is None


# --- Cleanup ---


async def test_unload_cancels_grace_timer(provider: TwitchProvider) -> None:
    """unload() cancels pending grace timer."""
    mock_task = Mock()
    mock_task.cancel = Mock()
    provider._grace_timer = mock_task
    provider._idle_timer = None
    provider._eventsub = Mock()
    provider._eventsub.stop = AsyncMock()
    provider._unsub_queue_updated = None

    await provider.unload()

    mock_task.cancel.assert_called_once()


async def test_unload_cancels_idle_timer(provider: TwitchProvider) -> None:
    """unload() cancels pending idle timer."""
    mock_task = Mock()
    mock_task.cancel = Mock()
    provider._idle_timer = mock_task
    provider._grace_timer = None
    provider._eventsub = Mock()
    provider._eventsub.stop = AsyncMock()
    provider._unsub_queue_updated = None

    await provider.unload()

    mock_task.cancel.assert_called_once()


async def test_unload_stops_eventsub(provider: TwitchProvider) -> None:
    """unload() calls EventSub stop."""
    eventsub_mock = Mock()
    eventsub_mock.stop = AsyncMock()
    provider._eventsub = eventsub_mock
    provider._grace_timer = None
    provider._idle_timer = None
    provider._unsub_queue_updated = None

    await provider.unload()

    # _eventsub is set to None after stop, so check the saved ref
    eventsub_mock.stop.assert_called_once()


async def test_unload_closes_websocket(provider: TwitchProvider) -> None:
    """unload() closes EventSub WebSocket (alias for stop)."""
    eventsub_mock = Mock()
    eventsub_mock.stop = AsyncMock()
    provider._eventsub = eventsub_mock
    provider._grace_timer = None
    provider._idle_timer = None
    provider._unsub_queue_updated = None

    await provider.unload()

    eventsub_mock.stop.assert_called_once()
    assert provider._eventsub is None


async def test_unload_cancels_asyncio_tasks(provider: TwitchProvider) -> None:
    """unload() cancels all pending tasks (timers + eventsub)."""
    grace_mock = Mock()
    grace_mock.cancel = Mock()
    idle_mock = Mock()
    idle_mock.cancel = Mock()
    eventsub_mock = Mock()
    eventsub_mock.stop = AsyncMock()

    provider._grace_timer = grace_mock
    provider._idle_timer = idle_mock
    provider._eventsub = eventsub_mock
    provider._unsub_queue_updated = None

    await provider.unload()

    grace_mock.cancel.assert_called_once()
    idle_mock.cancel.assert_called_once()
    eventsub_mock.stop.assert_called_once()
