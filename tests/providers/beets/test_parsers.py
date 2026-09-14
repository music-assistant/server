"""Tests for turning beets rows into Music Assistant media items."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from music_assistant_models.enums import AlbumType, ContentType, ExternalID, ImageType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import Album, Artist

from music_assistant.constants import VARIOUS_ARTISTS_MBID, VARIOUS_ARTISTS_NAME
from music_assistant.providers.beets.library import BeetsRow
from music_assistant.providers.beets.parsers import (
    ParseContext,
    album_checksum,
    expand_path,
    item_checksum,
    loudness_from_gains,
    parse_album,
    parse_album_type,
    parse_artist,
    parse_audio_format,
    parse_favorite,
    parse_track,
    split_multi_value,
)
from tests.providers.beets.beets_db import (
    ALBUM_MBID,
    ARTIST_MBID,
    GUEST_MBID,
    MULTI_VALUE_DELIMITER,
    RELEASE_GROUP_MBID,
    TRACK_MBID,
    album_fields,
    item_fields,
)

CTX = ParseContext(
    instance_id="beets--test",
    domain="beets",
    music_directory="/media/music",
    beets_directory="/home/kate/Music",
    favorite_rating_threshold=None,
)


def _row(row_id: int, fields: dict[str, Any], flex: dict[str, Any] | None = None) -> BeetsRow:
    """Build a row the way BeetsLibrary returns it."""
    return BeetsRow(id=row_id, fields={"id": row_id, **fields}, flex=flex or {})


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (f"a{MULTI_VALUE_DELIMITER}b", ["a", "b"]),
        ("a; b", ["a", "b"]),
        (f"a; b{MULTI_VALUE_DELIMITER}c", ["a; b", "c"]),
        (f"a{MULTI_VALUE_DELIMITER}", ["a", ""]),
        ("solo", ["solo"]),
        ("", []),
        (None, []),
    ],
)
def test_split_multi_value(value: object, expected: list[str]) -> None:
    """Values split on beets' delimiter, falling back to '; ' like beets does."""
    assert split_multi_value(value) == expected


@pytest.mark.parametrize(
    ("value", "beets_directory", "expected"),
    [
        (b"Artist/Album/01.flac", "/home/kate/Music", "/media/music/Artist/Album/01.flac"),
        ("Artist/01.mp3", None, "/media/music/Artist/01.mp3"),
        (b"/home/kate/Music/Artist/01.flac", "/home/kate/Music", "/media/music/Artist/01.flac"),
        (b"/home/kate/Music/Artist/01.flac", "/home/kate/Music/", "/media/music/Artist/01.flac"),
        (b"/home/kate/MusicExtra/01.flac", "/home/kate/Music", "/home/kate/MusicExtra/01.flac"),
        (b"/srv/other/01.flac", "/home/kate/Music", "/srv/other/01.flac"),
        (b"/srv/other/01.flac", None, "/srv/other/01.flac"),
        (b"", "/home/kate/Music", None),
        (None, "/home/kate/Music", None),
    ],
)
def test_expand_path(value: object, beets_directory: str | None, expected: str | None) -> None:
    """Relative paths join the music directory and the beets prefix is swapped on a boundary."""
    assert expand_path(value, "/media/music", beets_directory) == expected


@pytest.mark.parametrize(
    ("albumtypes", "albumtype", "expected"),
    [
        ("album; live", "album", AlbumType.LIVE),
        ("album; compilation; soundtrack", None, AlbumType.COMPILATION),
        (None, "ep", AlbumType.EP),
        ("Single", None, AlbumType.SINGLE),
        ("album", "album", AlbumType.ALBUM),
        ("broadcast", None, AlbumType.UNKNOWN),
        ("", "", AlbumType.UNKNOWN),
    ],
)
def test_parse_album_type(albumtypes: object, albumtype: object, expected: AlbumType) -> None:
    """The first beets type in priority order decides, case-insensitively."""
    assert parse_album_type(albumtypes, albumtype) == expected


def test_loudness_prefers_r128_then_replaygain() -> None:
    """R128 gains are relative to -23 LUFS, ReplayGain to -18."""
    assert loudness_from_gains({"r128_track_gain": 2.0, "rg_track_gain": -5.0}, "track") == -25.0
    assert loudness_from_gains({"rg_track_gain": -5.0}, "track") == -13.0
    assert loudness_from_gains({"rg_album_gain": 1.5}, "album") == -19.5
    assert loudness_from_gains({"rg_track_gain": "bogus"}, "track") is None
    assert loudness_from_gains({}, "track") is None


@pytest.mark.parametrize(
    ("flex", "threshold", "expected"),
    [
        ({"rating": "0.9"}, None, False),
        ({"rating": "0.9"}, 0.8, True),
        ({"rating": 0.8}, 0.8, True),
        ({"rating": "0.5"}, 0.8, False),
        ({"rating": "high"}, 0.8, False),
        ({}, 0.8, False),
    ],
)
def test_parse_favorite(flex: dict[str, Any], threshold: float | None, expected: bool) -> None:
    """Only a numeric rating at or above a configured threshold marks a favorite."""
    assert parse_favorite(flex, threshold) is expected


@pytest.mark.parametrize(
    ("path", "beets_format", "content_type", "codec_type"),
    [
        (b"a/01.flac", "FLAC", ContentType.FLAC, ContentType.FLAC),
        (b"a/01.m4a", "ALAC", ContentType.M4A, ContentType.ALAC),
        (b"a/01.WAV", "WAVE", ContentType.WAV, ContentType.UNKNOWN),
        (b"a/01.xyz", "Opus", ContentType.OPUS, ContentType.OPUS),
        (b"a/noext", None, ContentType.UNKNOWN, ContentType.UNKNOWN),
    ],
)
def test_parse_audio_format_types(
    path: bytes, beets_format: str | None, content_type: ContentType, codec_type: ContentType
) -> None:
    """The container comes from the extension and the codec from beets' format name."""
    audio_format = parse_audio_format({"path": path, "format": beets_format})
    assert (audio_format.content_type, audio_format.codec_type) == (content_type, codec_type)


def test_parse_audio_format_units_and_defaults() -> None:
    """Bitrate converts from bps to kbps and missing values keep the model defaults."""
    audio_format = parse_audio_format(
        item_fields(samplerate=96000, bitdepth=24, channels=6, bitrate=2304000)
    )
    assert (
        audio_format.sample_rate,
        audio_format.bit_depth,
        audio_format.channels,
        audio_format.bit_rate,
    ) == (96000, 24, 6, 2304)
    defaults = parse_audio_format({"path": b"a/01.flac"})
    assert (defaults.sample_rate, defaults.bit_depth, defaults.channels, defaults.bit_rate) == (
        44100,
        16,
        2,
        None,
    )


def test_item_checksum_follows_item_album_and_flex_changes() -> None:
    """Any change to the item, its album or either's flexible attributes changes the checksum."""
    album = _row(7, album_fields())
    item = _row(1, item_fields(album_id=7))
    base = item_checksum(item, album)

    assert item_checksum(_row(1, item_fields(album_id=7)), _row(7, album_fields())) == base
    assert item_checksum(_row(1, item_fields(album_id=7, title="Edited")), album) != base
    assert item_checksum(_row(1, item_fields(album_id=7), flex={"mood": "sad"}), album) != base
    assert item_checksum(item, _row(7, album_fields(label="Other"))) != base
    assert item_checksum(item, _row(7, album_fields(), flex={"rating": "1"})) != base
    assert item_checksum(item, None) != base
    assert album_checksum(album) != album_checksum(_row(7, album_fields(), flex={"a": "b"}))


def test_parse_artist_uses_name_as_id_and_drops_invalid_mbid() -> None:
    """Artists are keyed by name and only a valid MusicBrainz id is kept."""
    artist = parse_artist("Artist", CTX, sort_name="Artist, The", mbid="not-an-mbid")
    assert artist.item_id == "Artist"
    assert artist.sort_name == "Artist, The"
    assert artist.mbid is None
    mapping = next(iter(artist.provider_mappings))
    assert (mapping.provider_domain, mapping.provider_instance) == ("beets", "beets--test")


def test_parse_album_maps_fields() -> None:
    """Album fields, ids and art map onto the MA album."""
    row = _row(7, album_fields(artpath=b"Artist/Album/cover.jpg"))
    album = parse_album(row, CTX)

    assert album.item_id == "7"
    assert album.name == "Album"
    artist = album.artists[0]
    assert isinstance(artist, Artist)
    assert (artist.name, artist.mbid) == ("Artist", ARTIST_MBID)
    assert album.album_type == AlbumType.ALBUM
    assert album.year == 1999
    assert album.mbid == ALBUM_MBID
    assert (ExternalID.MB_RELEASEGROUP, RELEASE_GROUP_MBID) in album.external_ids
    assert (ExternalID.BARCODE, "0123456789012") in album.external_ids
    assert (ExternalID.ASIN, "B000002UAL") in album.external_ids
    assert album.metadata.label == "Label"
    assert album.metadata.genres == {"Rock", "Indie"}
    assert album.metadata.images is not None
    image = album.metadata.images[0]
    assert image.type == ImageType.THUMB
    assert image.path == f"album/7?cs={album_checksum(row)}"
    assert image.remotely_accessible is False


def test_parse_compilation_album_uses_various_artists() -> None:
    """A beets compilation gets the Various Artists album artist."""
    album = parse_album(_row(3, album_fields(comp=1)), CTX)
    artist = album.artists[0]
    assert isinstance(artist, Artist)
    assert (artist.name, artist.mbid) == (VARIOUS_ARTISTS_NAME, VARIOUS_ARTISTS_MBID)


def test_parse_album_without_art_or_original_year() -> None:
    """No artpath means no image, and year falls back when original_year is unset."""
    album = parse_album(_row(3, album_fields(original_year=0)), CTX)
    assert album.metadata.images is None
    assert album.year == 2001


def test_parse_legacy_album_uses_single_artist_fields() -> None:
    """A database without albumartists falls back to albumartist."""
    fields = {
        key: value
        for key, value in album_fields().items()
        if key not in {"albumartists", "albumartists_sort", "mb_albumartistids"}
    }
    assert [artist.name for artist in parse_album(_row(3, fields), CTX).artists] == ["Artist"]


def test_parse_track_maps_fields() -> None:
    """Track fields, ids, flexible attributes and the provider mapping map onto the MA track."""
    album = _row(7, album_fields(artpath=b"Artist/Album/cover.jpg"))
    item = _row(42, item_fields(album_id=7), flex={"mood": "happy", "rating": "0.9"})
    track = parse_track(item, album, replace(CTX, favorite_rating_threshold=0.8), "abc123")

    assert track.item_id == "42"
    assert track.provider == "beets--test"
    assert track.name == "Song"
    assert track.duration == 215
    assert (track.track_number, track.disc_number) == (1, 1)
    assert track.date_added == datetime(2023, 11, 14, 22, 13, 20, 500000, tzinfo=UTC)
    assert [artist.name for artist in track.artists] == ["Artist", "Guest"]
    assert [artist.mbid for artist in track.artists if isinstance(artist, Artist)] == [
        ARTIST_MBID,
        GUEST_MBID,
    ]
    assert isinstance(track.album, Album)
    assert track.album.item_id == "7"
    assert track.mbid == TRACK_MBID
    assert (ExternalID.ISRC, "USRC17607839") in track.external_ids
    assert (ExternalID.ACOUSTID, "9ff5a4e3-0a8b-4d1b-9d7e-0f1d6b9f1c11") in track.external_ids
    assert track.metadata.genres == {"Rock", "Indie"}
    assert track.metadata.style == "Shoegaze"
    assert track.metadata.grouping == "Side A"
    assert track.metadata.lyrics == "la la la"
    assert track.metadata.label == "Label"
    assert track.metadata.description == "A comment"
    assert track.metadata.release_date == datetime(2001, 5, 7, tzinfo=UTC)
    assert track.metadata.mood == "happy"
    assert track.favorite is True
    mapping = next(iter(track.provider_mappings))
    assert mapping.details == "abc123"
    assert mapping.in_library is True
    assert mapping.audio_format.content_type == ContentType.FLAC


def test_parse_track_without_album_is_a_singleton() -> None:
    """An item without an album row has no album."""
    assert parse_track(_row(1, item_fields()), None, CTX, "c").album is None


def test_parse_track_falls_back_to_single_artist_field() -> None:
    """Without the artists list the single artist field is used."""
    fields = item_fields(artists=None, artists_sort=None, mb_artistids=None)
    track = parse_track(_row(1, fields), None, CTX, "c")
    assert [artist.name for artist in track.artists] == ["Artist feat. Guest"]


def test_parse_track_uses_album_artists_when_track_has_none() -> None:
    """A track without any artist field takes its album's artists."""
    fields = item_fields(artists="", artist="")
    track = parse_track(_row(1, fields), _row(7, album_fields()), CTX, "c")
    assert [artist.name for artist in track.artists] == ["Artist"]


def test_album_without_artist_takes_the_track_artists() -> None:
    """An album without an album artist takes the artists of its track."""
    album = _row(7, album_fields(albumartists="", albumartist=""))
    track = parse_track(_row(1, item_fields(album_id=7)), album, CTX, "c")
    assert isinstance(track.album, Album)
    assert [artist.name for artist in track.album.artists] == ["Artist", "Guest"]


def test_parse_track_without_any_artist_is_invalid() -> None:
    """A track with no artist on the item or album cannot be imported."""
    with pytest.raises(InvalidDataError):
        parse_track(_row(1, item_fields(artists="", artist="")), None, CTX, "c")


def test_parse_track_name_falls_back_to_file_name() -> None:
    """An untitled item is named after its file."""
    assert parse_track(_row(1, item_fields(title="")), None, CTX, "c").name == "01 Song"


def test_parse_track_reads_legacy_genre_column() -> None:
    """A legacy database's single genre column fills the genres."""
    fields = {key: value for key, value in item_fields().items() if key != "genres"}
    track = parse_track(_row(1, {**fields, "genre": "Jazz"}), None, CTX, "c")
    assert track.metadata.genres == {"Jazz"}


def test_parse_track_without_dates() -> None:
    """Unset year and added values leave the dates empty."""
    track = parse_track(_row(1, item_fields(year=0, month=0, day=0, added=None)), None, CTX, "c")
    assert track.metadata.release_date is None
    assert track.date_added is None
