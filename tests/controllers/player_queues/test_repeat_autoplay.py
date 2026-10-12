"""
Regression tests for repeat locking autoplay.

Repeat ONE/ALL masks the effective autoplay toggle without changing the saved preference, and
releasing repeat restores the saved preference or the current global default.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import PlayerType, RepeatMode
from music_assistant_models.errors import InvalidCommand
from music_assistant_models.media_items import Podcast, PodcastEpisode, Track
from music_assistant_models.media_items.provider_mapping import ProviderMapping
from music_assistant_models.player_queue import PlayerQueue

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.autoplay import AutoplayMode
from music_assistant.controllers.player_queues.queue_loader import QueueLoaderMixin
from music_assistant.controllers.player_queues.state import PlayerQueueData


def _mappings(item_id: str, provider: str) -> set[ProviderMapping]:
    """Build a single provider mapping for the given item."""
    return {
        ProviderMapping(
            item_id=item_id,
            provider_domain=provider,
            provider_instance=provider,
        )
    }


def _track(item_id: str = "t1", *, provider: str = "library") -> Track:
    """Build a minimal Track."""
    return Track(
        item_id=item_id,
        provider=provider,
        name=f"Track {item_id}",
        provider_mappings=_mappings(item_id, provider),
    )


def _podcast(item_id: str = "show1", provider: str = "test_prov") -> Podcast:
    """Build a minimal Podcast."""
    return Podcast(
        item_id=item_id,
        provider=provider,
        name="Show",
        provider_mappings=_mappings(item_id, provider),
    )


def _episode(
    item_id: str,
    position: int,
    podcast: Podcast,
    *,
    provider: str = "test_prov",
) -> PodcastEpisode:
    """Build a minimal PodcastEpisode of the given podcast."""
    return PodcastEpisode(
        item_id=item_id,
        provider=provider,
        name=f"Episode {position}",
        provider_mappings=_mappings(item_id, provider),
        position=position,
        podcast=podcast,
    )


def _queue_item(media_item: Any) -> Any:
    """Build a queue item stand-in for the given media item."""
    return SimpleNamespace(
        media_item=media_item, media_type=media_item.media_type, name=media_item.name
    )


def _controller(*, global_autoplay: bool = True) -> tuple[Any, dict[str, bool]]:
    """Build a queue-controller stand-in with the real toggle resolver bound."""
    queues = MagicMock(spec=PlayerQueuesController)
    queues._queue_data = {}
    queues.mass = MagicMock()
    defaults = {"autoplay_enabled": global_autoplay, "crossfade_enabled": False}
    queues.mass.config.get_raw_core_config_value = MagicMock(
        side_effect=lambda _core_module, key, _default: defaults[key]
    )
    queues.mass.call_later = MagicMock()
    queues.mass.cancel_timer = MagicMock()
    queues.mass.streams.is_smart_fades_active = MagicMock(return_value=False)
    queues.signal_update = MagicMock()
    queues.update_next_item_on_player = MagicMock()
    queues.is_smart_shuffle_active = MagicMock(return_value=False)
    queues.get = MagicMock(side_effect=lambda queue_id: queues._queue_data[queue_id].queue)
    queues.set_autoplay = lambda queue_id, autoplay_enabled: PlayerQueuesController.set_autoplay(
        queues, queue_id, autoplay_enabled
    )
    queues._schedule_autoplay_fill = lambda queue_id: (
        PlayerQueuesController._schedule_autoplay_fill(queues, queue_id)
    )
    queues._resolve_default_toggles = lambda queue_data: (
        PlayerQueuesController._resolve_default_toggles(queues, queue_data)
    )
    return queues, defaults


def _queue_data(
    *,
    repeat_mode: RepeatMode = RepeatMode.OFF,
    autoplay_override: bool | None = None,
    autoplay_enabled: bool = True,
    items: int = 0,
    current_index: int | None = None,
    is_dynamic: bool = False,
) -> PlayerQueueData:
    """Build a queue record for the repeat/autoplay tests."""
    queue = PlayerQueue(
        queue_id="q1",
        active=True,
        display_name="Queue",
        available=True,
        items=items,
        autoplay_enabled=autoplay_enabled,
        repeat_mode=repeat_mode,
        current_index=current_index,
        is_dynamic=is_dynamic,
    )
    queue.sources = []
    return PlayerQueueData(queue=queue, autoplay_override=autoplay_override)


def _loader(
    queue: Any, *items: Any, seeds: list[Any] | None = None, userid: str | None = None
) -> Any:
    """Build a queue-loader stand-in with the requested queue and queue items."""
    loader = MagicMock()
    loader.get = MagicMock(return_value=queue)
    loader._queue_data = {
        "q1": SimpleNamespace(
            queue=queue,
            items=list(items),
            enqueued_media_items=list(seeds or []),
            userid=userid,
        )
    }
    loader.load = AsyncMock()
    loader._fill_autoplay_music_tracks = AsyncMock()
    loader._fill_autoplay_next_in_series = AsyncMock()
    return loader


def _player(player_id: str = "p1") -> Any:
    """Build a player stand-in with the state fields the queue snapshot is built from."""
    return SimpleNamespace(
        player_id=player_id, state=SimpleNamespace(name="Player A", available=True)
    )


@pytest.mark.parametrize(
    ("repeat_mode", "autoplay_override", "global_autoplay", "expected_when_off", "should_schedule"),
    [
        (RepeatMode.ONE, None, True, True, True),
        (RepeatMode.ALL, True, False, True, True),
        (RepeatMode.ONE, False, True, False, False),
    ],
)
async def test_repeat_masks_saved_autoplay_and_restores_it_on_release(
    repeat_mode: RepeatMode,
    autoplay_override: bool | None,
    global_autoplay: bool,
    expected_when_off: bool,
    should_schedule: bool,
) -> None:
    """Repeat masks autoplay without changing the stored preference."""
    queues, _ = _controller(global_autoplay=global_autoplay)
    queue_data = _queue_data(
        autoplay_override=autoplay_override,
        items=4,
        current_index=0,
    )
    queues._queue_data["q1"] = queue_data

    await PlayerQueuesController.set_repeat(queues, "q1", repeat_mode)

    assert queue_data.autoplay_override is autoplay_override
    assert queue_data.queue.autoplay_enabled is False
    queues.mass.cancel_timer.assert_called_once_with("fill_autoplay_tracks_q1")

    await PlayerQueuesController.set_repeat(queues, "q1", RepeatMode.OFF)

    assert queue_data.queue.autoplay_enabled is expected_when_off
    if should_schedule:
        queues.mass.call_later.assert_called_once()
        assert queues.mass.call_later.call_args.args[0] == 5
        assert queues.mass.call_later.call_args.args[2] == "q1"
        assert queues.mass.call_later.call_args.kwargs["task_id"] == "fill_autoplay_tracks_q1"
    else:
        queues.mass.call_later.assert_not_called()


async def test_repeat_release_uses_updated_global_default_when_preference_follows_global() -> None:
    """A follow-global queue restores the current global value when repeat turns off."""
    queues, defaults = _controller(global_autoplay=False)
    queue_data = _queue_data(current_index=None)
    queues._queue_data["q1"] = queue_data

    await PlayerQueuesController.set_repeat(queues, "q1", RepeatMode.ONE)

    defaults["autoplay_enabled"] = True
    config = cast("Any", SimpleNamespace(values={}))
    await PlayerQueuesController.update_config(queues, config, {"values/autoplay_enabled"})

    # widened locals so mypy does not carry the pre-release narrowing into the post-release assert
    masked_autoplay: bool = queue_data.queue.autoplay_enabled
    assert masked_autoplay is False

    await PlayerQueuesController.set_repeat(queues, "q1", RepeatMode.OFF)

    restored_autoplay: bool = queue_data.queue.autoplay_enabled
    assert restored_autoplay is True
    queues.mass.cancel_timer.assert_called_once_with("fill_autoplay_tracks_q1")
    queues.mass.call_later.assert_not_called()


@pytest.mark.parametrize("repeat_mode", [RepeatMode.ONE, RepeatMode.ALL])
async def test_set_autoplay_allows_disabling_but_rejects_enabling_while_repeat_locked(
    repeat_mode: RepeatMode,
) -> None:
    """The repeat lock only blocks enabling autoplay, not turning it off."""
    queues, _ = _controller()
    queue_data = _queue_data(repeat_mode=repeat_mode, autoplay_override=True)
    queues._queue_data["q1"] = queue_data
    queues._resolve_default_toggles(queue_data)

    PlayerQueuesController.set_autoplay(queues, "q1", False)

    assert queue_data.autoplay_override is False
    assert queue_data.queue.autoplay_enabled is False

    with pytest.raises(InvalidCommand):
        PlayerQueuesController.set_autoplay(queues, "q1", True)

    with pytest.raises(InvalidCommand):
        PlayerQueuesController.set_dont_stop_the_music(queues, "q1", True)

    assert queue_data.autoplay_override is False
    assert queue_data.queue.autoplay_enabled is False
    assert queues.signal_update.call_count == 1
    queues.mass.call_later.assert_not_called()


async def test_repeat_unknown_does_not_lock_autoplay() -> None:
    """UNKNOWN repeat behaves like repeat off for autoplay."""
    queues, _ = _controller()
    queue_data = _queue_data(repeat_mode=RepeatMode.UNKNOWN, autoplay_override=False)
    queues._queue_data["q1"] = queue_data
    queues._resolve_default_toggles(queue_data)

    PlayerQueuesController.set_autoplay(queues, "q1", True)

    assert queue_data.autoplay_override is True
    assert queue_data.queue.autoplay_enabled is True
    queues.mass.cancel_timer.assert_not_called()
    queues.mass.call_later.assert_not_called()


async def test_repeating_queue_is_restored_with_locked_autoplay_from_cache() -> None:
    """A restored repeating queue keeps the repeat lock without losing the saved override."""
    queues, _ = _controller()
    stored = PlayerQueueData(
        queue=PlayerQueue(
            queue_id="p1",
            active=False,
            display_name="Player A",
            available=True,
            items=0,
            repeat_mode=RepeatMode.ONE,
            autoplay_enabled=True,
        ),
        autoplay_override=True,
    )
    queues.mass.cache.get = AsyncMock(side_effect=[stored.to_cache(), []])

    await PlayerQueuesController.on_player_register(queues, _player())

    restored = queues._queue_data["p1"]
    assert restored.autoplay_override is True
    assert restored.queue.repeat_mode is RepeatMode.ONE
    assert restored.queue.autoplay_enabled is False


async def test_repeating_queue_is_transferred_with_locked_autoplay_override() -> None:
    """A transferred repeating queue keeps the saved autoplay override and stays locked off."""
    queues, _ = _controller(global_autoplay=False)
    target_player = MagicMock()
    target_player.state.type = PlayerType.PLAYER
    target_player.state.active_group = None
    target_player.state.synced_to = None
    queues.mass.players.get_player = MagicMock(return_value=target_player)

    source_queue = PlayerQueue(
        queue_id="src",
        active=True,
        display_name="Source",
        available=True,
        items=0,
        repeat_mode=RepeatMode.ONE,
    )
    source_queue.sources = []
    target_queue = PlayerQueue(
        queue_id="tgt",
        active=True,
        display_name="Target",
        available=True,
        items=0,
    )
    target_queue.sources = []
    queues._queue_data = {
        "src": PlayerQueueData(queue=source_queue, autoplay_override=True),
        "tgt": PlayerQueueData(queue=target_queue, autoplay_override=False),
    }
    queues.load = AsyncMock()
    queues.resume = AsyncMock()
    queues.update_items = MagicMock()
    queues._clear = MagicMock()
    queues._notify_audio_source_transferred = AsyncMock()

    await PlayerQueuesController.transfer_queue(queues, "src", "tgt", auto_play=False)

    target_data = queues._queue_data["tgt"]
    assert target_data.autoplay_override is True
    assert target_data.queue.repeat_mode is RepeatMode.ONE
    assert target_data.queue.autoplay_enabled is False


@pytest.mark.parametrize("repeat_mode", [RepeatMode.ONE, RepeatMode.ALL])
async def test_repeat_still_rejects_dynamic_queues(repeat_mode: RepeatMode) -> None:
    """Dynamic queues keep their own repeat rules."""
    queues, _ = _controller()
    queue_data = _queue_data(is_dynamic=True)
    queues._queue_data["q1"] = queue_data

    with pytest.raises(InvalidCommand):
        await PlayerQueuesController.set_repeat(queues, "q1", repeat_mode)

    queues.signal_update.assert_not_called()
    queues.mass.cancel_timer.assert_not_called()
    queues.mass.call_later.assert_not_called()


async def test_autoplay_dispatch_rechecks_lock_after_user_context_restore_for_music() -> None:
    """A repeat lock acquired during auth restore prevents a music refill from dispatching."""
    queue = SimpleNamespace(queue_id="q1", display_name="Queue", autoplay_enabled=True)
    track = _track("seed")
    loader = _loader(queue, _queue_item(track), seeds=[track], userid="u1")

    async def _disable_autoplay(*_args: Any, **_kwargs: Any) -> Any:
        queue.autoplay_enabled = False
        return SimpleNamespace(user_id="u1")

    loader.mass.webserver.auth.get_user = AsyncMock(side_effect=_disable_autoplay)

    await QueueLoaderMixin._fill_autoplay_tracks(loader, "q1")

    loader.mass.webserver.auth.get_user.assert_awaited_once_with("u1")
    loader._fill_autoplay_music_tracks.assert_not_awaited()
    loader._fill_autoplay_next_in_series.assert_not_awaited()


async def test_autoplay_dispatch_rechecks_lock_after_user_context_restore_for_series() -> None:
    """A repeat lock acquired during auth restore prevents a series refill from dispatching."""
    queue = SimpleNamespace(queue_id="q1", display_name="Queue", autoplay_enabled=True)
    podcast = _podcast()
    last_item = _queue_item(_episode("ep1", 1, podcast))
    loader = _loader(queue, last_item, userid="u1")

    async def _disable_autoplay(*_args: Any, **_kwargs: Any) -> Any:
        queue.autoplay_enabled = False
        return SimpleNamespace(user_id="u1")

    loader.mass.webserver.auth.get_user = AsyncMock(side_effect=_disable_autoplay)

    await QueueLoaderMixin._fill_autoplay_tracks(loader, "q1")

    loader.mass.webserver.auth.get_user.assert_awaited_once_with("u1")
    loader._fill_autoplay_music_tracks.assert_not_awaited()
    loader._fill_autoplay_next_in_series.assert_not_awaited()


async def test_music_autoplay_refill_rechecks_lock_before_load() -> None:
    """A repeat lock that appears during music refill work blocks the final queue append."""
    queue = SimpleNamespace(queue_id="q1", display_name="Queue", autoplay_enabled=True)
    seed = _track("seed")
    next_track = _track("next")
    loader = _loader(queue, _queue_item(seed), seeds=[seed])
    loader._autoplay = MagicMock()
    loader._autoplay.resolve_mode.return_value = AutoplayMode.LIBRARY

    async def _disable_and_return_tracks(*_args: Any, **_kwargs: Any) -> list[Track]:
        queue.autoplay_enabled = False
        return [next_track]

    loader._autoplay.get_library_tracks = AsyncMock(side_effect=_disable_and_return_tracks)
    loader._smart_shuffle = MagicMock()
    loader._smart_shuffle.windows = MagicMock(return_value=MagicMock())
    loader.mass.music.recency.snapshot = AsyncMock(return_value=MagicMock())

    with patch(
        "music_assistant.controllers.player_queues.queue_loader.gate_tracks",
        return_value=[next_track],
    ):
        await QueueLoaderMixin._fill_autoplay_music_tracks(loader, "q1")

    loader._autoplay.get_library_tracks.assert_awaited_once()
    loader.mass.music.recency.snapshot.assert_awaited_once()
    loader.load.assert_not_awaited()


async def test_series_autoplay_refill_rechecks_lock_before_load() -> None:
    """A repeat lock that appears during series refill work blocks the final queue append."""
    queue = SimpleNamespace(queue_id="q1", display_name="Queue", autoplay_enabled=True)
    podcast = _podcast()
    last_item = _queue_item(_episode("ep1", 1, podcast))
    next_item = _episode("ep2", 2, podcast)
    loader = _loader(queue, last_item)

    async def _disable_and_return_next(*_args: Any, **_kwargs: Any) -> PodcastEpisode:
        queue.autoplay_enabled = False
        return next_item

    loader._media_resolver = MagicMock()
    loader._media_resolver.get_next_podcast_episode = AsyncMock(
        side_effect=_disable_and_return_next
    )

    await QueueLoaderMixin._fill_autoplay_next_in_series(loader, "q1", last_item)

    loader._media_resolver.get_next_podcast_episode.assert_awaited_once()
    loader.load.assert_not_awaited()
