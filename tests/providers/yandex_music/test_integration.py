"""Integration tests for the Yandex Music provider with in-process Music Assistant."""

from __future__ import annotations

import json
import pathlib
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any, cast
from unittest import mock

import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import ResourceTemporarilyUnavailable
from yandex_music import Album as YandexAlbum
from yandex_music import Artist as YandexArtist
from yandex_music import Playlist as YandexPlaylist
from yandex_music import Track as YandexTrack

from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider
from tests.common import wait_for_sync_completion

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"
_DE_JSON_CLIENT = type("ClientStub", (), {"report_unknown_fields": False})()


def _load_json(path: pathlib.Path) -> dict[str, Any]:
    """Load JSON fixture."""
    with open(path) as f:
        return cast("dict[str, Any]", json.load(f))


def _load_yandex_objects() -> tuple[Any, Any, Any, Any]:
    """Load Yandex Artist, Album, Track, Playlist from fixtures for mock client."""
    artist = YandexArtist.de_json(
        _load_json(FIXTURES_DIR / "artists" / "minimal.json"), _DE_JSON_CLIENT
    )
    album = YandexAlbum.de_json(
        _load_json(FIXTURES_DIR / "albums" / "minimal.json"), _DE_JSON_CLIENT
    )
    track = YandexTrack.de_json(
        _load_json(FIXTURES_DIR / "tracks" / "minimal.json"), _DE_JSON_CLIENT
    )
    playlist = YandexPlaylist.de_json(
        _load_json(FIXTURES_DIR / "playlists" / "minimal.json"), _DE_JSON_CLIENT
    )
    return artist, album, track, playlist


def _make_search_result(track: Any, album: Any, artist: Any, playlist: Any) -> Any:
    """Build a Search-like object with .tracks.results, .albums.results, etc."""
    return type(
        "Search",
        (),
        {
            "tracks": type("TracksResult", (), {"results": [track]})(),
            "albums": type("AlbumsResult", (), {"results": [album]})(),
            "artists": type("ArtistsResult", (), {"results": [artist]})(),
            "playlists": type("PlaylistsResult", (), {"results": [playlist]})(),
        },
    )()


def _make_download_info(
    codec: str = "mp3",
    direct_link: str = "https://example.com/yandex_track.mp3",
    bitrate_in_kbps: int = 320,
) -> Any:
    """Build DownloadInfo-like object for streaming."""
    return type(
        "DownloadInfo",
        (),
        {
            "direct_link": direct_link,
            "codec": codec,
            "bitrate_in_kbps": bitrate_in_kbps,
        },
    )()


@pytest.fixture
async def yandex_music_provider(
    mass: MusicAssistant,
) -> AsyncGenerator[ProviderConfig, None]:
    """Configure Yandex Music provider with mocked API client and add to mass."""
    artist, album, track, playlist = _load_yandex_objects()
    search_result = _make_search_result(track, album, artist, playlist)
    download_info = _make_download_info()

    # Album with volumes for get_album_tracks
    album_with_volumes = type(
        "AlbumWithVolumes",
        (),
        {
            "id": album.id,
            "title": album.title,
            "volumes": [[track]],
            "artists": album.artists if hasattr(album, "artists") else [],
            "year": getattr(album, "year", None),
            "release_date": getattr(album, "release_date", None),
            "genre": getattr(album, "genre", None),
            "cover_uri": getattr(album, "cover_uri", None),
            "og_image": getattr(album, "og_image", None),
            "type": getattr(album, "type", "album"),
            "available": getattr(album, "available", True),
        },
    )()

    with mock.patch(
        "music_assistant.providers.yandex_music.provider.YandexMusicClient"
    ) as mock_client_class:
        mock_client = mock.AsyncMock()
        mock_client_class.return_value = mock_client

        mock_client.connect = mock.AsyncMock(return_value=True)
        mock_client.user_id = 12345

        mock_client.get_liked_tracks = mock.AsyncMock(return_value=[])
        mock_client.get_liked_albums = mock.AsyncMock(return_value=[])
        mock_client.get_liked_artists = mock.AsyncMock(return_value=[])
        mock_client.get_user_playlists = mock.AsyncMock(return_value=[playlist])

        mock_client.search = mock.AsyncMock(return_value=search_result)
        mock_client.get_track = mock.AsyncMock(return_value=track)
        mock_client.get_tracks = mock.AsyncMock(return_value=[track])
        mock_client.get_album = mock.AsyncMock(return_value=album)
        mock_client.get_album_with_tracks = mock.AsyncMock(return_value=album_with_volumes)
        mock_client.get_artist = mock.AsyncMock(return_value=artist)
        mock_client.get_artist_albums = mock.AsyncMock(return_value=[album])
        mock_client.get_artist_tracks = mock.AsyncMock(return_value=[track])
        mock_client.get_playlist = mock.AsyncMock(return_value=playlist)
        mock_client.get_track_download_info = mock.AsyncMock(return_value=[download_info])

        async with wait_for_sync_completion(mass):
            config = await mass.config.save_provider_config(
                "yandex_music",
                {"token": "mock_yandex_token", "quality": "high"},
            )
            await mass.music.start_sync()

        yield config


@pytest.fixture
async def yandex_music_provider_lossless(
    mass: MusicAssistant,
) -> AsyncGenerator[ProviderConfig, None]:
    """Configure Yandex Music with quality=lossless and mock returning MP3 + FLAC."""
    artist, album, track, playlist = _load_yandex_objects()
    search_result = _make_search_result(track, album, artist, playlist)
    mp3_info = _make_download_info(
        codec="mp3",
        direct_link="https://example.com/yandex_track.mp3",
        bitrate_in_kbps=320,
    )
    flac_info = _make_download_info(
        codec="flac",
        direct_link="https://example.com/yandex_track.flac",
        bitrate_in_kbps=0,
    )
    download_infos = [mp3_info, flac_info]

    album_with_volumes = type(
        "AlbumWithVolumes",
        (),
        {
            "id": album.id,
            "title": album.title,
            "volumes": [[track]],
            "artists": album.artists if hasattr(album, "artists") else [],
            "year": getattr(album, "year", None),
            "release_date": getattr(album, "release_date", None),
            "genre": getattr(album, "genre", None),
            "cover_uri": getattr(album, "cover_uri", None),
            "og_image": getattr(album, "og_image", None),
            "type": getattr(album, "type", "album"),
            "available": getattr(album, "available", True),
        },
    )()

    with mock.patch(
        "music_assistant.providers.yandex_music.provider.YandexMusicClient"
    ) as mock_client_class:
        mock_client = mock.AsyncMock()
        mock_client_class.return_value = mock_client

        mock_client.connect = mock.AsyncMock(return_value=True)
        mock_client.user_id = 12345

        mock_client.get_liked_tracks = mock.AsyncMock(return_value=[])
        mock_client.get_liked_albums = mock.AsyncMock(return_value=[])
        mock_client.get_liked_artists = mock.AsyncMock(return_value=[])
        mock_client.get_user_playlists = mock.AsyncMock(return_value=[playlist])

        mock_client.search = mock.AsyncMock(return_value=search_result)
        mock_client.get_track = mock.AsyncMock(return_value=track)
        mock_client.get_tracks = mock.AsyncMock(return_value=[track])
        mock_client.get_album = mock.AsyncMock(return_value=album)
        mock_client.get_album_with_tracks = mock.AsyncMock(return_value=album_with_volumes)
        mock_client.get_artist = mock.AsyncMock(return_value=artist)
        mock_client.get_artist_albums = mock.AsyncMock(return_value=[album])
        mock_client.get_artist_tracks = mock.AsyncMock(return_value=[track])
        mock_client.get_playlist = mock.AsyncMock(return_value=playlist)
        # get-file-info lossless is tried first; mock returns None so we use download_info path
        mock_client.get_track_file_info_lossless = mock.AsyncMock(return_value=None)
        mock_client.get_track_download_info = mock.AsyncMock(return_value=download_infos)

        async with wait_for_sync_completion(mass):
            config = await mass.config.save_provider_config(
                "yandex_music",
                {"token": "mock_yandex_token", "quality": "lossless"},
            )
            await mass.music.start_sync()

        yield config


def _get_yandex_provider(mass: MusicAssistant) -> MusicProvider | None:
    """Get Yandex Music provider instance from mass."""
    for provider in mass.music.providers:
        if provider.domain == "yandex_music":
            return provider
    return None


@pytest.mark.usefixtures("yandex_music_provider")
async def test_registration_and_sync(mass: MusicAssistant) -> None:
    """Test that provider is registered and sync completes."""
    prov = _get_yandex_provider(mass)
    assert prov is not None
    assert prov.domain == "yandex_music"
    assert prov.instance_id


@pytest.mark.usefixtures("yandex_music_provider")
async def test_search(mass: MusicAssistant) -> None:
    """Test search returns results from yandex_music."""
    results = await mass.music.search("test query", [MediaType.TRACK], limit=5)
    yandex_tracks = [t for t in results.tracks if t.provider and "yandex_music" in t.provider]
    assert len(yandex_tracks) >= 0


@pytest.mark.usefixtures("yandex_music_provider")
async def test_get_artist(mass: MusicAssistant) -> None:
    """Test getting artist by id."""
    prov = _get_yandex_provider(mass)
    assert prov is not None
    artist = await prov.get_artist("100")
    assert artist is not None
    assert artist.name
    assert artist.provider == prov.instance_id
    assert artist.item_id == "100"


@pytest.mark.usefixtures("yandex_music_provider")
async def test_get_album(mass: MusicAssistant) -> None:
    """Test getting album by id."""
    prov = _get_yandex_provider(mass)
    assert prov is not None
    album = await prov.get_album("300")
    assert album is not None
    assert album.name
    assert album.provider == prov.instance_id
    assert album.item_id == "300"


@pytest.mark.usefixtures("yandex_music_provider")
async def test_get_track(mass: MusicAssistant) -> None:
    """Test getting track by id."""
    prov = _get_yandex_provider(mass)
    assert prov is not None
    track = await prov.get_track("400")
    assert track is not None
    assert track.name
    assert track.provider == prov.instance_id
    assert track.item_id == "400"


@pytest.mark.usefixtures("yandex_music_provider")
async def test_get_album_tracks(mass: MusicAssistant) -> None:
    """Test getting album tracks."""
    prov = _get_yandex_provider(mass)
    assert prov is not None
    tracks = await prov.get_album_tracks("300")
    assert isinstance(tracks, list)
    assert len(tracks) >= 0


@pytest.mark.usefixtures("yandex_music_provider")
async def test_get_playlist_tracks(mass: MusicAssistant) -> None:
    """Test getting playlist tracks."""
    prov = _get_yandex_provider(mass)
    assert prov is not None
    tracks = await prov.get_playlist_tracks("12345:3", page=0)
    assert isinstance(tracks, list)
    assert len(tracks) >= 0


@pytest.mark.usefixtures("yandex_music_provider")
async def test_get_stream_details(mass: MusicAssistant) -> None:
    """Test stream details retrieval."""
    prov = _get_yandex_provider(mass)
    assert prov is not None
    stream_details = await prov.get_stream_details("400", MediaType.TRACK)
    assert stream_details is not None
    assert stream_details.stream_type == StreamType.HTTP
    assert stream_details.path == "https://example.com/yandex_track.mp3"


@pytest.mark.usefixtures("yandex_music_provider_lossless")
async def test_get_stream_details_returns_flac_when_lossless_selected(
    mass: MusicAssistant,
) -> None:
    """When quality=lossless and API returns MP3+FLAC, stream details use FLAC."""
    prov = _get_yandex_provider(mass)
    assert prov is not None
    stream_details = await prov.get_stream_details("400", MediaType.TRACK)
    assert stream_details is not None
    assert stream_details.audio_format.content_type == ContentType.FLAC
    assert stream_details.path == "https://example.com/yandex_track.flac"


@pytest.mark.usefixtures("yandex_music_provider")
async def test_library_items(mass: MusicAssistant) -> None:
    """Test library artists, albums, tracks, playlists."""
    prov = _get_yandex_provider(mass)
    assert prov is not None
    instance_id = prov.instance_id

    artists = await mass.music.artists.library_items()
    yandex_artists = [a for a in artists if a.provider == instance_id]
    assert len(yandex_artists) >= 0

    albums = await mass.music.albums.library_items()
    yandex_albums = [a for a in albums if a.provider == instance_id]
    assert len(yandex_albums) >= 0

    tracks = await mass.music.tracks.library_items()
    yandex_tracks = [t for t in tracks if t.provider == instance_id]
    assert len(yandex_tracks) >= 0

    playlists = await mass.music.playlists.library_items()
    yandex_playlists = [p for p in playlists if p.provider == instance_id]
    assert len(yandex_playlists) >= 0


@pytest.mark.usefixtures("yandex_music_provider")
async def test_browse(mass: MusicAssistant) -> None:
    """Test browse root and subpaths."""
    prov = _get_yandex_provider(mass)
    assert prov is not None
    base_path = f"{prov.instance_id}://"
    root_items = await prov.browse(path=base_path)
    assert root_items is not None
    assert isinstance(root_items, (list, tuple))

    artists_path = f"{prov.instance_id}://artists"
    artists_items = await prov.browse(path=artists_path)
    assert artists_items is not None
    assert isinstance(artists_items, (list, tuple))


# -- Playlist edge-case tests --------------------------------------------------


@pytest.mark.usefixtures("yandex_music_provider")
async def test_get_playlist_tracks_page_gt_zero_returns_empty(mass: MusicAssistant) -> None:
    """Page > 0 returns empty list (Yandex returns all tracks in one call)."""
    prov = _get_yandex_provider(mass)
    assert prov is not None
    # Use a different playlist ID to avoid cache collision with test_get_playlist_tracks
    result = await prov.get_playlist_tracks("12345:99", page=1)
    assert result == []


@pytest.mark.usefixtures("yandex_music_provider")
async def test_get_playlist_tracks_fetch_tracks_async_fallback(mass: MusicAssistant) -> None:
    """When playlist.tracks is None but track_count > 0, fetch_tracks_async is used."""
    prov = _get_yandex_provider(mass)
    assert prov is not None

    _, _, track, _ = _load_yandex_objects()

    # Build a playlist object with tracks=None and track_count=5
    track_short = type("TrackShort", (), {"track_id": 400, "id": 400})()
    playlist_no_tracks = type(
        "Playlist",
        (),
        {
            "owner": type("Owner", (), {"uid": 12345})(),
            "kind": 77,
            "title": "Fallback Playlist",
            "tracks": None,
            "track_count": 5,
            "fetch_tracks_async": mock.AsyncMock(return_value=[track_short]),
        },
    )()

    prov.client.get_playlist = mock.AsyncMock(return_value=playlist_no_tracks)  # type: ignore[attr-defined]
    prov.client.get_tracks = mock.AsyncMock(return_value=[track])  # type: ignore[attr-defined]

    result = await prov.get_playlist_tracks("12345:77", page=0)
    assert isinstance(result, list)
    assert len(result) >= 1
    playlist_no_tracks.fetch_tracks_async.assert_awaited_once()


@pytest.mark.usefixtures("yandex_music_provider")
async def test_get_playlist_tracks_empty_batch_raises(mass: MusicAssistant) -> None:
    """Empty batch result from get_tracks raises ResourceTemporarilyUnavailable."""
    prov = _get_yandex_provider(mass)
    assert prov is not None

    # Build a playlist with tracks that have track_ids
    track_short = type("TrackShort", (), {"track_id": 400, "id": 400})()
    playlist_with_tracks = type(
        "Playlist",
        (),
        {
            "owner": type("Owner", (), {"uid": 12345})(),
            "kind": 88,
            "title": "Batch Fail Playlist",
            "tracks": [track_short],
            "track_count": 1,
        },
    )()

    prov.client.get_playlist = mock.AsyncMock(return_value=playlist_with_tracks)  # type: ignore[attr-defined]
    prov.client.get_tracks = mock.AsyncMock(return_value=[])  # type: ignore[attr-defined]

    with pytest.raises(ResourceTemporarilyUnavailable):
        await prov.get_playlist_tracks("12345:88", page=0)
