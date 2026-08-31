"""Unit tests for Plex provider audiobook and podcast methods."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest import mock
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse

import plexapi.exceptions
import pytest
from music_assistant_models.enums import ContentType, MediaType, ProviderFeature, StreamType
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.plex import PlexProvider
from music_assistant.providers.plex.constants import (
    CONF_STREAM_QUALITY,
    STREAM_QUALITY_128,
    STREAM_QUALITY_ORIGINAL,
)
from music_assistant.providers.plex.helpers import SUPPORTED_FEATURES

LIBRARY_TYPE_AUDIOBOOKS = "audiobooks"
LIBRARY_TYPE_MUSIC = "music"
LIBRARY_TYPE_PODCASTS = "podcasts"


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_provider(
    library_type: str = LIBRARY_TYPE_MUSIC,
    stream_quality: str = STREAM_QUALITY_ORIGINAL,
) -> Any:
    """Create a minimal PlexProvider instance for testing."""
    mock_mass = MagicMock()
    mock_mass.cache = MagicMock()

    mock_config = MagicMock()
    mock_config.instance_id = "plex_instance_1"

    mock_config_values: dict[str, Any] = {
        "library_type": library_type,
        "log_level": "INFO",
        CONF_STREAM_QUALITY: stream_quality,
        "token": "local_auth",
    }

    class MockValue:
        """Simple wrapper for mock config values."""

        def __init__(self, val: Any) -> None:
            self.value = val

    mock_config.values = {k: MockValue(v) for k, v in mock_config_values.items()}
    mock_config.get_value = lambda key: mock_config_values.get(key)

    # the auth token + library type are read via get_setup_value (setup_data store)
    setup_data = {"library_type": library_type, "token": "local_auth"}
    mock_mass.config.get = lambda key, default=None: (
        setup_data if str(key).endswith("/setup_data") else default
    )
    mock_mass.config.get_raw_provider_config_value = lambda _instance_id, _key: None
    mock_mass.config.decrypt_string = lambda value: value

    mock_manifest = MagicMock()
    mock_manifest.type = "music"
    mock_manifest.domain = "plex"

    provider = PlexProvider(mock_mass, mock_manifest, mock_config, SUPPORTED_FEATURES)
    provider._baseurl = "http://localhost:32400"
    provider._plex_server = MagicMock()
    provider._plex_library = MagicMock()
    provider._myplex_account = MagicMock()

    return provider


class FakePlexTrack:
    """Minimal PlexTrack stub for testing."""

    def __init__(  # noqa: D107
        self,
        key: str = "/library/metadata/1",
        title: str = "Track 1",
        duration: int = 1000,
        has_media: bool = True,
        has_parts: bool = True,
        parent_index: int | None = 1,
        track_number: int | None = 1,
        container: str | None = "mp3",
        view_offset: int = 0,
        parent_key: str = "/library/metadata/100",
    ) -> None:
        self.key = key
        self.title = title
        self.duration = duration
        self.parentIndex = parent_index
        self.trackNumber = track_number
        self.parentKey = parent_key
        self.viewOffset = view_offset
        self.viewCount = 0
        self.summary = ""
        if has_media:
            media = MagicMock()
            media.container = container
            media_part = MagicMock()
            media_part.key = f"{key}/part"
            media_part.audioStreams.return_value = [MagicMock()]
            media.parts = [media_part] if has_parts else []
            self.media = [media]
        else:
            self.media = []

    def getWebURL(self, baseurl: str) -> str:  # noqa: N802, D102
        return f"{baseurl}/web/index.html#!/server/item/{self.key}"

    def firstAttr(self, *attrs: str) -> str | None:  # noqa: N802, D102
        return None

    def updateTimeline(self, offset_ms: int, *, state: str, duration: int | None = None) -> None:  # noqa: N802, D102
        pass


class FakePlexAlbum:
    """Minimal PlexAlbum stub for testing."""

    def __init__(  # noqa: D107
        self,
        tracks: list[Any],
        title: str = "Test Album",
        key: str = "/library/metadata/100",
        view_count: int = 0,
        last_viewed_at: datetime | None = None,
        album_duration: int = 0,
    ) -> None:
        self._tracks = tracks
        self.title = title
        self.key = key
        self.summary = ""
        self.year = None
        self.studio = None
        self.parentTitle = None
        self.grandparentTitle = None
        self.viewCount = view_count
        self.lastViewedAt = last_viewed_at
        self.duration = album_duration

    def tracks(self) -> list[Any]:  # noqa: D102
        return self._tracks

    def getWebURL(self, baseurl: str) -> str:  # noqa: N802, D102
        return f"{baseurl}/web/index.html#!/server/item/{self.key}"

    def firstAttr(self, *attrs: str) -> str | None:  # noqa: N802, D102
        return None

    def markPlayed(self) -> None:  # noqa: D102, N802
        pass

    def markUnplayed(self) -> None:  # noqa: D102, N802
        pass

    def reload(self) -> None:  # noqa: D102
        pass


@pytest.fixture
def audiobook_provider() -> Any:
    """Provide an audiobook-configured PlexProvider."""
    return _make_provider(LIBRARY_TYPE_AUDIOBOOKS)


@pytest.fixture
def podcast_provider() -> Any:
    """Provide a podcast-configured PlexProvider."""
    return _make_provider(LIBRARY_TYPE_PODCASTS)


@pytest.fixture
def music_provider() -> Any:
    """Provide a music-configured PlexProvider."""
    return _make_provider(LIBRARY_TYPE_MUSIC)


def _make_tracks(*specs: dict[str, Any]) -> list[FakePlexTrack]:
    """Build a list of FakePlexTrack from dict specs."""
    return [FakePlexTrack(**s) for s in specs]


def _make_album(tracks: list[Any], **kwargs: Any) -> FakePlexAlbum:
    """Build a FakePlexAlbum with the given tracks."""
    return FakePlexAlbum(tracks, **kwargs)


def _make_stream_track(container: str = "flac", has_audio_streams: bool = True) -> MagicMock:
    """Build a Plex track mock with one playable media part."""
    plex_track = MagicMock()
    plex_track.key = "/library/metadata/1"
    plex_track.duration = 123000

    audio_stream = MagicMock()
    audio_stream.samplingRate = 44100
    audio_stream.bitDepth = 16

    media_part = MagicMock()
    media_part.key = "/library/parts/1/file.flac"
    media_part.audioStreams.return_value = [audio_stream] if has_audio_streams else []

    media = MagicMock()
    media.container = container
    media.audioChannels = 2
    media.parts = [media_part]

    plex_track.media = [media]
    return plex_track


# ---------------------------------------------------------------------------
# Audiobook chapters
# ---------------------------------------------------------------------------


class TestBuildAudiobookChapters:
    """Tests for _build_audiobook_chapters numbering behavior."""

    @pytest.mark.asyncio
    async def test_chapters_numbered_sequentially(self, audiobook_provider: Any) -> None:
        """Chapters should have sequential positions starting from 1."""
        tracks = _make_tracks(
            {"key": "/1", "title": "Intro", "duration": 3000},
            {"key": "/2", "title": "Chapter 1", "duration": 5000},
            {"key": "/3", "title": "Outro", "duration": 2000},
        )
        chapters = await audiobook_provider._build_audiobook_chapters(_make_album(tracks))

        assert len(chapters) == 3
        assert chapters[0].position == 1
        assert chapters[1].position == 2
        assert chapters[2].position == 3
        assert chapters[0].name == "Intro"
        assert chapters[1].name == "Chapter 1"
        assert chapters[2].name == "Outro"

    @pytest.mark.asyncio
    async def test_chapters_skip_tracks_without_media(self, audiobook_provider: Any) -> None:
        """Tracks without media should be skipped without creating gaps in numbering."""
        tracks = _make_tracks(
            {"key": "/1", "title": "Valid Track 1", "duration": 3000},
            {"key": "/2", "title": "No Media", "duration": 5000, "has_media": False},
            {"key": "/3", "title": "Valid Track 2", "duration": 2000},
        )
        chapters = await audiobook_provider._build_audiobook_chapters(_make_album(tracks))

        assert len(chapters) == 2
        assert chapters[0].position == 1
        assert chapters[1].position == 2
        assert chapters[0].name == "Valid Track 1"
        assert chapters[1].name == "Valid Track 2"

    @pytest.mark.asyncio
    async def test_chapters_skip_tracks_without_parts(self, audiobook_provider: Any) -> None:
        """Tracks with media but no parts should be skipped without gaps."""
        tracks = _make_tracks(
            {"key": "/1", "title": "Valid", "duration": 3000},
            {"key": "/2", "title": "No Parts", "duration": 5000, "has_parts": False},
            {"key": "/3", "title": "Another Valid", "duration": 2000},
        )
        chapters = await audiobook_provider._build_audiobook_chapters(_make_album(tracks))

        assert len(chapters) == 2
        assert chapters[0].position == 1
        assert chapters[1].position == 2

    @pytest.mark.asyncio
    async def test_chapters_cumulative_times(self, audiobook_provider: Any) -> None:
        """Chapter start/end times should be cumulative across valid tracks."""
        tracks = _make_tracks(
            {"key": "/1", "title": "First", "duration": 1000},
            {"key": "/2", "title": "Second", "duration": 2000},
            {"key": "/3", "title": "Third", "duration": 3000},
        )
        chapters = await audiobook_provider._build_audiobook_chapters(_make_album(tracks))

        assert chapters[0].start == 0.0
        assert chapters[0].end == 1.0
        assert chapters[1].start == 1.0
        assert chapters[1].end == 3.0
        assert chapters[2].start == 3.0
        assert chapters[2].end == 6.0


class TestBuildPodcastEpisodes:
    """Tests for _build_podcast_episodes numbering behavior."""

    @pytest.mark.asyncio
    async def test_episodes_numbered_sequentially(self, podcast_provider: Any) -> None:
        """Episodes should have sequential positions starting from 1."""
        tracks = _make_tracks(
            {"key": "/1", "title": "Intro"},
            {"key": "/2", "title": "Main Episode"},
            {"key": "/3", "title": "Outro"},
        )
        episodes = await podcast_provider._build_podcast_episodes(
            _make_album(tracks, title="Test Podcast")
        )

        assert len(episodes) == 3
        assert episodes[0].position == 1
        assert episodes[1].position == 2
        assert episodes[2].position == 3
        assert episodes[0].name == "Intro"
        assert episodes[1].name == "Main Episode"
        assert episodes[2].name == "Outro"

    @pytest.mark.asyncio
    async def test_episodes_skip_tracks_without_media(self, podcast_provider: Any) -> None:
        """Tracks without media should be skipped without creating gaps."""
        tracks = _make_tracks(
            {"key": "/1", "title": "Valid Ep 1"},
            {"key": "/2", "title": "No Media", "has_media": False},
            {"key": "/3", "title": "Valid Ep 2"},
        )
        episodes = await podcast_provider._build_podcast_episodes(
            _make_album(tracks, title="Test Podcast")
        )

        assert len(episodes) == 2
        assert episodes[0].position == 1
        assert episodes[1].position == 2
        assert episodes[0].name == "Valid Ep 1"
        assert episodes[1].name == "Valid Ep 2"

    @pytest.mark.asyncio
    async def test_episode_default_name(self, podcast_provider: Any) -> None:
        """Tracks without titles should use default episode name with correct number."""
        tracks = _make_tracks(
            {"key": "/1", "title": ""},
            {"key": "/2", "title": "Has Title"},
            {"key": "/3", "title": ""},
        )
        episodes = await podcast_provider._build_podcast_episodes(
            _make_album(tracks, title="Test Podcast")
        )

        assert episodes[0].name == "Episode 1"
        assert episodes[1].name == "Has Title"
        assert episodes[2].name == "Episode 3"

    @pytest.mark.asyncio
    async def test_episode_podcast_reference(self, podcast_provider: Any) -> None:
        """Each episode should reference the parent podcast correctly."""
        tracks = _make_tracks({"key": "/1", "title": "Ep 1"})
        episodes = await podcast_provider._build_podcast_episodes(
            _make_album(tracks, title="My Podcast", key="/library/metadata/100")
        )

        assert len(episodes) == 1
        assert episodes[0].podcast.name == "My Podcast"
        assert episodes[0].podcast.item_id == "podcast:/library/metadata/100"


class TestStreamDetailsGuards:
    """Tests for library type guards in stream detail methods."""

    @pytest.mark.asyncio
    async def test_music_stream_original_uses_direct_download(self, music_provider: Any) -> None:
        """Original quality should preserve the direct Plex media part URL."""
        plex_track = _make_stream_track()
        music_provider._get_data = AsyncMock(return_value=plex_track)
        music_provider._plex_server.url.return_value = "http://plex.local/file.flac?download=1"

        result = await music_provider.get_stream_details("/library/metadata/1", MediaType.TRACK)

        assert result.stream_type == StreamType.HTTP
        assert result.path == "http://plex.local/file.flac?download=1"
        assert result.audio_format.content_type == ContentType.FLAC
        assert result.audio_format.channels == 2
        assert result.audio_format.sample_rate == 44100
        assert result.audio_format.bit_depth == 16
        plex_track.getStreamURL.assert_not_called()

    @pytest.mark.asyncio
    async def test_music_stream_without_media_parts_raises_not_found(
        self, music_provider: Any
    ) -> None:
        """Tracks without playable media parts should raise MediaNotFoundError."""
        plex_track = _make_stream_track()
        plex_track.media[0].parts = []
        music_provider._get_data = AsyncMock(return_value=plex_track)

        with pytest.raises(MediaNotFoundError, match="has no playable media parts"):
            await music_provider.get_stream_details("/library/metadata/1", MediaType.TRACK)

    @pytest.mark.asyncio
    async def test_music_stream_transcoded_uses_plex_hls(self) -> None:
        """Configured bitrate should request Plex HLS transcoding for music tracks."""
        provider = _make_provider(stream_quality=STREAM_QUALITY_128)
        plex_track = _make_stream_track()
        provider._get_data = AsyncMock(return_value=plex_track)
        provider._plex_server.url.side_effect = lambda path, _include_token: (
            f"http://plex.local{path}&X-Plex-Token=token"
        )

        result = await provider.get_stream_details("/library/metadata/1", MediaType.TRACK)
        parsed = urlparse(result.path)
        query = parse_qs(parsed.query)

        assert result.stream_type == StreamType.HLS
        assert parsed.path == "/music/:/transcode/universal/start.m3u8"
        assert query["path"] == ["/library/metadata/1"]
        assert query["protocol"] == ["hls"]
        assert query["minAudioBitrate"] == ["128"]
        assert query["maxAudioBitrate"] == ["128"]
        assert query["musicBitrate"] == ["128"]
        assert query["directStreamAudio"] == ["0"]
        assert query["X-Plex-Platform"] == ["Chrome"]
        profile_extra = query["X-Plex-Client-Profile-Extra"][0]
        assert "add-transcode-target(type=musicProfile" in profile_extra
        assert "audioCodec=opus" in profile_extra
        assert "type=upperBound&name=audio.bitrate&value=128" in profile_extra
        assert "type=lowerBound&name=audio.bitrate&value=128" in profile_extra
        assert result.audio_format.content_type == ContentType.OPUS
        assert result.audio_format.channels == 2
        assert result.audio_format.bit_rate == 128

    @pytest.mark.asyncio
    async def test_music_stream_transcode_without_audio_streams_uses_download(self) -> None:
        """Unanalyzed Plex tracks should keep the direct download fallback."""
        provider = _make_provider(stream_quality=STREAM_QUALITY_128)
        plex_track = _make_stream_track(has_audio_streams=False)
        provider._get_data = AsyncMock(return_value=plex_track)
        provider._plex_server.url.return_value = "http://plex.local/file.flac?download=1"

        result = await provider.get_stream_details("/library/metadata/1", MediaType.TRACK)

        assert result.stream_type == StreamType.HTTP
        assert result.path == "http://plex.local/file.flac?download=1"
        assert result.audio_format.content_type == ContentType.FLAC
        assert result.audio_format.bit_rate is None

    @pytest.mark.asyncio
    async def test_music_stream_transcode_without_size_uses_download(self) -> None:
        """Tracks without an analyzed size should keep the direct download fallback."""
        provider = _make_provider(stream_quality=STREAM_QUALITY_128)
        plex_track = _make_stream_track()
        plex_track.media[0].parts[0].size = None
        provider._get_data = AsyncMock(return_value=plex_track)
        provider._plex_server.url.return_value = "http://plex.local/file.flac?download=1"

        result = await provider.get_stream_details("/library/metadata/1", MediaType.TRACK)

        assert result.stream_type == StreamType.HTTP
        assert result.path == "http://plex.local/file.flac?download=1"
        assert result.audio_format.content_type == ContentType.FLAC
        assert result.audio_format.bit_rate is None
        provider._plex_server.url.assert_called_once_with(
            "/library/parts/1/file.flac?download=1", True
        )

    @pytest.mark.asyncio
    async def test_podcast_episode_stream_transcoded_uses_plex_hls(self) -> None:
        """Configured bitrate should request Plex HLS transcoding for podcast episodes."""
        provider = _make_provider(LIBRARY_TYPE_PODCASTS, stream_quality=STREAM_QUALITY_128)
        plex_track = _make_stream_track()
        provider._plex_library.fetchItem = MagicMock(return_value=plex_track)
        provider._run_async = AsyncMock(
            side_effect=lambda call, *args, **kwargs: call(*args, **kwargs)
        )
        provider._plex_server.url.side_effect = lambda path, _include_token: (
            f"http://plex.local{path}&X-Plex-Token=token"
        )

        result = await provider.get_stream_details(
            "podcast_episode:/library/metadata/1", MediaType.PODCAST_EPISODE
        )
        parsed = urlparse(result.path)
        query = parse_qs(parsed.query)

        assert result.stream_type == StreamType.HLS
        assert parsed.path == "/music/:/transcode/universal/start.m3u8"
        assert query["path"] == ["/library/metadata/1"]
        assert query["maxAudioBitrate"] == ["128"]
        assert result.audio_format.content_type == ContentType.OPUS
        assert result.audio_format.bit_rate == 128
        assert result.duration == 123

    @pytest.mark.asyncio
    async def test_podcast_episode_stream_transcode_without_audio_streams_uses_download(
        self,
    ) -> None:
        """Unanalyzed podcast episodes should keep the direct download fallback."""
        provider = _make_provider(LIBRARY_TYPE_PODCASTS, stream_quality=STREAM_QUALITY_128)
        plex_track = _make_stream_track(has_audio_streams=False)
        provider._plex_library.fetchItem = MagicMock(return_value=plex_track)
        provider._run_async = AsyncMock(
            side_effect=lambda call, *args, **kwargs: call(*args, **kwargs)
        )
        provider._plex_server.url.return_value = "http://plex.local/file.flac?download=1"

        result = await provider.get_stream_details(
            "podcast_episode:/library/metadata/1", MediaType.PODCAST_EPISODE
        )

        assert result.stream_type == StreamType.HTTP
        assert result.path == "http://plex.local/file.flac?download=1"
        assert result.audio_format.content_type == ContentType.FLAC
        assert result.audio_format.bit_rate is None
        assert result.duration == 123

    @pytest.mark.asyncio
    async def test_podcast_episode_stream_transcode_without_size_uses_download(self) -> None:
        """Podcast episodes without an analyzed size should use the download fallback."""
        provider = _make_provider(LIBRARY_TYPE_PODCASTS, stream_quality=STREAM_QUALITY_128)
        plex_track = _make_stream_track()
        plex_track.media[0].parts[0].size = None
        provider._plex_library.fetchItem = MagicMock(return_value=plex_track)
        provider._run_async = AsyncMock(
            side_effect=lambda call, *args, **kwargs: call(*args, **kwargs)
        )
        provider._plex_server.url.return_value = "http://plex.local/file.flac?download=1"

        result = await provider.get_stream_details(
            "podcast_episode:/library/metadata/1", MediaType.PODCAST_EPISODE
        )

        assert result.stream_type == StreamType.HTTP
        assert result.path == "http://plex.local/file.flac?download=1"
        assert result.audio_format.content_type == ContentType.FLAC
        assert result.audio_format.bit_rate is None
        provider._plex_server.url.assert_called_once_with(
            "/library/parts/1/file.flac?download=1", True
        )

    @pytest.mark.asyncio
    async def test_single_part_audiobook_stream_transcoded_uses_plex_hls(self) -> None:
        """Configured bitrate should request Plex HLS transcoding for single-part audiobooks."""
        provider = _make_provider(LIBRARY_TYPE_AUDIOBOOKS, stream_quality=STREAM_QUALITY_128)
        album = _make_album(_make_tracks({"key": "/library/metadata/1", "duration": 123000}))
        provider._plex_library.fetchItem = MagicMock(return_value=album)
        provider._run_async = AsyncMock(
            side_effect=lambda call, *args, **kwargs: call(*args, **kwargs)
        )
        provider._plex_server.url.side_effect = lambda path, _include_token: (
            f"http://plex.local{path}&X-Plex-Token=token"
        )

        result = await provider.get_stream_details(
            "audiobook:/library/metadata/100", MediaType.AUDIOBOOK
        )
        parsed = urlparse(result.path)
        query = parse_qs(parsed.query)

        assert result.stream_type == StreamType.HLS
        assert parsed.path == "/music/:/transcode/universal/start.m3u8"
        assert query["path"] == ["/library/metadata/1"]
        assert query["maxAudioBitrate"] == ["128"]
        assert result.audio_format.content_type == ContentType.OPUS
        assert result.audio_format.bit_rate == 128

    @pytest.mark.asyncio
    async def test_single_part_audiobook_without_audio_streams_uses_download(self) -> None:
        """Unanalyzed single-part audiobooks should keep the direct download fallback."""
        provider = _make_provider(LIBRARY_TYPE_AUDIOBOOKS, stream_quality=STREAM_QUALITY_128)
        tracks = _make_tracks({"key": "/library/metadata/1", "duration": 123000})
        tracks[0].media[0].parts[0].audioStreams.return_value = []
        album = _make_album(tracks)
        provider._plex_library.fetchItem = MagicMock(return_value=album)
        provider._run_async = AsyncMock(
            side_effect=lambda call, *args, **kwargs: call(*args, **kwargs)
        )
        provider._plex_server.url.return_value = "http://plex.local/file.mp3?download=1"

        result = await provider.get_stream_details(
            "audiobook:/library/metadata/100", MediaType.AUDIOBOOK
        )

        assert result.stream_type == StreamType.HTTP
        assert result.path == "http://plex.local/file.mp3?download=1"
        assert result.audio_format.content_type == ContentType.MP3
        assert result.audio_format.bit_rate is None
        assert result.duration == 123

    @pytest.mark.asyncio
    async def test_single_part_audiobook_without_size_uses_download(self) -> None:
        """Single-part audiobooks without an analyzed size should use the download fallback."""
        provider = _make_provider(LIBRARY_TYPE_AUDIOBOOKS, stream_quality=STREAM_QUALITY_128)
        tracks = _make_tracks({"key": "/library/metadata/1", "duration": 123000})
        tracks[0].media[0].parts[0].size = None
        album = _make_album(tracks)
        provider._plex_library.fetchItem = MagicMock(return_value=album)
        provider._run_async = AsyncMock(
            side_effect=lambda call, *args, **kwargs: call(*args, **kwargs)
        )
        provider._plex_server.url.return_value = "http://plex.local/file.mp3?download=1"

        result = await provider.get_stream_details(
            "audiobook:/library/metadata/100", MediaType.AUDIOBOOK
        )

        assert result.stream_type == StreamType.HTTP
        assert result.path == "http://plex.local/file.mp3?download=1"
        assert result.audio_format.content_type == ContentType.MP3
        assert result.audio_format.bit_rate is None
        assert result.duration == 123
        provider._plex_server.url.assert_called_once_with(
            "/library/metadata/1/part?download=1", True
        )

    @pytest.mark.asyncio
    async def test_music_stream_invalid_quality_falls_back_to_original(self) -> None:
        """Invalid stored quality values should not prevent playback."""
        provider = _make_provider(stream_quality="invalid")
        plex_track = _make_stream_track()
        provider._get_data = AsyncMock(return_value=plex_track)
        provider._plex_server.url.return_value = "http://plex.local/file.flac?download=1"

        result = await provider.get_stream_details("/library/metadata/1", MediaType.TRACK)

        assert result.stream_type == StreamType.HTTP
        assert result.path == "http://plex.local/file.flac?download=1"
        plex_track.getStreamURL.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_async_refreshes_myplex_token(self, music_provider: Any) -> None:
        """Plex API calls should refresh the authenticated account token."""
        music_provider.get_setup_value = MagicMock(return_value="auth_token")

        result = await music_provider._run_async(lambda: "ok")

        assert result == "ok"
        music_provider._myplex_account.ping.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_audiobook_stream_rejects_music_library(self, music_provider: Any) -> None:
        """_get_audiobook_stream_details should raise when library type is music."""
        with pytest.raises(MediaNotFoundError, match="not configured for audiobooks"):
            await music_provider._get_audiobook_stream_details("audiobook:/library/metadata/1")

    @pytest.mark.asyncio
    async def test_audiobook_stream_rejects_podcast_library(self, podcast_provider: Any) -> None:
        """_get_audiobook_stream_details should raise when library type is podcasts."""
        with pytest.raises(MediaNotFoundError, match="not configured for audiobooks"):
            await podcast_provider._get_audiobook_stream_details("audiobook:/library/metadata/1")

    @pytest.mark.asyncio
    async def test_podcast_stream_rejects_music_library(self, music_provider: Any) -> None:
        """_get_podcast_episode_stream_details should raise when library type is music."""
        with pytest.raises(MediaNotFoundError, match="not configured for podcasts"):
            await music_provider._get_podcast_episode_stream_details(
                "podcast_episode:/library/metadata/1"
            )

    @pytest.mark.asyncio
    async def test_podcast_stream_rejects_audiobook_library(self, audiobook_provider: Any) -> None:
        """_get_podcast_episode_stream_details should raise when library type is audiobooks."""
        with pytest.raises(MediaNotFoundError, match="not configured for podcasts"):
            await audiobook_provider._get_podcast_episode_stream_details(
                "podcast_episode:/library/metadata/1"
            )


class TestSupportedFeaturesProperty:
    """The supported_features property reflects the configured library type."""

    def test_music_returns_all_music_features(self) -> None:
        """A music library exposes the full music feature set."""
        result = _make_provider(LIBRARY_TYPE_MUSIC).supported_features

        assert ProviderFeature.LIBRARY_ARTISTS in result
        assert ProviderFeature.LIBRARY_ALBUMS in result
        assert ProviderFeature.LIBRARY_TRACKS in result
        assert ProviderFeature.LIBRARY_PLAYLISTS in result
        assert ProviderFeature.LIBRARY_AUDIOBOOKS not in result
        assert ProviderFeature.LIBRARY_PODCASTS not in result

    def test_audiobooks_returns_only_audiobook_features(self) -> None:
        """An audiobooks library narrows the feature set to audiobooks."""
        result = _make_provider(LIBRARY_TYPE_AUDIOBOOKS).supported_features

        assert ProviderFeature.LIBRARY_AUDIOBOOKS in result
        assert ProviderFeature.BROWSE in result
        assert ProviderFeature.SEARCH in result
        assert ProviderFeature.LIBRARY_ARTISTS not in result
        assert ProviderFeature.LIBRARY_ALBUMS not in result
        assert ProviderFeature.LIBRARY_TRACKS not in result
        assert ProviderFeature.LIBRARY_PLAYLISTS not in result
        assert ProviderFeature.LIBRARY_PODCASTS not in result

    def test_podcasts_returns_only_podcast_features(self) -> None:
        """A podcasts library narrows the feature set to podcasts."""
        result = _make_provider(LIBRARY_TYPE_PODCASTS).supported_features

        assert ProviderFeature.LIBRARY_PODCASTS in result
        assert ProviderFeature.BROWSE in result
        assert ProviderFeature.SEARCH in result
        assert ProviderFeature.LIBRARY_ARTISTS not in result
        assert ProviderFeature.LIBRARY_ALBUMS not in result
        assert ProviderFeature.LIBRARY_TRACKS not in result
        assert ProviderFeature.LIBRARY_PLAYLISTS not in result
        assert ProviderFeature.LIBRARY_AUDIOBOOKS not in result


# ---------------------------------------------------------------------------
# Resume / progress system tests
# ---------------------------------------------------------------------------


class TestCalcResumePosition:
    """Tests for _calc_resume_position_ms."""

    @pytest.mark.asyncio
    async def test_no_progress_returns_zero(self, audiobook_provider: Any) -> None:
        """When no track has a viewOffset, resume position is zero."""
        tracks = _make_tracks(
            {"duration": 10000, "view_offset": 0},
            {"duration": 20000, "view_offset": 0},
        )
        result = await audiobook_provider._calc_resume_position_ms(
            _make_album(tracks), fully_played=False
        )
        assert result == 0

    @pytest.mark.asyncio
    async def test_last_offset_determines_position(self, audiobook_provider: Any) -> None:
        """The last track with a non-zero viewOffset sets the resume point."""
        tracks = _make_tracks(
            {"duration": 10000, "view_offset": 5000},
            {"duration": 20000, "view_offset": 12000},
        )
        result = await audiobook_provider._calc_resume_position_ms(
            _make_album(tracks), fully_played=False
        )
        assert result == 10000 + 12000

    @pytest.mark.asyncio
    async def test_fully_played_with_no_offset_uses_album_duration(
        self, audiobook_provider: Any
    ) -> None:
        """When fully played and no offsets, return album duration."""
        tracks = _make_tracks(
            {"duration": 10000, "view_offset": 0},
            {"duration": 20000, "view_offset": 0},
        )
        result = await audiobook_provider._calc_resume_position_ms(
            _make_album(tracks, album_duration=30000), fully_played=True
        )
        assert result == 30000

    @pytest.mark.asyncio
    async def test_mixed_offsets_with_gap(self, audiobook_provider: Any) -> None:
        """Only the last offset matters; gaps are ignored."""
        tracks = _make_tracks(
            {"duration": 10000, "view_offset": 8000},
            {"duration": 20000, "view_offset": 0},
            {"duration": 15000, "view_offset": 5000},
        )
        result = await audiobook_provider._calc_resume_position_ms(
            _make_album(tracks), fully_played=False
        )
        assert result == 30000 + 5000


class TestFindTrackForPosition:
    """Tests for _find_track_for_position."""

    @pytest.mark.asyncio
    async def test_position_in_first_track(self, audiobook_provider: Any) -> None:
        """Position inside the first track returns it with correct offset."""
        tracks = _make_tracks(
            {"key": "/1", "duration": 10000},
            {"key": "/2", "duration": 20000},
        )
        track, offset = await audiobook_provider._find_track_for_position(
            _make_album(tracks), position=5
        )
        assert track is not None
        assert track.key == "/1"
        assert offset == 5000  # 5 seconds = 5000 ms

    @pytest.mark.asyncio
    async def test_position_in_second_track(self, audiobook_provider: Any) -> None:
        """Position inside second track returns it with offset relative to track start."""
        tracks = _make_tracks(
            {"key": "/1", "duration": 10000},
            {"key": "/2", "duration": 20000},
        )
        track, offset = await audiobook_provider._find_track_for_position(
            _make_album(tracks), position=12
        )
        assert track is not None
        assert track.key == "/2"
        assert offset == 2000

    @pytest.mark.asyncio
    async def test_position_past_end_clamps_to_last_track(self, audiobook_provider: Any) -> None:
        """Position beyond all tracks clamps to the end of the last track."""
        tracks = _make_tracks(
            {"key": "/1", "duration": 10000},
            {"key": "/2", "duration": 20000},
        )
        track, offset = await audiobook_provider._find_track_for_position(
            _make_album(tracks), position=40
        )
        assert track is not None
        assert track.key == "/2"
        assert offset == 20000  # full duration of last track

    @pytest.mark.asyncio
    async def test_empty_album_returns_none(self, audiobook_provider: Any) -> None:
        """An album with no tracks returns (None, 0)."""
        track, offset = await audiobook_provider._find_track_for_position(
            _make_album([]), position=5
        )
        assert track is None
        assert offset == 0


class TestOnPlayed:
    """Tests for on_played progress sync."""

    @staticmethod
    def _setup_call_log(provider: Any, album: Any) -> list[Any]:
        """Attach run_async logger and fetchItem mock to a provider."""
        provider._plex_library.fetchItem = MagicMock(return_value=album)
        call_log: list[Any] = []

        async def _run_async(call: Any, *args: Any, **kwargs: Any) -> Any:
            call_log.append((call, args, kwargs))
            return call(*args, **kwargs)

        provider._run_async = _run_async
        return call_log

    @pytest.mark.asyncio
    async def test_fully_played_calls_mark_played(self, audiobook_provider: Any) -> None:
        """When fully_played=True, markPlayed is called on the album."""
        album = _make_album(
            _make_tracks({"key": "/1", "duration": 10000}),
            key="/library/metadata/100",
        )
        call_log = self._setup_call_log(audiobook_provider, album)

        await audiobook_provider.on_played(
            MediaType.AUDIOBOOK,
            "audiobook:/library/metadata/100",
            fully_played=True,
            position=0,
            media_item=MagicMock(),
        )

        assert any(call_info[0] == album.markPlayed for call_info in call_log)

    @pytest.mark.asyncio
    async def test_zero_position_calls_mark_unplayed(self, audiobook_provider: Any) -> None:
        """When position is 0, markUnplayed is called on the album."""
        album = _make_album(
            _make_tracks({"key": "/1", "duration": 10000}),
            key="/library/metadata/100",
        )
        call_log = self._setup_call_log(audiobook_provider, album)

        await audiobook_provider.on_played(
            MediaType.AUDIOBOOK,
            "audiobook:/library/metadata/100",
            fully_played=False,
            position=0,
            media_item=MagicMock(),
        )

        assert any(call_info[0] == album.markUnplayed for call_info in call_log)

    @pytest.mark.asyncio
    async def test_mid_position_updates_timeline(self, audiobook_provider: Any) -> None:
        """When position is in the middle, updateTimeline is called on the correct track."""
        track = FakePlexTrack(key="/1", duration=30000)
        album = _make_album([track], key="/library/metadata/100")
        call_log = self._setup_call_log(audiobook_provider, album)

        await audiobook_provider.on_played(
            MediaType.AUDIOBOOK,
            "audiobook:/library/metadata/100",
            fully_played=False,
            position=10,
            media_item=MagicMock(),
            is_playing=True,
        )

        assert any(
            call_info[0] == track.updateTimeline and call_info[2].get("state") == "playing"
            for call_info in call_log
        )


class TestGetResumePosition:
    """Tests for get_resume_position."""

    @pytest.mark.asyncio
    async def test_audiobook_with_progress(self, audiobook_provider: Any) -> None:
        """Returns correct resume position for an audiobook with track offsets."""
        tracks = _make_tracks(
            {"duration": 10000, "view_offset": 0},
            {"duration": 20000, "view_offset": 15000},
        )
        viewed_at = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        album = _make_album(tracks, key="/library/metadata/100", last_viewed_at=viewed_at)

        audiobook_provider._plex_library.fetchItem = MagicMock(return_value=album)

        fully_played, position_ms, timestamp = await audiobook_provider.get_resume_position(
            "audiobook:/library/metadata/100", MediaType.AUDIOBOOK
        )

        assert fully_played is False
        assert position_ms == 10000 + 15000
        assert timestamp == viewed_at

    @pytest.mark.asyncio
    async def test_fully_played_audiobook(self, audiobook_provider: Any) -> None:
        """When fully played, returns album duration as resume position."""
        tracks = _make_tracks({"duration": 30000, "view_offset": 0})
        album = _make_album(tracks, key="/library/metadata/100", view_count=1, album_duration=30000)

        audiobook_provider._plex_library.fetchItem = MagicMock(return_value=album)

        fully_played, position_ms, _ = await audiobook_provider.get_resume_position(
            "audiobook:/library/metadata/100", MediaType.AUDIOBOOK
        )

        assert fully_played is True
        assert position_ms == 30000

    @pytest.mark.asyncio
    async def test_podcast_episode_reads_own_view_offset(self, podcast_provider: Any) -> None:
        """Podcast episode reads its own track-level viewOffset for resume position."""
        episode_track = FakePlexTrack(
            key="/library/metadata/200",
            duration=15000,
            view_offset=12000,
            parent_key="/library/metadata/100",
        )

        podcast_provider._plex_library.fetchItem = MagicMock(return_value=episode_track)

        fully_played, position_ms, _ = await podcast_provider.get_resume_position(
            "podcast_episode:/library/metadata/200", MediaType.PODCAST_EPISODE
        )

        assert fully_played is False
        assert position_ms == 12000
        # Verify we did NOT fetch the parent album
        assert podcast_provider._plex_library.fetchItem.call_count == 1

    @pytest.mark.asyncio
    async def test_podcast_episode_reads_own_fully_played(self, podcast_provider: Any) -> None:
        """Podcast episode reads its own viewCount to determine fully_played."""
        episode_track = FakePlexTrack(
            key="/library/metadata/200",
            duration=15000,
            view_offset=0,
            parent_key="/library/metadata/100",
        )

        podcast_provider._plex_library.fetchItem = MagicMock(return_value=episode_track)

        # Simulate viewCount by overriding the viewCount attribute
        episode_track.viewCount = 1

        fully_played, position_ms, _ = await podcast_provider.get_resume_position(
            "podcast_episode:/library/metadata/200", MediaType.PODCAST_EPISODE
        )

        assert fully_played is True
        assert position_ms == 0

    @pytest.mark.asyncio
    async def test_not_found_raises_media_not_found(self, audiobook_provider: Any) -> None:
        """If the Plex item is missing, MediaNotFoundError is raised."""
        audiobook_provider._run_async = AsyncMock(
            side_effect=plexapi.exceptions.NotFound("Item not found")
        )

        with pytest.raises(MediaNotFoundError):
            await audiobook_provider.get_resume_position(
                "audiobook:/library/metadata/999", MediaType.AUDIOBOOK
            )


# ---------------------------------------------------------------------------
# stale library-mapping cleanup (idempotent, runs on load)
# ---------------------------------------------------------------------------


class TestCleanupStaleLibraryMappings:
    """Tests for _cleanup_stale_library_mappings removing entries not matching the type."""

    @staticmethod
    def _setup_mocks(provider: Any, query_result: list[dict[str, Any]] | None = None) -> Any:
        """Wire standard mocks onto provider.mass for the cleanup tests."""
        provider.mass.music.database = MagicMock()
        provider.mass.music.database.get_rows_from_query = AsyncMock(
            return_value=query_result or []
        )
        provider.mass.music.get_controller = MagicMock()
        mock_ctrl = MagicMock()
        mock_ctrl.remove_provider_mappings = AsyncMock()
        provider.mass.music.get_controller.return_value = mock_ctrl
        return mock_ctrl

    @pytest.mark.asyncio
    async def test_music_library_cleans_non_music_types(self) -> None:
        """A music library removes mappings for the 3 non-music media types."""
        provider = _make_provider(LIBRARY_TYPE_MUSIC)
        mock_ctrl = self._setup_mocks(provider, query_result=[{"item_id": 10}])

        await provider._cleanup_stale_library_mappings()

        # stale for music = audiobook, podcast, podcast_episode
        assert provider.mass.music.get_controller.call_count == 3
        mock_ctrl.remove_provider_mappings.assert_has_awaits(
            [mock.call(10, "plex_instance_1")] * 3, any_order=True
        )

    @pytest.mark.asyncio
    async def test_audiobooks_library_cleans_all_other_types(self) -> None:
        """An audiobooks library removes mappings for the 6 non-audiobook media types."""
        provider = _make_provider(LIBRARY_TYPE_AUDIOBOOKS)
        mock_ctrl = self._setup_mocks(provider, query_result=[{"item_id": 1}])

        await provider._cleanup_stale_library_mappings()

        # stale for audiobooks = artist, album, track, playlist, podcast, podcast_episode
        assert provider.mass.music.get_controller.call_count == 6
        assert mock_ctrl.remove_provider_mappings.await_count == 6

    @pytest.mark.asyncio
    async def test_no_stale_rows_removes_nothing(self) -> None:
        """With no stale mappings present, no removals are performed."""
        provider = _make_provider(LIBRARY_TYPE_MUSIC)
        mock_ctrl = self._setup_mocks(provider, query_result=[])

        await provider._cleanup_stale_library_mappings()

        mock_ctrl.remove_provider_mappings.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_database_returns_early(self) -> None:
        """Cleanup is a no-op when the music database is not available."""
        provider = _make_provider(LIBRARY_TYPE_MUSIC)
        provider.mass.music.database = None
        provider.mass.music.get_controller = MagicMock()

        await provider._cleanup_stale_library_mappings()

        provider.mass.music.get_controller.assert_not_called()
