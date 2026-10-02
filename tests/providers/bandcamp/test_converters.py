"""Test Bandcamp converters."""

from collections.abc import Callable
from unittest.mock import Mock

import pytest
from bandcamp_async_api.models import (
    BCAlbum,
    BCArtist,
    BCTrack,
    CollectionItem,
    FeedTrack,
    FollowingItem,
)
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import MediaItem
from music_assistant_models.media_items import Track as MATrack

from music_assistant.providers.bandcamp.converters import (
    BandcampConverters,
    DiscographyItem,
    collection_album_id,
)


@pytest.fixture
def converters() -> BandcampConverters:
    """Return a BandcampConverters instance."""
    return BandcampConverters("bandcamp", "bandcamp_test")


def test_track_from_search(converters: BandcampConverters) -> None:
    """Test converting SearchResultTrack to MA Track."""
    # Create a mock SearchResultTrack
    search_result = Mock()
    search_result.artist_id = 123
    search_result.album_id = 456
    search_result.id = 789
    search_result.name = "Test Track"
    search_result.artist_name = "Test Artist"
    search_result.album_name = "Test Album"
    search_result.url = "https://test.bandcamp.com/track/test-track"

    result = converters.track_from_search(search_result)

    assert result.item_id == "123-456-789"
    assert result.name == "Test Track"
    assert result.provider == "bandcamp_test"


def test_album_from_search(converters: BandcampConverters) -> None:
    """Test converting SearchResultAlbum to MA Album."""
    # Create a mock SearchResultAlbum
    search_result = Mock()
    search_result.artist_id = 123
    search_result.id = 456
    search_result.name = "Test Album"
    search_result.artist_name = "Test Artist"
    search_result.image_url = "https://f4.bcbits.com/img/a1234567890_16.jpg"
    search_result.url = "https://test.bandcamp.com/album/test-album"
    search_result.artist_url = "https://test.bandcamp.com"

    result = converters.album_from_search(search_result)

    assert result.item_id == "123-456"
    assert result.name == "Test Album"
    assert result.provider == "bandcamp_test"
    # Without disambiguation context, the converter conservatively
    # synthesizes a `{band_id}:{slug}` artist ID — the provider's search
    # method overrides this with a real ID when a `b` result confirms
    # the page owner's name matches.
    artist = next(iter(result.artists))
    assert artist.item_id == "123:test-artist"


def test_album_from_search_uses_provided_artist_item_id(
    converters: BandcampConverters,
) -> None:
    """When the provider resolves the artist ID, the converter must honor it."""
    search_result = Mock()
    search_result.artist_id = 123
    search_result.id = 456
    search_result.name = "Test Album"
    search_result.artist_name = "Test Artist"
    search_result.image_url = None
    search_result.url = "https://test.bandcamp.com/album/test-album"
    search_result.artist_url = "https://test.bandcamp.com"

    result = converters.album_from_search(search_result, artist_item_id="123")

    artist = next(iter(result.artists))
    assert artist.item_id == "123"


def test_track_from_search_uses_provided_artist_item_id(
    converters: BandcampConverters,
) -> None:
    """Same dedup path for track results."""
    search_result = Mock()
    search_result.artist_id = 441379041
    search_result.album_id = 1938115920
    search_result.id = 2114682405
    search_result.name = "Haunted"
    search_result.artist_name = "Mortaja"
    search_result.album_name = "Combined Minds"
    search_result.url = "https://audiophob.bandcamp.com/track/haunted"

    result = converters.track_from_search(search_result, artist_item_id="441379041:mortaja")

    artist = next(iter(result.artists))
    assert artist.item_id == "441379041:mortaja"


def test_artist_from_search(converters: BandcampConverters) -> None:
    """Test converting SearchResultArtist to MA Artist."""
    # Create a mock SearchResultArtist
    search_result = Mock()
    search_result.id = 123
    search_result.name = "Test Artist"
    search_result.url = "https://test.bandcamp.com"
    search_result.image_url = "https://f4.bcbits.com/img/a1234567890_16.jpg"
    search_result.tags = ["rock", "indie"]

    result = converters.artist_from_search(search_result)

    assert result.item_id == "123"
    assert result.name == "Test Artist"
    assert result.provider == "bandcamp_test"


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        pytest.param(CollectionItem("album", 456, 123), "123-456", id="album"),
        pytest.param(
            CollectionItem("package", 900, 123, tralbum_type="a", tralbum_id=456),
            "123-456",
            id="package",
        ),
        pytest.param(CollectionItem("package", 901, 123), None, id="package_without_album"),
        pytest.param(
            CollectionItem("package", 902, 123, tralbum_type="t", tralbum_id=789),
            None,
            id="package_of_a_track",
        ),
        pytest.param(CollectionItem("track", 789, 123), None, id="track"),
        pytest.param(CollectionItem("band", 123, 123), None, id="band"),
    ],
)
def test_collection_album_id(item: CollectionItem, expected: str | None) -> None:
    """Only an album and a package with a digital album give an album ID."""
    assert collection_album_id(item) == expected


def test_album_from_collection(converters: BandcampConverters) -> None:
    """A collection entry gives the album its ID, and the title, band, URL and cover."""
    item = CollectionItem(
        item_type="package",
        item_id=4197129855,
        band_id=1772311897,
        tralbum_type="a",
        tralbum_id=3846833501,
        band_name="The Wow! Scenario",
        item_title="Stand in the Star. A Verse and a Chorus",
        item_url="https://jamesacaster.bandcamp.com/album/stand-in-the-star",
        art_url="https://f4.bcbits.com/img/a1234567890_10.jpg",
    )

    result = converters.album_from_collection(item, "1772311897-3846833501")

    assert (result.item_id, result.name) == (
        "1772311897-3846833501",
        "Stand in the Star. A Verse and a Chorus",
    )
    # band_name is the artist credit on the page of James Acaster, as measured on Bandcamp
    assert [(artist.item_id, artist.name) for artist in result.artists] == [
        ("1772311897:the-wow-scenario", "The Wow! Scenario")
    ]
    assert [mapping.url for mapping in result.provider_mappings] == [item.item_url]
    assert [image.path for image in result.metadata.images or []] == [item.art_url]


@pytest.mark.parametrize(
    ("album_id", "featured_track", "expected_id", "expected_album", "duration"),
    [
        pytest.param(None, 789, "123-0-789", None, 193, id="single"),
        pytest.param(456, 789, "123-456-789", ("123-456", "Album"), 193, id="album_track"),
        pytest.param(None, None, "123-0-789", None, 0, id="no_featured_track"),
    ],
)
def test_track_from_collection(
    converters: BandcampConverters,
    album_id: int | None,
    featured_track: int | None,
    expected_id: str,
    expected_album: tuple[str, str] | None,
    duration: int,
) -> None:
    """A track entry gives the track ID of the track page, its album and its duration."""
    item = CollectionItem(
        item_type="track",
        item_id=789,
        band_id=123,
        tralbum_id=789,
        band_name="Test Band",
        item_title="Track",
        album_id=album_id,
        album_title="Album" if album_id else None,
        featured_track=featured_track,
        featured_track_duration=193.5,
    )

    result = converters.track_from_collection(item)

    assert (result.item_id, result.duration) == (expected_id, duration)
    album = (result.album.item_id, result.album.name) if result.album else None
    assert album == expected_album


@pytest.mark.parametrize(
    ("num_streamable_tracks", "available"),
    [
        pytest.param(0, False, id="no_stream"),
        pytest.param(1, True, id="streams"),
        pytest.param(None, True, id="no_count"),
    ],
)
def test_track_from_collection_follows_the_stream_count(
    converters: BandcampConverters, num_streamable_tracks: int | None, available: bool
) -> None:
    """A track entry that Bandcamp does not stream is unavailable, and one without a count is."""
    item = CollectionItem(
        item_type="track",
        item_id=789,
        band_id=123,
        item_title="Track",
        num_streamable_tracks=num_streamable_tracks,
    )

    result = converters.track_from_collection(item)

    assert [mapping.available for mapping in result.provider_mappings] == [available]


def test_artist_from_following(converters: BandcampConverters) -> None:
    """A following entry gives the same ID, name, URL and URI as the band request."""
    item = FollowingItem(
        band_id=1772311897,
        name="James Acaster",
        url="https://jamesacaster.bandcamp.com",
        image_url="https://f4.bcbits.com/img/46508512_0.jpg",
        location="London, UK",
    )
    band = BCArtist(
        id=1772311897,
        name="James Acaster",
        url="https://jamesacaster.bandcamp.com",
        image_url="https://f4.bcbits.com/img/00046508512_0.png",
    )

    result = converters.artist_from_following(item)
    expected = converters.artist_from_api(band)

    assert (result.item_id, result.name, result.uri) == (
        expected.item_id,
        expected.name,
        expected.uri,
    )
    assert result.provider_mappings == expected.provider_mappings
    assert [image.path for image in result.metadata.images or []] == [item.image_url]


def test_track_from_api(converters: BandcampConverters) -> None:
    """Test converting API Track to MA Track."""
    # Create mock API models
    mock_artist = Mock()
    mock_artist.id = 123
    mock_artist.name = "Test Artist"
    mock_artist.url = "https://test.bandcamp.com"

    mock_track = Mock()
    mock_track.id = 789
    mock_track.title = "Test Track"
    mock_track.artist = mock_artist
    mock_track.url = "https://test.bandcamp.com/track/test-track"
    mock_track.duration = 300
    mock_track.lyrics = "Test lyrics"
    mock_track.track_number = 1
    mock_track.streaming_url = {"mp3-320": "https://example.com/track.mp3"}

    result = converters.track_from_api(
        track=mock_track,
        album_id=456,
        album_name="Test Album",
        album_image_url="https://f4.bcbits.com/img/a1234567890_16.jpg",
    )

    assert result.item_id == "123-456-789"
    assert result.name == "Test Track"
    assert result.provider == "bandcamp_test"
    # Without tralbum_artist there's no separate performer credit, so the
    # artist link is the plain band ID and the display name is the band.
    artist = next(iter(result.artists))
    assert artist.item_id == "123"
    assert artist.name == "Test Artist"


@pytest.mark.parametrize(
    ("streaming_url", "available"),
    [
        pytest.param({"mp3-128": "https://example.com/track.mp3"}, True, id="stream"),
        pytest.param({}, False, id="empty"),
        pytest.param(None, False, id="hidden"),
    ],
)
def test_track_from_api_availability(
    converters: BandcampConverters, streaming_url: dict[str, str] | None, available: bool
) -> None:
    """A track without a streaming URL is unavailable, so the core does not try to play it."""
    track = BCTrack(
        id=789,
        title="Track",
        artist=BCArtist(id=123, name="Test Band"),
        streaming_url=streaming_url,
    )

    result = converters.track_from_api(track=track, album_id=456)

    assert result.available is available
    assert [mapping.available for mapping in result.provider_mappings] == [available]


def test_track_from_api_label_release_uses_synthetic_artist_id(
    converters: BandcampConverters,
) -> None:
    """tralbum_artist != band's name → synthetic artist ID + performer display."""
    mock_artist = Mock()
    mock_artist.id = 441379041
    mock_artist.name = "audiophob"  # page owner — the label
    mock_artist.url = "https://audiophob.bandcamp.com"

    mock_track = Mock()
    mock_track.id = 2114682405
    mock_track.title = "Haunted"
    mock_track.artist = mock_artist
    mock_track.url = "https://audiophob.bandcamp.com/track/haunted"
    mock_track.duration = 344
    mock_track.lyrics = None
    mock_track.track_number = 4
    mock_track.streaming_url = {"mp3-128": "https://example.com/track.mp3"}

    result = converters.track_from_api(
        track=mock_track,
        album_id=1938115920,
        album_name="Combined Minds",
        album_image_url="https://f4.bcbits.com/img/a2825942492_16.jpg",
        tralbum_artist="Mortaja",
    )

    artist = next(iter(result.artists))
    assert artist.item_id == "441379041:mortaja"
    assert artist.name == "Mortaja"


def test_artist_from_api(converters: BandcampConverters) -> None:
    """Test converting API Artist to MA Artist."""
    # Create mock API artist
    mock_artist = Mock()
    mock_artist.id = 123
    mock_artist.name = "Test Artist"
    mock_artist.url = "https://test.bandcamp.com"
    mock_artist.image_url = "https://f4.bcbits.com/img/a1234567890_16.jpg"
    mock_artist.bio = "Test bio"

    result = converters.artist_from_api(mock_artist)

    assert result.item_id == "123"
    assert result.name == "Test Artist"
    assert result.provider == "bandcamp_test"


def test_album_from_api(converters: BandcampConverters) -> None:
    """Album by the band itself: real artist ID, no synthetic credit."""
    mock_artist = Mock()
    mock_artist.id = 123
    mock_artist.name = "Test Artist"
    mock_artist.url = "https://test.bandcamp.com"

    mock_album = Mock()
    mock_album.id = 456
    mock_album.title = "Test Album"
    mock_album.artist = mock_artist
    mock_album.url = "https://test.bandcamp.com/album/test-album"
    mock_album.art_url = "https://f4.bcbits.com/img/a1234567890_16.jpg"
    mock_album.release_date = 1609459200
    mock_album.about = "Test album description"
    mock_album.tralbum_artist = None  # no separate performer credit

    result = converters.album_from_api(mock_album)

    assert result.item_id == "123-456"
    assert result.name == "Test Album"
    assert result.provider == "bandcamp_test"
    artist = next(iter(result.artists))
    assert artist.item_id == "123"
    assert artist.name == "Test Artist"


def test_album_from_api_label_release_uses_synthetic_artist_id(
    converters: BandcampConverters,
) -> None:
    """
    Label release: page owner != performer → synthetic ``{band_id}:{slug}``.

    `artist.name` is the page owner (the label); `tralbum_artist` is the
    performer credit. The displayed artist on the album is the performer.
    """
    mock_artist = Mock()
    mock_artist.id = 441379041
    mock_artist.name = "audiophob"  # page owner — the label
    mock_artist.url = "https://audiophob.bandcamp.com"

    mock_album = Mock()
    mock_album.id = 1938115920
    mock_album.title = "Combined Minds"
    mock_album.artist = mock_artist
    mock_album.url = "https://audiophob.bandcamp.com/album/combined-minds"
    mock_album.art_url = "https://f4.bcbits.com/img/a2825942492_16.jpg"
    mock_album.release_date = 1539907200
    mock_album.about = ""
    mock_album.tralbum_artist = "Mortaja"  # the performer

    result = converters.album_from_api(mock_album)

    artist = next(iter(result.artists))
    assert artist.item_id == "441379041:mortaja"
    assert artist.name == "Mortaja"


def test_track_from_api_without_album_info(converters: BandcampConverters) -> None:
    """Test converting API Track without album info."""
    # Create mock API models
    mock_artist = BCArtist(id=123, name="Test Artist", url="https://test.bandcamp.com")
    mock_track = BCTrack(
        id=789,
        title="Test Track",
        artist=mock_artist,
        url="https://test.bandcamp.com/track/test-track",
        duration=300,
        lyrics="Test lyrics",
        track_number=1,
        streaming_url={"mp3-320": "https://example.com/track.mp3"},
    )

    result = converters.track_from_api(track=mock_track)

    assert result.item_id == "123-0-789"
    assert result.album is None
    assert result.metadata.lyrics == "Test lyrics"


def test_track_from_api_with_album(converters: BandcampConverters) -> None:
    """Test converting API Track with album information."""
    # Create mock API models
    mock_artist = BCArtist(id=123, name="Test Artist", url="https://test.bandcamp.com")
    mock_album = BCAlbum(
        id=456,
        title="Test Album",
        artist=mock_artist,
        url="https://test.bandcamp.com/album/test-album",
        art_url="https://f4.bcbits.com/img/a1234567890_16.jpg",
        release_date=1609459200,
        about="Test album description",
    )
    mock_track = BCTrack(
        id=789,
        title="Test Track",
        artist=mock_artist,
        album=mock_album,
        url="https://test.bandcamp.com/track/test-track",
        duration=300,
        lyrics="Test lyrics",
        track_number=1,
        streaming_url={"mp3-320": "https://example.com/track.mp3"},
    )

    result = converters.track_from_api(track=mock_track)

    assert result.item_id == "123-0-789"
    assert result.album is not None
    assert result.album.item_id == "123-456"
    assert result.album.name == "Test Album"


def _make_mock_track(streaming_url: dict[str, str]) -> Mock:
    """Create a mock API track with the given streaming URL."""
    mock_artist = Mock()
    mock_artist.id = 123
    mock_artist.name = "Test Artist"
    mock_artist.url = "https://test.bandcamp.com"

    mock_track = Mock()
    mock_track.id = 789
    mock_track.title = "Test Track"
    mock_track.artist = mock_artist
    mock_track.url = "https://test.bandcamp.com/track/test-track"
    mock_track.duration = 300
    mock_track.lyrics = None
    mock_track.track_number = 1
    mock_track.streaming_url = streaming_url
    return mock_track


def test_track_from_api_audio_format_mp3_320(converters: BandcampConverters) -> None:
    """Test that mp3-320 streaming URL sets audio format correctly."""
    mock_track = _make_mock_track({"mp3-320": "https://example.com/track.mp3"})
    result = converters.track_from_api(track=mock_track, album_id=456)
    mapping = next(iter(result.provider_mappings))
    assert mapping.audio_format.content_type == ContentType.MP3
    assert mapping.audio_format.bit_rate == 320


def test_track_from_api_audio_format_mp3_v0(converters: BandcampConverters) -> None:
    """Test that mp3-v0 streaming URL sets content type with no bitrate (VBR)."""
    mock_track = _make_mock_track({"mp3-v0": "https://example.com/track.mp3"})
    result = converters.track_from_api(track=mock_track, album_id=456)
    mapping = next(iter(result.provider_mappings))
    assert mapping.audio_format.content_type == ContentType.MP3
    assert mapping.audio_format.bit_rate is None


def test_track_from_api_audio_format_mp3_128(converters: BandcampConverters) -> None:
    """Test that mp3-128 streaming URL sets audio format correctly."""
    mock_track = _make_mock_track({"mp3-128": "https://example.com/track.mp3"})
    result = converters.track_from_api(track=mock_track, album_id=456)
    mapping = next(iter(result.provider_mappings))
    assert mapping.audio_format.content_type == ContentType.MP3
    assert mapping.audio_format.bit_rate == 128


def test_track_from_api_audio_format_none_streaming_url(converters: BandcampConverters) -> None:
    """Test that None streaming_url does not crash."""
    mock_track = _make_mock_track({"mp3-128": "https://example.com/track.mp3"})
    mock_track.streaming_url = None
    result = converters.track_from_api(track=mock_track, album_id=456)
    mapping = next(iter(result.provider_mappings))
    assert mapping.audio_format.content_type == ContentType.MP3
    assert mapping.audio_format.bit_rate is None


def test_track_from_feed(converters: BandcampConverters) -> None:
    """Test converting a FeedTrack to MA Track."""
    track = FeedTrack(
        track_id=789,
        title="Feed Track",
        band_id=123,
        band_name="Test Artist",
        album_id=456,
        album_title="Test Album",
        track_num=3,
        duration=212.5,
        streaming_url={"mp3-128": "https://example.com/feed.mp3"},
        art_id=987,
        track_url="https://test.bandcamp.com/track/feed-track",
    )

    result = converters.track_from_feed(track)

    assert result.item_id == "123-456-789"
    assert result.name == "Feed Track"
    assert result.duration == 212
    assert result.track_number == 3
    assert result.album is not None
    assert result.album.item_id == "123-456"
    assert result.provider == "bandcamp_test"


def test_track_from_feed_standalone(converters: BandcampConverters) -> None:
    """Test a feed track without an album maps to album_id 0 and omits the album."""
    track = FeedTrack(track_id=789, title="Single", band_id=123, band_name="Test Artist")

    result = converters.track_from_feed(track)

    assert result.item_id == "123-0-789"
    assert result.album is None


def test_streaming_url_priority_v0_over_320(converters: BandcampConverters) -> None:
    """Test that mp3-v0 is preferred over mp3-320."""
    url, bitrate, content_type = converters.streaming_url_from_api(
        {
            "mp3-320": "https://example.com/320.mp3",
            "mp3-v0": "https://example.com/v0.mp3",
        }
    )
    assert url == "https://example.com/v0.mp3"
    assert bitrate is None
    assert content_type == ContentType.MP3


def test_streaming_url_priority_320_over_128(converters: BandcampConverters) -> None:
    """Test that mp3-320 is preferred over mp3-128."""
    url, bitrate, content_type = converters.streaming_url_from_api(
        {
            "mp3-128": "https://example.com/128.mp3",
            "mp3-320": "https://example.com/320.mp3",
        }
    )
    assert url == "https://example.com/320.mp3"
    assert bitrate == 320
    assert content_type == ContentType.MP3


def test_streaming_url_priority_v0_over_320_over_128(converters: BandcampConverters) -> None:
    """Test full priority chain when all three formats are present."""
    url, bitrate, content_type = converters.streaming_url_from_api(
        {
            "mp3-128": "https://example.com/128.mp3",
            "mp3-320": "https://example.com/320.mp3",
            "mp3-v0": "https://example.com/v0.mp3",
        }
    )
    assert url == "https://example.com/v0.mp3"
    assert bitrate is None
    assert content_type == ContentType.MP3


def test_streaming_url_fallback_unknown_key(converters: BandcampConverters) -> None:
    """Test that an unknown streaming key falls back with UNKNOWN content type."""
    url, bitrate, content_type = converters.streaming_url_from_api(
        {"ogg-vorbis": "https://example.com/track.ogg"}
    )
    assert url == "https://example.com/track.ogg"
    assert bitrate is None
    assert content_type == ContentType.UNKNOWN


def test_streaming_url_empty_dict(converters: BandcampConverters) -> None:
    """Test that empty dict returns None for URL and bitrate."""
    url, bitrate, content_type = converters.streaming_url_from_api({})
    assert url is None
    assert bitrate is None
    assert content_type == ContentType.MP3


def test_album_from_discography_item(converters: BandcampConverters) -> None:
    """Test converting a full discography item to MA Album."""
    item: DiscographyItem = {
        "item_id": 394626934,
        "item_type": "album",
        "artist_name": "Amanda Palmer & Friends",
        "band_name": "Amanda Palmer",
        "title": "Forty-Five Degrees",
        "art_id": 3547137148,
        "release_date": "21 Feb 2020 00:00:00 GMT",
        "band_id": 3463798201,
    }
    result = converters.album_from_discography_item(item)

    assert result.item_id == "3463798201-394626934"
    assert result.name == "Forty-Five Degrees"
    assert result.year == 2020
    assert result.provider == "bandcamp_test"
    artists = list(result.artists)
    assert len(artists) == 1
    assert artists[0].name == "Amanda Palmer & Friends"
    # The collaboration credit differs from Amanda Palmer's own band
    # name — surface it as a synthetic performer scoped to her band so
    # users can navigate to *just* the collab releases.
    assert artists[0].item_id == "3463798201:amanda-palmer-and-friends"
    assert result.metadata.images
    assert any("a3547137148_0.jpg" in img.path for img in result.metadata.images)


def test_album_from_discography_item_missing_art_id(converters: BandcampConverters) -> None:
    """Test discography item with no art_id produces album without image."""
    item: DiscographyItem = {
        "item_id": 100,
        "item_type": "album",
        "band_name": "Test",
        "title": "No Art",
        "band_id": 200,
        "release_date": "01 Jan 2021 00:00:00 GMT",
    }
    result = converters.album_from_discography_item(item)
    assert result.item_id == "200-100"
    assert result.year == 2021
    assert not result.metadata.images


def test_album_from_discography_item_missing_release_date(converters: BandcampConverters) -> None:
    """Test discography item with no release_date sets year to None."""
    item: DiscographyItem = {
        "item_id": 100,
        "item_type": "album",
        "band_name": "Test",
        "title": "No Date",
        "band_id": 200,
        "art_id": 999,
    }
    result = converters.album_from_discography_item(item)
    assert result.year is None


def test_album_from_discography_item_artist_name_fallback(
    converters: BandcampConverters,
) -> None:
    """Test that band_name is used when artist_name is None."""
    item: DiscographyItem = {
        "item_id": 100,
        "item_type": "album",
        "artist_name": None,
        "band_name": "Fallback Artist",
        "title": "Test",
        "band_id": 200,
        "art_id": 999,
        "release_date": "15 Mar 2022 00:00:00 GMT",
    }
    result = converters.album_from_discography_item(item)
    artists = list(result.artists)
    assert artists[0].name == "Fallback Artist"
    # Performer == band → real artist ID, not synthetic.
    assert artists[0].item_id == "200"


def test_album_from_discography_item_label_release_uses_synthetic_artist(
    converters: BandcampConverters,
) -> None:
    """
    Discography of a label: per-item ``artist_name`` differs from ``band_name``.

    The artist link should point at a synthetic performer scoped to the
    label's band_id.
    """
    item: DiscographyItem = {
        "item_id": 1938115920,
        "item_type": "album",
        "artist_name": "Mortaja",
        "band_name": "audiophob",
        "title": "Combined Minds",
        "band_id": 441379041,
        "art_id": 2825942492,
        "release_date": "18 Oct 2018 00:00:00 GMT",
    }
    result = converters.album_from_discography_item(item)
    artists = list(result.artists)
    assert artists[0].name == "Mortaja"
    assert artists[0].item_id == "441379041:mortaja"


def test_synthetic_artist_basics(converters: BandcampConverters) -> None:
    """The synthetic_artist factory builds an Artist scoped to (band, performer)."""
    artist = converters.synthetic_artist(
        band_id=441379041,
        performer_name="Mortaja",
        url="https://audiophob.bandcamp.com",
        image_url="https://f4.bcbits.com/img/a2825942492_16.jpg",
    )
    assert artist.item_id == "441379041:mortaja"
    assert artist.name == "Mortaja"
    assert artist.provider == "bandcamp_test"
    assert artist.uri == "bandcamp_test://artist/441379041:mortaja"
    # The provider mapping pins the same composite ID and retains the
    # hosting page URL for navigation.
    mapping = next(iter(artist.provider_mappings))
    assert mapping.item_id == "441379041:mortaja"
    assert mapping.url == "https://audiophob.bandcamp.com"


@pytest.mark.parametrize(
    ("duration", "expected"),
    [
        pytest.param(326.895, 326, id="float"),
        pytest.param(300, 300, id="int"),
        pytest.param(None, 0, id="no_audio"),
    ],
)
def test_track_from_api_duration(
    converters: BandcampConverters, duration: float | None, expected: int
) -> None:
    """The duration is whole seconds, and a track with no audio still loads from the cache."""
    api_track = BCTrack(
        id=789,
        title="Test Track",
        artist=BCArtist(id=123, name="Test Artist"),
        duration=duration,
    )

    result = converters.track_from_api(api_track)

    assert result.duration == expected
    assert MATrack.from_dict(result.to_dict()).duration == expected


def _search_album(image_url: str | None) -> Mock:
    """Create a mock search album with the given image URL."""
    item = Mock()
    item.artist_id = 123
    item.id = 456
    item.name = "Test Album"
    item.artist_name = "Test Artist"
    item.image_url = image_url
    item.url = "https://test.bandcamp.com/album/test-album"
    item.artist_url = "https://test.bandcamp.com"
    return item


def _artist(image_url: str | None) -> Mock:
    """Create a mock search or API artist with the given image URL."""
    item = Mock()
    item.id = 123
    item.name = "Test Artist"
    item.url = "https://test.bandcamp.com"
    item.image_url = image_url
    item.tags = []
    item.bio = None
    return item


def _api_album(art_url: str | None) -> Mock:
    """Create a mock API album with the given cover URL."""
    album = Mock()
    album.id = 456
    album.title = "Test Album"
    album.artist = _artist(None)
    album.url = "https://test.bandcamp.com/album/test-album"
    album.art_url = art_url
    album.release_date = None
    album.about = None
    album.tralbum_artist = None
    return album


@pytest.mark.parametrize(
    "convert",
    [
        pytest.param(lambda c: c.album_from_search(_search_album(None)), id="album_from_search"),
        pytest.param(lambda c: c.artist_from_search(_artist(None)), id="artist_from_search"),
        pytest.param(lambda c: c.artist_from_api(_artist(None)), id="artist_from_api"),
        pytest.param(lambda c: c.album_from_api(_api_album(None)), id="album_from_api"),
        pytest.param(
            lambda c: c.artist_from_following(FollowingItem(band_id=123, name="Test Artist")),
            id="artist_from_following",
        ),
        pytest.param(
            lambda c: c.album_from_collection(CollectionItem("album", 456, 123), "123-456"),
            id="album_from_collection",
        ),
        pytest.param(
            lambda c: c.track_from_collection(CollectionItem("track", 789, 123)),
            id="track_from_collection",
        ),
    ],
)
def test_converters_add_no_image_without_a_path(
    converters: BandcampConverters, convert: Callable[[BandcampConverters], MediaItem]
) -> None:
    """An item without an image gets no image, not one with the path None."""
    result = convert(converters)

    assert not result.metadata.images
    for artist in getattr(result, "artists", []):
        assert artist.image is None
