"""Tests for playlist parsing and generation helpers."""

from types import TracebackType
from typing import Any, Self, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import client_exceptions
from music_assistant_models.enums import ContentType, ExternalID, ImageType, MediaType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import (
    Album,
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    MediaItemMetadata,
    ProviderMapping,
    Radio,
    SoundEffect,
    Track,
    UniqueList,
)

from music_assistant.controllers.music.media.playlists import PlaylistController
from music_assistant.helpers import playlists
from music_assistant.helpers.compare import TrackMatchConfidence, compare_track_evidence
from music_assistant.helpers.playlists import (
    AlbumInfo,
    ArtistInfo,
    ImageInfo,
    IsHLSPlaylist,
    PlaylistItem,
    ProviderMappingInfo,
    construct_media_item_from_playlist_item,
    fetch_playlist,
    generate_m3u,
    media_item_to_playlist_item,
    parse_extinf_title,
    parse_m3u,
    parse_m3u_playlist_image,
    parse_m3u_playlist_name,
    sanitize_m3u_value,
)

# --------------------------------------------------------------------------- #
#  Existing tests (EXTINF parsing)                                             #
# --------------------------------------------------------------------------- #


def test_m3u_extinf_duration_not_truncated() -> None:
    """Test that EXTINF duration is parsed as full string, not truncated to first char."""
    m3u_data = "#EXTM3U\n#EXTINF:120,Test Song\nhttp://example.com/song.mp3\n"
    result = parse_m3u(m3u_data)
    assert len(result) == 1
    assert result[0].length == "120"
    assert result[0].title == "Test Song"


def test_m3u_extinf_negative_duration() -> None:
    """Test that EXTINF with -1 duration is treated as None (unknown length)."""
    m3u_data = "#EXTM3U\n#EXTINF:-1,Live Stream\nhttp://example.com/stream\n"
    result = parse_m3u(m3u_data)
    assert len(result) == 1
    assert result[0].length is None
    assert result[0].title == "Live Stream"


def test_m3u_extinf_single_digit_duration() -> None:
    """Test that single-digit durations still work correctly."""
    m3u_data = "#EXTM3U\n#EXTINF:5,Short Clip\nhttp://example.com/clip.mp3\n"
    result = parse_m3u(m3u_data)
    assert len(result) == 1
    assert result[0].length == "5"


# --------------------------------------------------------------------------- #
#  parse_extinf_title                                                          #
# --------------------------------------------------------------------------- #


def test_parse_extinf_title_with_artist() -> None:
    """Test parsing 'Artist - Title' format."""
    artist, title = parse_extinf_title("Radiohead - Everything In Its Right Place")
    assert artist == "Radiohead"
    assert title == "Everything In Its Right Place"


def test_parse_extinf_title_without_artist() -> None:
    """Test parsing title without artist separator."""
    artist, title = parse_extinf_title("Just A Title")
    assert artist is None
    assert title == "Just A Title"


def test_parse_extinf_title_none() -> None:
    """Test parsing None title."""
    artist, title = parse_extinf_title(None)
    assert artist is None
    assert title is None


def test_parse_extinf_title_multiple_separators() -> None:
    """Test that only the first ' - ' is used as separator."""
    artist, title = parse_extinf_title("Artist - Title - Remix")
    assert artist == "Artist"
    assert title == "Title - Remix"


# --------------------------------------------------------------------------- #
#  EXTMA metadata parsing                                                      #
# --------------------------------------------------------------------------- #


def test_m3u_extma_parsing() -> None:
    """Test that #EXTMA metadata is parsed into the metadata dict."""
    m3u_data = (
        "#EXTM3U\n"
        "#EXTMA:media_type=track||isrc=USRC17607839||album=OK Computer\n"
        "#EXTINF:240,Radiohead - Everything In Its Right Place\n"
        "spotify://track/abc123\n"
    )
    result = parse_m3u(m3u_data)
    assert len(result) == 1
    assert result[0].metadata is not None
    assert result[0].metadata["media_type"] == "track"
    assert result[0].metadata["isrc"] == "USRC17607839"
    assert result[0].metadata["album"] == "OK Computer"


def test_m3u_extma_without_metadata() -> None:
    """Test that entries without EXTMA have None metadata."""
    m3u_data = "#EXTM3U\n#EXTINF:120,Test\nhttp://example.com/song.mp3\n"
    result = parse_m3u(m3u_data)
    assert result[0].metadata is None


# --------------------------------------------------------------------------- #
#  EXTPROV provider mapping parsing                                            #
# --------------------------------------------------------------------------- #


def test_m3u_extprov_parsing() -> None:
    """Test that #EXTPROV lines are parsed into provider mappings."""
    m3u_data = (
        "#EXTM3U\n"
        "#EXTPROV:spotify||abc123||spotify_1||flac||96000||24||320\n"
        "#EXTPROV:tidal||xyz789||tidal_1||flac||192000||24||0\n"
        "#EXTINF:240,Radiohead - Everything In Its Right Place\n"
        "spotify://track/abc123\n"
    )
    result = parse_m3u(m3u_data)
    assert len(result) == 1
    assert len(result[0].providers) == 2
    prov1 = result[0].providers[0]
    assert prov1.domain == "spotify"
    assert prov1.item_id == "abc123"
    assert prov1.instance_id == "spotify_1"
    assert prov1.content_type == "flac"
    assert prov1.sample_rate == 96000
    assert prov1.bit_depth == 24
    assert prov1.bit_rate == 320
    prov2 = result[0].providers[1]
    assert prov2.domain == "tidal"
    assert prov2.item_id == "xyz789"
    assert prov2.instance_id == "tidal_1"
    assert prov2.sample_rate == 192000


def test_m3u_extprov_minimal() -> None:
    """Test EXTPROV with only the 2 required fields (domain and item_id)."""
    m3u_data = "#EXTM3U\n#EXTPROV:spotify||abc123\n#EXTINF:120,Test\nspotify://track/abc123\n"
    result = parse_m3u(m3u_data)
    assert len(result[0].providers) == 1
    assert result[0].providers[0].domain == "spotify"
    assert result[0].providers[0].item_id == "abc123"
    assert result[0].providers[0].instance_id == ""
    assert result[0].providers[0].sample_rate == 0


def test_m3u_extprov_invalid_skipped() -> None:
    """Test that malformed EXTPROV lines are skipped."""
    m3u_data = "#EXTM3U\n#EXTPROV:onlyonefield\n#EXTINF:120,Test\nhttp://example.com/song.mp3\n"
    result = parse_m3u(m3u_data)
    assert len(result[0].providers) == 0


# --------------------------------------------------------------------------- #
#  EXTIMG image parsing                                                        #
# --------------------------------------------------------------------------- #


def test_m3u_extimg_parsing() -> None:
    """Test that #EXTIMG lines are parsed into image info."""
    m3u_data = (
        "#EXTM3U\n"
        "#EXTIMG:thumb||https://img.example.com/abc.jpg||spotify||true\n"
        "#EXTINF:120,Test\n"
        "spotify://track/abc123\n"
    )
    result = parse_m3u(m3u_data)
    assert len(result[0].images) == 1
    img = result[0].images[0]
    assert img.type == "thumb"
    assert img.path == "https://img.example.com/abc.jpg"
    assert img.provider == "spotify"
    assert img.remotely_accessible is True


def test_m3u_extimg_not_remotely_accessible() -> None:
    """Test EXTIMG with remotely_accessible=false."""
    m3u_data = "#EXTM3U\n#EXTIMG:thumb||/local/path.jpg||builtin||false\n#EXTINF:120,Test\ntest\n"
    result = parse_m3u(m3u_data)
    assert result[0].images[0].remotely_accessible is False


def test_m3u_extimg_multiple() -> None:
    """Test multiple EXTIMG lines per entry."""
    m3u_data = (
        "#EXTM3U\n"
        "#EXTIMG:thumb||https://thumb.jpg||spotify||true\n"
        "#EXTIMG:fanart||https://fanart.jpg||spotify||true\n"
        "#EXTINF:120,Test\n"
        "spotify://track/abc123\n"
    )
    result = parse_m3u(m3u_data)
    assert len(result[0].images) == 2


# --------------------------------------------------------------------------- #
#  #PLAYLIST directive                                                         #
# --------------------------------------------------------------------------- #


def test_parse_m3u_playlist_name() -> None:
    """Test extracting playlist name from #PLAYLIST directive."""
    m3u_data = "#EXTM3U\n#PLAYLIST:My Playlist\n#EXTINF:120,Test\ntest.mp3\n"
    assert parse_m3u_playlist_name(m3u_data) == "My Playlist"


def test_parse_m3u_playlist_name_missing() -> None:
    """Test that None is returned when no #PLAYLIST directive exists."""
    m3u_data = "#EXTM3U\n#EXTINF:120,Test\ntest.mp3\n"
    assert parse_m3u_playlist_name(m3u_data) is None


def test_parse_m3u_playlist_image() -> None:
    """Test extracting a playlist-level cover image from #EXTIMG."""
    m3u_data = (
        "#EXTM3U\n"
        "#EXTIMG:https://img.example.com/cover.jpg\n"
        "#PLAYLIST:My Playlist\n"
        "#EXTINF:120,Test\n"
        "test.mp3\n"
    )
    assert parse_m3u_playlist_image(m3u_data) == "https://img.example.com/cover.jpg"


def test_parse_m3u_playlist_image_missing() -> None:
    """Test that None is returned when no playlist-level image exists."""
    m3u_data = "#EXTM3U\n#PLAYLIST:My Playlist\n#EXTINF:120,Test\ntest.mp3\n"
    assert parse_m3u_playlist_image(m3u_data) is None


# --------------------------------------------------------------------------- #
#  generate_m3u                                                                #
# --------------------------------------------------------------------------- #


def test_generate_m3u_basic() -> None:
    """Test basic M3U generation with EXTINF."""
    items = [
        PlaylistItem(path="spotify://track/abc123", title="Artist - Song", length="240"),
    ]
    result = generate_m3u("My Playlist", items)
    assert "#EXTM3U\n" in result
    assert "#PLAYLIST:My Playlist\n" in result
    assert "#EXTINF:240,Artist - Song\n" in result
    assert "spotify://track/abc123\n" in result


def test_generate_m3u_with_metadata() -> None:
    """Test M3U generation with EXTMA metadata."""
    items = [
        PlaylistItem(
            path="spotify://track/abc123",
            title="Artist - Song",
            length="240",
            metadata={"media_type": "track", "isrc": "USRC123"},
        ),
    ]
    result = generate_m3u("Test", items)
    assert "#EXTMA:media_type=track||isrc=USRC123\n" in result


def test_generate_m3u_with_providers() -> None:
    """Test M3U generation with EXTPROV lines."""
    items = [
        PlaylistItem(
            path="spotify://track/abc123",
            title="Test",
            length="120",
            providers=[
                ProviderMappingInfo(
                    domain="spotify",
                    item_id="abc123",
                    instance_id="spotify_1",
                    content_type="flac",
                    sample_rate=96000,
                    bit_depth=24,
                    bit_rate=320,
                ),
            ],
        ),
    ]
    result = generate_m3u("Test", items)
    assert "#EXTPROV:spotify||abc123||spotify_1||flac||96000||24||320\n" in result


def test_generate_m3u_with_images() -> None:
    """Test M3U generation with EXTIMG lines."""
    items = [
        PlaylistItem(
            path="spotify://track/abc123",
            title="Test",
            length="120",
            images=[
                ImageInfo(
                    type="thumb",
                    path="https://img.jpg",
                    provider="spotify",
                    remotely_accessible=True,
                )
            ],
        ),
    ]
    result = generate_m3u("Test", items)
    assert "#EXTIMG:thumb||https://img.jpg||spotify||true\n" in result


def test_generate_m3u_with_playlist_image() -> None:
    """Test M3U generation with a playlist-level cover image."""
    items = [PlaylistItem(path="spotify://track/abc123", title="Test", length="120")]
    result = generate_m3u("Test", items, "https://img.example.com/cover.jpg")
    assert result.startswith("#EXTM3U\n#EXTIMG:https://img.example.com/cover.jpg\n#PLAYLIST:Test\n")


def test_generate_m3u_empty() -> None:
    """Test generating an empty M3U playlist."""
    result = generate_m3u("Empty Playlist", [])
    assert result == "#EXTM3U\n#PLAYLIST:Empty Playlist\n"


@pytest.mark.parametrize(
    "line_break",
    ["\n", "\r\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"],
)
def test_generate_m3u_never_splits_an_entry_over_multiple_lines(line_break: str) -> None:
    """Test that a line break inside a value cannot split an entry into a second entry."""
    items = [
        PlaylistItem(
            path="spotify://track/abc123",
            length="240",
            title=f"Artist - Song (feat. Mad{line_break}elyn Brown)",
            metadata={"media_type": "track", "name": f"Song{line_break}X"},
            providers=[ProviderMappingInfo(domain="spotify", item_id="abc123")],
            images=[
                ImageInfo(type="thumb", path=f"https://img{line_break}.jpg", provider="spotify")
            ],
            artists=[
                ArtistInfo(
                    name=f"Mad{line_break}elyn Brown",
                    provider_domain="spotify",
                    item_id="art1",
                    provider_instance="spotify",
                )
            ],
            album=AlbumInfo(
                name=f"The{line_break}Album",
                provider_domain="spotify",
                item_id="alb1",
                provider_instance="spotify",
            ),
        ),
    ]
    result = generate_m3u(f"My{line_break}Playlist", items, f"https://cover{line_break}.jpg")

    parsed = parse_m3u(result)
    assert len(parsed) == 1
    assert parsed[0].path == "spotify://track/abc123"
    assert parse_m3u_playlist_name(result) == "My Playlist".replace(" ", " " * len(line_break))


def test_generate_m3u_leaves_values_without_line_breaks_alone() -> None:
    """Test that sanitizing only touches line breaks."""
    title = "Artist - Song (feat. Madelyn Brown)"
    assert sanitize_m3u_value(title) == title
    items = [PlaylistItem(path="spotify://track/abc123", title=title, length="240")]
    assert f"#EXTINF:240,{title}\n" in generate_m3u("My Playlist", items)


def test_generate_m3u_no_extinf_without_title() -> None:
    """Test that entries without title/length skip the EXTINF line."""
    items = [PlaylistItem(path="spotify://track/abc123")]
    result = generate_m3u("Test", items)
    assert "#EXTINF" not in result
    assert "spotify://track/abc123\n" in result


# --------------------------------------------------------------------------- #
#  Round-trip: generate -> parse                                               #
# --------------------------------------------------------------------------- #


def test_round_trip_full() -> None:
    """Test that generate_m3u output can be parsed back with all metadata preserved."""
    original = PlaylistItem(
        path="spotify://track/abc123",
        title="Radiohead - Everything In Its Right Place",
        length="240",
        metadata={"media_type": "track", "isrc": "USRC17607839", "album": "OK Computer"},
        providers=[
            ProviderMappingInfo(
                domain="spotify",
                item_id="abc123",
                instance_id="spotify_1",
                content_type="flac",
                sample_rate=96000,
                bit_depth=24,
                bit_rate=320,
            ),
            ProviderMappingInfo(
                domain="tidal",
                item_id="xyz789",
                instance_id="tidal_1",
                content_type="flac",
                sample_rate=192000,
                bit_depth=24,
                bit_rate=0,
            ),
        ],
        images=[
            ImageInfo(
                type="thumb",
                path="https://img.example.com/thumb.jpg",
                provider="spotify",
                remotely_accessible=True,
            ),
        ],
    )

    m3u_data = generate_m3u("Test Playlist", [original])
    parsed = parse_m3u(m3u_data)

    assert len(parsed) == 1
    item = parsed[0]
    assert item.path == original.path
    assert item.title == original.title
    assert item.length == original.length

    # metadata round-trip
    assert item.metadata == original.metadata

    # provider round-trip
    assert len(item.providers) == 2
    assert item.providers[0].domain == "spotify"
    assert item.providers[0].item_id == "abc123"
    assert item.providers[0].instance_id == "spotify_1"
    assert item.providers[0].content_type == "flac"
    assert item.providers[0].sample_rate == 96000
    assert item.providers[0].bit_depth == 24
    assert item.providers[0].bit_rate == 320
    assert item.providers[1].domain == "tidal"
    assert item.providers[1].sample_rate == 192000

    # image round-trip
    assert len(item.images) == 1
    assert item.images[0].type == "thumb"
    assert item.images[0].path == "https://img.example.com/thumb.jpg"
    assert item.images[0].provider == "spotify"
    assert item.images[0].remotely_accessible is True

    # playlist name round-trip
    assert parse_m3u_playlist_name(m3u_data) == "Test Playlist"


def test_round_trip_multiple_entries() -> None:
    """Test round-trip with multiple entries preserves order and all data."""
    items = [
        PlaylistItem(
            path="spotify://track/track1",
            title="Artist A - Song 1",
            length="200",
            metadata={"media_type": "track"},
        ),
        PlaylistItem(
            path="tidal://track/track2",
            title="Artist B - Song 2",
            length="300",
            metadata={"media_type": "track"},
        ),
        PlaylistItem(
            path="builtin://radio/http://stream.example.com",
            title="Radio Station",
            length="-1",  # will be parsed as None
            metadata={"media_type": "radio"},
        ),
    ]
    m3u_data = generate_m3u("Multi", items)
    parsed = parse_m3u(m3u_data)

    assert len(parsed) == 3
    assert parsed[0].path == "spotify://track/track1"
    assert parsed[1].path == "tidal://track/track2"
    assert parsed[2].path == "builtin://radio/http://stream.example.com"
    # -1 duration is normalized to None on parse
    assert parsed[2].length is None


def test_round_trip_unknown_length_keeps_title() -> None:
    """Test that an entry with unknown length survives repeated rewrites."""
    original = PlaylistItem(
        path="builtin://radio/http://stream.example.com",
        title="My Custom Station Name",
        metadata={"media_type": "radio", "name": "My Custom Station Name"},
        images=[
            ImageInfo(
                type="thumb",
                path="https://img.example.com/station.jpg",
                provider="builtin",
                remotely_accessible=True,
            ),
        ],
    )
    # playlist files are rewritten on every edit, so parse -> generate must be lossless
    parsed = parse_m3u(generate_m3u("Radio", [original]))
    reparsed = parse_m3u(generate_m3u("Radio", parsed))

    assert len(reparsed) == 1
    assert reparsed[0].title == "My Custom Station Name"
    assert reparsed[0].length is None
    assert reparsed[0].metadata == original.metadata
    assert reparsed[0].images[0].path == "https://img.example.com/station.jpg"


def test_round_trip_bare_uris() -> None:
    """Test round-trip with bare URIs (no metadata) - migrated playlists."""
    items = [
        PlaylistItem(path="spotify://track/abc123"),
        PlaylistItem(path="tidal://track/xyz789"),
    ]
    m3u_data = generate_m3u("Migrated", items)
    parsed = parse_m3u(m3u_data)
    assert len(parsed) == 2
    assert parsed[0].path == "spotify://track/abc123"
    assert parsed[0].metadata is None
    assert parsed[0].providers == []
    assert parsed[1].path == "tidal://track/xyz789"


def test_construct_media_item_from_playlist_item_sound_effect() -> None:
    """Stored playlist metadata reconstructs a SoundEffect with mappings and artwork."""

    class DummyProvider:
        def __init__(self, domain: str, instance_id: str) -> None:
            self.domain = domain
            self.instance_id = instance_id

    mass = MagicMock()
    builtin_provider = DummyProvider("builtin", "builtin_1")
    mass.get_provider.side_effect = lambda ref: {
        "builtin": builtin_provider,
        "builtin_1": builtin_provider,
    }.get(ref)
    item = PlaylistItem(
        path="builtin://sound_effect/http://example.com/chime.mp3",
        title="Chime",
        length="12",
        metadata={
            "media_type": MediaType.SOUND_EFFECT.value,
            "name": "Chime",
            "mbid": "soundeffect-mbid",
        },
        providers=[
            ProviderMappingInfo(
                domain="builtin",
                item_id="http://example.com/chime.mp3",
                instance_id="builtin_1",
                content_type="mp3",
                sample_rate=44100,
                bit_depth=16,
                bit_rate=192,
            )
        ],
        images=[
            ImageInfo(
                type="thumb",
                path="https://example.com/chime.jpg",
                provider="builtin",
                remotely_accessible=True,
            )
        ],
    )

    result = construct_media_item_from_playlist_item(item, mass)

    assert isinstance(result, SoundEffect)
    assert result.name == "Chime"
    assert result.duration == 12
    assert result.get_external_id(ExternalID.MB_RECORDING) == "soundeffect-mbid"
    assert result.provider_mappings
    mapping = next(iter(result.provider_mappings))
    assert mapping.provider_domain == "builtin"
    assert mapping.provider_instance == "builtin_1"
    assert mapping.item_id == "http://example.com/chime.mp3"
    assert result.metadata.images
    assert result.metadata.images[0].path == "https://example.com/chime.jpg"


def test_media_item_to_playlist_item_sound_effect_round_trip() -> None:
    """SoundEffect playlist items round-trip through M3U metadata without losing data."""

    class DummyProvider:
        def __init__(self, domain: str, instance_id: str) -> None:
            self.domain = domain
            self.instance_id = instance_id

    mass = MagicMock()
    builtin_provider = DummyProvider("builtin", "builtin_1")
    mass.get_provider.side_effect = lambda ref: {
        "builtin": builtin_provider,
        "builtin_1": builtin_provider,
    }.get(ref)
    image = MediaItemImage(
        type=ImageType.THUMB,
        path="https://example.com/chime.jpg",
        provider="builtin",
        remotely_accessible=True,
    )
    sound_effect = SoundEffect(
        item_id="http://example.com/chime.mp3",
        provider="builtin",
        name="Chime",
        provider_mappings={
            ProviderMapping(
                item_id="http://example.com/chime.mp3",
                provider_domain="builtin",
                provider_instance="builtin_1",
                audio_format=AudioFormat(
                    content_type=ContentType.MP3,
                    sample_rate=44100,
                    bit_depth=16,
                    bit_rate=192,
                ),
            )
        },
        external_ids={(ExternalID.MB_RECORDING, "soundeffect-mbid")},
        metadata=MediaItemMetadata(images=UniqueList([image])),
    )
    sound_effect.duration = 12

    playlist_item = media_item_to_playlist_item(sound_effect)
    parsed = parse_m3u(generate_m3u("FX", [playlist_item]))
    reconstructed = construct_media_item_from_playlist_item(parsed[0], mass)

    assert playlist_item.path == "builtin://sound_effect/http://example.com/chime.mp3"
    assert playlist_item.metadata == {
        "media_type": MediaType.SOUND_EFFECT.value,
        "name": "Chime",
        "mbid": "soundeffect-mbid",
    }
    assert playlist_item.length == "12"
    assert playlist_item.images[0].path == "https://example.com/chime.jpg"
    assert isinstance(reconstructed, SoundEffect)
    assert reconstructed.duration == 12
    assert reconstructed.provider_mappings
    mapping = next(iter(reconstructed.provider_mappings))
    assert mapping.provider_instance == "builtin_1"
    assert mapping.item_id == "http://example.com/chime.mp3"
    assert reconstructed.metadata.images
    assert reconstructed.metadata.images[0].path == "https://example.com/chime.jpg"


def test_media_item_to_playlist_item_track_round_trip_preserves_mb_track() -> None:
    """A track's release-track (MB_TRACK) evidence survives export, generate and re-parse."""

    class DummyProvider:
        def __init__(self, domain: str, instance_id: str) -> None:
            self.domain = domain
            self.instance_id = instance_id

    mass = MagicMock()
    spotify_provider = DummyProvider("spotify", "spotify_1")
    mass.get_provider.side_effect = lambda ref: {
        "spotify": spotify_provider,
        "spotify_1": spotify_provider,
    }.get(ref)
    track = Track(
        item_id="abc123",
        provider="spotify",
        name="Everything In Its Right Place",
        duration=240,
        provider_mappings={
            ProviderMapping(
                item_id="abc123",
                provider_domain="spotify",
                provider_instance="spotify_1",
                audio_format=AudioFormat(content_type=ContentType.FLAC),
            ),
        },
        external_ids={
            (ExternalID.ISRC, "USRC17607839"),
            (ExternalID.MB_RECORDING, "recording-mbid"),
            (ExternalID.MB_TRACK, "release-track-mbid"),
        },
    )

    playlist_item = media_item_to_playlist_item(track)
    parsed = parse_m3u(generate_m3u("Kid A", [playlist_item]))
    reconstructed = construct_media_item_from_playlist_item(parsed[0], mass)

    assert playlist_item.metadata is not None
    assert playlist_item.metadata["mb_track"] == "release-track-mbid"
    assert isinstance(reconstructed, Track)
    assert reconstructed.get_external_id(ExternalID.MB_TRACK) == "release-track-mbid"
    assert reconstructed.get_external_id(ExternalID.MB_RECORDING) == "recording-mbid"
    assert reconstructed.get_external_id(ExternalID.ISRC) == "USRC17607839"


def test_media_item_to_playlist_item_track_round_trip_preserves_album_instance() -> None:
    """
    A track's album keeps its exact originating instance, not just its bare domain.

    Reconstructing with only the domain would let matching expand across every allowed
    sibling instance of that domain instead of the one #EXTALBUM actually captured,
    letting an unrelated sibling account's same-domain album ID supply release evidence
    for exact-tier matching.
    """

    class DummyProvider:
        def __init__(self, domain: str, instance_id: str) -> None:
            self.domain = domain
            self.instance_id = instance_id

    mass = MagicMock()
    spotify_1 = DummyProvider("spotify", "spotify--1")
    spotify_2 = DummyProvider("spotify", "spotify--2")
    mass.get_provider.side_effect = lambda ref, **_kwargs: {
        "spotify": spotify_1,
        "spotify--1": spotify_1,
        "spotify--2": spotify_2,
    }.get(ref)
    album = Album(
        item_id="alb1",
        provider="spotify--2",
        name="Kid A",
        provider_mappings={
            ProviderMapping(
                item_id="alb1", provider_domain="spotify", provider_instance="spotify--2"
            )
        },
    )
    track = Track(
        item_id="abc123",
        provider="spotify--1",
        name="Everything In Its Right Place",
        duration=240,
        provider_mappings={
            ProviderMapping(
                item_id="abc123",
                provider_domain="spotify",
                provider_instance="spotify--1",
                audio_format=AudioFormat(content_type=ContentType.FLAC),
            ),
        },
        album=album,
    )

    playlist_item = media_item_to_playlist_item(track)
    parsed = parse_m3u(generate_m3u("Kid A", [playlist_item]))
    reconstructed = construct_media_item_from_playlist_item(parsed[0], mass)

    assert isinstance(reconstructed, Track)
    assert reconstructed.album is not None
    # the album was captured on spotify--2, a *different* instance than the track's own
    # spotify--1 - the reconstructed reference must pin to that exact instance
    assert reconstructed.album.provider == "spotify--2"


def test_media_item_to_playlist_item_track_round_trip_falls_back_to_album_domain() -> None:
    """
    An album pinned to an instance id unknown on this server falls back to its domain.

    A playlist exported from a different Music Assistant install carries instance ids
    that are randomly generated per install and will never resolve here; reconstructing
    with that foreign instance id verbatim would leave the album permanently
    unhydratable. Falling back to the domain lets downstream hydration
    (``TracksController._get_full_track_album``) try every allowed sibling instance of
    it instead.
    """

    class DummyProvider:
        def __init__(self, domain: str, instance_id: str) -> None:
            self.domain = domain
            self.instance_id = instance_id

    mass = MagicMock()
    # spotify--2 (the account this album was originally captured on, on a *different*
    # Music Assistant install) does not exist on this server at all - only an unrelated
    # sibling of the same domain does
    spotify_9 = DummyProvider("spotify", "spotify--9")
    mass.get_provider.side_effect = lambda ref, **_kwargs: {"spotify--9": spotify_9}.get(ref)
    album = Album(
        item_id="alb1",
        provider="spotify--2",
        name="Kid A",
        provider_mappings={
            ProviderMapping(
                item_id="alb1", provider_domain="spotify", provider_instance="spotify--2"
            )
        },
    )
    track = Track(
        item_id="abc123",
        provider="spotify--2",
        name="Everything In Its Right Place",
        duration=240,
        provider_mappings={
            ProviderMapping(
                item_id="abc123",
                provider_domain="spotify",
                provider_instance="spotify--2",
                audio_format=AudioFormat(content_type=ContentType.FLAC),
            ),
        },
        album=album,
    )

    playlist_item = media_item_to_playlist_item(track)
    parsed = parse_m3u(generate_m3u("Kid A", [playlist_item]))
    reconstructed = construct_media_item_from_playlist_item(parsed[0], mass)

    assert isinstance(reconstructed, Track)
    assert reconstructed.album is not None
    # falls back to the bare domain instead of the foreign, unresolvable instance id
    assert reconstructed.album.provider == "spotify"


def test_media_item_to_playlist_item_album_prefers_track_instance_when_multiple() -> None:
    """
    An album with mappings on multiple sibling instances pins to the track's own account.

    Picking an arbitrary album mapping (set iteration order is not guaranteed stable)
    would let an unrelated sibling account's same-domain album ID nondeterministically
    become the serialized - and later pinned - album reference; the mapping matching the
    track's own instance is unambiguous and must win instead.
    """
    album = Album(
        item_id="alb1",
        provider="spotify--2",
        name="Kid A",
        provider_mappings={
            ProviderMapping(
                item_id="alb1a", provider_domain="spotify", provider_instance="spotify--2"
            ),
            ProviderMapping(
                item_id="alb1b", provider_domain="spotify", provider_instance="spotify--1"
            ),
            ProviderMapping(
                item_id="alb1c", provider_domain="spotify", provider_instance="spotify--3"
            ),
        },
    )
    track = Track(
        item_id="abc123",
        provider="spotify--1",
        name="Everything In Its Right Place",
        duration=240,
        provider_mappings={
            ProviderMapping(
                item_id="abc123",
                provider_domain="spotify",
                provider_instance="spotify--1",
                audio_format=AudioFormat(content_type=ContentType.FLAC),
            ),
        },
        album=album,
    )

    playlist_item = media_item_to_playlist_item(track)

    assert playlist_item.album is not None
    assert playlist_item.album.provider_instance == "spotify--1"
    assert playlist_item.album.item_id == "alb1b"


def test_media_item_to_playlist_item_album_deterministic_among_same_instance_mappings() -> None:
    """
    Two mappings sharing the track's own instance still resolve to the same one.

    An album can hold more than one mapping for the same account (for example an
    alternate catalog ID for the same release) - matching by instance alone is not
    enough to guarantee a single candidate, so the deterministic ranking must still
    apply among those matches instead of leaving the choice to set iteration order.
    """
    album = Album(
        item_id="alb1",
        provider="spotify--1",
        name="Kid A",
        provider_mappings={
            ProviderMapping(
                item_id="alb1-alt",
                provider_domain="spotify",
                provider_instance="spotify--1",
                available=False,
            ),
            ProviderMapping(
                item_id="alb1-main",
                provider_domain="spotify",
                provider_instance="spotify--1",
                available=True,
            ),
        },
    )
    track = Track(
        item_id="abc123",
        provider="spotify--1",
        name="Everything In Its Right Place",
        duration=240,
        provider_mappings={
            ProviderMapping(
                item_id="abc123",
                provider_domain="spotify",
                provider_instance="spotify--1",
                audio_format=AudioFormat(content_type=ContentType.FLAC),
            ),
        },
        album=album,
    )

    playlist_item = media_item_to_playlist_item(track)

    assert playlist_item.album is not None
    assert playlist_item.album.provider_instance == "spotify--1"
    assert playlist_item.album.item_id == "alb1-main"


def test_media_item_to_playlist_item_album_mapping_selection_is_deterministic_without_match() -> (
    None
):
    """
    Without a mapping matching the track's own instance, the choice is still deterministic.

    Falling back to a set's iteration order would let hash-order variance pick a
    different sibling account across runs; the same ranking used for the track's own
    primary provider (available, higher quality, then domain/instance/item_id) applies
    here too, instead of whichever mapping the set happens to iterate first.
    """
    album = Album(
        item_id="alb1",
        provider="tidal--1",
        name="Kid A",
        provider_mappings={
            ProviderMapping(item_id="alb1z", provider_domain="tidal", provider_instance="tidal--2"),
            ProviderMapping(item_id="alb1a", provider_domain="tidal", provider_instance="tidal--1"),
        },
    )
    track = Track(
        item_id="abc123",
        provider="spotify--1",
        name="Everything In Its Right Place",
        duration=240,
        provider_mappings={
            ProviderMapping(
                item_id="abc123",
                provider_domain="spotify",
                provider_instance="spotify--1",
                audio_format=AudioFormat(content_type=ContentType.FLAC),
            ),
        },
        album=album,
    )

    playlist_item = media_item_to_playlist_item(track)

    assert playlist_item.album is not None
    assert playlist_item.album.provider_instance == "tidal--1"
    assert playlist_item.album.item_id == "alb1a"


def test_media_item_to_playlist_item_album_prefers_available_mapping() -> None:
    """
    An available mapping wins over an unavailable one, regardless of iteration order.

    Without a match to the track's own instance, the fallback ranking must still put an
    available sibling ahead of an unavailable one.
    """
    album = Album(
        item_id="alb1",
        provider="tidal--2",
        name="Kid A",
        provider_mappings={
            ProviderMapping(
                item_id="alb1a",
                provider_domain="tidal",
                provider_instance="tidal--1",
                available=False,
            ),
            ProviderMapping(
                item_id="alb1b",
                provider_domain="tidal",
                provider_instance="tidal--2",
                available=True,
            ),
        },
    )
    track = Track(
        item_id="abc123",
        provider="spotify--1",
        name="Everything In Its Right Place",
        duration=240,
        provider_mappings={
            ProviderMapping(
                item_id="abc123",
                provider_domain="spotify",
                provider_instance="spotify--1",
                audio_format=AudioFormat(content_type=ContentType.FLAC),
            ),
        },
        album=album,
    )

    playlist_item = media_item_to_playlist_item(track)

    assert playlist_item.album is not None
    assert playlist_item.album.provider_instance == "tidal--2"
    assert playlist_item.album.item_id == "alb1b"


def test_media_item_to_playlist_item_track_round_trip_preserves_exact_capable_album() -> None:
    """
    An album with several sibling mappings round-trips to the account holding exact evidence.

    Exporting must pick the mapping matching the track's own account (rather than an
    arbitrary sibling) so the reconstructed reference resolves to the same account that
    supplied the track, keeping strict Exact-tier evidence available after a round trip.
    """
    mass = MagicMock()

    class DummyProvider:
        def __init__(self, domain: str, instance_id: str) -> None:
            self.domain = domain
            self.instance_id = instance_id

    tidal_1 = DummyProvider("tidal", "tidal--1")
    mass.get_provider.side_effect = lambda ref, **_kwargs: {
        "tidal": tidal_1,
        "tidal--1": tidal_1,
    }.get(ref)
    album = Album(
        item_id="alb1",
        provider="tidal--1",
        name="Kid A",
        provider_mappings={
            ProviderMapping(item_id="alb1a", provider_domain="tidal", provider_instance="tidal--1"),
            ProviderMapping(item_id="alb1b", provider_domain="tidal", provider_instance="tidal--2"),
            ProviderMapping(item_id="alb1c", provider_domain="tidal", provider_instance="tidal--3"),
        },
    )
    track = Track(
        item_id="abc123",
        provider="tidal--1",
        name="Everything In Its Right Place",
        duration=240,
        provider_mappings={
            ProviderMapping(
                item_id="abc123",
                provider_domain="tidal",
                provider_instance="tidal--1",
                audio_format=AudioFormat(content_type=ContentType.FLAC),
            ),
        },
        album=album,
    )

    playlist_item = media_item_to_playlist_item(track)
    parsed = parse_m3u(generate_m3u("Kid A", [playlist_item]))
    reconstructed = construct_media_item_from_playlist_item(parsed[0], mass)

    assert isinstance(reconstructed, Track)
    assert reconstructed.album is not None
    # the track's own account (tidal--1) holds the album's exact-evidence mapping (alb1a) -
    # the round trip must resolve to that specific instance, not an arbitrary sibling
    assert reconstructed.album.provider == "tidal--1"
    assert reconstructed.album.item_id == "alb1a"


def test_media_item_to_playlist_item_omits_conflicting_merged_external_ids() -> None:
    """Conflicting merged external IDs from different providers are not exported arbitrarily."""
    track = Track(
        item_id="abc123",
        provider="library",
        name="Everything In Its Right Place",
        duration=240,
        provider_mappings={
            ProviderMapping(
                item_id="abc123", provider_domain="spotify", provider_instance="spotify_1"
            ),
            ProviderMapping(item_id="xyz789", provider_domain="qobuz", provider_instance="qobuz_1"),
        },
        # a library track merges external IDs from every matched provider - two
        # providers disagreeing on MB_TRACK means they identify different releases,
        # so neither value is reliable release evidence for the exported entry
        external_ids={
            (ExternalID.ISRC, "USRC17607839"),
            (ExternalID.MB_TRACK, "spotify-release-track-mbid"),
            (ExternalID.MB_TRACK, "qobuz-release-track-mbid"),
        },
    )

    playlist_item = media_item_to_playlist_item(track)

    assert playlist_item.metadata is not None
    assert "mb_track" not in playlist_item.metadata
    # unambiguous external IDs are unaffected
    assert playlist_item.metadata["isrc"] == "USRC17607839"


def test_media_item_to_playlist_item_merges_equivalent_external_ids() -> None:
    """Providers formatting the same MBID differently is not treated as a conflict."""
    track = Track(
        item_id="abc123",
        provider="library",
        name="Everything In Its Right Place",
        duration=240,
        provider_mappings={
            ProviderMapping(
                item_id="abc123", provider_domain="spotify", provider_instance="spotify_1"
            ),
            ProviderMapping(item_id="xyz789", provider_domain="qobuz", provider_instance="qobuz_1"),
        },
        # both providers actually agree on the same MusicBrainz track ID, just
        # formatted differently (braces + uppercase vs bare lowercase) - this must
        # still be recognized as the single, unambiguous release-track identifier
        external_ids={
            (ExternalID.MB_TRACK, "a1b2c3d4-e5f6-7890-abcd-ef1234567890"),
            (ExternalID.MB_TRACK, "{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}"),
        },
    )

    playlist_item = media_item_to_playlist_item(track)

    assert playlist_item.metadata is not None
    assert playlist_item.metadata["mb_track"] == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"


def test_import_match_policy_reachability_new_vs_legacy_m3u() -> None:
    """
    EXACT is only reachable when the parsed M3U carries release-track evidence.

    A new-format export with MB_TRACK reconstructs a track that reaches EXACT confidence
    against a candidate sharing that release-track id, while a legacy M3U that only ever
    carried an ISRC caps out at LIKELY (the SAME_RECORDING policy tier), even against a
    candidate that would otherwise be a plausible substitute.
    """

    class DummyProvider:
        def __init__(self, domain: str, instance_id: str) -> None:
            self.domain = domain
            self.instance_id = instance_id

    mass = MagicMock()
    spotify_provider = DummyProvider("spotify", "spotify_1")
    mass.get_provider.side_effect = lambda ref: {
        "spotify": spotify_provider,
        "spotify_1": spotify_provider,
    }.get(ref)

    new_m3u_item = PlaylistItem(
        path="spotify://track/abc123",
        title="Everything In Its Right Place",
        length="240",
        metadata={
            "media_type": "track",
            "name": "Everything In Its Right Place",
            "isrc": "USRC17607839",
            "mb_track": "release-track-mbid",
        },
    )
    legacy_m3u_item = PlaylistItem(
        path="spotify://track/abc123",
        title="Everything In Its Right Place",
        length="240",
        metadata={
            "media_type": "track",
            "name": "Everything In Its Right Place",
            "isrc": "USRC17607839",
        },
    )
    candidate = Track(
        item_id="def456",
        provider="qobuz",
        name="Everything In Its Right Place",
        duration=240,
        provider_mappings={
            ProviderMapping(item_id="def456", provider_domain="qobuz", provider_instance="qobuz--1")
        },
        external_ids={
            (ExternalID.ISRC, "USRC17607839"),
            (ExternalID.MB_TRACK, "release-track-mbid"),
        },
    )

    new_track = construct_media_item_from_playlist_item(new_m3u_item, mass)
    legacy_track = construct_media_item_from_playlist_item(legacy_m3u_item, mass)
    assert isinstance(new_track, Track)
    assert isinstance(legacy_track, Track)

    assert (
        compare_track_evidence(new_track, candidate, allow_item_id_match=False)
        is TrackMatchConfidence.EXACT
    )
    assert (
        compare_track_evidence(legacy_track, candidate, allow_item_id_match=False)
        is TrackMatchConfidence.LIKELY
    )


# --------------------------------------------------------------------------- #
#  media_item_to_playlist_item tests                                           #
# --------------------------------------------------------------------------- #


def test_media_item_to_playlist_item_track() -> None:
    """Test conversion of a Track with full metadata to PlaylistItem."""
    artist = ItemMapping(
        item_id="art1", provider="spotify", name="Radiohead", media_type=MediaType.ARTIST
    )
    album = ItemMapping(
        item_id="alb1", provider="spotify", name="Kid A", media_type=MediaType.ALBUM
    )
    img = MediaItemImage(
        type=ImageType.THUMB,
        path="https://example.com/img.jpg",
        provider="spotify",
        remotely_accessible=True,
    )
    track = Track(
        item_id="abc123",
        provider="spotify",
        name="Everything In Its Right Place",
        duration=240,
        version="Deluxe",
        provider_mappings={
            ProviderMapping(
                item_id="abc123",
                provider_domain="spotify",
                provider_instance="spotify_1",
                audio_format=AudioFormat(
                    content_type=ContentType.FLAC,
                    sample_rate=44100,
                    bit_depth=16,
                    bit_rate=320,
                ),
            ),
        },
        artists=UniqueList([artist]),
        album=album,
        external_ids={(ExternalID.ISRC, "USRC17607839")},
        metadata=MediaItemMetadata(images=UniqueList([img])),
    )

    result = media_item_to_playlist_item(track)

    assert result.path == "spotify://track/abc123"
    assert result.title == "Radiohead - Everything In Its Right Place"
    assert result.length == "240"
    assert result.metadata is not None
    assert result.metadata["media_type"] == "track"
    assert result.metadata["name"] == "Everything In Its Right Place"
    assert result.metadata["isrc"] == "USRC17607839"
    assert result.metadata["version"] == "Deluxe"
    assert "mb_track" not in result.metadata
    assert len(result.providers) == 1
    assert result.providers[0].domain == "spotify"
    assert result.providers[0].item_id == "abc123"
    assert result.providers[0].content_type == "flac"
    assert result.providers[0].sample_rate == 44100
    assert result.providers[0].bit_rate == 320
    assert len(result.artists) == 1
    assert result.artists[0].name == "Radiohead"
    assert result.album is not None
    assert result.album.name == "Kid A"
    assert len(result.images) == 1
    assert result.images[0].type == "thumb"
    assert result.images[0].remotely_accessible is True


def test_media_item_to_playlist_item_radio() -> None:
    """Test conversion of a Radio to PlaylistItem."""
    radio = Radio(
        item_id="radio1",
        provider="builtin",
        name="Test FM",
        provider_mappings={
            ProviderMapping(
                item_id="radio1",
                provider_domain="builtin",
                provider_instance="builtin",
                audio_format=AudioFormat(content_type=ContentType.OGG),
            ),
        },
    )

    result = media_item_to_playlist_item(radio)

    assert result.path == "builtin://radio/radio1"
    assert result.title == "Test FM"
    assert result.metadata is not None
    assert result.metadata["media_type"] == "radio"
    assert result.podcast is None
    assert result.album is None
    assert len(result.artists) == 0
    # a radio has no duration, which must still yield an #EXTINF line carrying the name
    assert result.length is None
    assert "#EXTINF:-1,Test FM" in generate_m3u("Radio Stations", [result])


def test_media_item_to_playlist_item_no_version() -> None:
    """Test that version is omitted from metadata when empty."""
    track = Track(
        item_id="t1",
        provider="tidal",
        name="Simple Track",
        duration=180,
        provider_mappings={
            ProviderMapping(
                item_id="t1",
                provider_domain="tidal",
                provider_instance="tidal_1",
                audio_format=AudioFormat(content_type=ContentType.FLAC),
            ),
        },
    )

    result = media_item_to_playlist_item(track)

    assert result.metadata is not None
    assert "version" not in result.metadata


def test_media_item_to_playlist_item_multiple_providers() -> None:
    """Test that multiple provider mappings are collected, one per domain, highest quality first."""
    track = Track(
        item_id="t1",
        provider="spotify",
        name="Multi Provider Track",
        duration=200,
        provider_mappings={
            ProviderMapping(
                item_id="t1",
                provider_domain="spotify",
                provider_instance="spotify_1",
                audio_format=AudioFormat(
                    content_type=ContentType.OGG, sample_rate=44100, bit_depth=0, bit_rate=320
                ),
            ),
            ProviderMapping(
                item_id="t2",
                provider_domain="tidal",
                provider_instance="tidal_1",
                audio_format=AudioFormat(
                    content_type=ContentType.FLAC, sample_rate=96000, bit_depth=24, bit_rate=0
                ),
            ),
        },
    )

    result = media_item_to_playlist_item(track)

    assert len(result.providers) == 2
    domains = {p.domain for p in result.providers}
    assert domains == {"spotify", "tidal"}
    # primary URI uses the highest quality provider
    assert result.path == "tidal://track/t2"


def test_media_item_to_playlist_item_prefers_available_provider_as_primary() -> None:
    """A higher-quality but unavailable mapping must not be chosen as the primary URI."""
    track = Track(
        item_id="t1",
        provider="spotify",
        name="Track With An Unavailable Best Match",
        duration=200,
        provider_mappings={
            ProviderMapping(
                item_id="unavailable-best",
                provider_domain="tidal",
                provider_instance="tidal_1",
                available=False,
                audio_format=AudioFormat(
                    content_type=ContentType.FLAC, sample_rate=96000, bit_depth=24, bit_rate=0
                ),
            ),
            ProviderMapping(
                item_id="s1",
                provider_domain="spotify",
                provider_instance="spotify_1",
                audio_format=AudioFormat(
                    content_type=ContentType.OGG, sample_rate=44100, bit_depth=0, bit_rate=320
                ),
            ),
        },
    )

    result = media_item_to_playlist_item(track)

    # the tidal mapping has the higher quality, but it is unavailable - the primary
    # URI must fall back to the playable spotify mapping instead
    assert result.path == "spotify://track/s1"


def test_media_item_to_playlist_item_ties_are_broken_deterministically() -> None:
    """Equal-quality provider mappings resolve to the same primary URI every time."""
    tied_format = AudioFormat(
        content_type=ContentType.FLAC, sample_rate=44100, bit_depth=16, bit_rate=1000
    )
    track = Track(
        item_id="t1",
        provider="zulu",
        name="Tied Quality Track",
        duration=200,
        provider_mappings={
            ProviderMapping(
                item_id="z1",
                provider_domain="zulu",
                provider_instance="zulu_1",
                audio_format=tied_format,
            ),
            ProviderMapping(
                item_id="a1",
                provider_domain="alpha",
                provider_instance="alpha_1",
                audio_format=tied_format,
            ),
            ProviderMapping(
                item_id="m1",
                provider_domain="mid",
                provider_instance="mid_1",
                audio_format=tied_format,
            ),
        },
    )

    result = media_item_to_playlist_item(track)

    # provider_mappings is a set: without a tie-break, equal-quality mappings would
    # resolve to whichever the set happens to iterate first
    assert result.path == "alpha://track/a1"


def test_radio_entries_round_trip() -> None:
    """Test that radio entries survive generation and parsing unchanged."""
    items = [
        PlaylistItem(
            path="http://stream.example.com/radio1",
            title="Jazz FM",
            length="-1",
            metadata={"media_type": "radio"},
        ),
        PlaylistItem(
            path="http://stream.example.com/radio2",
            title="Classic Rock Radio",
            length="-1",
            metadata={"media_type": "radio"},
        ),
    ]
    m3u_data = generate_m3u("Radio Stations", items)
    parsed = parse_m3u(m3u_data)

    assert len(parsed) == 2
    assert parsed[0].path == "http://stream.example.com/radio1"
    assert parsed[0].title == "Jazz FM"
    assert parsed[0].metadata is not None
    assert parsed[0].metadata["media_type"] == "radio"
    assert parsed[1].path == "http://stream.example.com/radio2"
    assert parsed[1].title == "Classic Rock Radio"
    assert parse_m3u_playlist_name(m3u_data) == "Radio Stations"


def test_zero_duration_track_round_trips_unknown_length() -> None:
    """Test that a zero-duration track gets an #EXTINF:-1 line that parses back to None."""
    track = Track(
        item_id="t1",
        provider="builtin",
        name="Unknown Length",
        duration=0,
        provider_mappings={
            ProviderMapping(
                item_id="http://example.com/live.mp3",
                provider_domain="builtin",
                provider_instance="builtin",
            ),
        },
    )

    m3u_data = generate_m3u("Playlist", [media_item_to_playlist_item(track)])

    assert "#EXTINF:-1,Unknown Length" in m3u_data
    parsed = parse_m3u(m3u_data)
    assert len(parsed) == 1
    assert parsed[0].length is None
    assert parsed[0].title == "Unknown Length"


# --------------------------------------------------------------------------- #
#  construct_media_item_from_playlist_item — entries without #EXTPROV          #
# --------------------------------------------------------------------------- #


def _mass_with_builtin() -> MagicMock:
    """Return a mock mass whose only resolvable provider is builtin."""

    class DummyProvider:
        domain = "builtin"
        instance_id = "builtin"

    mass = MagicMock()
    mass.get_provider.side_effect = lambda ref: DummyProvider() if ref == "builtin" else None
    return mass


def test_construct_plain_url_gets_builtin_mapping() -> None:
    """A bare stream URL with no #EXTPROV must still produce a usable provider mapping."""
    item = PlaylistItem(path="http://stream.example.com/radio1")

    media_item = construct_media_item_from_playlist_item(
        item, cast("Any", _mass_with_builtin()), MediaType.RADIO
    )

    assert isinstance(media_item, Radio)
    # an item with no mappings never reaches library_add and stays unplayable
    assert len(media_item.provider_mappings) == 1
    mapping = next(iter(media_item.provider_mappings))
    assert mapping.provider_domain == "builtin"
    assert mapping.available is True
    # builtin's item_id *is* the stream url, so the whole url carries through
    assert mapping.item_id == "http://stream.example.com/radio1"
    assert media_item.item_id == "http://stream.example.com/radio1"


@pytest.mark.parametrize(
    "path",
    [
        "some/relative/file.mp3",
        "/media/library/file.mp3",
        # an MA-style provider URI is not a stream URL; builtin would ffprobe it and fail
        "radiobrowser://radio/123",
        "file://host/share/file.mp3",
    ],
)
def test_construct_non_stream_url_gets_no_fallback_mapping(path: str) -> None:
    """Only a plain stream URL falls back to builtin; anything else stays unmapped."""
    media_item = construct_media_item_from_playlist_item(
        PlaylistItem(path=path), cast("Any", _mass_with_builtin()), MediaType.RADIO
    )

    assert media_item is not None
    # claiming builtin availability here would mask an entry that cannot be played
    assert media_item.provider_mappings == set()


def test_construct_defaults_to_requested_media_type() -> None:
    """An entry without #EXTMA media_type honours the caller's default instead of Track."""
    item = PlaylistItem(path="http://stream.example.com/radio1")
    mass = cast("Any", _mass_with_builtin())

    assert isinstance(
        construct_media_item_from_playlist_item(item, mass, MediaType.RADIO),
        Radio,
    )
    # the default stays Track for every existing caller
    assert isinstance(construct_media_item_from_playlist_item(item, mass), Track)


def test_construct_keeps_the_provider_instance() -> None:
    """The entry's own instance is used, not whichever instance of the domain loaded first."""

    class DummyProvider:
        def __init__(self, domain: str, instance_id: str) -> None:
            self.domain = domain
            self.instance_id = instance_id

    first = DummyProvider("radiobrowser", "radiobrowser--AAA")
    second = DummyProvider("radiobrowser", "radiobrowser--BBB")
    providers = {
        # a bare domain lookup resolves to the first configured instance
        "radiobrowser": first,
        "radiobrowser--AAA": first,
        "radiobrowser--BBB": second,
    }
    mass = MagicMock()
    mass.get_provider.side_effect = lambda ref: providers.get(ref)
    item = PlaylistItem(
        path="radiobrowser://radio/station-9",
        metadata={"media_type": MediaType.RADIO.value, "name": "Station Nine"},
        providers=[ProviderMappingInfo("radiobrowser", "station-9", "radiobrowser--BBB")],
    )

    media_item = construct_media_item_from_playlist_item(item, cast("Any", mass), MediaType.RADIO)

    assert media_item is not None
    assert media_item.provider == "radiobrowser--BBB"


# --------------------------------------------------------------------------- #
#  PlaylistController.tracks() — provider-driven pagination                    #
# --------------------------------------------------------------------------- #


def _make_controller() -> PlaylistController:
    """Return a PlaylistController with a minimal mock mass."""
    controller = PlaylistController.__new__(PlaylistController)
    controller.mass = MagicMock()
    return controller


@pytest.mark.asyncio
async def test_tracks_relays_all_provider_pages() -> None:
    """tracks() relays every non-empty provider page until exhausted (provider decides size)."""
    controller = _make_controller()
    page0 = [MagicMock(spec=Track) for _ in range(20)]
    page1 = [MagicMock(spec=Track) for _ in range(15)]
    get_tracks = AsyncMock(side_effect=[page0, page1, []])
    cast("Any", controller)._get_provider_playlist_tracks = get_tracks

    result = [t async for t in controller.tracks("abc", "some_provider")]

    # No controller-side cap: a multi-page (e.g. dynamic) list is relayed in full.
    assert len(result) == 35
    assert get_tracks.await_count == 3


@pytest.mark.asyncio
async def test_tracks_stops_on_empty_page() -> None:
    """tracks() stops fetching once the provider yields an empty page."""
    controller = _make_controller()
    page0 = [MagicMock(spec=Track) for _ in range(7)]
    get_tracks = AsyncMock(side_effect=[page0, []])
    cast("Any", controller)._get_provider_playlist_tracks = get_tracks

    result = [t async for t in controller.tracks("abc", "some_provider")]

    assert len(result) == 7
    assert get_tracks.await_count == 2


# --------------------------------------------------------------------------- #
#  fetch_playlist                                                              #
# --------------------------------------------------------------------------- #

M3U_PLAYLIST = "#EXTM3U\n#EXTINF:-1,Test Station\nhttp://stream.example.com/aac\n"
PLS_PLAYLIST = (
    "[playlist]\n"
    "NumberOfEntries=1\n"
    "File1=http://stream.example.com/aac\n"
    "Title1=Test Station\n"
    "Length1=-1\n"
)
HLS_MEDIA_PLAYLIST = (
    "#EXTM3U\n"
    "#EXT-X-VERSION:3\n"
    "#EXT-X-TARGETDURATION:10\n"
    "#EXTINF:10.0,\n"
    "http://stream.example.com/segment1.aac\n"
)
VERSIONLESS_HLS_MEDIA_PLAYLIST = (
    "#EXTM3U\n"
    "#EXT-X-TARGETDURATION:10\n"
    '#EXT-X-KEY:METHOD=AES-128,URI="skd://test-key"\n'
    "#EXTINF:10,\n"
    "segment1.aac\n"
)
HLS_MASTER_PLAYLIST = (
    "#EXTM3U\n"
    '#EXT-X-STREAM-INF:BANDWIDTH=64000,CODECS="mp4a.40.2"\n'
    "http://stream.example.com/low.m3u8\n"
)


class _FakeContent:
    """Stand-in for the payload stream of an aiohttp response."""

    def __init__(self, raw_data: bytes, chunk_size: int | None = None) -> None:
        self._raw_data = raw_data
        # like the real stream, a read hands over what has arrived so far, not the full
        # amount asked for
        self._chunk_size = chunk_size or max(len(raw_data), 1)
        self._pos = 0

    async def read(self, n: int = -1) -> bytes:
        available = len(self._raw_data) - self._pos
        size = available if n < 0 else min(n, self._chunk_size, available)
        chunk = self._raw_data[self._pos : self._pos + size]
        self._pos += size
        return chunk


class _FakeResponse:
    """Stand-in for the aiohttp response of a playlist fetch."""

    def __init__(
        self,
        raw_data: bytes,
        charset: str | None,
        status: int = 200,
        chunk_size: int | None = None,
    ) -> None:
        self.charset = charset
        self.content = _FakeContent(raw_data, chunk_size)
        self.status = status

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise client_exceptions.ClientResponseError(
                request_info=MagicMock(),
                history=(),
                status=self.status,
            )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        return None


class _FailingRequest:
    """Stand-in for a playlist request that fails before a response is available."""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def __aenter__(self) -> Self:
        raise self._error

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        return None


def _mass_serving(
    raw_data: bytes,
    charset: str | None = None,
    status: int = 200,
    chunk_size: int | None = None,
) -> Any:
    """
    Return a mock mass whose http session serves the given playlist bytes.

    :param raw_data: Raw response body handed to fetch_playlist.
    :param charset: Charset the server declares in its Content-Type header, if any.
    :param status: HTTP status the server answers with.
    :param chunk_size: Largest amount a single read hands over, if the body is chunked.
    """
    mass = MagicMock()
    mass.http_session.get = MagicMock(
        return_value=_FakeResponse(raw_data, charset, status, chunk_size)
    )
    return mass


def _mass_failing(error: BaseException) -> Any:
    """
    Return a mock mass whose http session fails the request with the given error.

    :param error: Exception raised when the request is entered.
    """
    mass = MagicMock()
    mass.http_session.get = MagicMock(return_value=_FailingRequest(error))
    return mass


@pytest.mark.asyncio
async def test_fetch_playlist_timeout() -> None:
    """A timed out fetch is reported as invalid data instead of surfacing raw."""
    mass = _mass_failing(TimeoutError())

    with pytest.raises(InvalidDataError, match="Timeout while fetching playlist"):
        await fetch_playlist(mass, "http://example.com/station.m3u")


@pytest.mark.asyncio
async def test_fetch_playlist_client_error() -> None:
    """A connection failure is reported as invalid data instead of surfacing raw."""
    mass = _mass_failing(client_exceptions.ClientConnectionError("boom"))

    with pytest.raises(InvalidDataError, match="Error while fetching playlist"):
        await fetch_playlist(mass, "http://example.com/station.m3u")


@pytest.mark.asyncio
async def test_fetch_playlist_error_status() -> None:
    """
    An error response is rejected instead of parsed.

    Without the status check every markup line of the error page becomes an entry.
    """
    error_page = (
        b"<html>\n<head><title>404 Not Found</title></head>\n"
        b"<body>\n<center><h1>404 Not Found</h1></center>\n</body>\n</html>\n"
    )
    mass = _mass_serving(error_page, status=404)

    with pytest.raises(InvalidDataError, match="Error while fetching playlist"):
        await fetch_playlist(mass, "http://example.com/station.m3u")


@pytest.mark.asyncio
async def test_fetch_playlist_hls_media_playlist() -> None:
    """An HLS media playlist is rejected for callers that cannot handle segments."""
    mass = _mass_serving(HLS_MEDIA_PLAYLIST.encode())

    with pytest.raises(IsHLSPlaylist):
        await fetch_playlist(mass, "http://example.com/station.m3u8")


@pytest.mark.asyncio
async def test_fetch_playlist_versionless_hls_media_playlist() -> None:
    """A version-less HLS media playlist is recognised by its required tag."""
    mass = _mass_serving(VERSIONLESS_HLS_MEDIA_PLAYLIST.encode())

    with pytest.raises(IsHLSPlaylist):
        await fetch_playlist(mass, "http://example.com/station.m3u8")


@pytest.mark.asyncio
async def test_fetch_playlist_hls_media_playlist_allowed() -> None:
    """With raise_on_hls disabled an HLS media playlist parses like any other M3U."""
    mass = _mass_serving(HLS_MEDIA_PLAYLIST.encode())

    result = await fetch_playlist(mass, "http://example.com/station.m3u8", raise_on_hls=False)

    assert len(result) == 1
    assert result[0].path == "http://stream.example.com/segment1.aac"


@pytest.mark.asyncio
async def test_fetch_playlist_versionless_hls_media_playlist_allowed() -> None:
    """Disabling HLS detection still exposes a version-less playlist's segment and key."""
    mass = _mass_serving(VERSIONLESS_HLS_MEDIA_PLAYLIST.encode())

    result = await fetch_playlist(mass, "http://example.com/station.m3u8", raise_on_hls=False)

    assert len(result) == 1
    assert result[0].path == "segment1.aac"
    assert result[0].key == "skd://test-key"


@pytest.mark.asyncio
async def test_fetch_playlist_hls_master_playlist_always_raises() -> None:
    """A master playlist holds no playable entries, so it is rejected either way."""
    mass = _mass_serving(HLS_MASTER_PLAYLIST.encode())

    with pytest.raises(IsHLSPlaylist):
        await fetch_playlist(mass, "http://example.com/station.m3u8", raise_on_hls=False)


@pytest.mark.asyncio
async def test_fetch_playlist_pls_by_extension(monkeypatch: pytest.MonkeyPatch) -> None:
    """A .pls url picks the PLS parser on the extension alone."""
    # every real PLS body carries the marker too, so only a marker-free body can
    # show which of the two conditions selected the parser
    parsed = [PlaylistItem(path="http://stream.example.com/aac")]
    parse_pls_mock = MagicMock(return_value=parsed)
    monkeypatch.setattr(playlists, "parse_pls", parse_pls_mock)
    mass = _mass_serving(M3U_PLAYLIST.encode())

    result = await fetch_playlist(mass, "http://example.com/station.pls")

    parse_pls_mock.assert_called_once_with(M3U_PLAYLIST)
    assert result == parsed


@pytest.mark.asyncio
async def test_fetch_playlist_pls_by_marker() -> None:
    """PLS content behind a url without the extension is still parsed as PLS."""
    mass = _mass_serving(PLS_PLAYLIST.encode())

    result = await fetch_playlist(mass, "http://example.com/listen")

    assert len(result) == 1
    assert result[0].path == "http://stream.example.com/aac"
    assert result[0].title == "Test Station"


@pytest.mark.asyncio
async def test_fetch_playlist_m3u() -> None:
    """Anything else is parsed as M3U."""
    mass = _mass_serving(M3U_PLAYLIST.encode())

    result = await fetch_playlist(mass, "http://example.com/station.m3u")

    assert len(result) == 1
    assert result[0].path == "http://stream.example.com/aac"
    assert result[0].title == "Test Station"
    assert result[0].length is None


@pytest.mark.asyncio
async def test_fetch_playlist_empty() -> None:
    """A playlist without a single entry is rejected."""
    mass = _mass_serving(b"#EXTM3U\n")

    with pytest.raises(InvalidDataError, match="Empty playlist"):
        await fetch_playlist(mass, "http://example.com/station.m3u")


@pytest.mark.asyncio
async def test_fetch_playlist_unknown_charset_falls_back_to_detection() -> None:
    """
    A charset the remote server made up must not break the fetch.

    Stations do send names Python has no codec for, which decode() answers with a
    LookupError that no caller on this path catches.
    """
    mass = _mass_serving(M3U_PLAYLIST.encode(), charset="utf8mb4")

    result = await fetch_playlist(mass, "http://example.com/station.m3u")

    assert len(result) == 1
    assert result[0].path == "http://stream.example.com/aac"


@pytest.mark.asyncio
async def test_fetch_playlist_undecodable_byte_degrades_instead_of_raising() -> None:
    """One bad byte costs a character, not the whole playlist."""
    raw_data = M3U_PLAYLIST.encode().replace(b"#EXTM3U", b"#EXTM3U\n#\xff")
    mass = _mass_serving(raw_data, charset="utf-8")

    result = await fetch_playlist(mass, "http://example.com/station.m3u")

    assert len(result) == 1
    assert result[0].path == "http://stream.example.com/aac"


@pytest.mark.asyncio
async def test_fetch_playlist_declared_charset_is_used() -> None:
    """A legacy charset the server declares is taken over guesswork."""
    # a mostly-ASCII body gives the detector too little to go on, so a station that
    # names its charset is the only thing keeping such a title readable
    raw_data = "#EXTM3U\n#EXTINF:-1,Хит\nhttp://stream.example.com/aac\n".encode("cp1251")
    mass = _mass_serving(raw_data, charset="cp1251")

    result = await fetch_playlist(mass, "http://example.com/station.m3u")

    assert result[0].title == "Хит"


@pytest.mark.asyncio
async def test_fetch_playlist_reads_a_body_that_arrives_in_chunks() -> None:
    """A playlist spread over several chunks is parsed whole, not just its first chunk."""
    mass = _mass_serving(
        b"#EXTM3U\n#EXTINF:-1,Station\nhttp://stream.example.com/aac\n", chunk_size=8
    )

    result = await fetch_playlist(mass, "http://example.com/station.m3u")

    assert [x.path for x in result] == ["http://stream.example.com/aac"]


@pytest.mark.asyncio
async def test_fetch_playlist_reads_only_the_head_of_the_body() -> None:
    """An oversized playlist is truncated instead of being pulled in whole."""
    padding = "#" + " " * (64 * 1024)
    mass = _mass_serving(f"#EXTM3U\n{padding}\nhttp://stream.example.com/aac\n".encode())

    # the entry sits past the read limit, so nothing is left to parse
    with pytest.raises(InvalidDataError, match="Empty playlist"):
        await fetch_playlist(mass, "http://example.com/station.m3u")
