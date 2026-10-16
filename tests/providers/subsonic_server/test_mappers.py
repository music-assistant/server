"""Tests for the conversion of Music Assistant media items to Subsonic objects."""

from __future__ import annotations

from datetime import UTC, datetime

from music_assistant_models.enums import AlbumType, ContentType, MediaType
from music_assistant_models.media_items import (
    Album,
    Artist,
    ItemMapping,
    MediaItemMetadata,
    ProviderMapping,
    Track,
    UniqueList,
)
from music_assistant_models.media_items.audio_format import AudioFormat

from music_assistant.providers.subsonic_server import mappers
from music_assistant.providers.subsonic_server.ids import to_subsonic_id

NAVIDROME = "opensubsonic--Bnc7DaHk"
ADDED = datetime(2026, 10, 16, 15, 12, 6, tzinfo=UTC)


def _mapping(item_id: str, content_type: ContentType = ContentType.FLAC) -> ProviderMapping:
    return ProviderMapping(
        item_id=item_id,
        provider_domain="opensubsonic",
        provider_instance=NAVIDROME,
        audio_format=AudioFormat(
            content_type=content_type, sample_rate=44100, bit_depth=16, channels=2, bit_rate=900
        ),
    )


def _track(provider: str = "library", favorite: bool = False) -> Track:
    return Track(
        item_id="3399" if provider == "library" else "V1RccGAZDO2SsPgKvAaYBv",
        provider=provider,
        name="Searchin'",
        version="Live",
        provider_mappings={_mapping("V1RccGAZDO2SsPgKvAaYBv")},
        duration=218,
        disc_number=1,
        track_number=8,
        artists=UniqueList(
            [
                ItemMapping(
                    media_type=MediaType.ARTIST,
                    item_id="130",
                    provider="library",
                    name="Never 2 Far",
                )
            ]
        ),
        album=ItemMapping(
            media_type=MediaType.ALBUM,
            item_id="285",
            provider="library",
            name="Infinite LP",
            year=2009,
        ),
        metadata=MediaItemMetadata(genres={"Hip-Hop", "Electro"}),
        favorite=favorite,
        date_added=ADDED,
    )


def test_song_from_library_track() -> None:
    """A library track maps to a Subsonic song with library ids and audio details."""
    song = mappers.song(_track())
    assert song["id"] == "tr-3399"
    assert song["isDir"] is False
    assert song["title"] == "Searchin' (Live)"
    assert song["album"] == "Infinite LP"
    assert song["albumId"] == "al-285"
    assert song["parent"] == "al-285"
    assert song["coverArt"] == "al-285"
    assert song["artist"] == "Never 2 Far"
    assert song["artistId"] == "ar-130"
    assert song["artists"] == [{"id": "ar-130", "name": "Never 2 Far"}]
    assert song["year"] == 2009
    assert song["genre"] == "Electro"
    assert song["genres"] == [{"name": "Electro"}, {"name": "Hip-Hop"}]
    assert song["track"] == 8
    assert song["discNumber"] == 1
    assert song["duration"] == 218
    assert song["contentType"] == "audio/flac"
    assert song["suffix"] == "flac"
    assert song["bitRate"] == 900
    assert song["samplingRate"] == 44100
    assert song["created"] == "2026-10-16T15:12:06Z"
    assert "starred" not in song
    assert song["type"] == "music"
    assert song["mediaType"] == "song"


def test_song_starred_and_mp3_suffix() -> None:
    """A liked track is starred, and MPEG audio is reported as mp3."""
    track = _track(favorite=True)
    track.provider_mappings = {_mapping("x", ContentType.MPEG)}
    song = mappers.song(track)
    assert song["starred"] == "2026-10-16T15:12:06Z"
    assert song["contentType"] == "audio/mpeg"
    assert song["suffix"] == "mp3"


def test_song_from_provider_track() -> None:
    """A track that is not in the library gets a provider id."""
    song = mappers.song(_track(provider=NAVIDROME))
    assert song["id"] == to_subsonic_id(MediaType.TRACK, "V1RccGAZDO2SsPgKvAaYBv", NAVIDROME)
    assert song["albumId"] == "al-285"


def test_album() -> None:
    """An album carries the given track count and duration, and its songs when given."""
    album = Album(
        item_id="285",
        provider="library",
        name="Infinite LP",
        provider_mappings={_mapping("al-1")},
        year=2009,
        album_type=AlbumType.COMPILATION,
        artists=UniqueList(
            [Artist(item_id="130", provider="library", name="Never 2 Far", provider_mappings=set())]
        ),
        date_added=ADDED,
    )
    data = mappers.album(album, song_count=12, duration=2400, songs=[{"id": "tr-1"}])
    assert data["id"] == "al-285"
    assert data["coverArt"] == "al-285"
    assert data["songCount"] == 12
    assert data["duration"] == 2400
    assert data["year"] == 2009
    assert data["artistId"] == "ar-130"
    assert data["isCompilation"] is True
    assert data["releaseTypes"] == ["Compilation"]
    assert data["song"] == [{"id": "tr-1"}]


def test_artist_without_image_has_no_cover_art() -> None:
    """Only an artist with an image announces cover art."""
    item = Artist(item_id="130", provider="library", name="Never 2 Far", provider_mappings=set())
    data = mappers.artist(item, album_count=3)
    assert data == {
        "id": "ar-130",
        "name": "Never 2 Far",
        "albumCount": 3,
        "sortName": "never 2 far",
    }


def test_iso_timestamp() -> None:
    """Timestamps are rendered in UTC with a Z suffix; a missing one gives the epoch."""
    assert mappers.iso_timestamp(None) == "1970-01-01T00:00:00Z"
    assert mappers.iso_timestamp("2026-10-16T17:12:06+02:00") == "2026-10-16T15:12:06Z"
    assert mappers.iso_timestamp(datetime(2026, 10, 16, 15, 12, 6))  # noqa: DTZ001 - naive input == "2026-10-16T15:12:06Z"
