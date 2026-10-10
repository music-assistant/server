"""Tests for the cache of the listings assembled from the providers."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, patch

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.enums import ListingType, MediaType
from music_assistant_models.helpers import create_uri
from music_assistant_models.media_items import Playlist, ProviderMapping, Track

from music_assistant.controllers.music.listing_cache import (
    Listing,
    cached_listing,
    invalidate_listings,
)
from music_assistant.mass import MusicAssistant

from .helpers import create_album, create_track

USER_A = User(user_id="user-a", username="user-a", role=UserRole.USER)
USER_B = User(user_id="user-b", username="user-b", role=UserRole.USER)


@pytest.fixture(scope="module", name="mass")
async def mass_fixture(music_mass_module: MusicAssistant) -> MusicAssistant:
    """Return the module-scoped database-only fixture, with its cache store set up."""
    await music_mass_module.cache.setup(await music_mass_module.config.get_core_config("cache"))
    return music_mass_module


async def _album_tracks(
    mass: MusicAssistant,
    uri: str,
    assemble: AsyncMock,
    narrowed_to: list[str] | None = None,
) -> list[Track]:
    return await cached_listing(
        mass, ListingType.ALBUM_TRACKS, uri, assemble, item_type=Track, narrowed_to=narrowed_to
    )


async def test_a_listing_is_assembled_once(mass: MusicAssistant) -> None:
    """The second call is served from the cache, with items rebuilt from it."""
    tracks = [create_track("spotify_1", "t1", name="One"), create_track("spotify_1", "t2")]
    assemble = AsyncMock(return_value=Listing(tracks))
    uri = create_uri(MediaType.ALBUM, "spotify_1", "assembled_once")

    first = await _album_tracks(mass, uri, assemble)
    second = await _album_tracks(mass, uri, assemble)

    assemble.assert_awaited_once()
    assert first is tracks
    assert second == tracks
    assert second[0] is not tracks[0]
    assert second[0].name == "One"
    assert second[0].artists[0].name == "Test Artist"


async def test_the_users_music_sources_tell_listings_apart(mass: MusicAssistant) -> None:
    """A listing narrowed to the user's sources is kept apart from the whole one."""
    assemble = AsyncMock(return_value=Listing([create_track("spotify_1", "t1")]))
    uri = create_uri(MediaType.ALBUM, "spotify_1", "narrowed")

    await _album_tracks(mass, uri, assemble)
    await _album_tracks(mass, uri, assemble, narrowed_to=["spotify_1"])
    await _album_tracks(mass, uri, assemble, narrowed_to=["spotify_1"])

    assert assemble.await_count == 2


async def test_a_refresh_assembles_anew_and_replaces_the_cached_listing(
    mass: MusicAssistant,
) -> None:
    """A refresh request bypasses the cache and what it assembles is served from then on."""
    assemble = AsyncMock(return_value=Listing([create_track("spotify_1", "old")]))
    uri = create_uri(MediaType.ALBUM, "spotify_1", "refreshed")
    await _album_tracks(mass, uri, assemble)

    assemble.return_value = Listing([create_track("spotify_1", "new")])
    async with mass.cache.handle_refresh(True):
        refreshed = await _album_tracks(mass, uri, assemble)
    served = await _album_tracks(mass, uri, assemble)

    assert assemble.await_count == 2
    assert [track.item_id for track in refreshed] == ["new"]
    assert [track.item_id for track in served] == ["new"]


async def test_an_incomplete_listing_is_served_but_not_kept(mass: MusicAssistant) -> None:
    """A listing a source could not contribute to is assembled again on the next call."""
    assemble = AsyncMock(return_value=Listing([create_track("spotify_1", "t1")], complete=False))
    uri = create_uri(MediaType.ALBUM, "spotify_1", "incomplete")

    first = await _album_tracks(mass, uri, assemble)
    second = await _album_tracks(mass, uri, assemble)

    assert assemble.await_count == 2
    assert [track.item_id for track in first] == [track.item_id for track in second] == ["t1"]


async def test_a_playlist_edit_drops_its_cached_listing(mass: MusicAssistant) -> None:
    """An edit drops the listing under the playlist's library and provider ids."""
    playlist = Playlist(
        item_id="pl1",
        provider="spotify_1",
        name="Edited",
        provider_mappings={
            ProviderMapping(item_id="pl1", provider_domain="spotify", provider_instance="spotify_1")
        },
    )
    db_playlist = await mass.music.playlists.add_item_to_library(playlist)
    uris = [cast("str", db_playlist.uri), create_uri(MediaType.PLAYLIST, "spotify_1", "pl1")]
    assemble = AsyncMock(return_value=Listing([create_track("spotify_1", "t1")]))
    for uri in uris:
        await cached_listing(mass, ListingType.PLAYLIST_TRACKS, uri, assemble, item_type=Track)

    # what add_playlist_tracks and remove_playlist_tracks end with
    await mass.music.playlists._request_metadata_refresh(db_playlist)

    for uri in uris:
        await cached_listing(mass, ListingType.PLAYLIST_TRACKS, uri, assemble, item_type=Track)
    assert assemble.await_count == 4


async def test_a_refresh_of_the_item_drops_the_listings_under_every_id(
    mass: MusicAssistant,
) -> None:
    """Refreshing an item drops its listings under its library id and its provider ids."""
    db_album = await mass.music.albums.add_item_to_library(create_album("spotify_1", "alb"))
    uris = [cast("str", db_album.uri), create_uri(MediaType.ALBUM, "spotify_1", "alb")]
    assemble = AsyncMock(return_value=Listing([create_track("spotify_1", "t1")]))
    for uri in uris:
        await _album_tracks(mass, uri, assemble)

    await invalidate_listings(mass, db_album)

    for uri in uris:
        await _album_tracks(mass, uri, assemble)
    assert assemble.await_count == 4


async def test_favorite_state_follows_the_calling_user(mass: MusicAssistant) -> None:
    """A cached listing carries the likes of whoever asks, never of whoever filled it."""
    library_track = await mass.music.tracks.add_item_to_library(create_track("spotify_1", "liked"))
    await mass.music.favorites.set(MediaType.TRACK, int(library_track.item_id), True, ["user-a"])
    library_track.favorite = True
    assemble = AsyncMock(return_value=Listing([library_track]))
    uri = create_uri(MediaType.ALBUM, "spotify_1", "favorites")
    await _album_tracks(mass, uri, assemble)

    with patch(
        "music_assistant.controllers.music.listing_cache.get_current_user", return_value=USER_B
    ):
        assert [track.favorite for track in await _album_tracks(mass, uri, assemble)] == [None]
    with patch(
        "music_assistant.controllers.music.listing_cache.get_current_user", return_value=USER_A
    ):
        assert [track.favorite for track in await _album_tracks(mass, uri, assemble)] == [True]
    assemble.assert_awaited_once()
