"""Integration tests for the Bandcamp provider."""

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING
from unittest import mock

import pytest
from bandcamp_async_api import SearchResultArtist, SearchResultTrack
from music_assistant_models.enums import MediaType, StreamType

from music_assistant.controllers.metadata import MetaDataController
from music_assistant.mass import MusicAssistant
from music_assistant.providers.bandcamp import BandcampProvider
from tests.common import wait_for_sync_completion

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig


@pytest.fixture
async def bandcamp_provider(  # noqa: PLR0915
    mass: MusicAssistant,
) -> AsyncGenerator[ProviderConfig]:
    """Configure a Bandcamp test fixture, and add a provider to mass that uses it."""
    # Mock the BandcampAPIClient to avoid real API calls
    with (
        mock.patch("music_assistant.providers.bandcamp.BandcampAPIClient") as mock_client_class,
        # the real throttler spreads background requests over its rate limit period, which
        # would make every test wait out its own sync
        mock.patch.object(BandcampProvider.throttler.throttler, "acquire", return_value=0.0),
        # the MusicBrainz link run after the sync would look up the Bandcamp URLs online
        mock.patch.object(
            MetaDataController,
            "link_providers_via_musicbrainz",
            new_callable=mock.PropertyMock,
            return_value=False,
        ),
    ):
        mock_client = mock.AsyncMock()
        mock_client_class.return_value = mock_client

        # Configure mock client for collection access
        mock_collection = mock.AsyncMock(has_more=False, last_token=None)
        mock_collection.items = []

        # Mock collection items for library tests
        mock_item_artist = mock.AsyncMock()
        mock_item_artist.item_type = "band"
        mock_item_artist.item_id = 123
        mock_item_artist.band_name = "Test Artist"
        mock_item_artist.item_url = "https://test.bandcamp.com"

        mock_item_album = mock.AsyncMock()
        mock_item_album.item_type = "album"
        mock_item_album.band_id = 123
        mock_item_album.item_id = 456
        mock_item_album.item_title = "Test Album"
        mock_item_album.item_url = "https://test.bandcamp.com/album/test-album"

        mock_collection.items = [mock_item_artist, mock_item_album]
        mock_client.get_collection_items.return_value = mock_collection

        # Mock artist and album data
        mock_artist = mock.AsyncMock()
        mock_artist.id = 123
        mock_artist.name = "Test Artist"
        mock_artist.url = "https://test.bandcamp.com"
        mock_client.get_artist.return_value = mock_artist

        mock_album = mock.AsyncMock()
        mock_album.id = 456
        mock_album.title = "Test Album"
        mock_album.artist = mock_artist
        mock_album.url = "https://test.bandcamp.com/album/test-album"
        mock_album.art_url = "https://f4.bcbits.com/img/a1234567890_16.jpg"
        mock_album.release_date = 1609459200
        mock_album.about = "Test album description"
        # Concrete performer credit string — the converter feeds it to
        # slugify_performer which only accepts strings. None means
        # "no separate performer; the album is by the band itself".
        mock_album.tralbum_artist = None

        mock_track = mock.AsyncMock()
        mock_track.id = 789
        mock_track.title = "Test Track"
        mock_track.artist = mock_artist
        mock_track.url = "https://test.bandcamp.com/track/test-track"
        mock_track.duration = 300
        mock_track.streaming_url = {"mp3-320": "https://example.com/track.mp3"}
        mock_track.track_number = 1
        mock_track.lyrics = "Test lyrics"
        mock_track.tralbum_artist = None

        # Configure the streaming_url to behave like a dictionary
        mock_track.configure_mock(streaming_url={"mp3-320": "https://example.com/track.mp3"})

        mock_album.tracks = [mock_track]
        mock_client.get_album.return_value = mock_album
        mock_client.get_track.return_value = mock_track

        async with wait_for_sync_completion(mass):
            config = await mass.config._create_provider_instance(
                "bandcamp",
                {"search_limit": 10, "top_tracks_limit": 50},
                # the identity cookie is collected by the setup flow and lives in setup_data
                setup_data=mass.config._encrypt_values({"identity": "mock_identity_token"}),
            )
            await mass.music.start_sync()

        yield config


@pytest.mark.usefixtures("bandcamp_provider")
async def test_initial_sync(mass: MusicAssistant) -> None:
    """Test that the initial sync adds the collection to the library."""
    artists = await mass.music.artists.library_items()
    albums = await mass.music.albums.library_items()
    tracks = await mass.music.tracks.library_items()

    assert [artist.name for artist in artists] == ["Test Artist"]
    assert [album.name for album in albums] == ["Test Album"]
    assert [track.name for track in tracks] == ["Test Track"]
    for item in (*artists, *albums, *tracks):
        assert {mapping.provider_domain for mapping in item.provider_mappings} == {"bandcamp"}


@pytest.mark.usefixtures("bandcamp_provider")
async def test_search_functionality(mass: MusicAssistant) -> None:
    """Test that a global search returns the Bandcamp results."""
    bandcamp_provider = next(prov for prov in mass.music.providers if prov.domain == "bandcamp")
    assert isinstance(bandcamp_provider, BandcampProvider)
    search_results = [
        # the band row resolves the track's artist without a performer lookup search
        SearchResultArtist(id=321, name="Search Test Artist", url="https://search.bandcamp.com"),
        SearchResultTrack(
            id=987,
            name="Search Test Track",
            url="https://search.bandcamp.com/track/search-test",
            artist_id=321,
            artist_name="Search Test Artist",
            album_id=654,
            album_name="Search Test Album",
        ),
    ]

    with mock.patch.object(
        bandcamp_provider._client, "search", return_value=search_results
    ) as mock_search:
        results = await mass.music.search("test query", [MediaType.TRACK], limit=5)

    mock_search.assert_awaited_once_with("test query")
    assert [(track.name, track.item_id) for track in results.tracks] == [
        ("Search Test Track", "321-654-987")
    ]
    assert results.tracks[0].provider == bandcamp_provider.instance_id


@pytest.mark.usefixtures("bandcamp_provider")
async def test_get_artist_details(mass: MusicAssistant) -> None:
    """Test getting artist details."""
    # Get the bandcamp provider instance
    bandcamp_provider = None
    for provider in mass.music.providers:
        if provider.domain == "bandcamp":
            bandcamp_provider = provider
            break

    assert bandcamp_provider is not None

    # Test artist retrieval
    artist = await bandcamp_provider.get_artist("123")
    assert artist is not None
    assert artist.name == "Test Artist"
    assert artist.provider == bandcamp_provider.instance_id


@pytest.mark.usefixtures("bandcamp_provider")
async def test_get_album_details(mass: MusicAssistant) -> None:
    """Test getting album details."""
    # Get the bandcamp provider instance
    bandcamp_provider = None
    for provider in mass.music.providers:
        if provider.domain == "bandcamp":
            bandcamp_provider = provider
            break

    assert bandcamp_provider is not None

    # Test album retrieval
    album = await bandcamp_provider.get_album("123-456")
    assert album is not None
    assert album.name == "Test Album"
    assert album.provider == bandcamp_provider.instance_id


@pytest.mark.usefixtures("bandcamp_provider")
async def test_get_track_details(mass: MusicAssistant) -> None:
    """Test getting track details."""
    # Get the bandcamp provider instance
    bandcamp_provider = None
    for provider in mass.music.providers:
        if provider.domain == "bandcamp":
            bandcamp_provider = provider
            break

    assert bandcamp_provider is not None

    # Test track retrieval
    track = await bandcamp_provider.get_track("123-456-789")
    assert track is not None
    assert track.name == "Test Track"
    assert track.provider == bandcamp_provider.instance_id


@pytest.mark.usefixtures("bandcamp_provider")
async def test_get_album_tracks(mass: MusicAssistant) -> None:
    """Test getting album tracks."""
    # Get the bandcamp provider instance
    bandcamp_provider = None
    for provider in mass.music.providers:
        if provider.domain == "bandcamp":
            bandcamp_provider = provider
            break

    assert bandcamp_provider is not None

    # Test album tracks retrieval
    tracks = await bandcamp_provider.get_album_tracks("123-456")
    assert len(tracks) == 1
    assert tracks[0].name == "Test Track"


@pytest.mark.usefixtures("bandcamp_provider")
async def test_stream_details(mass: MusicAssistant) -> None:
    """Test stream details retrieval."""
    # Get the bandcamp provider instance
    bandcamp_provider = None
    for provider in mass.music.providers:
        if provider.domain == "bandcamp":
            bandcamp_provider = provider
            break

    assert bandcamp_provider is not None

    # Test stream details retrieval
    stream_details = await bandcamp_provider.get_stream_details("123-456-789", MediaType.TRACK)
    assert stream_details is not None
    assert stream_details.stream_type == StreamType.HTTP
    assert stream_details.path == "https://example.com/track.mp3"
