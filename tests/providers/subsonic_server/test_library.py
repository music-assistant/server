"""Tests for the library aggregates, against a real library database."""

from __future__ import annotations

from unittest.mock import patch

from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import AlbumType, ProviderSharing
from music_assistant_models.media_items import Album, Artist, ProviderMapping, Track, UniqueList

from music_assistant.mass import MusicAssistant
from music_assistant.providers.subsonic_server.library import (
    album_track_stats,
    artist_album_counts,
)
from tests.common import set_music_source_access

USER_A = User(user_id="user-a", username="user-a", role=UserRole.USER)


def _mapping(provider_instance: str, item_id: str, in_library: bool = True) -> ProviderMapping:
    return ProviderMapping(
        item_id=item_id,
        provider_domain=provider_instance.removesuffix("_inst"),
        provider_instance=provider_instance,
        in_library=in_library,
    )


async def _seed(mass: MusicAssistant) -> tuple[str, str]:
    """Seed an artist with an album per source, the first one holding a track per source."""
    artist = await mass.music.artists.add_item_to_library(
        Artist(
            item_id="0",
            provider="library",
            name="Artist",
            provider_mappings={_mapping("prov_a_inst", "artist_a")},
        )
    )
    albums = []
    for name, provider_instance in (("Album A", "prov_a_inst"), ("Album B", "prov_b_inst")):
        album = Album(
            item_id="0",
            provider="library",
            name=name,
            album_type=AlbumType.ALBUM,
            provider_mappings={_mapping(provider_instance, name)},
            artists=UniqueList([artist]),
        )
        albums.append(await mass.music.albums.add_item_to_library(album))
    for number, (provider_instance, duration) in enumerate(
        (("prov_a_inst", 100), ("prov_b_inst", 200)), start=1
    ):
        track = Track(
            item_id="0",
            provider="library",
            name=f"Track {number}",
            duration=duration,
            provider_mappings={_mapping(provider_instance, f"track_{number}")},
            artists=UniqueList([artist]),
            album=albums[0],
            disc_number=1,
            track_number=number,
        )
        await mass.music.tracks.add_item_to_library(track)
    return str(artist.item_id), str(albums[0].item_id)


async def test_counts_follow_the_callers_music_sources(mass: MusicAssistant) -> None:
    """A user only counts the albums and tracks of the music sources they may see."""
    artist_id, album_id = await _seed(mass)
    set_music_source_access(
        mass,
        {
            "prov_a_inst": ProviderAccess(owner=USER_A.user_id, sharing=ProviderSharing.PRIVATE),
            "prov_b_inst": ProviderAccess(owner="user-b", sharing=ProviderSharing.PRIVATE),
        },
    )
    assert await album_track_stats(mass, [album_id]) == {album_id: (2, 300)}
    assert await artist_album_counts(mass, [artist_id]) == {artist_id: 2}
    with patch(
        "music_assistant.providers.subsonic_server.library.get_current_user",
        return_value=USER_A,
    ):
        assert await album_track_stats(mass, [album_id]) == {album_id: (1, 100)}
        assert await artist_album_counts(mass, [artist_id]) == {artist_id: 1}


async def test_artist_counts_only_albums_in_a_provider_library(mass: MusicAssistant) -> None:
    """An album none of its providers holds in their library is not counted, as in getArtist."""
    artist_id, album_id = await _seed(mass)
    await mass.music.database.update(
        "provider_mappings",
        {"media_type": "album", "item_id": int(album_id)},
        {"in_library": False},
    )
    assert await artist_album_counts(mass, [artist_id]) == {artist_id: 1}
    assert await album_track_stats(mass, [album_id]) == {album_id: (2, 300)}
