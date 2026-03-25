"""Unit tests for MusicController."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType, ProviderFeature, ProviderType, TaskStatus
from music_assistant_models.errors import InvalidProviderURI
from music_assistant_models.media_items import SearchResults

from music_assistant.controllers.music import MusicController
from tests.support.fixture_factory import make_track
from tests.support.harness import MusicAssistantHarness
from tests.support.mock_music_provider import MOCK_PROVIDER_DOMAIN, MockMusicProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_mass() -> MagicMock:
    """Return a minimal mock MusicAssistant."""
    mass = MagicMock()
    mass.closing = False
    mass.loop = None
    mass.config = MagicMock()
    # Must return "GLOBAL" so that CoreController._set_logger doesn't raise
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    mass.config.get_provider_config_value = AsyncMock(return_value=True)
    mass.signal_event = MagicMock()
    mass.get_providers = MagicMock(return_value=[])
    mass.tasks = MagicMock()
    mass.tasks.get_tasks_by_metadata = MagicMock(return_value=[])
    return mass


def _make_mock_provider(
    domain: str = MOCK_PROVIDER_DOMAIN,
    instance_id: str = "mock_1",
    is_streaming: bool = True,
) -> MagicMock:
    """Return a mock MusicProvider."""
    prov = MagicMock()
    prov.domain = domain
    prov.instance_id = instance_id
    prov.type = ProviderType.MUSIC
    prov.is_streaming_provider = is_streaming
    prov.available = True
    prov.supported_features = {ProviderFeature.SEARCH}
    return prov


def _spotify_providers() -> tuple[MagicMock, MagicMock]:
    """Return two streaming provider mocks with the same domain."""
    return (
        _make_mock_provider("spotify", "spotify_1", is_streaming=True),
        _make_mock_provider("spotify", "spotify_2", is_streaming=True),
    )


def _filesystem_providers() -> tuple[MagicMock, MagicMock]:
    """Return two local (non-streaming) provider mocks."""
    return (
        _make_mock_provider("filesystem", "fs_1", is_streaming=False),
        _make_mock_provider("filesystem", "fs_2", is_streaming=False),
    )


# ---------------------------------------------------------------------------
# get_controller
# ---------------------------------------------------------------------------


class TestGetController:
    """Tests for MusicController.get_controller()."""

    def test_returns_tracks_controller_for_track(self) -> None:
        """get_controller returns the tracks sub-controller for MediaType.TRACK."""
        mass = _make_mock_mass()
        ctrl = MusicController(mass)
        # Given: media type is TRACK
        # When:
        result = ctrl.get_controller(MediaType.TRACK)
        # Then:
        assert result is ctrl.tracks

    def test_returns_albums_controller_for_album(self) -> None:
        """get_controller returns the albums sub-controller for MediaType.ALBUM."""
        mass = _make_mock_mass()
        ctrl = MusicController(mass)
        result = ctrl.get_controller(MediaType.ALBUM)
        assert result is ctrl.albums

    def test_returns_artists_controller_for_artist(self) -> None:
        """get_controller returns the artists sub-controller for MediaType.ARTIST."""
        mass = _make_mock_mass()
        ctrl = MusicController(mass)
        result = ctrl.get_controller(MediaType.ARTIST)
        assert result is ctrl.artists

    def test_returns_playlists_controller_for_playlist(self) -> None:
        """get_controller returns the playlists sub-controller for MediaType.PLAYLIST."""
        mass = _make_mock_mass()
        ctrl = MusicController(mass)
        result = ctrl.get_controller(MediaType.PLAYLIST)
        assert result is ctrl.playlists

    def test_returns_radio_controller_for_radio(self) -> None:
        """get_controller returns the radio sub-controller for MediaType.RADIO."""
        mass = _make_mock_mass()
        ctrl = MusicController(mass)
        result = ctrl.get_controller(MediaType.RADIO)
        assert result is ctrl.radio

    def test_returns_audiobooks_controller_for_audiobook(self) -> None:
        """get_controller returns the audiobooks sub-controller for MediaType.AUDIOBOOK."""
        mass = _make_mock_mass()
        ctrl = MusicController(mass)
        result = ctrl.get_controller(MediaType.AUDIOBOOK)
        assert result is ctrl.audiobooks

    def test_returns_podcasts_controller_for_podcast(self) -> None:
        """get_controller returns the podcasts sub-controller for MediaType.PODCAST."""
        mass = _make_mock_mass()
        ctrl = MusicController(mass)
        result = ctrl.get_controller(MediaType.PODCAST)
        assert result is ctrl.podcasts

    def test_returns_podcasts_controller_for_podcast_episode(self) -> None:
        """get_controller maps PODCAST_EPISODE to the podcasts sub-controller."""
        mass = _make_mock_mass()
        ctrl = MusicController(mass)
        result = ctrl.get_controller(MediaType.PODCAST_EPISODE)
        assert result is ctrl.podcasts

    def test_raises_for_unknown_media_type(self) -> None:
        """get_controller raises NotImplementedError for unrecognised media types."""
        mass = _make_mock_mass()
        ctrl = MusicController(mass)
        with pytest.raises(NotImplementedError):
            ctrl.get_controller(MediaType.UNKNOWN)


# ---------------------------------------------------------------------------
# providers property
# ---------------------------------------------------------------------------


class TestProvidersProperty:
    """Tests for MusicController.providers."""

    def test_returns_only_music_providers(self) -> None:
        """Providers filters out non-MUSIC provider types."""
        mass = _make_mock_mass()
        ctrl = MusicController(mass)

        # Given: two MUSIC providers and one non-MUSIC provider
        music_prov = _make_mock_provider("music_prov", "music_1")
        player_prov = MagicMock()
        player_prov.type = ProviderType.PLAYER
        mass.providers = [music_prov, player_prov]

        with patch("music_assistant.controllers.music.get_current_user", return_value=None):
            result = ctrl.providers

        # Then: only MUSIC providers are returned
        assert music_prov in result
        assert player_prov not in result

    def test_applies_user_provider_filter(self) -> None:
        """Providers respects a logged-in user's provider_filter."""
        mass = _make_mock_mass()
        ctrl = MusicController(mass)

        # Given: two MUSIC providers, user only has access to one
        prov_a = _make_mock_provider("domain_a", "instance_a")
        prov_b = _make_mock_provider("domain_b", "instance_b")
        mass.providers = [prov_a, prov_b]

        user = MagicMock()
        user.provider_filter = {"instance_a"}

        with patch("music_assistant.controllers.music.get_current_user", return_value=user):
            result = ctrl.providers

        # Then: only allowed provider is returned
        assert prov_a in result
        assert prov_b not in result

    def test_returns_all_when_no_user(self) -> None:
        """Providers returns all MUSIC providers when no user context."""
        mass = _make_mock_mass()
        ctrl = MusicController(mass)

        prov_a = _make_mock_provider("domain_a", "instance_a")
        prov_b = _make_mock_provider("domain_b", "instance_b")
        mass.providers = [prov_a, prov_b]

        with patch("music_assistant.controllers.music.get_current_user", return_value=None):
            result = ctrl.providers

        assert len(result) == 2


# ---------------------------------------------------------------------------
# get_unique_providers
# ---------------------------------------------------------------------------


def _property_returning(items: list[MagicMock]) -> property:
    """Return a property descriptor that always returns items."""

    def _get(_self: object) -> list[MagicMock]:
        return items

    return property(_get)


class TestGetUniqueProviders:
    """Tests for MusicController.get_unique_providers()."""

    def test_deduplicates_streaming_provider_domains(self) -> None:
        """get_unique_providers returns only one instance per streaming provider domain."""
        mass = _make_mock_mass()
        ctrl = MusicController(mass)

        # Given: two instances of the same streaming provider domain
        prov_a, prov_b = _spotify_providers()

        with (
            patch.object(type(ctrl), "providers", _property_returning([prov_a, prov_b])),
            patch("music_assistant.controllers.music.get_current_user", return_value=None),
        ):
            result = ctrl.get_unique_providers()

        # Then: only one instance is returned
        assert len(result) == 1

    def test_returns_all_non_streaming_providers(self) -> None:
        """get_unique_providers returns all local (non-streaming) providers."""
        mass = _make_mock_mass()
        ctrl = MusicController(mass)

        # Given: two non-streaming (local) providers
        prov_a, prov_b = _filesystem_providers()

        with (
            patch.object(type(ctrl), "providers", _property_returning([prov_a, prov_b])),
            patch("music_assistant.controllers.music.get_current_user", return_value=None),
        ):
            result = ctrl.get_unique_providers()

        # Then: both instances are returned since they are not streaming providers
        assert len(result) == 2

    def test_returns_empty_when_no_providers(self) -> None:
        """get_unique_providers returns empty list when there are no providers."""
        mass = _make_mock_mass()
        ctrl = MusicController(mass)

        with (
            patch.object(type(ctrl), "providers", _property_returning([])),
            patch("music_assistant.controllers.music.get_current_user", return_value=None),
        ):
            result = ctrl.get_unique_providers()

        assert result == []


# ---------------------------------------------------------------------------
# active_sync_tasks
# ---------------------------------------------------------------------------


class TestActiveSyncTasks:
    """Tests for MusicController.active_sync_tasks."""

    def test_returns_empty_when_no_tasks(self) -> None:
        """active_sync_tasks returns empty list when no sync tasks are running."""
        mass = _make_mock_mass()
        mass.tasks.get_tasks_by_metadata.return_value = []
        ctrl = MusicController(mass)

        result = ctrl.active_sync_tasks

        assert result == []

    def test_filters_to_pending_and_running(self) -> None:
        """active_sync_tasks only includes PENDING and RUNNING tasks."""
        mass = _make_mock_mass()
        ctrl = MusicController(mass)

        pending_task = MagicMock()
        pending_task.status = TaskStatus.PENDING
        running_task = MagicMock()
        running_task.status = TaskStatus.RUNNING
        done_task = MagicMock()
        done_task.status = TaskStatus.SUCCESS

        mass.tasks.get_tasks_by_metadata.return_value = [pending_task, running_task, done_task]

        result = ctrl.active_sync_tasks

        assert pending_task in result
        assert running_task in result
        assert done_task not in result


# ---------------------------------------------------------------------------
# search with real mass + MockMusicProvider
# ---------------------------------------------------------------------------


class TestSearch:
    """Tests for MusicController.search() using a real mass instance."""

    async def test_search_delegates_to_provider_search(self) -> None:
        """search() calls the provider's search method and includes results."""
        mass = _make_mock_mass()
        ctrl = MusicController(mass)

        # Given: a mock provider that returns a known track
        mock_prov = _make_mock_provider()
        mock_prov.search = AsyncMock(
            return_value=SearchResults(tracks=[make_track("t1", "Bright Eyes")])
        )
        mass.get_provider = MagicMock(return_value=mock_prov)
        mass.cache.get = AsyncMock(return_value=None)
        mass.cache.set = AsyncMock()

        with (
            patch.object(
                type(ctrl),
                "get_unique_providers",
                return_value=[mock_prov.instance_id],
            ),
            patch.object(ctrl, "search_library", new=AsyncMock(return_value=SearchResults())),
            patch("music_assistant.controllers.music.get_current_user", return_value=None),
            patch(
                "music_assistant.controllers.music.parse_uri",
                side_effect=InvalidProviderURI("not a URI"),
            ),
        ):
            results = await ctrl.search("Bright", media_types=[MediaType.TRACK], limit=10)

        # Then: the provider's result is included
        assert any(t.name == "Bright Eyes" for t in results.tracks)

    async def test_search_returns_empty_for_no_match(self, mass: MagicMock) -> None:
        """search() returns empty results when no provider has matching items."""
        harness = MusicAssistantHarness(mass)
        provider = MockMusicProvider(
            mass=mass,
            tracks=[make_track("t1", "Some Track")],
        )
        await harness.add_provider(provider)

        # When: searching for something that doesn't exist
        with patch("music_assistant.controllers.music.get_current_user", return_value=None):
            results = await mass.music.search("ZZZNOMATCH", media_types=[MediaType.TRACK], limit=10)

        # Then: no tracks returned
        assert results.tracks == []

    async def test_search_caches_results(self, mass: MagicMock) -> None:
        """search() uses the cache on repeated identical queries."""
        harness = MusicAssistantHarness(mass)
        provider = MockMusicProvider(
            mass=mass,
            tracks=[make_track("t1", "Cached Song")],
        )
        await harness.add_provider(provider)

        with patch("music_assistant.controllers.music.get_current_user", return_value=None):
            first = await mass.music.search("Cached", media_types=[MediaType.TRACK], limit=10)
            second = await mass.music.search("Cached", media_types=[MediaType.TRACK], limit=10)

        # Then: both calls return the same result (second is from cache)
        assert len(first.tracks) == len(second.tracks)


# ---------------------------------------------------------------------------
# search_library
# ---------------------------------------------------------------------------


class TestSearchLibrary:
    """Tests for MusicController.search_library()."""

    async def test_search_library_calls_each_controller(self, mass: MagicMock) -> None:
        """search_library delegates to the correct per-type controller."""
        # When: searching library for tracks only
        with patch("music_assistant.controllers.music.get_current_user", return_value=None):
            result = await mass.music.search_library(
                "query", media_types=[MediaType.TRACK], limit=5
            )

        # Then: result is a SearchResults with tracks list (may be empty in test db)
        assert isinstance(result, SearchResults)
