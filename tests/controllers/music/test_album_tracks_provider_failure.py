"""Tests for album track listings when one of the album's providers fails."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from unittest.mock import patch

import pytest
from music_assistant_models.enums import AlbumType
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError
from music_assistant_models.helpers import set_global_cache_values
from music_assistant_models.media_items import (
    Album,
    Artist,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.mass import MusicAssistant

pytestmark = pytest.mark.asyncio


def _mapping(provider_instance: str, item_id: str, in_library: bool = True) -> ProviderMapping:
    return ProviderMapping(
        item_id=item_id,
        provider_domain=provider_instance.removesuffix("_inst"),
        provider_instance=provider_instance,
        in_library=in_library,
    )


async def _seed_album(mass: MusicAssistant, *, with_library_tracks: bool) -> Album:
    """Seed a library album mapped to a local provider and a non-library streaming provider."""
    artist = Artist(
        item_id="0",
        provider="library",
        name="Test Artist",
        provider_mappings={_mapping("local_inst", "artist_local")},
    )
    db_artist = await mass.music.artists.add_item_to_library(artist)
    album = Album(
        item_id="0",
        provider="library",
        name="Test Album",
        album_type=AlbumType.ALBUM,
        provider_mappings={
            _mapping("local_inst", "album_local"),
            _mapping("streaming_inst", "album_streaming", in_library=False),
        },
        artists=UniqueList([db_artist]),
    )
    db_album = await mass.music.albums.add_item_to_library(album)
    if not with_library_tracks:
        return db_album
    for idx, name in enumerate(["Track One", "Track Two"], start=1):
        track = Track(
            item_id="0",
            provider="library",
            name=name,
            provider_mappings={_mapping("local_inst", f"track_local_{idx}")},
            artists=UniqueList([db_artist]),
            album=db_album,
            disc_number=1,
            track_number=idx,
        )
        await mass.music.tracks.add_item_to_library(track)
    return db_album


def _failing_provider_fetch(
    error: Exception,
) -> Callable[[str, str], Awaitable[list[Track]]]:
    """Return a fake provider tracklist fetch that fails for the streaming provider only."""

    async def _fetch(_item_id: str, provider_instance_id_or_domain: str) -> list[Track]:
        if provider_instance_id_or_domain == "streaming_inst":
            raise error
        return []

    return _fetch


@pytest.mark.parametrize(
    "error",
    [
        MediaNotFoundError("Failed to get album tracks"),
        InvalidDataError("Bandcamp returned a response that is not usable JSON"),
    ],
)
async def test_album_tracks_skip_failing_provider(
    mass: MusicAssistant, error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    """A failing secondary provider is skipped (and logged) so the album still plays."""
    db_album = await _seed_album(mass, with_library_tracks=True)
    # both providers are loaded and available; the streaming one merely errors on the fetch
    await set_global_cache_values({"available_providers": {"local_inst", "streaming_inst"}})
    with patch.object(
        mass.music.albums,
        "_get_provider_album_tracks",
        side_effect=_failing_provider_fetch(error),
    ):
        tracks = await mass.music.albums.tracks(db_album.item_id, "library")
    assert [track.name for track in tracks] == ["Track One", "Track Two"]
    assert "Unable to fetch tracks for album Test Album from provider streaming_inst" in caplog.text


async def test_album_tracks_raise_when_nothing_playable(mass: MusicAssistant) -> None:
    """Pin that the guard does not over-suppress: with nothing playable, the error still surfaces."""
    db_album = await _seed_album(mass, with_library_tracks=False)
    error = MediaNotFoundError("Failed to get album tracks")
    with (
        patch.object(
            mass.music.albums,
            "_get_provider_album_tracks",
            side_effect=_failing_provider_fetch(error),
        ),
        pytest.raises(MediaNotFoundError),
    ):
        await mass.music.albums.tracks(db_album.item_id, "library")


async def test_album_tracks_raise_when_library_tracks_unavailable(mass: MusicAssistant) -> None:
    """Library tracks that are all unavailable do not count as playable: the error still surfaces."""
    db_album = await _seed_album(mass, with_library_tracks=True)
    # only the (failing) streaming provider is available, so the library tracks are unplayable
    await set_global_cache_values({"available_providers": {"streaming_inst"}})
    error = MediaNotFoundError("Failed to get album tracks")
    with (
        patch.object(
            mass.music.albums,
            "_get_provider_album_tracks",
            side_effect=_failing_provider_fetch(error),
        ),
        pytest.raises(MediaNotFoundError),
    ):
        await mass.music.albums.tracks(db_album.item_id, "library")
