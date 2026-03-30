"""Tests for playlist parsing and generation helpers."""

from music_assistant.helpers.playlists import (
    ImageInfo,
    PlaylistItem,
    ProviderMappingInfo,
    generate_m3u,
    parse_extinf_title,
    parse_m3u,
    parse_m3u_playlist_name,
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


def test_generate_m3u_empty() -> None:
    """Test generating an empty M3U playlist."""
    result = generate_m3u("Empty Playlist", [])
    assert result == "#EXTM3U\n#PLAYLIST:Empty Playlist\n"


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
