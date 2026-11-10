"""Tests for the batched library lookups of play stats, track counts, formats and likes."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from music_assistant_models.enums import AlbumType, ContentType, MediaType
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ProviderMapping,
    Track,
    UniqueList,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

pytestmark = pytest.mark.asyncio

PROVIDER = "prov_a_inst"
USER_A = "user-a"
USER_B = "user-b"


def _mapping(
    item_id: str, audio_format: AudioFormat | None = None, available: bool = True
) -> ProviderMapping:
    """Return a mapping of the test music source."""
    return ProviderMapping(
        item_id=item_id,
        provider_domain="prov_a",
        provider_instance=PROVIDER,
        available=available,
        audio_format=audio_format or AudioFormat(),
    )


async def _add_artist(mass: MusicAssistant) -> Artist:
    """Add the library artist of the test albums."""
    return await mass.music.artists.add_item_to_library(
        Artist(
            item_id="0",
            provider="library",
            name="Lookup Artist",
            provider_mappings={_mapping("artist")},
        )
    )


async def _add_album(
    mass: MusicAssistant,
    artist: Artist,
    name: str,
    audio_format: AudioFormat | None = None,
    available: bool = True,
) -> Album:
    """Add a library album with one mapping."""
    return await mass.music.albums.add_item_to_library(
        Album(
            item_id="0",
            provider="library",
            name=name,
            album_type=AlbumType.ALBUM,
            provider_mappings={_mapping(f"album_{name}", audio_format, available)},
            artists=UniqueList([artist]),
        )
    )


async def _add_track(
    mass: MusicAssistant,
    artist: Artist,
    album: Album | None,
    name: str,
    audio_format: AudioFormat | None = None,
    available: bool = True,
) -> Track:
    """Add a library track with one mapping, on the given album."""
    return await mass.music.tracks.add_item_to_library(
        Track(
            item_id="0",
            provider="library",
            name=name,
            provider_mappings={_mapping(f"track_{name}", audio_format, available)},
            artists=UniqueList([artist]),
            album=album,
            disc_number=1 if album else 0,
            track_number=1 if album else 0,
        )
    )


async def _set_plays(
    mass: MusicAssistant, table: str, item_id: str | int, play_count: int, last_played: int
) -> None:
    """Store the play count and last played timestamp of a library item."""
    await mass.music.database.execute(
        f"UPDATE {table} SET play_count = :play_count, last_played = :last_played "
        "WHERE item_id = :item_id",
        {"play_count": play_count, "last_played": last_played, "item_id": int(item_id)},
    )
    await mass.music.database.commit()


async def test_play_stats_of_tracks_leave_out_unknown_ids(music_mass: MusicAssistant) -> None:
    """A track returns its own plays; an unplayed one 0 and an unknown id nothing."""
    mass = music_mass
    artist = await _add_artist(mass)
    played = await _add_track(mass, artist, None, "Played")
    unplayed = await _add_track(mass, artist, None, "Unplayed")
    await _set_plays(mass, mass.music.tracks.db_table, played.item_id, 3, 1700000000)

    stats = await mass.music.tracks.get_library_play_stats(
        [int(played.item_id), int(unplayed.item_id), 99999]
    )

    assert stats == {int(played.item_id): (3, 1700000000), int(unplayed.item_id): (0, 0)}
    assert await mass.music.tracks.get_library_play_stats([]) == {}


async def test_play_stats_of_an_album_include_its_tracks(music_mass: MusicAssistant) -> None:
    """An album adds the plays of its library tracks and takes their latest moment."""
    mass = music_mass
    artist = await _add_artist(mass)
    album = await _add_album(mass, artist, "Played Album")
    empty = await _add_album(mass, artist, "Empty Album")
    first = await _add_track(mass, artist, album, "First")
    second = await _add_track(mass, artist, album, "Second")
    await _set_plays(mass, mass.music.albums.db_table, album.item_id, 1, 1600000000)
    await _set_plays(mass, mass.music.tracks.db_table, first.item_id, 2, 1700000000)
    await _set_plays(mass, mass.music.tracks.db_table, second.item_id, 4, 1650000000)

    stats = await mass.music.albums.get_library_play_stats([int(album.item_id), int(empty.item_id)])

    assert stats == {int(album.item_id): (7, 1700000000), int(empty.item_id): (0, 0)}


async def test_track_counts_of_albums(music_mass: MusicAssistant) -> None:
    """An album counts its library tracks; one without tracks is left out."""
    mass = music_mass
    artist = await _add_artist(mass)
    album = await _add_album(mass, artist, "Counted Album")
    empty = await _add_album(mass, artist, "Trackless Album")
    await _add_track(mass, artist, album, "One")
    await _add_track(mass, artist, album, "Two")

    counts = await mass.music.albums.get_library_track_counts(
        [int(album.item_id), int(empty.item_id)]
    )

    assert counts == {int(album.item_id): 2}


async def test_audio_formats_of_albums_skip_unavailable_mappings(
    music_mass: MusicAssistant,
) -> None:
    """An album returns the best format per type of its and its tracks' available mappings."""
    mass = music_mass
    artist = await _add_artist(mass)
    album = await _add_album(
        mass, artist, "Format Album", AudioFormat(content_type=ContentType.MP3, bit_depth=16)
    )
    gone = await _add_album(
        mass,
        artist,
        "Unavailable Album",
        AudioFormat(content_type=ContentType.FLAC),
        available=False,
    )
    await _add_track(
        mass,
        artist,
        album,
        "Lossless",
        AudioFormat(content_type=ContentType.FLAC, bit_depth=16, sample_rate=44100),
    )
    await _add_track(
        mass,
        artist,
        album,
        "Hi-Res",
        AudioFormat(content_type=ContentType.FLAC, bit_depth=24, sample_rate=96000),
    )
    await _add_track(
        mass,
        artist,
        album,
        "Unavailable Track",
        AudioFormat(content_type=ContentType.DSF, bit_depth=1, sample_rate=2822400),
        available=False,
    )

    formats = await mass.music.albums.get_library_audio_formats(
        [int(album.item_id), int(gone.item_id)]
    )

    assert set(formats) == {int(album.item_id)}
    found = {
        (f.content_type, f.codec_type, f.bit_depth, f.sample_rate)
        for f in formats[int(album.item_id)]
    }
    assert found == {
        (ContentType.MP3, ContentType.UNKNOWN, 16, 44100),
        (ContentType.FLAC, ContentType.UNKNOWN, 24, 96000),
    }


async def test_liked_timestamps_only_hold_the_users_likes(music_mass: MusicAssistant) -> None:
    """Only the likes of the given user on items of the given media type are returned."""
    mass = music_mass
    artist = await _add_artist(mass)
    liked = await _add_track(mass, artist, None, "Liked")
    disliked = await _add_track(mass, artist, None, "Disliked")
    other_user = await _add_track(mass, artist, None, "Liked By B")
    await mass.music.favorites.set(MediaType.TRACK, int(liked.item_id), True, [USER_A])
    await mass.music.favorites.set(MediaType.TRACK, int(disliked.item_id), False, [USER_A])
    await mass.music.favorites.set(MediaType.TRACK, int(other_user.item_id), True, [USER_B])
    # an artist with the same library id as the liked track must not count
    await mass.music.favorites.set(MediaType.ARTIST, int(liked.item_id), True, [USER_A])
    ids = [int(liked.item_id), int(disliked.item_id), int(other_user.item_id)]

    likes = await mass.music.favorites.liked_timestamps(USER_A, MediaType.TRACK, ids)

    assert set(likes) == {int(liked.item_id)}
    assert likes[int(liked.item_id)] > 0
    assert await mass.music.favorites.liked_timestamps(USER_A, MediaType.ALBUM, ids) == {}
