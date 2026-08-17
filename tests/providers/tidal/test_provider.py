"""Test Tidal Provider integration."""

import json
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.enums import ExternalID, MediaType
from music_assistant_models.errors import LoginFailed, MediaNotFoundError
from music_assistant_models.media_items import Album, Artist, Playlist, Track

from music_assistant.providers.tidal.provider import TidalProvider
from tests.common import use_real_create_task


@pytest.fixture
def mass_mock() -> Mock:
    """Return a mock MusicAssistant instance."""
    mass = Mock()
    mass.http_session = AsyncMock()
    mass.metadata.locale = "en_US"
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    mass.cache.set = AsyncMock()
    mass.cache.delete = AsyncMock()
    use_real_create_task(mass)
    return mass


@pytest.fixture
def manifest_mock() -> Mock:
    """Return a mock provider manifest."""
    manifest = Mock()
    manifest.domain = "tidal"
    return manifest


@pytest.fixture
def config_mock() -> Mock:
    """Return a mock provider config."""
    config = Mock()
    config.name = "Tidal Test"
    config.instance_id = "tidal_test"
    config.enabled = True
    config.get_value.side_effect = lambda key: {
        "auth_token": "mock_access_token",
        "refresh_token": "mock_refresh_token",
        "expiry_time": 1234567890,
        "user_id": "12345",
        "log_level": "INFO",
    }.get(key, "INFO" if "log" in key else None)
    return config


@pytest.fixture
def provider(mass_mock: Mock, manifest_mock: Mock, config_mock: Mock) -> TidalProvider:
    """Return a TidalProvider instance."""
    return TidalProvider(mass_mock, manifest_mock, config_mock)


async def test_provider_initialization(
    mass_mock: Mock, manifest_mock: Mock, config_mock: Mock
) -> None:
    """Test provider initialization creates all managers."""
    provider = TidalProvider(mass_mock, manifest_mock, config_mock)

    assert provider.auth is not None
    assert provider.api is not None
    assert provider.library is not None
    assert provider.media is not None
    assert provider.playlists is not None
    assert provider.recommendations_manager is not None
    assert provider.streaming is not None


_SETUP_VALUES = {
    "auth_token": "mock_access_token",
    "refresh_token": "mock_refresh_token",
    "expiry_time": 1234567890,
    "user_id": "12345",
}


async def test_handle_async_init_success(provider: TidalProvider) -> None:
    """Test successful async initialization."""
    with (
        patch.object(
            provider,
            "get_setup_value",
            side_effect=lambda key, default=None: _SETUP_VALUES.get(key, default),
        ),
        patch.object(provider.auth, "initialize", new_callable=AsyncMock) as mock_init,
        patch.object(provider.api, "get", new_callable=AsyncMock) as mock_get,
        patch.object(provider, "get_user", new_callable=AsyncMock) as mock_get_user,
        patch.object(provider.auth, "update_user_info", new_callable=AsyncMock),
    ):
        mock_init.return_value = True
        mock_get.return_value = {"userId": "12345", "sessionId": "session_123"}
        mock_get_user.return_value = {"id": "12345", "username": "testuser"}

        await provider.handle_async_init()

        mock_init.assert_called_once()
        mock_get.assert_called_with("sessions")


async def test_handle_async_init_migrates_iso_expiry(provider: TidalProvider) -> None:
    """Test a legacy ISO-string expiry_time is converted to a timestamp and persisted."""
    values = dict(_SETUP_VALUES)
    values["expiry_time"] = "2026-01-01T12:00:00+00:00"

    with (
        patch.object(
            provider,
            "get_setup_value",
            side_effect=lambda key, default=None: values.get(key, default),
        ),
        patch.object(provider, "_update_setup_data") as mock_update,
        patch.object(provider.auth, "initialize", new_callable=AsyncMock) as mock_init,
        patch.object(provider.api, "get", new_callable=AsyncMock) as mock_get,
        patch.object(provider, "get_user", new_callable=AsyncMock) as mock_get_user,
        patch.object(provider.auth, "update_user_info", new_callable=AsyncMock),
    ):
        mock_init.return_value = True
        mock_get.return_value = {"userId": "12345", "sessionId": "session_123"}
        mock_get_user.return_value = {"id": "12345"}

        await provider.handle_async_init()

    expected_ts = datetime.fromisoformat("2026-01-01T12:00:00+00:00").timestamp()
    mock_update.assert_called_once_with("expiry_time", expected_ts)
    auth_blob = json.loads(mock_init.call_args[0][0])
    assert auth_blob["expires_at"] == expected_ts


async def test_handle_async_init_missing_auth() -> None:
    """Test async initialization fails with missing auth."""
    mass = Mock()
    mass.http_session = AsyncMock()
    mass.metadata.locale = "en_US"

    manifest = Mock()
    manifest.domain = "tidal"

    config = Mock()
    config.name = "Tidal Test"
    config.instance_id = "tidal_test"
    config.enabled = True
    config.get_value.side_effect = lambda key: "INFO" if "log" in key else None  # Missing auth data

    provider = TidalProvider(mass, manifest, config)

    with (
        patch.object(provider, "get_setup_value", return_value=None),
        pytest.raises(LoginFailed, match="Missing authentication data"),
    ):
        await provider.handle_async_init()


async def test_handle_async_init_auth_failed(provider: TidalProvider) -> None:
    """Test async initialization fails when auth initialize fails."""
    with (
        patch.object(
            provider,
            "get_setup_value",
            side_effect=lambda key, default=None: _SETUP_VALUES.get(key, default),
        ),
        patch.object(provider.auth, "initialize", new_callable=AsyncMock) as mock_init,
    ):
        mock_init.return_value = False

        with pytest.raises(LoginFailed, match="Failed to authenticate with Tidal"):
            await provider.handle_async_init()


async def test_search_delegates_to_media(provider: TidalProvider) -> None:
    """Test search delegates to media manager."""
    with patch.object(provider.media, "search", new_callable=AsyncMock) as mock_search:
        mock_search.return_value = Mock()

        await provider.search("test query", [MediaType.ARTIST], limit=10)

        mock_search.assert_called_with("test query", [MediaType.ARTIST], 10)


async def test_get_similar_tracks_delegates_to_media(provider: TidalProvider) -> None:
    """Test get_similar_tracks delegates to media manager."""
    with patch.object(provider.media, "get_similar_tracks", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = []

        result = await provider.get_similar_tracks("123", limit=30)

        mock_get.assert_called_with("123", 30)
        assert result == []


async def test_get_artist_delegates_to_media(provider: TidalProvider) -> None:
    """Test get_artist delegates to media manager."""
    with patch.object(provider.media, "get_artist", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = Mock(spec=Artist)

        result = await provider.get_artist("123")

        mock_get.assert_called_with("123")
        assert result is not None


async def test_get_album_delegates_to_media(provider: TidalProvider) -> None:
    """Test get_album delegates to media manager."""
    with patch.object(provider.media, "get_album", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = Mock(spec=Album)

        result = await provider.get_album("123")

        mock_get.assert_called_with("123")
        assert result is not None


async def test_get_track_delegates_to_media(provider: TidalProvider) -> None:
    """Test get_track delegates to media manager."""
    with patch.object(provider.media, "get_track", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = Mock(spec=Track)

        result = await provider.get_track("123")

        mock_get.assert_called_with("123")
        assert result is not None


async def test_get_playlist_delegates_to_media(provider: TidalProvider) -> None:
    """Test get_playlist delegates to media manager."""
    with patch.object(provider.media, "get_playlist", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = Mock(spec=Playlist)

        result = await provider.get_playlist("123")

        mock_get.assert_called_with("123")
        assert result is not None


async def test_get_album_tracks_delegates_to_media(provider: TidalProvider) -> None:
    """Test get_album_tracks delegates to media manager."""
    with patch.object(provider.media, "get_album_tracks", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = []

        result = await provider.get_album_tracks("123")

        mock_get.assert_called_with("123")
        assert result == []


async def test_get_artist_albums_delegates_to_media(provider: TidalProvider) -> None:
    """Test get_artist_albums delegates to media manager."""
    with patch.object(provider.media, "get_artist_albums", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = []

        result = await provider.get_artist_albums("123")

        mock_get.assert_called_with("123")
        assert result == []


async def test_get_artist_toptracks_delegates_to_media(provider: TidalProvider) -> None:
    """Test get_artist_toptracks delegates to media manager."""
    with patch.object(provider.media, "get_artist_toptracks", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = []

        await provider.get_artist_toptracks("123")

        mock_get.assert_called_with("123")


async def test_get_playlist_tracks_delegates_to_media(provider: TidalProvider) -> None:
    """Test get_playlist_tracks delegates to media manager."""
    with patch.object(provider.media, "get_playlist_tracks", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = []

        await provider.get_playlist_tracks("123", page=2)

        mock_get.assert_called_with("123", 2)


async def test_get_stream_details_delegates_to_streaming(provider: TidalProvider) -> None:
    """Test get_stream_details delegates to streaming manager."""
    with patch.object(provider.streaming, "get_stream_details", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = Mock()

        result = await provider.get_stream_details("123")

        mock_get.assert_called_with("123")
        assert result is not None


async def test_get_item_mapping(provider: TidalProvider) -> None:
    """Test get_item_mapping creates correct ItemMapping."""
    mapping = provider.get_item_mapping(MediaType.ARTIST, "123", "Test Artist")

    assert mapping.media_type == MediaType.ARTIST
    assert mapping.item_id == "123"
    assert mapping.provider == provider.instance_id
    assert mapping.name == "Test Artist"


async def test_get_library_artists_delegates_to_library(provider: TidalProvider) -> None:
    """Test get_library_artists delegates to library manager."""

    async def mock_generator() -> AsyncGenerator[Any]:
        yield Mock(spec=Artist)
        yield Mock(spec=Artist)

    with patch.object(provider.library, "get_artists", return_value=mock_generator()):
        artists = []
        async for artist in provider.get_library_artists():
            artists.append(artist)

        assert len(artists) == 2


async def test_get_library_albums_delegates_to_library(provider: TidalProvider) -> None:
    """Test get_library_albums delegates to library manager."""

    async def mock_generator() -> AsyncGenerator[Any]:
        yield Mock(spec=Album)

    with patch.object(provider.library, "get_albums", return_value=mock_generator()):
        albums = []
        async for album in provider.get_library_albums():
            albums.append(album)

        assert len(albums) == 1


async def test_get_library_tracks_delegates_to_library(provider: TidalProvider) -> None:
    """Test get_library_tracks delegates to library manager."""

    async def mock_generator() -> AsyncGenerator[Any]:
        yield Mock(spec=Track)
        yield Mock(spec=Track)
        yield Mock(spec=Track)

    with patch.object(provider.library, "get_tracks", return_value=mock_generator()):
        tracks = []
        async for track in provider.get_library_tracks():
            tracks.append(track)

        assert len(tracks) == 3


async def test_get_library_playlists_delegates_to_library(provider: TidalProvider) -> None:
    """Test get_library_playlists delegates to library manager."""

    async def mock_generator() -> AsyncGenerator[Any]:
        yield Mock(spec=Playlist)

    with patch.object(provider.library, "get_playlists", return_value=mock_generator()):
        playlists = []
        async for playlist in provider.get_library_playlists():
            playlists.append(playlist)

        assert len(playlists) == 1


async def test_library_add_delegates_to_library(provider: TidalProvider) -> None:
    """Test library_add delegates to library manager."""
    with patch.object(provider.library, "add_item", new_callable=AsyncMock) as mock_add:
        mock_add.return_value = True
        item = Mock()

        result = await provider.library_add(item)

        assert result is True
        mock_add.assert_called_with(item)


async def test_library_remove_delegates_to_library(provider: TidalProvider) -> None:
    """Test library_remove delegates to library manager."""
    with patch.object(provider.library, "remove_item", new_callable=AsyncMock) as mock_remove:
        mock_remove.return_value = True

        result = await provider.library_remove("123", MediaType.TRACK)

        assert result is True
        mock_remove.assert_called_with("123", MediaType.TRACK)


async def test_create_playlist_delegates_to_playlists(provider: TidalProvider) -> None:
    """Test create_playlist delegates to playlist manager."""
    with patch.object(provider.playlists, "create", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = Mock(spec=Playlist)

        await provider.create_playlist("New Playlist", media_types={MediaType.TRACK})

        mock_create.assert_called_with("New Playlist")


async def test_add_playlist_tracks_delegates_to_playlists(provider: TidalProvider) -> None:
    """Test add_playlist_tracks delegates to playlist manager."""
    with patch.object(provider.playlists, "add_tracks", new_callable=AsyncMock) as mock_add:
        await provider.add_playlist_tracks("123", ["track1", "track2"])

        mock_add.assert_called_with("123", ["track1", "track2"])


async def test_remove_playlist_tracks_delegates_to_playlists(provider: TidalProvider) -> None:
    """Test remove_playlist_tracks delegates to playlist manager."""
    with patch.object(provider.playlists, "remove_tracks", new_callable=AsyncMock) as mock_remove:
        await provider.remove_playlist_tracks("123", (1, 2, 3))

        mock_remove.assert_called_with("123", (1, 2, 3))


async def test_get_user(provider: TidalProvider) -> None:
    """Test get_user fetches user data."""
    with patch.object(provider.api, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = {"id": "123", "username": "testuser"}

        user = await provider.get_user("123")

        assert user["id"] == "123"
        mock_get.assert_called_with("users/123")


async def test_redirect_cached_id_hit(provider: TidalProvider, mass_mock: Mock) -> None:
    """Test redirect_cached_id returns the cached live id on a cache hit."""
    mass_mock.cache.get = AsyncMock(return_value="live_456")

    result = await provider.redirect_cached_id("stale_123")

    assert result == "live_456"
    mass_mock.cache.get.assert_called_with(
        "stale_123",
        provider=provider.instance_id,
        category=2,  # CACHE_CATEGORY_ISRC_MAP
    )


async def test_redirect_cached_id_miss(provider: TidalProvider, mass_mock: Mock) -> None:
    """Test redirect_cached_id returns the original id on a cache miss."""
    mass_mock.cache.get = AsyncMock(return_value=None)

    result = await provider.redirect_cached_id("stale_123")

    assert result == "stale_123"


async def test_note_replaced_track_schedules_healing(
    provider: TidalProvider, mass_mock: Mock
) -> None:
    """Test a REPLACED projection is turned into a cached redirect and a mapping heal."""
    item = {
        "id": "live_456",
        "type": "tracks",
        "meta": {
            "replacement": {"status": "REPLACED", "original": {"id": "stale_123", "type": "tracks"}}
        },
    }

    mass_mock.create_task = Mock(side_effect=lambda coro, **_kw: coro.close())

    with patch.object(provider, "_apply_replacement", new_callable=AsyncMock) as apply_mock:
        provider.note_replaced_track(item)

    mass_mock.create_task.assert_called_once()
    apply_mock.assert_called_once_with("stale_123", "live_456")


async def test_apply_replacement_caches_and_heals(provider: TidalProvider, mass_mock: Mock) -> None:
    """Test _apply_replacement stores the redirect and heals an existing library mapping."""
    lib_track = Mock()
    lib_track.item_id = 42
    mass_mock.music.tracks.get_library_item_by_prov_id = AsyncMock(return_value=lib_track)

    with patch.object(provider, "_heal_track_mapping", new_callable=AsyncMock) as heal_mock:
        await provider._apply_replacement("stale_123", "live_456")

    mass_mock.cache.set.assert_called_once_with(
        key="stale_123",
        data="live_456",
        provider=provider.instance_id,
        category=2,  # CACHE_CATEGORY_ISRC_MAP
        persistent=True,
        expiration=86400 * 90,
    )
    heal_mock.assert_called_once_with(42, "stale_123", "live_456")


async def test_apply_replacement_without_library_track(
    provider: TidalProvider, mass_mock: Mock
) -> None:
    """Test _apply_replacement still caches the redirect when no library track exists."""
    mass_mock.music.tracks.get_library_item_by_prov_id = AsyncMock(return_value=None)

    with patch.object(provider, "_heal_track_mapping", new_callable=AsyncMock) as heal_mock:
        await provider._apply_replacement("stale_123", "live_456")

    mass_mock.cache.set.assert_called_once()
    heal_mock.assert_not_called()


@pytest.mark.parametrize(
    "meta",
    [
        {},
        {"replacement": {"status": "ORIGINAL"}},
        {"replacement": {"status": "NOT_REPLACED", "original": {"id": "live_456"}}},
        {"replacement": {"status": "REPLACED", "original": {"id": "live_456"}}},
    ],
    ids=["no-meta", "original", "not-replaced", "replaced-with-same-id"],
)
async def test_note_replaced_track_ignores_non_replacements(
    provider: TidalProvider, mass_mock: Mock, meta: dict[str, Any]
) -> None:
    """Test nothing is scheduled unless the id actually changed."""
    provider.note_replaced_track({"id": "live_456", "type": "tracks", "meta": meta})

    mass_mock.create_task.assert_not_called()


async def test_resolve_live_track_id_cache_hit_different(
    provider: TidalProvider, mass_mock: Mock
) -> None:
    """Test resolve_live_track_id returns the cached id when it is still live."""
    mass_mock.cache.get = AsyncMock(return_value="live_456")

    # The liveness check must bypass the cached provider wrapper, so it is
    # served by the media manager directly; the wrapper must not be touched.
    with (
        patch.object(provider.media, "get_track", new_callable=AsyncMock),
        patch.object(provider, "get_track", new_callable=AsyncMock) as mock_cached,
    ):
        result = await provider.resolve_live_track_id("stale_123")

    assert result == "live_456"
    mock_cached.assert_not_called()


async def test_resolve_live_track_id_cache_hit_dead_reresolves(
    provider: TidalProvider, mass_mock: Mock
) -> None:
    """Test a dead cached id is dropped and re-resolved from the ISRC (double churn)."""
    mass_mock.cache.get = AsyncMock(return_value="dead_456")
    mass_mock.cache.delete = AsyncMock()
    mass_mock.cache.set = AsyncMock()
    lib_track = Mock()
    lib_track.item_id = 1
    lib_track.external_ids = [(ExternalID.ISRC, "US1234567890")]
    mass_mock.music.tracks.get_library_item_by_prov_id = AsyncMock(return_value=lib_track)
    mass_mock.create_task = Mock(side_effect=lambda coro, **_kw: coro.close())

    with (
        patch.object(
            provider.media,
            "get_track",
            new_callable=AsyncMock,
            side_effect=MediaNotFoundError("gone"),
        ),
        patch.object(provider, "get_track", new_callable=AsyncMock) as mock_cached,
        patch.object(provider.api, "get", new_callable=AsyncMock) as mock_get,
        patch.object(provider, "_heal_track_mapping", new_callable=AsyncMock),
    ):
        mock_get.return_value = {"data": [{"id": "new_789"}]}

        result = await provider.resolve_live_track_id("stale_123")

    assert result == "new_789"
    mass_mock.cache.delete.assert_called_once()
    # the liveness check must not consult the cached wrapper, which would keep
    # serving a stale track for a dead id
    mock_cached.assert_not_called()


async def test_resolve_live_track_id_cache_hit_same(
    provider: TidalProvider, mass_mock: Mock
) -> None:
    """Test resolve_live_track_id returns None when the cached id equals the input."""
    mass_mock.cache.get = AsyncMock(return_value="123")

    result = await provider.resolve_live_track_id("123")

    assert result is None


async def test_resolve_live_track_id_no_library_item(
    provider: TidalProvider, mass_mock: Mock
) -> None:
    """Test resolve_live_track_id returns None when there is no library item."""
    mass_mock.cache.get = AsyncMock(return_value=None)
    mass_mock.music.tracks.get_library_item_by_prov_id = AsyncMock(return_value=None)

    result = await provider.resolve_live_track_id("123")

    assert result is None


async def test_resolve_live_track_id_no_isrc(provider: TidalProvider, mass_mock: Mock) -> None:
    """Test resolve_live_track_id returns None when the library item has no ISRC."""
    mass_mock.cache.get = AsyncMock(return_value=None)
    lib_track = Mock()
    lib_track.external_ids = [(ExternalID.BARCODE, "some-id")]
    mass_mock.music.tracks.get_library_item_by_prov_id = AsyncMock(return_value=lib_track)

    result = await provider.resolve_live_track_id("123")

    assert result is None


async def test_resolve_live_track_id_isrc_lookup_empty(
    provider: TidalProvider, mass_mock: Mock
) -> None:
    """Test resolve_live_track_id returns None when the ISRC lookup returns no data."""
    mass_mock.cache.get = AsyncMock(return_value=None)
    lib_track = Mock()
    lib_track.external_ids = [(ExternalID.ISRC, "US1234567890")]
    mass_mock.music.tracks.get_library_item_by_prov_id = AsyncMock(return_value=lib_track)

    with patch.object(provider.api, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = {"data": []}

        result = await provider.resolve_live_track_id("123")

    assert result is None


async def test_resolve_live_track_id_not_stale(provider: TidalProvider, mass_mock: Mock) -> None:
    """Test resolve_live_track_id returns None when the resolved id equals the input."""
    mass_mock.cache.get = AsyncMock(return_value=None)
    lib_track = Mock()
    lib_track.external_ids = [(ExternalID.ISRC, "US1234567890")]
    mass_mock.music.tracks.get_library_item_by_prov_id = AsyncMock(return_value=lib_track)

    with patch.object(provider.api, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = {"data": [{"id": "123"}]}

        result = await provider.resolve_live_track_id("123")

    assert result is None


async def test_resolve_live_track_id_stale_caches_and_schedules_heal(
    provider: TidalProvider, mass_mock: Mock
) -> None:
    """Test resolve_live_track_id returns the live id, caches it, and schedules a heal."""
    mass_mock.cache.get = AsyncMock(return_value=None)
    mass_mock.cache.set = AsyncMock()

    lib_track = Mock()
    lib_track.item_id = 1
    lib_track.external_ids = [(ExternalID.ISRC, "US1234567890")]
    mass_mock.music.tracks.get_library_item_by_prov_id = AsyncMock(return_value=lib_track)

    # Discard the scheduled coroutine (avoid "never awaited" warnings) while
    # still recording the call for assertions.
    mass_mock.create_task = Mock(side_effect=lambda coro, **_kw: coro.close())

    with (
        patch.object(provider.api, "get", new_callable=AsyncMock) as mock_get,
        patch.object(provider, "_heal_track_mapping", new_callable=AsyncMock) as mock_heal,
    ):
        mock_get.return_value = {"data": [{"id": "NEW"}]}

        result = await provider.resolve_live_track_id("123")

    assert result == "NEW"
    mass_mock.cache.set.assert_called_once_with(
        key="123",
        data="NEW",
        provider=provider.instance_id,
        category=2,  # CACHE_CATEGORY_ISRC_MAP
        persistent=True,
        expiration=86400 * 90,
    )
    mass_mock.create_task.assert_called_once()
    mock_heal.assert_called_once_with(1, "123", "NEW")


async def test_heal_track_mapping_adds_before_removing(
    provider: TidalProvider, mass_mock: Mock
) -> None:
    """Test _heal_track_mapping adds the new mapping before removing the stale one."""
    call_order: list[str] = []

    async def _fake_add(*_args: Any, **_kwargs: Any) -> None:
        call_order.append("add")

    async def _fake_remove(*_args: Any, **_kwargs: Any) -> None:
        call_order.append("remove")

    new_mapping = Mock()
    new_mapping.provider_instance = provider.instance_id

    live_track = Mock()
    live_track.provider_mappings = [new_mapping]

    mass_mock.music.tracks.add_provider_mappings = AsyncMock(side_effect=_fake_add)
    mass_mock.music.tracks.remove_provider_mapping = AsyncMock(side_effect=_fake_remove)

    with patch.object(provider, "get_track", new_callable=AsyncMock) as mock_get_track:
        mock_get_track.return_value = live_track

        await provider._heal_track_mapping(1, "stale_123", "NEW")

    assert call_order == ["add", "remove"]
    mass_mock.music.tracks.add_provider_mappings.assert_called_once_with(1, [new_mapping])
    mass_mock.music.tracks.remove_provider_mapping.assert_called_once_with(
        1, provider.instance_id, "stale_123"
    )


async def test_heal_track_mapping_no_matching_mapping_does_nothing(
    provider: TidalProvider, mass_mock: Mock
) -> None:
    """Test _heal_track_mapping does nothing when the live track has no matching mapping."""
    other_mapping = Mock()
    other_mapping.provider_instance = "some_other_instance"

    live_track = Mock()
    live_track.provider_mappings = [other_mapping]

    mass_mock.music.tracks.add_provider_mappings = AsyncMock()
    mass_mock.music.tracks.remove_provider_mapping = AsyncMock()

    with patch.object(provider, "get_track", new_callable=AsyncMock) as mock_get_track:
        mock_get_track.return_value = live_track

        await provider._heal_track_mapping(1, "stale_123", "NEW")

    mass_mock.music.tracks.add_provider_mappings.assert_not_called()
    mass_mock.music.tracks.remove_provider_mapping.assert_not_called()


async def test_heal_track_mapping_swallows_errors(provider: TidalProvider) -> None:
    """Test _heal_track_mapping swallows expected errors without raising."""
    with patch.object(provider, "get_track", new_callable=AsyncMock) as mock_get_track:
        mock_get_track.side_effect = MediaNotFoundError("Track not found")

        # Should not raise.
        await provider._heal_track_mapping(1, "stale_123", "NEW")
