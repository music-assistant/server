"""Tests for the similar artists Autoplay mode."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.media_items import Artist, ItemMapping, Track, UniqueList
from music_assistant_models.media_items.provider_mapping import ProviderMapping

from music_assistant.controllers.player_queues.autoplay import Autoplay, AutoplayMode
from music_assistant.controllers.player_queues.queue_loader import QueueLoaderMixin


def _mappings(item_id: str, provider: str) -> set[ProviderMapping]:
    """Build a single provider mapping for the given item."""
    return {ProviderMapping(item_id=item_id, provider_domain=provider, provider_instance=provider)}


def _artist(name: str, provider: str = "library") -> Artist:
    """Build an artist with an id taken from its name."""
    item_id = name.lower().replace(" ", "-")
    return Artist(
        item_id=item_id,
        provider=provider,
        name=name,
        provider_mappings=_mappings(item_id, provider),
    )


def _track(name: str, artist: Artist) -> Track:
    """Build a track by the given artist, from the artist's provider."""
    item_id = f"{artist.item_id}-{name.lower().replace(' ', '-')}"
    return Track(
        item_id=item_id,
        provider=artist.provider,
        name=name,
        provider_mappings=_mappings(item_id, artist.provider),
        artists=UniqueList([ItemMapping.from_item(artist)]),
    )


def _queue_items(*tracks: Track) -> list[Any]:
    """Build queue item stand-ins for the given tracks."""
    return [SimpleNamespace(media_item=track) for track in tracks]


def _autoplay(*tracks: Track, similar: list[Artist]) -> tuple[Autoplay, MagicMock]:
    """Build an Autoplay over a queue holding the tracks, with the given similar artists."""
    queues = MagicMock()
    queues.queue_data = MagicMock(return_value=SimpleNamespace(items=_queue_items(*tracks)))
    queues.mass.music.artists.similar_artists = AsyncMock(
        side_effect=lambda _item_id, _provider, limit: similar[:limit] if limit else similar
    )
    queues.mass.music.artists.get_library_item_by_prov_id = AsyncMock(return_value=None)
    return Autoplay(queues), queues


def _queue() -> Any:
    return SimpleNamespace(queue_id="q1", display_name="Queue", autoplay_enabled=True)


async def test_lists_the_similar_artists_not_in_the_queue() -> None:
    """A similar artist that already has a track in the queue is skipped."""
    abba, boney_m = _artist("ABBA"), _artist("Boney M.")
    bee_gees, chic = _artist("Bee Gees"), _artist("Chic")
    autoplay, queues = _autoplay(
        _track("Rivers of Babylon", boney_m),
        _track("Waterloo", abba),
        similar=[boney_m, bee_gees, chic],
    )

    assert await autoplay.get_similar_artists(_queue()) == [bee_gees, chic]
    queues.mass.music.artists.similar_artists.assert_awaited_once_with("abba", "library", limit=0)


async def test_picks_a_similar_artist_further_down_the_list() -> None:
    """A similar artist further down the list is picked when those above it are in the queue."""
    abba = _artist("ABBA")
    hits = {
        "Boney M.": "Rasputin",
        "Bee Gees": "Stayin' Alive",
        "Chic": "Le Freak",
        "Donna Summer": "I Feel Love",
        "Gloria Gaynor": "I Will Survive",
        "Village People": "Y.M.C.A.",
        "KC and the Sunshine Band": "Get Down Tonight",
        "Earth, Wind & Fire": "September",
        "Sister Sledge": "We Are Family",
        "Kool & the Gang": "Celebration",
    }
    queued = [_artist(name) for name in hits]
    diana_ross = _artist("Diana Ross")
    autoplay, _ = _autoplay(
        *(_track(hits[artist.name], artist) for artist in queued),
        _track("Waterloo", abba),
        similar=[*queued, diana_ross],
    )

    assert await autoplay.get_similar_artists(_queue()) == [diana_ross]


async def test_uses_the_library_copy_of_a_provider_artist() -> None:
    """The similar artists of a provider track's artist come from its library copy."""
    spotify_abba, library_abba = _artist("ABBA", "spotify"), _artist("ABBA")
    autoplay, queues = _autoplay(_track("Waterloo", spotify_abba), similar=[_artist("Bee Gees")])
    queues.mass.music.artists.get_library_item_by_prov_id.return_value = library_abba

    await autoplay.get_similar_artists(_queue())

    queues.mass.music.artists.similar_artists.assert_awaited_once_with("abba", "library", limit=0)


async def test_no_similar_artist_left() -> None:
    """Without a similar artist outside the queue there is nothing to play."""
    abba, boney_m = _artist("ABBA"), _artist("Boney M.")
    autoplay, _ = _autoplay(
        _track("Daddy Cool", boney_m), _track("Waterloo", abba), similar=[boney_m]
    )

    assert await autoplay.get_similar_artists(_queue()) == []


def _loader(similar: list[Artist], tracks: dict[str, list[Track]]) -> Any:
    """Build a queue loader stand-in in the similar artists mode."""
    abba = _artist("ABBA")
    loader = MagicMock()
    loader.get = MagicMock(return_value=_queue())
    loader._queue_data = {
        "q1": SimpleNamespace(
            queue=loader.get.return_value,
            items=_queue_items(_track("Waterloo", abba)),
            enqueued_media_items=[abba],
            userid="user",
        )
    }
    loader._autoplay.resolve_mode.return_value = AutoplayMode.SIMILAR_ARTISTS
    loader._autoplay.get_similar_artists = AsyncMock(return_value=similar)
    loader._media_resolver.get_artist_tracks = AsyncMock(
        side_effect=lambda artist: tracks[artist.name]
    )
    loader._fill_autoplay_similar_artist = lambda queue_id: (
        QueueLoaderMixin._fill_autoplay_similar_artist(loader, queue_id)
    )
    loader.load = AsyncMock()
    return loader


async def _fill(loader: Any, disliked: set[str] | None = None) -> None:
    """Run the Autoplay refill, with the given track names disliked."""

    async def without_disliked(_mass: Any, _userid: Any, tracks: list[Track]) -> list[Track]:
        return [track for track in tracks if track.name not in (disliked or set())]

    with (
        patch(
            "music_assistant.controllers.player_queues.queue_loader.without_disliked_tracks",
            side_effect=without_disliked,
        ),
        patch(
            "music_assistant.controllers.player_queues.queue_loader.build_queue_item",
            side_effect=lambda _queue_id, item, origin: (item.name, origin.container.name),
        ),
    ):
        await QueueLoaderMixin._fill_autoplay_music_tracks(loader, "q1")


async def test_queues_the_similar_artist_the_same_way_as_playing_it() -> None:
    """The similar artist's tracks are added in the order playing that artist gives them."""
    bee_gees = _artist("Bee Gees")
    loader = _loader(
        [bee_gees],
        {"Bee Gees": [_track("Stayin' Alive", bee_gees), _track("Night Fever", bee_gees)]},
    )

    await _fill(loader)

    assert loader.load.await_args.args[1] == [
        ("Stayin' Alive", "Bee Gees"),
        ("Night Fever", "Bee Gees"),
    ]
    assert loader.load.await_args.kwargs == {"insert_at_index": 2}


async def test_leaves_out_disliked_tracks() -> None:
    """A disliked track of the similar artist is not added."""
    bee_gees = _artist("Bee Gees")
    loader = _loader(
        [bee_gees],
        {"Bee Gees": [_track("Stayin' Alive", bee_gees), _track("Night Fever", bee_gees)]},
    )

    await _fill(loader, disliked={"Stayin' Alive"})

    assert loader.load.await_args.args[1] == [("Night Fever", "Bee Gees")]


async def test_adds_nothing_without_a_similar_artist() -> None:
    """Without a similar artist nothing is added."""
    loader = _loader([], {})

    await _fill(loader)

    loader.load.assert_not_awaited()


async def test_tries_the_next_artist_when_one_fails_to_load() -> None:
    """When a similar artist's tracks fail to load, the next similar artist is added."""
    boney_m, bee_gees = _artist("Boney M."), _artist("Bee Gees")
    loader = _loader([boney_m, bee_gees], {})

    async def artist_tracks(artist: Artist) -> list[Track]:
        if artist is boney_m:
            raise MusicAssistantError("failed")
        return [_track("Night Fever", bee_gees)]

    loader._media_resolver.get_artist_tracks.side_effect = artist_tracks

    await _fill(loader)

    assert loader.load.await_args.args[1] == [("Night Fever", "Bee Gees")]
