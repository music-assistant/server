"""Unit tests for Plex provider audiobook and podcast methods."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.plex import PlexProvider
from music_assistant.providers.plex.helpers import get_supported_features

LIBRARY_TYPE_AUDIOBOOKS = "audiobooks"
LIBRARY_TYPE_MUSIC = "music"
LIBRARY_TYPE_PODCASTS = "podcasts"


def _make_provider(library_type: str = LIBRARY_TYPE_MUSIC) -> Any:
    """Create a minimal PlexProvider instance for testing."""
    mock_mass = MagicMock()
    mock_mass.cache = MagicMock()

    mock_config = MagicMock()
    mock_config.instance_id = "plex_instance_1"

    # Set up a proper dict-like values container so _get_library_type works
    mock_config_values: dict[str, Any] = {
        "library_type": library_type,
        "log_level": "INFO",
        "token": "local_auth",
    }

    class MockValue:
        """Simple wrapper for mock config values."""

        def __init__(self, val: Any) -> None:
            self.value = val

    mock_config.values = {k: MockValue(v) for k, v in mock_config_values.items()}
    mock_config.get_value = lambda key: mock_config_values.get(key)

    mock_manifest = MagicMock()
    mock_manifest.type = "music"
    mock_manifest.domain = "plex"

    provider = PlexProvider(mock_mass, mock_manifest, mock_config)
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
        if has_media:
            media = MagicMock()
            media.container = container
            media.parts = [MagicMock()] if has_parts else []
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

    def markPlayed(self) -> None:  # noqa: D102
        pass

    def markUnplayed(self) -> None:  # noqa: D102
        pass

    def reload(self) -> None:  # noqa: D102
        pass


class TestBuildAudiobookChapters:
    """Tests for _build_audiobook_chapters numbering behavior."""

    @pytest.mark.asyncio
    async def test_chapters_numbered_sequentially(self) -> None:
        """Chapters should have sequential positions starting from 1."""
        provider = _make_provider(LIBRARY_TYPE_AUDIOBOOKS)
        tracks = [
            FakePlexTrack(key="/1", title="Intro", duration=3000),
            FakePlexTrack(key="/2", title="Chapter 1", duration=5000),
            FakePlexTrack(key="/3", title="Outro", duration=2000),
        ]
        album = FakePlexAlbum(tracks)

        chapters = await provider._build_audiobook_chapters(album)

        assert len(chapters) == 3
        assert chapters[0].position == 1
        assert chapters[1].position == 2
        assert chapters[2].position == 3
        assert chapters[0].name == "Intro"
        assert chapters[1].name == "Chapter 1"
        assert chapters[2].name == "Outro"

    @pytest.mark.asyncio
    async def test_chapters_skip_tracks_without_media(self) -> None:
        """Tracks without media should be skipped without creating gaps in numbering."""
        provider = _make_provider(LIBRARY_TYPE_AUDIOBOOKS)
        tracks = [
            FakePlexTrack(key="/1", title="Valid Track 1", duration=3000),
            FakePlexTrack(key="/2", title="No Media", duration=5000, has_media=False),
            FakePlexTrack(key="/3", title="Valid Track 2", duration=2000),
        ]
        album = FakePlexAlbum(tracks)

        chapters = await provider._build_audiobook_chapters(album)

        assert len(chapters) == 2
        assert chapters[0].position == 1
        assert chapters[1].position == 2
        assert chapters[0].name == "Valid Track 1"
        assert chapters[1].name == "Valid Track 2"

    @pytest.mark.asyncio
    async def test_chapters_skip_tracks_without_parts(self) -> None:
        """Tracks with media but no parts should be skipped without gaps."""
        provider = _make_provider(LIBRARY_TYPE_AUDIOBOOKS)
        tracks = [
            FakePlexTrack(key="/1", title="Valid", duration=3000),
            FakePlexTrack(key="/2", title="No Parts", duration=5000, has_parts=False),
            FakePlexTrack(key="/3", title="Another Valid", duration=2000),
        ]
        album = FakePlexAlbum(tracks)

        chapters = await provider._build_audiobook_chapters(album)

        assert len(chapters) == 2
        assert chapters[0].position == 1
        assert chapters[1].position == 2

    @pytest.mark.asyncio
    async def test_chapters_cumulative_times(self) -> None:
        """Chapter start/end times should be cumulative across valid tracks."""
        provider = _make_provider(LIBRARY_TYPE_AUDIOBOOKS)
        tracks = [
            FakePlexTrack(key="/1", title="First", duration=1000),
            FakePlexTrack(key="/2", title="Second", duration=2000),
            FakePlexTrack(key="/3", title="Third", duration=3000),
        ]
        album = FakePlexAlbum(tracks)

        chapters = await provider._build_audiobook_chapters(album)

        assert chapters[0].start == 0.0
        assert chapters[0].end == 1.0
        assert chapters[1].start == 1.0
        assert chapters[1].end == 3.0
        assert chapters[2].start == 3.0
        assert chapters[2].end == 6.0


class TestBuildPodcastEpisodes:
    """Tests for _build_podcast_episodes numbering behavior."""

    @pytest.mark.asyncio
    async def test_episodes_numbered_sequentially(self) -> None:
        """Episodes should have sequential positions starting from 1."""
        provider = _make_provider(LIBRARY_TYPE_PODCASTS)
        tracks = [
            FakePlexTrack(key="/1", title="Intro"),
            FakePlexTrack(key="/2", title="Main Episode"),
            FakePlexTrack(key="/3", title="Outro"),
        ]
        album = FakePlexAlbum(tracks, title="Test Podcast")

        episodes = await provider._build_podcast_episodes(album)

        assert len(episodes) == 3
        assert episodes[0].position == 1
        assert episodes[1].position == 2
        assert episodes[2].position == 3
        assert episodes[0].name == "Intro"
        assert episodes[1].name == "Main Episode"
        assert episodes[2].name == "Outro"

    @pytest.mark.asyncio
    async def test_episodes_skip_tracks_without_media(self) -> None:
        """Tracks without media should be skipped without creating gaps."""
        provider = _make_provider(LIBRARY_TYPE_PODCASTS)
        tracks = [
            FakePlexTrack(key="/1", title="Valid Ep 1"),
            FakePlexTrack(key="/2", title="No Media", has_media=False),
            FakePlexTrack(key="/3", title="Valid Ep 2"),
        ]
        album = FakePlexAlbum(tracks, title="Test Podcast")

        episodes = await provider._build_podcast_episodes(album)

        assert len(episodes) == 2
        assert episodes[0].position == 1
        assert episodes[1].position == 2
        assert episodes[0].name == "Valid Ep 1"
        assert episodes[1].name == "Valid Ep 2"

    @pytest.mark.asyncio
    async def test_episode_default_name(self) -> None:
        """Tracks without titles should use default episode name with correct number."""
        provider = _make_provider(LIBRARY_TYPE_PODCASTS)
        tracks = [
            FakePlexTrack(key="/1", title=""),
            FakePlexTrack(key="/2", title="Has Title"),
            FakePlexTrack(key="/3", title=""),
        ]
        album = FakePlexAlbum(tracks, title="Test Podcast")

        episodes = await provider._build_podcast_episodes(album)

        assert episodes[0].name == "Episode 1"
        assert episodes[1].name == "Has Title"
        assert episodes[2].name == "Episode 3"

    @pytest.mark.asyncio
    async def test_episode_podcast_reference(self) -> None:
        """Each episode should reference the parent podcast correctly."""
        provider = _make_provider(LIBRARY_TYPE_PODCASTS)
        tracks = [
            FakePlexTrack(key="/1", title="Ep 1"),
        ]
        album = FakePlexAlbum(tracks, title="My Podcast", key="/library/metadata/100")

        episodes = await provider._build_podcast_episodes(album)

        assert len(episodes) == 1
        assert episodes[0].podcast.name == "My Podcast"
        assert episodes[0].podcast.item_id == "podcast:/library/metadata/100"


class TestStreamDetailsGuards:
    """Tests for library type guards in stream detail methods."""

    @pytest.mark.asyncio
    async def test_audiobook_stream_rejects_music_library(self) -> None:
        """_get_audiobook_stream_details should raise when library type is music."""
        provider = _make_provider(LIBRARY_TYPE_MUSIC)

        with pytest.raises(MediaNotFoundError, match="not configured for audiobooks"):
            await provider._get_audiobook_stream_details("audiobook:/library/metadata/1")

    @pytest.mark.asyncio
    async def test_audiobook_stream_rejects_podcast_library(self) -> None:
        """_get_audiobook_stream_details should raise when library type is podcasts."""
        provider = _make_provider(LIBRARY_TYPE_PODCASTS)

        with pytest.raises(MediaNotFoundError, match="not configured for audiobooks"):
            await provider._get_audiobook_stream_details("audiobook:/library/metadata/1")

    @pytest.mark.asyncio
    async def test_podcast_stream_rejects_music_library(self) -> None:
        """_get_podcast_episode_stream_details should raise when library type is music."""
        provider = _make_provider(LIBRARY_TYPE_MUSIC)

        with pytest.raises(MediaNotFoundError, match="not configured for podcasts"):
            await provider._get_podcast_episode_stream_details(
                "podcast_episode:/library/metadata/1"
            )

    @pytest.mark.asyncio
    async def test_podcast_stream_rejects_audiobook_library(self) -> None:
        """_get_podcast_episode_stream_details should raise when library type is audiobooks."""
        provider = _make_provider(LIBRARY_TYPE_AUDIOBOOKS)

        with pytest.raises(MediaNotFoundError, match="not configured for podcasts"):
            await provider._get_podcast_episode_stream_details(
                "podcast_episode:/library/metadata/1"
            )


class TestGetSupportedFeatures:
    """Tests for the dynamic get_supported_features function."""

    def test_music_returns_all_music_features(self) -> None:
        """When library_type is music, all music features are returned."""
        result = get_supported_features({"library_type": "music"})

        assert ProviderFeature.LIBRARY_ARTISTS in result
        assert ProviderFeature.LIBRARY_ALBUMS in result
        assert ProviderFeature.LIBRARY_TRACKS in result
        assert ProviderFeature.LIBRARY_PLAYLISTS in result
        assert ProviderFeature.LIBRARY_AUDIOBOOKS not in result
        assert ProviderFeature.LIBRARY_PODCASTS not in result

    def test_audiobooks_returns_only_audiobook_features(self) -> None:
        """When library_type is audiobooks, only audiobook features are returned."""
        result = get_supported_features({"library_type": "audiobooks"})

        assert ProviderFeature.LIBRARY_AUDIOBOOKS in result
        assert ProviderFeature.BROWSE in result
        assert ProviderFeature.SEARCH in result
        assert ProviderFeature.LIBRARY_ARTISTS not in result
        assert ProviderFeature.LIBRARY_ALBUMS not in result
        assert ProviderFeature.LIBRARY_TRACKS not in result
        assert ProviderFeature.LIBRARY_PLAYLISTS not in result
        assert ProviderFeature.LIBRARY_PODCASTS not in result

    def test_podcasts_returns_only_podcast_features(self) -> None:
        """When library_type is podcasts, only podcast features are returned."""
        result = get_supported_features({"library_type": "podcasts"})

        assert ProviderFeature.LIBRARY_PODCASTS in result
        assert ProviderFeature.BROWSE in result
        assert ProviderFeature.SEARCH in result
        assert ProviderFeature.LIBRARY_ARTISTS not in result
        assert ProviderFeature.LIBRARY_ALBUMS not in result
        assert ProviderFeature.LIBRARY_TRACKS not in result
        assert ProviderFeature.LIBRARY_PLAYLISTS not in result
        assert ProviderFeature.LIBRARY_AUDIOBOOKS not in result

    def test_default_none_returns_music_features(self) -> None:
        """When values is None, music features are returned as default."""
        result = get_supported_features(None)

        assert ProviderFeature.LIBRARY_ARTISTS in result
        assert ProviderFeature.LIBRARY_ALBUMS in result
        assert ProviderFeature.LIBRARY_TRACKS in result

    def test_default_empty_dict_returns_music_features(self) -> None:
        """When values is an empty dict, music features are returned as default."""
        result = get_supported_features({})

        assert ProviderFeature.LIBRARY_ARTISTS in result
        assert ProviderFeature.LIBRARY_ALBUMS in result
        assert ProviderFeature.LIBRARY_TRACKS in result


# ---------------------------------------------------------------------------
# Resume / progress system tests
# ---------------------------------------------------------------------------


class TestCalcResumePosition:
    """Tests for _calc_resume_position_ms."""

    @pytest.mark.asyncio
    async def test_no_progress_returns_zero(self) -> None:
        """When no track has a viewOffset, resume position is zero."""
        provider = _make_provider(LIBRARY_TYPE_AUDIOBOOKS)
        tracks = [
            FakePlexTrack(duration=10000, view_offset=0),
            FakePlexTrack(duration=20000, view_offset=0),
        ]
        album = FakePlexAlbum(tracks)
        result = await provider._calc_resume_position_ms(album, fully_played=False)
        assert result == 0

    @pytest.mark.asyncio
    async def test_last_offset_determines_position(self) -> None:
        """The last track with a non-zero viewOffset sets the resume point."""
        provider = _make_provider(LIBRARY_TYPE_AUDIOBOOKS)
        tracks = [
            FakePlexTrack(duration=10000, view_offset=5000),
            FakePlexTrack(duration=20000, view_offset=12000),
        ]
        album = FakePlexAlbum(tracks)
        result = await provider._calc_resume_position_ms(album, fully_played=False)
        # First track contributes 10000 ms, second offset is 12000 ms
        assert result == 10000 + 12000

    @pytest.mark.asyncio
    async def test_fully_played_with_no_offset_uses_album_duration(self) -> None:
        """When fully played and no offsets, return album duration."""
        provider = _make_provider(LIBRARY_TYPE_AUDIOBOOKS)
        tracks = [
            FakePlexTrack(duration=10000, view_offset=0),
            FakePlexTrack(duration=20000, view_offset=0),
        ]
        album = FakePlexAlbum(tracks, album_duration=30000)
        result = await provider._calc_resume_position_ms(album, fully_played=True)
        assert result == 30000

    @pytest.mark.asyncio
    async def test_mixed_offsets_with_gap(self) -> None:
        """Only the last offset matters; gaps are ignored."""
        provider = _make_provider(LIBRARY_TYPE_AUDIOBOOKS)
        tracks = [
            FakePlexTrack(duration=10000, view_offset=8000),
            FakePlexTrack(duration=20000, view_offset=0),
            FakePlexTrack(duration=15000, view_offset=5000),
        ]
        album = FakePlexAlbum(tracks)
        result = await provider._calc_resume_position_ms(album, fully_played=False)
        # Cumulative before last track = 10000 + 20000 = 30000
        # Last offset = 5000
        assert result == 30000 + 5000


class TestFindTrackForPosition:
    """Tests for _find_track_for_position."""

    @pytest.mark.asyncio
    async def test_position_in_first_track(self) -> None:
        """Position inside the first track returns it with correct offset."""
        provider = _make_provider(LIBRARY_TYPE_AUDIOBOOKS)
        tracks = [
            FakePlexTrack(key="/1", duration=10000),
            FakePlexTrack(key="/2", duration=20000),
        ]
        album = FakePlexAlbum(tracks)
        track, offset = await provider._find_track_for_position(album, position=5)
        assert track is not None
        assert track.key == "/1"
        assert offset == 5000  # 5 seconds = 5000 ms

    @pytest.mark.asyncio
    async def test_position_in_second_track(self) -> None:
        """Position inside second track returns it with offset relative to track start."""
        provider = _make_provider(LIBRARY_TYPE_AUDIOBOOKS)
        tracks = [
            FakePlexTrack(key="/1", duration=10000),
            FakePlexTrack(key="/2", duration=20000),
        ]
        album = FakePlexAlbum(tracks)
        track, offset = await provider._find_track_for_position(album, position=12)
        # 12 seconds = 12000 ms. First track is 10000 ms, so 2000 ms into second track.
        assert track is not None
        assert track.key == "/2"
        assert offset == 2000

    @pytest.mark.asyncio
    async def test_position_past_end_clamps_to_last_track(self) -> None:
        """Position beyond all tracks clamps to the end of the last track."""
        provider = _make_provider(LIBRARY_TYPE_AUDIOBOOKS)
        tracks = [
            FakePlexTrack(key="/1", duration=10000),
            FakePlexTrack(key="/2", duration=20000),
        ]
        album = FakePlexAlbum(tracks)
        track, offset = await provider._find_track_for_position(album, position=40)
        # Total duration is 30 seconds; position 40 is past end.
        assert track is not None
        assert track.key == "/2"
        assert offset == 20000  # full duration of last track

    @pytest.mark.asyncio
    async def test_empty_album_returns_none(self) -> None:
        """An album with no tracks returns (None, 0)."""
        provider = _make_provider(LIBRARY_TYPE_AUDIOBOOKS)
        album = FakePlexAlbum([])
        track, offset = await provider._find_track_for_position(album, position=5)
        assert track is None
        assert offset == 0


class TestOnPlayed:
    """Tests for on_played progress sync."""

    @pytest.mark.asyncio
    async def test_fully_played_calls_mark_played(self) -> None:
        """When fully_played=True, markPlayed is called on the album."""
        provider = _make_provider(LIBRARY_TYPE_AUDIOBOOKS)
        album = FakePlexAlbum(
            [FakePlexTrack(key="/1", duration=10000)],
            key="/library/metadata/100",
        )
        provider._plex_library.fetchItem = MagicMock(return_value=album)

        call_log: list[Any] = []

        async def _run_async(call: Any, *args: Any, **kwargs: Any) -> Any:
            call_log.append((call, args, kwargs))
            return call(*args, **kwargs)

        provider._run_async = _run_async

        await provider.on_played(
            MediaType.AUDIOBOOK,
            "audiobook:/library/metadata/100",
            fully_played=True,
            position=0,
            media_item=MagicMock(),
        )

        mark_played_called = any(call_info[0] == album.markPlayed for call_info in call_log)
        assert mark_played_called

    @pytest.mark.asyncio
    async def test_zero_position_calls_mark_unplayed(self) -> None:
        """When position is 0, markUnplayed is called on the album."""
        provider = _make_provider(LIBRARY_TYPE_AUDIOBOOKS)
        album = FakePlexAlbum(
            [FakePlexTrack(key="/1", duration=10000)],
            key="/library/metadata/100",
        )
        provider._plex_library.fetchItem = MagicMock(return_value=album)

        call_log: list[Any] = []

        async def _run_async(call: Any, *args: Any, **kwargs: Any) -> Any:
            call_log.append((call, args, kwargs))
            return call(*args, **kwargs)

        provider._run_async = _run_async

        await provider.on_played(
            MediaType.AUDIOBOOK,
            "audiobook:/library/metadata/100",
            fully_played=False,
            position=0,
            media_item=MagicMock(),
        )

        mark_unplayed_called = any(call_info[0] == album.markUnplayed for call_info in call_log)
        assert mark_unplayed_called

    @pytest.mark.asyncio
    async def test_mid_position_updates_timeline(self) -> None:
        """When position is in the middle, updateTimeline is called on the correct track."""
        provider = _make_provider(LIBRARY_TYPE_AUDIOBOOKS)
        track = FakePlexTrack(key="/1", duration=30000)
        album = FakePlexAlbum([track], key="/library/metadata/100")
        provider._plex_library.fetchItem = MagicMock(return_value=album)

        call_log: list[Any] = []

        async def _run_async(call: Any, *args: Any, **kwargs: Any) -> Any:
            call_log.append((call, args, kwargs))
            return call(*args, **kwargs)

        provider._run_async = _run_async

        await provider.on_played(
            MediaType.AUDIOBOOK,
            "audiobook:/library/metadata/100",
            fully_played=False,
            position=10,
            media_item=MagicMock(),
            is_playing=True,
        )

        timeline_called = any(
            call_info[0] == track.updateTimeline and call_info[2].get("state") == "playing"
            for call_info in call_log
        )
        assert timeline_called


class TestGetResumePosition:
    """Tests for get_resume_position."""

    @pytest.mark.asyncio
    async def test_audiobook_with_progress(self) -> None:
        """Returns correct resume position for an audiobook with track offsets."""
        provider = _make_provider(LIBRARY_TYPE_AUDIOBOOKS)
        tracks = [
            FakePlexTrack(duration=10000, view_offset=0),
            FakePlexTrack(duration=20000, view_offset=15000),
        ]
        viewed_at = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        album = FakePlexAlbum(
            tracks,
            key="/library/metadata/100",
            last_viewed_at=viewed_at,
        )

        provider._plex_library.fetchItem = MagicMock(return_value=album)

        fully_played, position_ms, timestamp = await provider.get_resume_position(
            "audiobook:/library/metadata/100", MediaType.AUDIOBOOK
        )

        assert fully_played is False
        assert position_ms == 10000 + 15000
        assert timestamp == viewed_at

    @pytest.mark.asyncio
    async def test_fully_played_audiobook(self) -> None:
        """When fully played, returns album duration as resume position."""
        provider = _make_provider(LIBRARY_TYPE_AUDIOBOOKS)
        tracks = [FakePlexTrack(duration=30000, view_offset=0)]
        album = FakePlexAlbum(
            tracks,
            key="/library/metadata/100",
            view_count=1,
            album_duration=30000,
        )

        provider._plex_library.fetchItem = MagicMock(return_value=album)

        fully_played, position_ms, _ = await provider.get_resume_position(
            "audiobook:/library/metadata/100", MediaType.AUDIOBOOK
        )

        assert fully_played is True
        assert position_ms == 30000

    @pytest.mark.asyncio
    async def test_podcast_episode_delegates_to_album(self) -> None:
        """Podcast episode delegates resume position to parent album."""
        provider = _make_provider(LIBRARY_TYPE_PODCASTS)
        episode_track = FakePlexTrack(
            key="/library/metadata/200",
            duration=15000,
            parent_key="/library/metadata/100",
        )
        album = FakePlexAlbum(
            [episode_track],
            key="/library/metadata/100",
            album_duration=15000,
        )

        provider._plex_library.fetchItem = MagicMock(side_effect=[episode_track, album])

        fully_played, position_ms, _ = await provider.get_resume_position(
            "podcast_episode:/library/metadata/200", MediaType.PODCAST_EPISODE
        )

        assert fully_played is False
        assert position_ms == 0

    @pytest.mark.asyncio
    async def test_not_found_raises_media_not_found(self) -> None:
        """If the Plex item is missing, MediaNotFoundError is raised."""
        import plexapi.exceptions

        provider = _make_provider(LIBRARY_TYPE_AUDIOBOOKS)
        provider._run_async = AsyncMock(side_effect=plexapi.exceptions.NotFound("Item not found"))

        with pytest.raises(MediaNotFoundError):
            await provider.get_resume_position(
                "audiobook:/library/metadata/999", MediaType.AUDIOBOOK
            )


class TestUpdateConfig:
    """Tests for update_config media type cleanup when library_type changes."""

    @pytest.mark.asyncio
    async def test_type_change_from_audiobooks_to_podcasts_cleans_audiobooks(self) -> None:
        """Changing library_type from audiobooks to podcasts removes audiobook entries."""
        provider = _make_provider(LIBRARY_TYPE_AUDIOBOOKS)
        provider.mass.music.database = MagicMock()
        provider.mass.music.database.get_rows_from_query = AsyncMock(
            return_value=[{"item_id": 1}, {"item_id": 2}]
        )
        provider.mass.music.get_controller = MagicMock()
        mock_ctrl = MagicMock()
        mock_ctrl.remove_provider_mappings = AsyncMock()
        provider.mass.music.get_controller.return_value = mock_ctrl
        provider.mass.cache.delete = AsyncMock()

        new_config = MagicMock()
        new_config.get_value = lambda key: (
            LIBRARY_TYPE_PODCASTS if key == "library_type" else "plex_instance_1"
        )
        new_config.values = {"library_type": LIBRARY_TYPE_PODCASTS}
        new_config.instance_id = "plex_instance_1"

        async def _mock_super_update_config(config: Any, changed_keys: set[str]) -> None:
            """Mock super().update_config that updates provider.config."""
            provider.config = config

        with mock.patch(
            "music_assistant.models.provider.Provider.update_config",
            side_effect=_mock_super_update_config,
        ) as mock_super:
            await provider.update_config(
                new_config,
                changed_keys={"values/library_type"},
            )

        mock_super.assert_awaited_once()

        # The old type was audiobooks, new type is podcasts
        # Audiobook is a stale media type (not present in podcasts)
        mock_ctrl.remove_provider_mappings.assert_has_awaits(
            [
                mock.call(1, "plex_instance_1"),
                mock.call(2, "plex_instance_1"),
            ]
        )
        # Cache clear for audiobook IDs
        provider.mass.cache.delete.assert_awaited_once_with(
            key="audiobook",
            provider="plex_instance_1",
            category="prev_library_ids",
        )

    @pytest.mark.asyncio
    async def test_type_change_from_music_to_audiobooks_cleans_music_items(self) -> None:
        """Changing library_type from music to audiobooks removes music entries."""
        provider = _make_provider(LIBRARY_TYPE_MUSIC)
        provider.mass.music.database = MagicMock()
        provider.mass.music.database.get_rows_from_query = AsyncMock(return_value=[{"item_id": 10}])
        provider.mass.music.get_controller = MagicMock()
        mock_ctrl = MagicMock()
        mock_ctrl.remove_provider_mappings = AsyncMock()
        provider.mass.music.get_controller.return_value = mock_ctrl
        provider.mass.cache.delete = AsyncMock()

        new_config = MagicMock()
        new_config.get_value = lambda key: (
            LIBRARY_TYPE_AUDIOBOOKS if key == "library_type" else "plex_instance_1"
        )
        new_config.values = {"library_type": LIBRARY_TYPE_AUDIOBOOKS}
        new_config.instance_id = "plex_instance_1"

        async def _mock_super_update_config(config: Any, changed_keys: set[str]) -> None:
            """Mock super().update_config that updates provider.config."""
            provider.config = config

        with mock.patch(
            "music_assistant.models.provider.Provider.update_config",
            side_effect=_mock_super_update_config,
        ):
            await provider.update_config(
                new_config,
                changed_keys={"values/library_type"},
            )

        # Music types (artist, album, track, playlist) are stale
        # All of them should have had their provider mappings queried and removed
        music_ctrl = provider.mass.music.get_controller
        assert music_ctrl.call_count == 4  # artist, album, track, playlist
        mock_ctrl.remove_provider_mappings.assert_has_awaits(
            [
                mock.call(10, "plex_instance_1"),
                mock.call(10, "plex_instance_1"),
                mock.call(10, "plex_instance_1"),
                mock.call(10, "plex_instance_1"),
            ]
        )

    @pytest.mark.asyncio
    async def test_no_change_does_nothing(self) -> None:
        """When library_type does not change, no cleanup is performed."""
        provider = _make_provider(LIBRARY_TYPE_AUDIOBOOKS)
        provider.mass.music.database = MagicMock()
        provider.mass.music.database.get_rows_from_query = AsyncMock()
        provider.mass.music.get_controller = MagicMock()
        provider.mass.cache.delete = AsyncMock()

        new_config = MagicMock()
        new_config.get_value = lambda key: (
            LIBRARY_TYPE_AUDIOBOOKS if key == "library_type" else "plex_instance_1"
        )
        new_config.values = {"library_type": LIBRARY_TYPE_AUDIOBOOKS}

        async def _mock_super_update_config(config: Any, changed_keys: set[str]) -> None:
            """Mock super().update_config that updates provider.config."""
            provider.config = config

        with mock.patch(
            "music_assistant.models.provider.Provider.update_config",
            side_effect=_mock_super_update_config,
        ):
            await provider.update_config(
                new_config,
                changed_keys={"values/library_type"},
            )

        # No database queries, no controller lookups
        provider.mass.music.database.get_rows_from_query.assert_not_awaited()
        provider.mass.music.get_controller.assert_not_called()
        provider.mass.cache.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_other_config_changes_ignored(self) -> None:
        """Non-library_type config changes do not trigger cleanup."""
        provider = _make_provider(LIBRARY_TYPE_AUDIOBOOKS)
        provider.mass.music.database = MagicMock()
        provider.mass.music.database.get_rows_from_query = AsyncMock()
        provider.mass.music.get_controller = MagicMock()
        provider.mass.cache.delete = AsyncMock()

        new_config = MagicMock()
        new_config.get_value = lambda key: (
            LIBRARY_TYPE_AUDIOBOOKS if key == "library_type" else "plex_instance_1"
        )
        new_config.values = {"library_type": LIBRARY_TYPE_AUDIOBOOKS}

        async def _mock_super_update_config(config: Any, changed_keys: set[str]) -> None:
            """Mock super().update_config that updates provider.config."""
            provider.config = config

        with mock.patch(
            "music_assistant.models.provider.Provider.update_config",
            side_effect=_mock_super_update_config,
        ):
            await provider.update_config(
                new_config,
                changed_keys={"values/token"},
            )

        provider.mass.music.database.get_rows_from_query.assert_not_awaited()
        provider.mass.music.get_controller.assert_not_called()
        provider.mass.cache.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stale_sync_config_values_are_removed(self) -> None:
        """When library_type changes, old sync config values are purged."""
        provider = _make_provider(LIBRARY_TYPE_MUSIC)
        provider.mass.music.database = MagicMock()
        provider.mass.music.database.get_rows_from_query = AsyncMock(return_value=[])
        provider.mass.music.get_controller = MagicMock(return_value=None)
        provider.mass.cache.delete = AsyncMock()
        provider.mass.config.remove_provider_config_value = AsyncMock()

        new_config = MagicMock()
        new_config.get_value = lambda key: (
            LIBRARY_TYPE_AUDIOBOOKS if key == "library_type" else "plex_instance_1"
        )
        new_config.values = {"library_type": LIBRARY_TYPE_AUDIOBOOKS}

        async def _mock_super_update_config(config: Any, changed_keys: set[str]) -> None:
            """Mock super().update_config that updates provider.config."""
            provider.config = config

        with (
            mock.patch(
                "music_assistant.models.provider.Provider.update_config",
                side_effect=_mock_super_update_config,
            ),
            mock.patch.object(
                type(provider), "instance_id", new_callable=mock.PropertyMock
            ) as mock_instance_id,
        ):
            mock_instance_id.return_value = "plex_instance_1"
            await provider.update_config(
                new_config,
                changed_keys={"values/library_type"},
            )

        # Music sync config keys should be removed for the stale types
        provider.mass.config.remove_provider_config_value.assert_has_awaits(
            [
                mock.call("plex_instance_1", "library_sync_artists"),
                mock.call("plex_instance_1", "library_sync_albums"),
                mock.call("plex_instance_1", "library_sync_tracks"),
                mock.call("plex_instance_1", "library_sync_playlists"),
            ]
        )
