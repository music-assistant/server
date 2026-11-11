"""Tests for album track listings when one of the album's providers fails."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from unittest.mock import MagicMock, patch

import aiohttp
import pytest
from music_assistant_models.enums import AlbumType
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    ProviderPermissionDenied,
)
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


@pytest.fixture(name="mass")
def mass_fixture(music_mass_with_cache: MusicAssistant) -> MusicAssistant:
    """Run on a library-only instance with a cache store: album listings are cached."""
    return music_mass_with_cache


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
    ("error", "log_level"),
    [
        # an album a provider no longer lists stays that way: a note, not a warning on every play
        (MediaNotFoundError("Failed to get album tracks"), logging.DEBUG),
        (InvalidDataError("Bandcamp returned a response that is not usable JSON"), logging.WARNING),
        (ProviderPermissionDenied("Not available in your region"), logging.WARNING),
        # the transport error a provider's HTTP client raises on an HTML error page
        (
            aiohttp.ContentTypeError(
                MagicMock(),
                (),
                message="Attempt to decode JSON with unexpected mimetype: text/html",
            ),
            logging.WARNING,
        ),
        (aiohttp.ClientConnectionError("connection reset"), logging.WARNING),
        (aiohttp.ClientPayloadError("response payload is not completed"), logging.WARNING),
    ],
)
async def test_album_tracks_skip_failing_provider(
    mass: MusicAssistant, error: Exception, log_level: int, caplog: pytest.LogCaptureFixture
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
    assert [
        record.levelno
        for record in caplog.records
        if record.getMessage().startswith(
            "Unable to fetch tracks for album Test Album from provider streaming_inst"
        )
    ] == [log_level]


@pytest.mark.parametrize(
    "error",
    [
        LoginFailed("token expired"),
        # an HTTP status the provider did not translate: not one of the expected fetch
        # failures, so not skipped over either, unlike the HTML error page above
        aiohttp.ClientResponseError(MagicMock(), (), status=401, message="Unauthorized"),
        aiohttp.ClientResponseError(MagicMock(), (), status=500, message="Internal Server Error"),
    ],
)
async def test_album_tracks_do_not_hide_an_unexpected_provider_error(
    mass: MusicAssistant, error: Exception
) -> None:
    """A failure that is not a fetch failure is not skipped over, even with playable tracks left."""
    db_album = await _seed_album(mass, with_library_tracks=True)
    await set_global_cache_values({"available_providers": {"local_inst", "streaming_inst"}})
    with (
        patch.object(
            mass.music.albums,
            "_get_provider_album_tracks",
            side_effect=_failing_provider_fetch(error),
        ),
        pytest.raises(type(error)),
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


def _streaming_mapping_available(album: Album) -> bool:
    return next(
        mapping.available
        for mapping in album.provider_mappings
        if mapping.provider_instance == "streaming_inst"
    )


def _loaded_provider(instance_id: str) -> Callable[..., MagicMock]:
    """Return a fake provider lookup that resolves every provider to the given instance."""

    def _get_provider(*_args: object, **_kwargs: object) -> MagicMock:
        provider = MagicMock()
        provider.instance_id = instance_id
        return provider

    return _get_provider


async def _album_tracks_with_failure(
    mass: MusicAssistant, db_album: Album, error: Exception, served_by: str = "streaming_inst"
) -> MagicMock:
    """List the album's tracks with the streaming lookup failing, served by the given instance."""
    await set_global_cache_values({"available_providers": {"local_inst", "streaming_inst"}})
    with (
        patch.object(mass, "get_provider", side_effect=_loaded_provider(served_by)),
        patch.object(
            mass.music.albums,
            "_get_provider_album_tracks",
            side_effect=_failing_provider_fetch(error),
        ) as fetch,
    ):
        await mass.music.albums.tracks(db_album.item_id, "library")
    return fetch


async def test_album_tracks_mark_a_mapping_the_provider_does_not_find(mass: MusicAssistant) -> None:
    """A provider that no longer finds the album gets its mapping marked unavailable."""
    db_album = await _seed_album(mass, with_library_tracks=True)
    await _album_tracks_with_failure(mass, db_album, MediaNotFoundError("Album not found"))
    stored = await mass.music.albums.get_library_item(db_album.item_id)
    assert not _streaming_mapping_available(stored)
    assert len(stored.provider_mappings) == 2


async def test_album_tracks_keep_a_mapping_on_a_transient_failure(mass: MusicAssistant) -> None:
    """A provider that fails for another reason keeps its mapping available."""
    db_album = await _seed_album(mass, with_library_tracks=True)
    await _album_tracks_with_failure(mass, db_album, ProviderPermissionDenied("Not in your region"))
    stored = await mass.music.albums.get_library_item(db_album.item_id)
    assert _streaming_mapping_available(stored)


async def test_album_tracks_keep_a_mapping_another_account_does_not_find(
    mass: MusicAssistant,
) -> None:
    """Another account of the service standing in and lacking the album says nothing about it."""
    db_album = await _seed_album(mass, with_library_tracks=True)
    await _album_tracks_with_failure(
        mass, db_album, MediaNotFoundError("Album not found"), served_by="streaming_inst_2"
    )
    stored = await mass.music.albums.get_library_item(db_album.item_id)
    assert _streaming_mapping_available(stored)


async def test_album_tracks_skip_an_unavailable_mapping(mass: MusicAssistant) -> None:
    """A mapping already marked unavailable is not fetched again."""
    db_album = await _seed_album(mass, with_library_tracks=True)
    await _album_tracks_with_failure(mass, db_album, MediaNotFoundError("Album not found"))
    stored = await mass.music.albums.get_library_item(db_album.item_id)
    # assembled anew, as a refresh does, instead of served from the cache
    async with mass.cache.handle_refresh(True):
        fetch = await _album_tracks_with_failure(
            mass, stored, MediaNotFoundError("Album not found")
        )
    assert [call.args[1] for call in fetch.call_args_list] == ["local_inst"]


async def test_a_mapping_found_again_is_available_again(mass: MusicAssistant) -> None:
    """A mapping marked unavailable is marked available again when it is linked once more."""
    db_album = await _seed_album(mass, with_library_tracks=True)
    await _album_tracks_with_failure(mass, db_album, MediaNotFoundError("Album not found"))
    added = await mass.music.albums.add_unclaimed_provider_mappings(
        db_album.item_id, [_mapping("streaming_inst", "album_streaming", in_library=False)]
    )
    stored = await mass.music.albums.get_library_item(db_album.item_id)
    assert added == []
    assert _streaming_mapping_available(stored)
