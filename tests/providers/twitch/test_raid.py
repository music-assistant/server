"""Test raid following: ref-counted streams, multi-queue support."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, Mock, patch

from music_assistant_models.enums import PlaybackState

from music_assistant.providers.twitch import TwitchProvider

# --- Stream Tracking (ref counting) ---


async def test_track_stream_start_increments(provider: TwitchProvider) -> None:
    """First stream for a channel sets count to 1."""
    provider._active_streams = {}
    provider._unsubscribe_timers = {}
    provider._auto_raid = False  # avoid EventSub side effects

    provider._track_stream_start("streamer_a")

    assert provider._active_streams["streamer_a"] == 1


async def test_track_stream_start_increments_multiple(provider: TwitchProvider) -> None:
    """Second stream for same channel increments to 2."""
    provider._active_streams = {"streamer_a": 1}
    provider._unsubscribe_timers = {}
    provider._auto_raid = False

    provider._track_stream_start("streamer_a")

    assert provider._active_streams["streamer_a"] == 2


async def test_track_stream_start_cancels_pending_unsubscribe(provider: TwitchProvider) -> None:
    """Starting a stream cancels any pending delayed unsubscribe for that channel."""
    provider._active_streams = {}
    provider._auto_raid = False
    mock_timer = Mock()
    mock_timer.cancel = Mock()
    provider._unsubscribe_timers = {"streamer_a": mock_timer}

    provider._track_stream_start("streamer_a")

    mock_timer.cancel.assert_called_once()
    assert "streamer_a" not in provider._unsubscribe_timers


async def test_track_stream_start_subscribes_on_first(provider: TwitchProvider) -> None:
    """First stream (0->1) triggers EventSub subscription."""
    provider._active_streams = {}
    provider._unsubscribe_timers = {}
    provider._auto_raid = True
    provider._access_token = "test"
    provider._client_id = "test"
    provider._eventsub = Mock()
    provider._eventsub.subscribe_raids = AsyncMock()
    provider._eventsub.start = AsyncMock()

    subscribed = asyncio.Event()
    original_subscribe = provider._eventsub.subscribe_raids

    async def subscribe_and_signal(*args: object, **kwargs: object) -> None:
        await original_subscribe(*args, **kwargs)
        subscribed.set()

    provider._eventsub.subscribe_raids = AsyncMock(side_effect=subscribe_and_signal)

    with patch.object(provider, "_get_users", new_callable=AsyncMock, return_value=[{"id": "123"}]):
        provider._track_stream_start("streamer_a")
        await asyncio.wait_for(subscribed.wait(), timeout=1.0)

    provider._eventsub.subscribe_raids.assert_called_once_with("123")


async def test_track_stream_start_no_subscribe_on_second(provider: TwitchProvider) -> None:
    """Second stream (1->2) does not trigger another subscription."""
    provider._active_streams = {"streamer_a": 1}
    provider._unsubscribe_timers = {}
    provider._auto_raid = True
    provider._access_token = "test"

    with patch.object(provider, "_subscribe_raids_for_channel", new_callable=AsyncMock) as mock_sub:
        provider._track_stream_start("streamer_a")
        await asyncio.sleep(0)  # yield to event loop — no task should be pending

    mock_sub.assert_not_called()


async def test_track_stream_end_decrements(provider: TwitchProvider) -> None:
    """Ending one of two streams decrements count."""
    provider._active_streams = {"streamer_a": 2}
    provider._unsubscribe_timers = {}

    provider._track_stream_end("streamer_a")

    assert provider._active_streams["streamer_a"] == 1


async def test_track_stream_end_last_starts_grace_timer(provider: TwitchProvider) -> None:
    """Ending last stream starts delayed unsubscribe timer."""
    provider._active_streams = {"streamer_a": 1}
    provider._unsubscribe_timers = {}

    provider._track_stream_end("streamer_a")

    assert "streamer_a" not in provider._active_streams
    assert "streamer_a" in provider._unsubscribe_timers
    # Clean up the task
    provider._unsubscribe_timers["streamer_a"].cancel()


# --- Delayed Unsubscribe ---


async def test_delayed_unsubscribe_calls_eventsub(provider: TwitchProvider) -> None:
    """After grace period, unsubscribe_raids is called."""
    provider._eventsub = Mock()
    provider._eventsub.unsubscribe_raids = AsyncMock()
    provider._unsubscribe_timers = {"streamer_a": Mock()}

    with (
        patch.object(provider, "_get_users", new_callable=AsyncMock, return_value=[{"id": "123"}]),
        patch("music_assistant.providers.twitch.asyncio.sleep", new_callable=AsyncMock),
    ):
        await provider._delayed_unsubscribe("streamer_a")

    provider._eventsub.unsubscribe_raids.assert_called_once_with("123")


# --- Raid Handling ---


async def test_raid_switches_playing_queues(provider: TwitchProvider) -> None:
    """Raid switches all queues playing the raiding channel."""
    provider._active_streams = {"streamer_a": 1}
    provider._unsubscribe_timers = {}

    queue1 = Mock()
    queue1.state = PlaybackState.PLAYING
    queue1.current_item = Mock()
    queue1.current_item.streamdetails = Mock()
    queue1.current_item.streamdetails.item_id = "streamer_a"
    queue1.queue_id = "queue_1"

    queue2 = Mock()
    queue2.state = PlaybackState.PLAYING
    queue2.current_item = Mock()
    queue2.current_item.streamdetails = Mock()
    queue2.current_item.streamdetails.item_id = "streamer_a"
    queue2.queue_id = "queue_2"

    provider.mass.player_queues.all = Mock(  # type: ignore[method-assign]
        return_value=(queue1, queue2)
    )
    provider.mass.player_queues.play_media = AsyncMock()  # type: ignore[method-assign]

    await provider._on_raid("streamer_a", "streamer_c")

    assert provider.mass.player_queues.play_media.call_count == 2
    calls = provider.mass.player_queues.play_media.call_args_list
    assert calls[0].kwargs["queue_id"] == "queue_1"
    assert calls[1].kwargs["queue_id"] == "queue_2"
    assert "streamer_c" in str(calls[0])


async def test_raid_skips_paused_queues(provider: TwitchProvider) -> None:
    """Raid does not switch paused queues."""
    provider._active_streams = {"streamer_a": 1}
    provider._unsubscribe_timers = {}

    queue1 = Mock()
    queue1.state = PlaybackState.PAUSED
    queue1.current_item = Mock()
    queue1.current_item.streamdetails = Mock()
    queue1.current_item.streamdetails.item_id = "streamer_a"

    provider.mass.player_queues.all = Mock(  # type: ignore[method-assign]
        return_value=(queue1,)
    )
    provider.mass.player_queues.play_media = AsyncMock()  # type: ignore[method-assign]

    await provider._on_raid("streamer_a", "streamer_c")

    provider.mass.player_queues.play_media.assert_not_called()


async def test_raid_skips_other_channels(provider: TwitchProvider) -> None:
    """Raid only switches queues playing the raiding channel."""
    provider._active_streams = {"streamer_a": 1}
    provider._unsubscribe_timers = {}

    queue1 = Mock()
    queue1.state = PlaybackState.PLAYING
    queue1.current_item = Mock()
    queue1.current_item.streamdetails = Mock()
    queue1.current_item.streamdetails.item_id = "streamer_b"  # different channel

    provider.mass.player_queues.all = Mock(  # type: ignore[method-assign]
        return_value=(queue1,)
    )
    provider.mass.player_queues.play_media = AsyncMock()  # type: ignore[method-assign]

    await provider._on_raid("streamer_a", "streamer_c")

    provider.mass.player_queues.play_media.assert_not_called()


async def test_raid_cleans_up_active_streams(provider: TwitchProvider) -> None:
    """Raid removes the raiding channel from active_streams."""
    provider._active_streams = {"streamer_a": 2}
    provider._unsubscribe_timers = {}
    provider.mass.player_queues.all = Mock(  # type: ignore[method-assign]
        return_value=()
    )
    provider.mass.player_queues.play_media = AsyncMock()  # type: ignore[method-assign]

    await provider._on_raid("streamer_a", "streamer_c")

    assert "streamer_a" not in provider._active_streams


async def test_raid_cancels_pending_unsubscribe(provider: TwitchProvider) -> None:
    """Raid cancels any pending unsubscribe timer for the raiding channel."""
    provider._active_streams = {"streamer_a": 1}
    mock_timer = Mock()
    mock_timer.cancel = Mock()
    provider._unsubscribe_timers = {"streamer_a": mock_timer}
    provider.mass.player_queues.all = Mock(  # type: ignore[method-assign]
        return_value=()
    )
    provider.mass.player_queues.play_media = AsyncMock()  # type: ignore[method-assign]

    await provider._on_raid("streamer_a", "streamer_c")

    mock_timer.cancel.assert_called_once()
    assert "streamer_a" not in provider._unsubscribe_timers


async def test_stale_raid_ignored(provider: TwitchProvider) -> None:
    """Raid from channel not in active_streams or grace period is ignored."""
    provider._active_streams = {}
    provider._unsubscribe_timers = {}
    provider.mass.player_queues.play_media = AsyncMock()  # type: ignore[method-assign]

    await provider._on_raid("streamer_b", "streamer_c")

    provider.mass.player_queues.play_media.assert_not_called()


async def test_raid_error_handled(provider: TwitchProvider) -> None:
    """play_media error is logged, not raised."""
    provider._active_streams = {"streamer_a": 1}
    provider._unsubscribe_timers = {}

    queue1 = Mock()
    queue1.state = PlaybackState.PLAYING
    queue1.current_item = Mock()
    queue1.current_item.streamdetails = Mock()
    queue1.current_item.streamdetails.item_id = "streamer_a"
    queue1.queue_id = "queue_1"

    provider.mass.player_queues.all = Mock(  # type: ignore[method-assign]
        return_value=(queue1,)
    )
    provider.mass.player_queues.play_media = AsyncMock(  # type: ignore[method-assign]
        side_effect=Exception("offline")
    )

    # Should not raise
    await provider._on_raid("streamer_a", "streamer_c")


# --- Raid During Grace Period (IDLE queues) ---


async def test_raid_switches_idle_queue_in_grace_period(provider: TwitchProvider) -> None:
    """Raid during grace period switches IDLE queues that were playing the raiding channel."""
    provider._active_streams = {}
    # Simulate grace period — timer exists for this channel
    mock_timer = Mock()
    mock_timer.cancel = Mock()
    provider._unsubscribe_timers = {"streamer_a": mock_timer}

    queue1 = Mock()
    queue1.state = PlaybackState.IDLE
    queue1.current_item = Mock()
    queue1.current_item.streamdetails = Mock()
    queue1.current_item.streamdetails.item_id = "streamer_a"
    queue1.queue_id = "queue_1"
    queue1.elapsed_time_last_updated = time.time() - 5  # idle for 5s

    provider.mass.player_queues.all = Mock(  # type: ignore[method-assign]
        return_value=(queue1,)
    )
    provider.mass.player_queues.play_media = AsyncMock()  # type: ignore[method-assign]

    await provider._on_raid("streamer_a", "streamer_c")

    provider.mass.player_queues.play_media.assert_called_once()


async def test_raid_ignores_idle_queue_too_long(provider: TwitchProvider) -> None:
    """IDLE queue that's been idle longer than 2x grace period is not switched."""
    provider._active_streams = {}
    mock_timer = Mock()
    mock_timer.cancel = Mock()
    provider._unsubscribe_timers = {"streamer_a": mock_timer}

    queue1 = Mock()
    queue1.state = PlaybackState.IDLE
    queue1.current_item = Mock()
    queue1.current_item.streamdetails = Mock()
    queue1.current_item.streamdetails.item_id = "streamer_a"
    queue1.queue_id = "queue_1"
    queue1.elapsed_time_last_updated = time.time() - 60  # idle for 60s (> 30s threshold)

    provider.mass.player_queues.all = Mock(  # type: ignore[method-assign]
        return_value=(queue1,)
    )
    provider.mass.player_queues.play_media = AsyncMock()  # type: ignore[method-assign]

    await provider._on_raid("streamer_a", "streamer_c")

    provider.mass.player_queues.play_media.assert_not_called()


async def test_raid_ignores_idle_queue_without_grace_period(provider: TwitchProvider) -> None:
    """IDLE queue is not switched when NOT in grace period (no timer)."""
    provider._active_streams = {"streamer_a": 1}
    provider._unsubscribe_timers = {}  # no grace timer

    queue1 = Mock()
    queue1.state = PlaybackState.IDLE
    queue1.current_item = Mock()
    queue1.current_item.streamdetails = Mock()
    queue1.current_item.streamdetails.item_id = "streamer_a"
    queue1.queue_id = "queue_1"

    provider.mass.player_queues.all = Mock(  # type: ignore[method-assign]
        return_value=(queue1,)
    )
    provider.mass.player_queues.play_media = AsyncMock()  # type: ignore[method-assign]

    await provider._on_raid("streamer_a", "streamer_c")

    provider.mass.player_queues.play_media.assert_not_called()


async def test_raid_ignores_paused_queue_in_grace_period(provider: TwitchProvider) -> None:
    """Paused queue is never switched, even during grace period."""
    provider._active_streams = {}
    mock_timer = Mock()
    mock_timer.cancel = Mock()
    provider._unsubscribe_timers = {"streamer_a": mock_timer}

    queue1 = Mock()
    queue1.state = PlaybackState.PAUSED
    queue1.current_item = Mock()
    queue1.current_item.streamdetails = Mock()
    queue1.current_item.streamdetails.item_id = "streamer_a"
    queue1.queue_id = "queue_1"

    provider.mass.player_queues.all = Mock(  # type: ignore[method-assign]
        return_value=(queue1,)
    )
    provider.mass.player_queues.play_media = AsyncMock()  # type: ignore[method-assign]

    await provider._on_raid("streamer_a", "streamer_c")

    provider.mass.player_queues.play_media.assert_not_called()


async def test_raid_in_grace_period_accepted(provider: TwitchProvider) -> None:
    """Raid is accepted when channel is only in _unsubscribe_timers (not _active_streams)."""
    provider._active_streams = {}
    mock_timer = Mock()
    mock_timer.cancel = Mock()
    provider._unsubscribe_timers = {"streamer_a": mock_timer}

    # No matching queues (all ended), but raid should still be accepted and timer cancelled
    provider.mass.player_queues.all = Mock(  # type: ignore[method-assign]
        return_value=()
    )
    provider.mass.player_queues.play_media = AsyncMock()  # type: ignore[method-assign]

    await provider._on_raid("streamer_a", "streamer_c")

    # Timer should be cancelled even with no matching queues
    mock_timer.cancel.assert_called_once()
    assert "streamer_a" not in provider._unsubscribe_timers


# --- Auto-Raid Toggle ---


async def test_auto_raid_disabled_ignores_raids(provider: TwitchProvider) -> None:
    """With auto_raid=False, raid events are ignored."""
    provider._auto_raid = False
    provider._active_streams = {"streamer_a": 1}
    provider.mass.player_queues.play_media = AsyncMock()  # type: ignore[method-assign]

    await provider._on_raid("streamer_a", "streamer_c")

    provider.mass.player_queues.play_media.assert_not_called()


async def test_auto_raid_disabled_no_subscribe(provider: TwitchProvider) -> None:
    """With auto_raid=False, _subscribe_raids_for_channel is a no-op."""
    provider._auto_raid = False
    provider._access_token = "test"
    provider._eventsub = None

    await provider._subscribe_raids_for_channel("streamer_a")

    assert provider._eventsub is None


# --- Cleanup ---


async def test_unload_cancels_timers(provider: TwitchProvider) -> None:
    """unload() cancels all pending unsubscribe timers."""
    mock_timer1 = Mock()
    mock_timer1.cancel = Mock()
    mock_timer2 = Mock()
    mock_timer2.cancel = Mock()
    provider._unsubscribe_timers = {"a": mock_timer1, "b": mock_timer2}
    provider._active_streams = {"a": 1}
    provider._eventsub = Mock()
    provider._eventsub.stop = AsyncMock()

    await provider.unload()

    mock_timer1.cancel.assert_called_once()
    mock_timer2.cancel.assert_called_once()
    assert len(provider._unsubscribe_timers) == 0
    assert len(provider._active_streams) == 0


async def test_unload_stops_eventsub(provider: TwitchProvider) -> None:
    """unload() stops EventSub and sets it to None."""
    eventsub_mock = Mock()
    eventsub_mock.stop = AsyncMock()
    provider._eventsub = eventsub_mock
    provider._unsubscribe_timers = {}
    provider._active_streams = {}

    await provider.unload()

    eventsub_mock.stop.assert_called_once()
    assert provider._eventsub is None
