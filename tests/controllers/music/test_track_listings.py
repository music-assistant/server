"""Tests for the track listings: searched, sorted and paged on the server."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import ProviderFeature, SortDirection, SortField
from music_assistant_models.errors import InvalidDataError, ProviderUnavailableError
from music_assistant_models.media_items import SearchResults

from music_assistant.controllers.music.constants import SIMILAR_ITEMS_LIMIT
from music_assistant.models.music_provider import MusicProvider

from .helpers import create_album, create_track

if TYPE_CHECKING:
    from music_assistant_models.media_items import Track

    from music_assistant.mass import MusicAssistant


@pytest.fixture(name="mass")
def mass_fixture(music_mass_with_cache: MusicAssistant) -> MusicAssistant:
    """Run on a library-only instance with a cache store: provider listings are cached."""
    return music_mass_with_cache


def _song_on(album_id: str, album_name: str) -> Track:
    """Return the test song as a search finds it on the given album."""
    track = create_track("qobuz_1", f"song_{album_id}", name="Song")
    track.album = create_album("qobuz_1", album_id, name=album_name)
    return track


def _names(items: list[Any]) -> list[str]:
    return [item.name for item in items]


async def test_track_albums_follow_the_listing_contract(mass: MusicAssistant) -> None:
    """The albums of a track are searched for once, then searched, sorted and paged."""
    await mass.music.albums.add_item_to_library(create_album("qobuz_1", "lib", name="Library"))
    search = AsyncMock(
        return_value=SearchResults(
            tracks=[
                _song_on("zebra", "Zebra"),
                _song_on("lib", "Library"),
                _song_on("apple", "Apple"),
            ]
        )
    )

    async def _albums(**kwargs: Any) -> list[str]:
        return _names(await mass.music.tracks.albums("song", "qobuz_1", **kwargs))

    with (
        patch.object(mass, "get_provider", return_value=SimpleNamespace(instance_id="qobuz_1")),
        patch.object(mass.music.tracks, "get", AsyncMock(return_value=_song_on("x", "X"))),
        patch.object(mass.music, "search", search),
    ):
        # the order the search finds them in
        assert await _albums() == ["Zebra", "Library", "Apple"]
        assert await _albums(sort_field=SortField.NAME) == ["Apple", "Library", "Zebra"]
        assert await _albums(search="zeb") == ["Zebra"]
        assert await _albums(limit=1, offset=2) == ["Apple"]
        assert await _albums(in_library_only=True) == ["Library"]
        with pytest.raises(InvalidDataError):
            await _albums(sort_field=SortField.DURATION)
    search.assert_awaited_once()


async def test_library_track_albums_are_the_whole_set(mass: MusicAssistant) -> None:
    """The library albums of a track are read without the listing page size."""
    query = AsyncMock(return_value=[])
    with patch.object(mass.music.albums, "get_library_items_by_query", query):
        await mass.music.tracks.get_library_track_albums("1")
    assert query.await_args is not None
    assert query.await_args.kwargs["limit"] == 0


def _similar_tracks_provider() -> MagicMock:
    """Return the music provider of the reference track, which lists similar tracks."""
    provider = MagicMock(spec=MusicProvider)
    provider.instance_id = "qobuz_1"
    provider.supported_features = {ProviderFeature.SIMILAR_TRACKS}
    return provider


async def test_similar_tracks_follow_the_listing_contract(mass: MusicAssistant) -> None:
    """The similar tracks are fetched once, in the provider's ranking, then sorted and paged."""
    similar = [
        create_track("qobuz_1", "t1", name="Zebra", duration=100),
        create_track("qobuz_1", "t2", name="Apple", duration=300),
        create_track("qobuz_1", "t3", name="Mango", duration=200),
    ]
    fetch = AsyncMock(return_value=(similar, None))

    async def _similar(**kwargs: Any) -> list[str]:
        return _names(await mass.music.tracks.similar_tracks("ref", "qobuz_1", **kwargs))

    with (
        patch.object(mass, "get_provider", return_value=_similar_tracks_provider()),
        patch.object(
            mass.music.tracks, "get", AsyncMock(return_value=create_track("qobuz_1", "ref"))
        ),
        patch.object(mass.music.tracks, "_get_similar_tracks_from_provider", fetch),
    ):
        assert await _similar() == ["Zebra", "Apple", "Mango"]
        assert await _similar(sort_field=SortField.DURATION, sort_direction=SortDirection.DESC) == [
            "Apple",
            "Mango",
            "Zebra",
        ]
        assert await _similar(search="ango") == ["Mango"]
        assert await _similar(limit=2) == ["Zebra", "Apple"]
        assert await _similar(limit=2, offset=2) == ["Mango"]
        with pytest.raises(InvalidDataError):
            await _similar(sort_field=SortField.YEAR)
    fetch.assert_awaited_once()
    assert fetch.await_args is not None
    # the provider is asked for the whole listing, whatever page is asked for
    assert fetch.await_args.args[2] == SIMILAR_ITEMS_LIMIT


async def test_similar_tracks_with_a_lookup_are_assembled_every_time(mass: MusicAssistant) -> None:
    """A lookup changes what is assembled, so that listing is not kept."""
    fetch = AsyncMock(return_value=([create_track("qobuz_1", "t1", name="One")], None))
    with (
        patch.object(mass, "get_provider", return_value=_similar_tracks_provider()),
        patch.object(
            mass.music.tracks, "get", AsyncMock(return_value=create_track("qobuz_1", "ref"))
        ),
        patch.object(mass.music.tracks, "_get_similar_tracks_from_provider", fetch),
    ):
        for _ in range(2):
            tracks = await mass.music.tracks.similar_tracks("ref", "qobuz_1", allow_lookup=True)
            assert _names(tracks) == ["One"]
    assert fetch.await_count == 2


async def test_similar_tracks_after_a_failing_provider_are_assembled_again(
    mass: MusicAssistant,
) -> None:
    """Similar tracks a failing provider might have answered first are served, not kept."""
    music_provider = _similar_tracks_provider()
    metadata_provider = SimpleNamespace(name="metadata")

    async def _fetch(provider: Any, *_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        if provider is music_provider:
            return None, ProviderUnavailableError("rate limited")
        return [create_track("qobuz_1", "t1", name="One")], None

    fetch = AsyncMock(side_effect=_fetch)
    with (
        patch.object(mass, "get_provider", return_value=music_provider),
        patch.object(mass, "get_providers_supporting_feature", return_value=[metadata_provider]),
        patch.object(
            mass.music.tracks, "get", AsyncMock(return_value=create_track("qobuz_1", "ref"))
        ),
        patch.object(mass.music.tracks, "_get_similar_tracks_from_provider", fetch),
    ):
        for _ in range(2):
            assert _names(await mass.music.tracks.similar_tracks("ref", "qobuz_1")) == ["One"]
    # the failing provider and the fallback, on both calls
    assert fetch.await_count == 4
