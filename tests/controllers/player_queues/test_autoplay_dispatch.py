"""
Tests for what Autoplay appends per media type.

Autoplay is a single "keep going" switch: music continues with more music, a podcast episode
or audiobook with its own successor, and a live source (radio/audio source) is left alone -
including when the user stops it manually.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType, PlaybackState
from music_assistant_models.errors import ProviderUnavailableError
from music_assistant_models.media_items import (
    Audiobook,
    MediaCollection,
    Podcast,
    PodcastEpisode,
    Radio,
    Track,
    UniqueList,
)
from music_assistant_models.media_items.metadata import MediaItemCollection, MediaItemMetadata
from music_assistant_models.media_items.provider_mapping import ProviderMapping

from music_assistant.controllers.player_queues.autoplay import AutoplayMode
from music_assistant.controllers.player_queues.media_resolver import MediaResolver
from music_assistant.controllers.player_queues.playback_tracker import PlaybackTrackerMixin
from music_assistant.controllers.player_queues.queue_loader import QueueLoaderMixin
from music_assistant.helpers.collections import get_collection_item_id

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.media_items import PlayableMediaItemType
    from music_assistant_models.player_queue import PlayerQueue

    from music_assistant.controllers.player_queues.helpers import CompareState


def _mappings(item_id: str, provider: str, available: bool = True) -> set[ProviderMapping]:
    """Build a single provider mapping for the given item."""
    return {
        ProviderMapping(
            item_id=item_id,
            provider_domain=provider,
            provider_instance=provider,
            available=available,
        )
    }


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
    fully_played: bool | None = None,
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
        fully_played=fully_played,
    )


def _audiobook(
    item_id: str, *, provider: str = "library", collections: list[str] | None = None
) -> Audiobook:
    """Build a minimal Audiobook, optionally part of the given collection(s)."""
    return Audiobook(
        item_id=item_id,
        provider=provider,
        name=f"Book {item_id}",
        provider_mappings=_mappings(item_id, provider),
        metadata=MediaItemMetadata(
            collections=UniqueList([MediaItemCollection(title=x) for x in collections or []])
            if collections
            else None
        ),
    )


def _collection(name: str, *books: Audiobook) -> MediaCollection[Audiobook]:
    """Build a library audiobook collection holding the given books, in order."""
    return MediaCollection(
        item_id=get_collection_item_id(name, MediaType.AUDIOBOOK),
        name=name,
        provider="library",
        provider_mappings=set(),
        items=UniqueList(list(books)),
    )


async def _aiter(episodes: list[PodcastEpisode]) -> AsyncGenerator[PodcastEpisode]:
    """Yield the given episodes as the podcasts controller does."""
    for episode in episodes:
        yield episode


def _resolver() -> tuple[MediaResolver, MagicMock]:
    """Build a MediaResolver and return it with its mocked owning controller."""
    queues = MagicMock()
    return MediaResolver(queues), queues


# --- next podcast episode ---


async def test_next_podcast_episode_follows_the_played_one() -> None:
    """The episode after the given one is returned, with its resume position applied."""
    resolver, queues = _resolver()
    podcast = _podcast()
    played = _episode("ep1", 1, podcast)
    queues.mass.music.podcasts.episodes = MagicMock(
        return_value=_aiter(
            [_episode("ep1", 1, podcast), _episode("ep2", 2, podcast), _episode("ep3", 3, podcast)]
        )
    )
    queues.mass.music.get_resume_position = AsyncMock(return_value=(False, 42_000))

    result = await resolver.get_next_podcast_episode(played)

    assert result is not None
    assert result.item_id == "ep2"
    assert result.resume_position_ms == 42_000


async def test_next_podcast_episode_skips_fully_played() -> None:
    """An episode the user already finished is passed over."""
    resolver, queues = _resolver()
    podcast = _podcast()
    queues.mass.music.podcasts.episodes = MagicMock(
        return_value=_aiter(
            [
                _episode("ep1", 1, podcast),
                _episode("ep2", 2, podcast, fully_played=True),
                _episode("ep3", 3, podcast),
            ]
        )
    )
    queues.mass.music.get_resume_position = AsyncMock(return_value=(False, 0))

    result = await resolver.get_next_podcast_episode(_episode("ep1", 1, podcast))

    assert result is not None
    assert result.item_id == "ep3"


async def test_next_podcast_episode_at_end_of_feed() -> None:
    """The last episode of a podcast has no successor, so playback stops there."""
    resolver, queues = _resolver()
    podcast = _podcast()
    queues.mass.music.podcasts.episodes = MagicMock(
        return_value=_aiter([_episode("ep1", 1, podcast), _episode("ep2", 2, podcast)])
    )

    assert await resolver.get_next_podcast_episode(_episode("ep2", 2, podcast)) is None


# --- next audiobook ---


async def test_next_audiobook_follows_the_collection_order() -> None:
    """The next book of the series is returned, with its resume position applied."""
    resolver, queues = _resolver()
    book1 = _audiobook("1", collections=["Series"])
    book2 = _audiobook("2", collections=["Series"])
    queues.mass.music.audiobooks.get_collection = AsyncMock(
        return_value=_collection("Series", book1, book2)
    )
    queues.mass.music.get_resume_position = AsyncMock(return_value=(False, 120_000))

    result = await resolver.get_next_audiobook(book1)

    assert result is not None
    assert result.item_id == "2"
    assert result.resume_position_ms == 120_000
    assert queues.mass.music.audiobooks.get_collection.await_args.args == (
        get_collection_item_id("Series", MediaType.AUDIOBOOK),
    )


async def test_next_audiobook_skips_fully_played_books() -> None:
    """A book of the series that was already finished is passed over."""
    resolver, queues = _resolver()
    books = [_audiobook(str(i), collections=["Series"]) for i in (1, 2, 3)]
    queues.mass.music.audiobooks.get_collection = AsyncMock(
        return_value=_collection("Series", *books)
    )
    queues.mass.music.get_resume_position = AsyncMock(side_effect=[(True, 0), (False, 0)])

    result = await resolver.get_next_audiobook(books[0])

    assert result is not None
    assert result.item_id == "3"


async def test_next_audiobook_at_end_of_collection() -> None:
    """The last book of a series has no successor, so playback stops there."""
    resolver, queues = _resolver()
    book1 = _audiobook("1", collections=["Series"])
    book2 = _audiobook("2", collections=["Series"])
    queues.mass.music.audiobooks.get_collection = AsyncMock(
        return_value=_collection("Series", book1, book2)
    )

    assert await resolver.get_next_audiobook(book2) is None


async def test_standalone_audiobook_has_no_successor() -> None:
    """A book that is not part of a collection ends the queue."""
    resolver, queues = _resolver()

    assert await resolver.get_next_audiobook(_audiobook("1")) is None
    queues.mass.music.audiobooks.get_collection.assert_not_called()


async def test_audiobook_outside_the_library_has_no_successor() -> None:
    """Collections are built from library metadata, so a provider-only book stops the queue."""
    resolver, queues = _resolver()
    queues.mass.music.audiobooks.get_library_item_by_prov_id = AsyncMock(return_value=None)

    assert await resolver.get_next_audiobook(_audiobook("1", provider="test_prov")) is None


# --- dispatch on the queue's last item ---


def _queue_item(media_item: PlayableMediaItemType) -> Any:
    """Build a queue item stand-in for the given media item."""
    return SimpleNamespace(
        media_item=media_item, media_type=media_item.media_type, name=media_item.name
    )


def _loader(*items: Any, seeds: list[Any] | None = None) -> Any:
    """Build a queue loader stand-in holding a queue with the given items (autoplay on)."""
    loader = MagicMock()
    loader.get = MagicMock(
        return_value=SimpleNamespace(queue_id="q1", display_name="Queue", autoplay_enabled=True)
    )
    loader._queue_data = {
        "q1": SimpleNamespace(items=list(items), enqueued_media_items=seeds or [], userid=None)
    }
    loader._fill_autoplay_music_tracks = AsyncMock()
    loader._fill_autoplay_next_in_series = AsyncMock()
    loader.load = AsyncMock()
    return loader


def _track(item_id: str = "t1") -> Track:
    """Build a minimal Track."""
    return Track(
        item_id=item_id,
        provider="library",
        name=f"Track {item_id}",
        provider_mappings=_mappings(item_id, "library"),
    )


async def test_queue_ending_on_music_appends_music() -> None:
    """A queue that ends on a track keeps using the configured (music) Autoplay mode."""
    loader = _loader(_queue_item(_track()), seeds=[_track()])

    await QueueLoaderMixin._fill_autoplay_tracks(loader, "q1")

    loader._fill_autoplay_music_tracks.assert_awaited_once_with("q1")
    loader._fill_autoplay_next_in_series.assert_not_awaited()


async def test_queue_ending_on_audiobook_never_appends_music() -> None:
    """Music enqueued earlier must not leak into a queue that now ends on an audiobook."""
    book = _queue_item(_audiobook("1"))
    loader = _loader(_queue_item(_track()), book, seeds=[_track()])

    await QueueLoaderMixin._fill_autoplay_tracks(loader, "q1")

    loader._fill_autoplay_music_tracks.assert_not_awaited()
    loader._fill_autoplay_next_in_series.assert_awaited_once_with("q1", book)


async def test_queue_ending_on_podcast_episode_continues_the_podcast() -> None:
    """A queue that ends on a podcast episode continues with the next episode."""
    episode = _queue_item(_episode("ep1", 1, _podcast()))
    loader = _loader(episode, seeds=[_track()])

    await QueueLoaderMixin._fill_autoplay_tracks(loader, "q1")

    loader._fill_autoplay_music_tracks.assert_not_awaited()
    loader._fill_autoplay_next_in_series.assert_awaited_once_with("q1", episode)


async def test_queue_ending_on_live_source_appends_nothing() -> None:
    """Radio and audio sources have no natural end, so Autoplay does not apply."""
    for media_item in (
        Radio(
            item_id="r1",
            provider="test_prov",
            name="Radio",
            provider_mappings=_mappings("r1", "test_prov"),
        ),
        SimpleNamespace(media_type=MediaType.AUDIO_SOURCE, name="Line in"),
    ):
        loader = _loader(_queue_item(cast("PlayableMediaItemType", media_item)), seeds=[_track()])

        await QueueLoaderMixin._fill_autoplay_tracks(loader, "q1")

        loader._fill_autoplay_music_tracks.assert_not_awaited()
        loader._fill_autoplay_next_in_series.assert_not_awaited()


async def test_autoplay_disabled_appends_nothing() -> None:
    """With Autoplay off nothing is appended, whatever the queue ends on."""
    loader = _loader(_queue_item(_track()), seeds=[_track()])
    loader.get.return_value.autoplay_enabled = False

    await QueueLoaderMixin._fill_autoplay_tracks(loader, "q1")

    loader._fill_autoplay_music_tracks.assert_not_awaited()
    loader._fill_autoplay_next_in_series.assert_not_awaited()


async def test_autoplay_dispatch_bails_when_the_queue_is_removed_mid_lookup() -> None:
    """A queue removed while the owner's user context is restored gets no refill."""
    loader = _loader(_queue_item(_track()), seeds=[_track()])
    loader._queue_data["q1"].userid = "u1"

    async def _lookup_and_remove_queue(*_args: Any, **_kwargs: Any) -> Any:
        loader._queue_data.pop("q1")
        return MagicMock()

    loader.mass.webserver.auth.get_user = AsyncMock(side_effect=_lookup_and_remove_queue)

    await QueueLoaderMixin._fill_autoplay_tracks(loader, "q1")

    loader._fill_autoplay_music_tracks.assert_not_awaited()
    loader._fill_autoplay_next_in_series.assert_not_awaited()


async def test_music_refill_without_seeds_appends_nothing() -> None:
    """The music refill needs an enqueued item as its seed."""
    loader = _loader(_queue_item(_track()))

    await QueueLoaderMixin._fill_autoplay_music_tracks(loader, "q1")

    loader.load.assert_not_awaited()


async def test_auto_music_refill_falls_back_to_library_after_provider_failure() -> None:
    """AUTO mode falls back to library tracks when similar-track providers fail."""
    loader = _loader(_queue_item(_track()), seeds=[_track()])
    loader._autoplay.resolve_mode.return_value = AutoplayMode.AUTO
    loader._autoplay.get_library_tracks = AsyncMock(return_value=[])
    loader._get_similar_tracks = AsyncMock(side_effect=ProviderUnavailableError("offline"))
    loader.mass.music.recency.snapshot = AsyncMock(return_value=MagicMock())

    with patch(
        "music_assistant.controllers.player_queues.queue_loader.gate_tracks", return_value=[]
    ):
        await QueueLoaderMixin._fill_autoplay_music_tracks(loader, "q1")

    loader._autoplay.get_library_tracks.assert_awaited_once()


@pytest.mark.parametrize("reregistered", [False, True])
async def test_music_refill_bails_when_the_queue_is_removed_mid_fetch(
    reregistered: bool,
) -> None:
    """A queue removed (or re-registered fresh) mid-fetch must not receive the stale batch."""
    track = _track()
    loader = _loader(_queue_item(track), seeds=[track])
    loader._autoplay.resolve_mode.return_value = AutoplayMode.LIBRARY

    async def _fetch_and_remove_queue(*_args: Any, **_kwargs: Any) -> list[Track]:
        loader._queue_data.pop("q1")
        if reregistered:
            loader._queue_data["q1"] = SimpleNamespace(
                items=[], enqueued_media_items=[], userid=None
            )
        return [track]

    loader._autoplay.get_library_tracks = AsyncMock(side_effect=_fetch_and_remove_queue)
    loader.mass.music.recency.snapshot = AsyncMock(return_value=MagicMock())

    with patch(
        "music_assistant.controllers.player_queues.queue_loader.gate_tracks",
        return_value=[track],
    ):
        await QueueLoaderMixin._fill_autoplay_music_tracks(loader, "q1")

    loader.load.assert_not_awaited()


# --- appending the resolved successor ---


async def test_next_in_series_appends_the_successor() -> None:
    """The resolved next episode is appended behind the queue's last item."""
    podcast = _podcast()
    last_item = _queue_item(_episode("ep1", 1, podcast))
    loader = _loader(last_item)
    loader._media_resolver.get_next_podcast_episode = AsyncMock(
        return_value=_episode("ep2", 2, podcast)
    )

    await QueueLoaderMixin._fill_autoplay_next_in_series(loader, "q1", last_item)

    loader.load.assert_awaited_once()
    queue_items = loader.load.await_args.args[1]
    assert [x.media_item.item_id for x in queue_items] == ["ep2"]


async def test_next_in_series_without_successor_appends_nothing() -> None:
    """Nothing is appended when the podcast/series has nothing left to play."""
    last_item = _queue_item(_episode("ep1", 1, _podcast()))
    loader = _loader(last_item)
    loader._media_resolver.get_next_podcast_episode = AsyncMock(return_value=None)

    await QueueLoaderMixin._fill_autoplay_next_in_series(loader, "q1", last_item)

    loader.load.assert_not_awaited()


async def test_next_in_series_skips_an_already_queued_item() -> None:
    """A successor the user already queued themselves is not added twice."""
    podcast = _podcast()
    next_episode = _episode("ep2", 2, podcast)
    last_item = _queue_item(_episode("ep1", 1, podcast))
    loader = _loader(_queue_item(next_episode), last_item)
    loader._media_resolver.get_next_podcast_episode = AsyncMock(return_value=next_episode)

    await QueueLoaderMixin._fill_autoplay_next_in_series(loader, "q1", last_item)

    loader.load.assert_not_awaited()


# --- live sources survive a manual stop ---


def _stop_states(prev_item: Any) -> tuple[CompareState, CompareState]:
    """Build the state pair for a queue that went from playing the given item to idle."""
    prev_state = {
        "state": PlaybackState.PLAYING,
        "current_item_id": "i1",
        "current_item": prev_item,
        "last_playing_elapsed_time": 3600,
    }
    return cast("CompareState", prev_state), cast("CompareState", {"state": PlaybackState.IDLE})


def _tracker() -> Any:
    """Build a playback tracker stand-in for a single non-flow queue."""
    tracker = MagicMock()
    tracker._queue_data = {"q1": SimpleNamespace(flow_mode_stream_log=[])}
    return tracker


def _stopped_queue() -> PlayerQueue:
    """Build a queue stand-in that has no next item left to play."""
    return cast("PlayerQueue", SimpleNamespace(queue_id="q1", next_item=None, flow_mode=False))


def test_live_source_queue_survives_a_manual_stop() -> None:
    """Stopping a radio means the source stopped, not that the queue ran out: keep the items."""
    for media_type in (MediaType.RADIO, MediaType.AUDIO_SOURCE):
        tracker = _tracker()
        prev_state, new_state = _stop_states(
            SimpleNamespace(media_type=media_type, streamdetails=None, duration=None)
        )

        PlaybackTrackerMixin._handle_end_of_queue(tracker, _stopped_queue(), prev_state, new_state)

        tracker.mass.create_task.assert_not_called()


def test_finished_track_queue_is_settled_on_stop() -> None:
    """A queue that ran out of music is still settled (resumed, ended or cleared) as before."""
    tracker = _tracker()
    prev_state, new_state = _stop_states(
        SimpleNamespace(media_type=MediaType.TRACK, streamdetails=None, duration=3600)
    )

    PlaybackTrackerMixin._handle_end_of_queue(tracker, _stopped_queue(), prev_state, new_state)

    tracker.mass.create_task.assert_called_once()
