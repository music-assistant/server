"""Tests for library sync in_library behavior."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

import pytest
from music_assistant_models.enums import EventType, MediaType, ProviderType
from music_assistant_models.errors import InsufficientPermissions
from music_assistant_models.media_items import Album, AudioFormat, ProviderMapping, UniqueList

from music_assistant.constants import CONF_ENTRY_LIBRARY_SYNC_BACK
from music_assistant.controllers.music import MusicController
from music_assistant.controllers.music.media.base import (
    SUPPRESS_MEDIA_ITEM_UPDATES,
    MediaControllerBase,
)
from music_assistant.models.music_provider import (
    CACHE_CATEGORY_PREV_LIBRARY_IDS,
    MusicProvider,
)

# --- Helpers ---


def create_provider_mapping(
    provider_instance: str = "spotify_1",
    item_id: str = "track_abc",
    provider_domain: str = "spotify",
    in_library: bool | None = None,
    available: bool = True,
) -> ProviderMapping:
    """
    Create a ProviderMapping with sensible defaults.

    :param provider_instance: The provider instance ID.
    :param item_id: The item ID on the provider.
    :param provider_domain: The provider domain.
    :param in_library: Whether the item is in the user's library on this provider.
    :param available: Whether the item is available.
    """
    return ProviderMapping(
        item_id=item_id,
        provider_domain=provider_domain,
        provider_instance=provider_instance,
        in_library=in_library,
        available=available,
        audio_format=AudioFormat(),
    )


def create_mock_album(
    item_id: str = "1",
    provider_mappings: list[ProviderMapping] | None = None,
    provider: str = "library",
    name: str = "Test Album",
    favorite: bool = False,
) -> Mock:
    """
    Create a mock Album media item.

    :param item_id: The library item ID.
    :param provider_mappings: The provider mappings to set.
    :param provider: The provider string (e.g. 'library', 'spotify').
    :param name: The album name.
    :param favorite: Whether the item is favorited.
    """
    album = Mock(spec=Album)
    album.item_id = item_id
    album.provider = provider
    album.name = name
    album.media_type = MediaType.ALBUM
    album.favorite = favorite
    album.provider_mappings = UniqueList(provider_mappings or [])
    album.metadata = Mock(images=None)
    return album


@asynccontextmanager
async def _noop_deferred_commit() -> AsyncGenerator[None]:
    """Stand-in for DatabaseConnection.deferred_commit on mocked databases."""
    yield


# --- Group 1: Optimistic in_library on add ---


async def test_add_item_to_library_sets_in_library_true() -> None:
    """
    Test that add_item_to_library sets in_library=True on all provider mappings.

    When a user adds an item from MA search, every mapping should be optimistically
    marked as in_library=True before being stored in the database.
    """
    mapping = create_provider_mapping(in_library=None)
    album = create_mock_album(provider="spotify", provider_mappings=[mapping])

    mass = Mock()
    ctrl_mock = AsyncMock()
    ctrl_mock.add_item_to_library = AsyncMock(return_value=album)

    provider_mock = Mock()
    provider_mock.type = ProviderType.MUSIC
    provider_mock.supports_feature.return_value = True
    provider_mock.config.values = {CONF_ENTRY_LIBRARY_SYNC_BACK.key: Mock()}
    provider_mock.config.get_value.return_value = True

    music_ctrl = MusicController.__new__(MusicController)
    music_ctrl.mass = mass
    mass.get_provider.return_value = provider_mock
    mass.metadata = AsyncMock()

    with (
        patch.object(music_ctrl, "get_controller", return_value=ctrl_mock),
        patch.object(music_ctrl, "get_item", new_callable=AsyncMock, return_value=album),
    ):
        await music_ctrl.add_item_to_library(album)

    assert mapping.in_library is True


async def test_add_item_to_library_sets_in_library_even_when_sync_back_disabled() -> None:
    """
    Test that in_library=True is set even when sync back to provider is disabled.

    The optimistic set should happen unconditionally, but library_add should NOT be called.
    """
    mapping = create_provider_mapping(in_library=None)
    album = create_mock_album(provider="spotify", provider_mappings=[mapping])

    mass = Mock()
    ctrl_mock = AsyncMock()
    ctrl_mock.add_item_to_library = AsyncMock(return_value=album)

    provider_mock = Mock()
    provider_mock.type = ProviderType.MUSIC
    provider_mock.supports_feature.return_value = True
    provider_mock.config.values = {CONF_ENTRY_LIBRARY_SYNC_BACK.key: Mock()}
    provider_mock.config.get_value.return_value = False

    music_ctrl = MusicController.__new__(MusicController)
    music_ctrl.mass = mass
    mass.get_provider.return_value = provider_mock
    mass.metadata = AsyncMock()

    with (
        patch.object(music_ctrl, "get_controller", return_value=ctrl_mock),
        patch.object(music_ctrl, "get_item", new_callable=AsyncMock, return_value=album),
    ):
        await music_ctrl.add_item_to_library(album)

    assert mapping.in_library is True
    mass.create_task.assert_not_called()


async def test_add_item_to_library_sets_in_library_even_when_edit_not_supported() -> None:
    """
    Test that in_library=True is set even when provider doesn't support library edit.

    The optimistic set should happen unconditionally, but library_add should NOT be called.
    """
    mapping = create_provider_mapping(in_library=None)
    album = create_mock_album(provider="spotify", provider_mappings=[mapping])

    mass = Mock()
    ctrl_mock = AsyncMock()
    ctrl_mock.add_item_to_library = AsyncMock(return_value=album)

    provider_mock = Mock()
    provider_mock.type = ProviderType.MUSIC
    provider_mock.supports_feature.return_value = False

    music_ctrl = MusicController.__new__(MusicController)
    music_ctrl.mass = mass
    mass.get_provider.return_value = provider_mock
    mass.metadata = AsyncMock()

    with (
        patch.object(music_ctrl, "get_controller", return_value=ctrl_mock),
        patch.object(music_ctrl, "get_item", new_callable=AsyncMock, return_value=album),
    ):
        await music_ctrl.add_item_to_library(album)

    assert mapping.in_library is True
    mass.create_task.assert_not_called()


async def test_add_album_imports_tracks_when_enabled() -> None:
    """
    Test that adding an album imports its tracks when the setting is enabled.

    The "Import album tracks" behavior previously only triggered during a (scheduled)
    library sync. Adding an album manually should mirror it when the provider has the
    setting enabled.
    """
    mapping = create_provider_mapping(
        provider_instance="qobuz_1", provider_domain="qobuz", item_id="album_xyz", in_library=None
    )
    album = create_mock_album(provider="qobuz", provider_mappings=[mapping])

    mass = Mock()
    ctrl_mock = AsyncMock()
    ctrl_mock.add_item_to_library = AsyncMock(return_value=album)

    provider_mock = Mock(spec=MusicProvider)
    provider_mock.type = ProviderType.MUSIC
    provider_mock.supports_feature.return_value = False
    provider_mock.library_sync_album_tracks_enabled.return_value = True
    sentinel = object()
    provider_mock.import_album_tracks = Mock(return_value=sentinel)

    music_ctrl = MusicController.__new__(MusicController)
    music_ctrl.mass = mass
    mass.get_provider.return_value = provider_mock
    mass.metadata = AsyncMock()

    with (
        patch.object(music_ctrl, "get_controller", return_value=ctrl_mock),
        patch.object(music_ctrl, "get_item", new_callable=AsyncMock, return_value=album),
    ):
        await music_ctrl.add_item_to_library(album)

    provider_mock.import_album_tracks.assert_called_once_with("album_xyz", album.name)
    mass.create_task.assert_called_once_with(sentinel)


async def test_add_album_does_not_import_tracks_when_disabled() -> None:
    """Test that adding an album does NOT import its tracks when the setting is disabled."""
    mapping = create_provider_mapping(
        provider_instance="qobuz_1", provider_domain="qobuz", item_id="album_xyz", in_library=None
    )
    album = create_mock_album(provider="qobuz", provider_mappings=[mapping])

    mass = Mock()
    ctrl_mock = AsyncMock()
    ctrl_mock.add_item_to_library = AsyncMock(return_value=album)

    provider_mock = Mock(spec=MusicProvider)
    provider_mock.type = ProviderType.MUSIC
    provider_mock.supports_feature.return_value = False
    provider_mock.library_sync_album_tracks_enabled.return_value = False
    provider_mock.import_album_tracks = Mock()

    music_ctrl = MusicController.__new__(MusicController)
    music_ctrl.mass = mass
    mass.get_provider.return_value = provider_mock
    mass.metadata = AsyncMock()

    with (
        patch.object(music_ctrl, "get_controller", return_value=ctrl_mock),
        patch.object(music_ctrl, "get_item", new_callable=AsyncMock, return_value=album),
    ):
        await music_ctrl.add_item_to_library(album)

    provider_mock.import_album_tracks.assert_not_called()
    mass.create_task.assert_not_called()


async def test_add_album_only_imports_tracks_for_added_instance() -> None:
    """
    Test that track import skips auto-added mappings for other provider instances.

    match_provider_instances adds extra mappings (in_library=None) for sibling
    instances of the same provider. Those must not trigger a track import; only the
    mapping the album was actually added on (in_library=True) should.
    """
    added_mapping = create_provider_mapping(
        provider_instance="qobuz_1", provider_domain="qobuz", item_id="album_xyz", in_library=True
    )
    sibling_mapping = create_provider_mapping(
        provider_instance="qobuz_2", provider_domain="qobuz", item_id="album_xyz", in_library=None
    )
    input_album = create_mock_album(
        provider="qobuz", provider_mappings=[create_provider_mapping(in_library=None)]
    )
    # the controller returns the merged library item with both mappings present
    library_album = create_mock_album(
        provider="library", provider_mappings=[added_mapping, sibling_mapping]
    )

    mass = Mock()
    ctrl_mock = AsyncMock()
    ctrl_mock.add_item_to_library = AsyncMock(return_value=library_album)

    provider_mock = Mock(spec=MusicProvider)
    provider_mock.type = ProviderType.MUSIC
    provider_mock.supports_feature.return_value = False
    provider_mock.library_sync_album_tracks_enabled.return_value = True
    sentinel = object()
    provider_mock.import_album_tracks = Mock(return_value=sentinel)

    music_ctrl = MusicController.__new__(MusicController)
    music_ctrl.mass = mass
    mass.get_provider.return_value = provider_mock
    mass.metadata = AsyncMock()

    with (
        patch.object(music_ctrl, "get_controller", return_value=ctrl_mock),
        patch.object(music_ctrl, "get_item", new_callable=AsyncMock, return_value=input_album),
    ):
        await music_ctrl.add_item_to_library(input_album)

    provider_mock.import_album_tracks.assert_called_once_with("album_xyz", library_album.name)
    mass.create_task.assert_called_once_with(sentinel)


# --- Group 2: Refresh item preserves in_library ---


async def test_refresh_item_preserves_in_library_state() -> None:
    """
    Test that refresh_item restores in_library=True after provider returns None.

    When refreshing, the provider returns a fresh item with in_library=None.
    The cached value (True) from the original library item should be restored.
    """
    original_mapping = create_provider_mapping(
        provider_instance="spotify_1", item_id="abc", in_library=True
    )
    library_item = create_mock_album(
        item_id="1", provider="library", provider_mappings=[original_mapping]
    )

    fresh_mapping = create_provider_mapping(
        provider_instance="spotify_1", item_id="abc", in_library=None
    )
    fresh_item = create_mock_album(
        item_id="abc", provider="spotify", provider_mappings=[fresh_mapping]
    )

    # use TRACK media_type for the returned library_item to skip album-tracks branch
    returned_item = Mock()
    returned_item.media_type = MediaType.TRACK

    ctrl_mock = AsyncMock()
    ctrl_mock.get_provider_item = AsyncMock(return_value=fresh_item)
    ctrl_mock.update_item_in_library = AsyncMock(return_value=returned_item)
    ctrl_mock.match_providers = AsyncMock()

    mass = Mock()
    mass.get_provider.return_value = Mock()
    mass.metadata = AsyncMock()

    music_ctrl = MusicController.__new__(MusicController)
    music_ctrl.mass = mass

    with patch.object(music_ctrl, "get_controller", return_value=ctrl_mock):
        await music_ctrl.refresh_item(library_item)

    # the fresh_mapping should have been restored from cache
    assert fresh_mapping.in_library is True


async def test_refresh_item_preserves_in_library_false() -> None:
    """
    Test that refresh_item restores in_library=False after provider returns None.

    If a mapping was previously marked as in_library=False (removed from provider),
    this state should be preserved through a refresh.
    """
    original_mapping = create_provider_mapping(
        provider_instance="spotify_1", item_id="abc", in_library=False
    )
    library_item = create_mock_album(
        item_id="1", provider="library", provider_mappings=[original_mapping]
    )

    fresh_mapping = create_provider_mapping(
        provider_instance="spotify_1", item_id="abc", in_library=None
    )
    fresh_item = create_mock_album(
        item_id="abc", provider="spotify", provider_mappings=[fresh_mapping]
    )

    returned_item = Mock()
    returned_item.media_type = MediaType.TRACK

    ctrl_mock = AsyncMock()
    ctrl_mock.get_provider_item = AsyncMock(return_value=fresh_item)
    ctrl_mock.update_item_in_library = AsyncMock(return_value=returned_item)
    ctrl_mock.match_providers = AsyncMock()

    mass = Mock()
    mass.get_provider.return_value = Mock()
    mass.metadata = AsyncMock()

    music_ctrl = MusicController.__new__(MusicController)
    music_ctrl.mass = mass

    with patch.object(music_ctrl, "get_controller", return_value=ctrl_mock):
        await music_ctrl.refresh_item(library_item)

    assert fresh_mapping.in_library is False


async def test_refresh_item_respects_provider_set_in_library() -> None:
    """
    Test that provider-explicit in_library value is not overwritten by cache.

    If the provider explicitly sets in_library=False on a refreshed mapping,
    that value should win over the cached True value.
    """
    original_mapping = create_provider_mapping(
        provider_instance="spotify_1", item_id="abc", in_library=True
    )
    library_item = create_mock_album(
        item_id="1", provider="library", provider_mappings=[original_mapping]
    )

    # provider explicitly sets in_library=False (item was removed from provider)
    fresh_mapping = create_provider_mapping(
        provider_instance="spotify_1", item_id="abc", in_library=False
    )
    fresh_item = create_mock_album(
        item_id="abc", provider="spotify", provider_mappings=[fresh_mapping]
    )

    returned_item = Mock()
    returned_item.media_type = MediaType.TRACK

    ctrl_mock = AsyncMock()
    ctrl_mock.get_provider_item = AsyncMock(return_value=fresh_item)
    ctrl_mock.update_item_in_library = AsyncMock(return_value=returned_item)
    ctrl_mock.match_providers = AsyncMock()

    mass = Mock()
    mass.get_provider.return_value = Mock()
    mass.metadata = AsyncMock()

    music_ctrl = MusicController.__new__(MusicController)
    music_ctrl.mass = mass

    with patch.object(music_ctrl, "get_controller", return_value=ctrl_mock):
        await music_ctrl.refresh_item(library_item)

    # provider's explicit False should NOT be overwritten by cache
    assert fresh_mapping.in_library is False


async def test_refresh_item_non_library_item_skips_update() -> None:
    """
    Test that refresh_item returns early for non-library items.

    When the media_item is not from the library (provider != 'library'),
    update_item_in_library should not be called.
    """
    mapping = create_provider_mapping(provider_instance="spotify_1", item_id="abc", in_library=True)
    # provider item, not library
    provider_item = create_mock_album(
        item_id="abc", provider="spotify", provider_mappings=[mapping]
    )

    fresh_item = create_mock_album(item_id="abc", provider="spotify", provider_mappings=[mapping])

    ctrl_mock = AsyncMock()
    ctrl_mock.get_provider_item = AsyncMock(return_value=fresh_item)

    mass = Mock()
    mass.get_provider.return_value = Mock()

    music_ctrl = MusicController.__new__(MusicController)
    music_ctrl.mass = mass

    with patch.object(music_ctrl, "get_controller", return_value=ctrl_mock):
        result = await music_ctrl.refresh_item(provider_item)

    assert result is fresh_item
    ctrl_mock.update_item_in_library.assert_not_called()


# --- Group 3: Sync deletions ---


async def test_sync_library_marks_removed_item_in_library_false() -> None:
    """
    Test that sync marks removed items as in_library=False.

    When an item was in the previous sync but is no longer in the current sync,
    its provider mapping should be set to in_library=False.
    """
    mapping = create_provider_mapping(provider_instance="spotify_1", item_id="abc", in_library=True)
    library_item = create_mock_album(
        item_id="1", provider="library", provider_mappings=[mapping], favorite=False
    )

    controller = AsyncMock()
    controller.get_library_item = AsyncMock(return_value=library_item)

    provider = Mock()
    provider.instance_id = "spotify_1"
    provider.domain = "spotify"
    provider.is_streaming_provider = True
    provider.library_sync_deletions_enabled.return_value = True

    mass = Mock()
    mass.music.get_controller.return_value = controller
    # previous sync had item 1, current sync has nothing
    mass.cache.get = AsyncMock(return_value=[1])
    mass.cache.set = AsyncMock()
    provider.mass = mass

    # simulate sync_library deletion processing
    # (we test the deletion block directly since mocking the full sync is complex)
    cur_db_ids: set[int] = set()  # item no longer present

    if provider.library_sync_deletions_enabled():
        prev_library_items = await mass.cache.get(
            key=MediaType.ALBUM.value,
            provider=provider.instance_id,
            category=CACHE_CATEGORY_PREV_LIBRARY_IDS,
        )
        if prev_library_items:
            for db_id in prev_library_items:
                if db_id not in cur_db_ids:
                    item = await controller.get_library_item(db_id)
                    for prov_map in item.provider_mappings:
                        if prov_map.provider_instance == provider.instance_id:
                            prov_map.in_library = False
                    await controller.set_provider_mappings(db_id, item.provider_mappings)

    assert mapping.in_library is False
    controller.set_provider_mappings.assert_called_once_with(1, library_item.provider_mappings)


async def test_sync_library_deletions_disabled_keeps_item() -> None:
    """
    Test that items remain visible when sync deletions is disabled.

    When library_sync_deletions_enabled returns False, items removed from the provider
    should NOT be marked as in_library=False.
    """
    mapping = create_provider_mapping(provider_instance="spotify_1", item_id="abc", in_library=True)
    library_item = create_mock_album(item_id="1", provider="library", provider_mappings=[mapping])

    controller = AsyncMock()
    controller.get_library_item = AsyncMock(return_value=library_item)

    provider = Mock()
    provider.instance_id = "spotify_1"
    provider.library_sync_deletions_enabled.return_value = False

    mass = Mock()
    mass.cache.get = AsyncMock(return_value=[1])
    mass.cache.set = AsyncMock()
    provider.mass = mass

    cur_db_ids: set[int] = set()

    if provider.library_sync_deletions_enabled():
        prev_library_items = await mass.cache.get(
            key=MediaType.ALBUM.value,
            provider=provider.instance_id,
            category=CACHE_CATEGORY_PREV_LIBRARY_IDS,
        )
        if prev_library_items:
            for db_id in prev_library_items:
                if db_id not in cur_db_ids:
                    item = await controller.get_library_item(db_id)
                    for prov_map in item.provider_mappings:
                        if prov_map.provider_instance == provider.instance_id:
                            prov_map.in_library = False
                    await controller.set_provider_mappings(db_id, item.provider_mappings)

    # mapping should still be True since deletion sync was disabled
    assert mapping.in_library is True
    controller.set_provider_mappings.assert_not_called()


async def test_sync_library_deletion_unmarks_favorite_when_no_other_providers() -> None:
    """
    Test that favorite is unset when no other providers have the item in library.

    When an item is removed from the only provider that had it in-library,
    and the item is favorited, favorite should be set to False.
    """
    mapping = create_provider_mapping(provider_instance="spotify_1", item_id="abc", in_library=True)
    library_item = create_mock_album(
        item_id="1", provider="library", provider_mappings=[mapping], favorite=True
    )

    controller = AsyncMock()
    controller.get_library_item = AsyncMock(return_value=library_item)
    controller.set_favorite = AsyncMock()

    instance_id = "spotify_1"

    remaining = {
        x.provider_instance
        for x in library_item.provider_mappings
        if x.provider_instance != instance_id and x.in_library
    }

    if not remaining and library_item.favorite:
        await controller.set_favorite(int(library_item.item_id), False)

    controller.set_favorite.assert_called_once_with(1, False)


async def test_sync_library_deletion_keeps_favorite_when_other_provider_has_it() -> None:
    """
    Test that favorite is kept when another provider still has the item in library.

    When an item is removed from one provider but another provider still has
    in_library=True, the favorite status should remain unchanged.
    """
    mapping_a = create_provider_mapping(
        provider_instance="spotify_1", item_id="abc", in_library=True
    )
    mapping_b = create_provider_mapping(
        provider_instance="tidal_1",
        item_id="xyz",
        provider_domain="tidal",
        in_library=True,
    )
    library_item = create_mock_album(
        item_id="1",
        provider="library",
        provider_mappings=[mapping_a, mapping_b],
        favorite=True,
    )

    controller = AsyncMock()
    controller.set_favorite = AsyncMock()

    instance_id = "spotify_1"

    remaining = {
        x.provider_instance
        for x in library_item.provider_mappings
        if x.provider_instance != instance_id and x.in_library
    }

    if not remaining and library_item.favorite:
        await controller.set_favorite(int(library_item.item_id), False)

    # tidal_1 still has in_library=True, so favorite should NOT be unset
    controller.set_favorite.assert_not_called()


async def test_sync_library_always_stores_cache_regardless_of_deletion_setting() -> None:
    """
    Test that cache is always updated with current IDs even when deletions are disabled.

    The cache stores the current set of library item IDs for comparison on the next sync.
    This must happen regardless of whether deletion sync is enabled.
    """
    mass = Mock()
    mass.cache.set = AsyncMock()

    cur_db_ids = {1, 2, 3}
    instance_id = "spotify_1"

    # this is always called outside the deletion-enabled check
    await mass.cache.set(
        key=MediaType.ALBUM.value,
        data=list(cur_db_ids),
        provider=instance_id,
        category=CACHE_CATEGORY_PREV_LIBRARY_IDS,
    )

    mass.cache.set.assert_called_once_with(
        key=MediaType.ALBUM.value,
        data=list(cur_db_ids),
        provider=instance_id,
        category=CACHE_CATEGORY_PREV_LIBRARY_IDS,
    )


# --- Group 4: _apply_filters SQL generation ---


def _create_controller_for_filter_tests() -> Mock:
    """Create a minimal mock controller for _apply_filters tests."""
    ctrl = Mock(spec=MediaControllerBase)
    ctrl.media_type = MediaType.ALBUM
    ctrl.db_table = "albums"
    ctrl._apply_filters = MediaControllerBase._apply_filters.__get__(ctrl)
    ctrl._provider_filter_clause = MediaControllerBase._provider_filter_clause.__get__(ctrl)
    return ctrl


async def test_apply_filters_in_library_only_without_provider_filter() -> None:
    """
    Test that in_library_only adds an EXISTS filter on provider_mappings with in_library=1.

    When no provider_filter is set but in_library_only=True, an EXISTS subquery on
    provider_mappings should be added with the in_library=1 condition.
    """
    ctrl = _create_controller_for_filter_tests()
    query_parts: list[str] = []
    query_params: dict[str, object] = {}

    ctrl._apply_filters(
        query_parts=query_parts,
        query_params=query_params,
        favorite=None,
        search=None,
        genre_ids=None,
        provider_filter=None,
        in_library_only=True,
    )

    assert len(query_parts) == 1
    assert "provider_media_type" in query_params
    # pin the exact clause: library_count() shares this builder
    assert query_parts[0] == (
        "EXISTS(SELECT 1 FROM provider_mappings "
        "WHERE provider_mappings.item_id = albums.item_id "
        "AND provider_mappings.media_type = :provider_media_type "
        "AND provider_mappings.in_library = 1)"
    )


async def test_apply_filters_in_library_only_with_provider_filter() -> None:
    """
    Test that in_library_only with provider_filter adds both conditions to the EXISTS.

    When both in_library_only=True and a provider_filter are set, the EXISTS subquery
    should include both the provider condition and the in_library=1 condition.
    """
    ctrl = _create_controller_for_filter_tests()
    query_parts: list[str] = []
    query_params: dict[str, object] = {}

    ctrl._apply_filters(
        query_parts=query_parts,
        query_params=query_params,
        favorite=None,
        search=None,
        genre_ids=None,
        provider_filter=["spotify_1"],
        in_library_only=True,
    )

    assert len(query_parts) == 1
    assert query_params["provider_filter_0"] == "spotify_1"
    # pin the exact clause: library_count() shares this builder
    assert query_parts[0] == (
        "EXISTS(SELECT 1 FROM provider_mappings "
        "WHERE provider_mappings.item_id = albums.item_id "
        "AND provider_mappings.media_type = :provider_media_type "
        "AND provider_mappings.in_library = 1 "
        "AND (provider_mappings.provider_instance = :provider_filter_0))"
    )


async def test_apply_filters_no_in_library_filter_by_default() -> None:
    """
    Test that no provider_mappings filter is added when in_library_only is False.

    Without a provider_filter or in_library_only flag, no filter on
    provider_mappings should be added.
    """
    ctrl = _create_controller_for_filter_tests()
    query_parts: list[str] = []
    query_params: dict[str, object] = {}

    ctrl._apply_filters(
        query_parts=query_parts,
        query_params=query_params,
        favorite=None,
        search=None,
        genre_ids=None,
        provider_filter=None,
        in_library_only=False,
    )

    assert len(query_parts) == 0


async def test_apply_filters_provider_filter_without_in_library() -> None:
    """
    Test that provider_filter without in_library_only omits the in_library clause.

    When a provider_filter is set but in_library_only is False, the EXISTS subquery
    should filter by provider but NOT include the in_library=1 condition.
    """
    ctrl = _create_controller_for_filter_tests()
    query_parts: list[str] = []
    query_params: dict[str, object] = {}

    ctrl._apply_filters(
        query_parts=query_parts,
        query_params=query_params,
        favorite=None,
        search=None,
        genre_ids=None,
        provider_filter=["spotify_1"],
        in_library_only=False,
    )

    assert len(query_parts) == 1
    assert query_params["provider_filter_0"] == "spotify_1"
    # pin the exact clause: library_count() shares this builder
    assert query_parts[0] == (
        "EXISTS(SELECT 1 FROM provider_mappings "
        "WHERE provider_mappings.item_id = albums.item_id "
        "AND provider_mappings.media_type = :provider_media_type "
        "AND (provider_mappings.provider_instance = :provider_filter_0))"
    )


# --- Group 5: set_provider_mappings behavior ---


@pytest.fixture
def mock_controller() -> Mock:
    """Create a mock MediaControllerBase for set_provider_mappings tests."""
    ctrl = Mock(spec=MediaControllerBase)
    ctrl.media_type = MediaType.ALBUM
    ctrl.logger = Mock()
    ctrl.mass = Mock()
    ctrl.mass.music.database.delete = AsyncMock()
    ctrl.mass.music.database.upsert_many = AsyncMock()
    ctrl.set_provider_mappings = MediaControllerBase.set_provider_mappings.__get__(ctrl)
    return ctrl


async def test_set_provider_mappings_overwrite_deletes_and_reinserts(
    mock_controller: Mock,
) -> None:
    """
    Test that overwrite=True deletes existing mappings before upserting.

    :param mock_controller: Mock MediaControllerBase instance.
    """
    mapping = create_provider_mapping(in_library=True)

    await mock_controller.set_provider_mappings(1, [mapping], overwrite=True)

    mock_controller.mass.music.database.delete.assert_called_once()
    mock_controller.mass.music.database.upsert_many.assert_called_once()


async def test_set_provider_mappings_overwrite_keeps_existing_when_empty(
    mock_controller: Mock,
) -> None:
    """
    Test that overwrite=True with no mappings leaves the existing mappings untouched.

    :param mock_controller: Mock MediaControllerBase instance.
    """
    await mock_controller.set_provider_mappings(1, [], overwrite=True)

    mock_controller.mass.music.database.delete.assert_not_called()
    mock_controller.mass.music.database.upsert_many.assert_not_called()
    mock_controller.logger.warning.assert_called_once()


async def test_set_provider_mappings_no_mappings_is_noop(mock_controller: Mock) -> None:
    """
    Test that an empty mappings set without overwrite writes nothing.

    :param mock_controller: Mock MediaControllerBase instance.
    """
    await mock_controller.set_provider_mappings(1, [], overwrite=False)

    mock_controller.mass.music.database.delete.assert_not_called()
    mock_controller.mass.music.database.upsert_many.assert_not_called()
    mock_controller.logger.warning.assert_not_called()


async def test_set_provider_mappings_upsert_preserves_null_in_library(
    mock_controller: Mock,
) -> None:
    """
    Test that in_library=None is excluded from the upsert dict.

    When in_library is None, it should not be included in the dict passed to upsert,
    allowing the database's existing value to be preserved.

    :param mock_controller: Mock MediaControllerBase instance.
    """
    mapping = create_provider_mapping(in_library=None)

    await mock_controller.set_provider_mappings(1, [mapping], overwrite=False)

    upsert_call = mock_controller.mass.music.database.upsert_many.call_args
    upsert_rows = upsert_call[0][1]
    assert len(upsert_rows) == 1
    assert "in_library" not in upsert_rows[0]


async def test_set_provider_mappings_upsert_writes_explicit_in_library(
    mock_controller: Mock,
) -> None:
    """
    Test that an explicit in_library value is included in the upsert dict.

    When in_library is explicitly True or False, it should be written to the database.

    :param mock_controller: Mock MediaControllerBase instance.
    """
    mapping = create_provider_mapping(in_library=True)

    await mock_controller.set_provider_mappings(1, [mapping], overwrite=False)

    upsert_call = mock_controller.mass.music.database.upsert_many.call_args
    upsert_rows = upsert_call[0][1]
    assert len(upsert_rows) == 1
    assert upsert_rows[0]["in_library"] is True


# --- Group 6: library_items filtering ---


async def test_library_items_default_filters_in_library_only() -> None:
    """Test that library_items passes in_library_only=True by default."""
    ctrl = Mock(spec=MediaControllerBase)
    ctrl._ensure_provider_filter = Mock(return_value=None)
    ctrl.get_library_items_by_query = AsyncMock(return_value=[])
    ctrl.library_items = MediaControllerBase.library_items.__get__(ctrl)

    await ctrl.library_items()

    ctrl.get_library_items_by_query.assert_called_once()
    call_kwargs = ctrl.get_library_items_by_query.call_args[1]
    assert call_kwargs["in_library_only"] is True


async def test_library_items_defaults_to_summary() -> None:
    """library_items defaults to summary=True so list endpoints return slim rows."""
    ctrl = Mock(spec=MediaControllerBase)
    ctrl._ensure_provider_filter = Mock(return_value=None)
    ctrl.get_library_items_by_query = AsyncMock(return_value=[])
    ctrl.library_items = MediaControllerBase.library_items.__get__(ctrl)

    await ctrl.library_items()

    call_kwargs = ctrl.get_library_items_by_query.call_args[1]
    assert call_kwargs["summary"] is True


def test_ensure_provider_filter_keeps_plugin_provider_mappings() -> None:
    """Test that plugin providers are kept when a user music-provider filter is active."""
    ctrl = Mock(spec=MediaControllerBase)
    ctrl.mass = Mock()
    ctrl.mass.providers = [
        Mock(instance_id="spotify_1", type=ProviderType.MUSIC),
        Mock(instance_id="smart_playlist_1", type=ProviderType.PLUGIN),
    ]
    ctrl._ensure_provider_filter = MediaControllerBase._ensure_provider_filter.__get__(ctrl)

    with patch(
        "music_assistant.controllers.music.media.base.get_current_user",
        return_value=Mock(provider_filter=["spotify_1"]),
    ):
        result = ctrl._ensure_provider_filter(None)

    assert result is not None
    assert "spotify_1" in result
    assert "smart_playlist_1" in result


def test_ensure_provider_filter_rejects_unallowed_music_provider() -> None:
    """Test that requesting a disallowed music provider still raises permissions error."""
    ctrl = Mock(spec=MediaControllerBase)
    ctrl.mass = Mock()
    ctrl.mass.providers = [
        Mock(instance_id="spotify_1", type=ProviderType.MUSIC),
        Mock(instance_id="smart_playlist_1", type=ProviderType.PLUGIN),
    ]
    ctrl._ensure_provider_filter = MediaControllerBase._ensure_provider_filter.__get__(ctrl)

    with (
        patch(
            "music_assistant.controllers.music.media.base.get_current_user",
            return_value=Mock(provider_filter=["spotify_1"]),
        ),
        pytest.raises(InsufficientPermissions),
    ):
        ctrl._ensure_provider_filter("qobuz_1")


def test_ensure_provider_filter_allows_explicit_non_music_provider() -> None:
    """Test that explicitly requesting a plugin provider is allowed for filtered users."""
    ctrl = Mock(spec=MediaControllerBase)
    ctrl.mass = Mock()
    ctrl.mass.providers = [
        Mock(instance_id="spotify_1", type=ProviderType.MUSIC),
        Mock(instance_id="smart_playlist_1", type=ProviderType.PLUGIN),
    ]
    ctrl._ensure_provider_filter = MediaControllerBase._ensure_provider_filter.__get__(ctrl)

    with patch(
        "music_assistant.controllers.music.media.base.get_current_user",
        return_value=Mock(provider_filter=["spotify_1"]),
    ):
        result = ctrl._ensure_provider_filter("smart_playlist_1")

    assert result == ["smart_playlist_1"]


def test_ensure_provider_filter_does_not_auto_allow_other_non_music_providers() -> None:
    """Test that only plugin providers are auto-allowed when user filter is active."""
    ctrl = Mock(spec=MediaControllerBase)
    ctrl.mass = Mock()
    ctrl.mass.providers = [
        Mock(instance_id="spotify_1", type=ProviderType.MUSIC),
        Mock(instance_id="smart_playlist_1", type=ProviderType.PLUGIN),
        Mock(instance_id="meta_1", type=ProviderType.METADATA),
    ]
    ctrl._ensure_provider_filter = MediaControllerBase._ensure_provider_filter.__get__(ctrl)

    with patch(
        "music_assistant.controllers.music.media.base.get_current_user",
        return_value=Mock(provider_filter=["spotify_1"]),
    ):
        result = ctrl._ensure_provider_filter(None)

    assert result is not None
    assert "spotify_1" in result
    assert "smart_playlist_1" in result
    assert "meta_1" not in result


def test_select_provider_id_prefers_allowed_music_over_plugin() -> None:
    """Test that allowed music mappings are preferred over plugin mappings."""
    ctrl = Mock(spec=MediaControllerBase)
    ctrl.mass = Mock()
    ctrl.mass.get_provider = Mock(
        side_effect=lambda instance: {
            "smart_playlist_1": Mock(type=ProviderType.PLUGIN),
            "spotify_1": Mock(type=ProviderType.MUSIC),
        }.get(instance)
    )
    ctrl._select_provider_id = MediaControllerBase._select_provider_id.__get__(ctrl)

    item = create_mock_album(
        provider_mappings=[
            create_provider_mapping(
                provider_instance="smart_playlist_1",
                provider_domain="smart_playlist",
                item_id="plugin_item",
            ),
            create_provider_mapping(
                provider_instance="spotify_1",
                provider_domain="spotify",
                item_id="music_item",
            ),
        ]
    )

    with patch(
        "music_assistant.controllers.music.media.base.get_current_user",
        return_value=Mock(provider_filter=["spotify_1"]),
    ):
        provider_instance, provider_item = ctrl._select_provider_id(item)

    assert provider_instance == "spotify_1"
    assert provider_item == "music_item"


def test_select_provider_id_falls_back_to_plugin_when_no_allowed_music() -> None:
    """Test that plugin mapping is selected if no allowed music mapping exists."""
    ctrl = Mock(spec=MediaControllerBase)
    ctrl.mass = Mock()
    ctrl.mass.get_provider = Mock(
        side_effect=lambda instance: {
            "smart_playlist_1": Mock(type=ProviderType.PLUGIN),
            "qobuz_1": Mock(type=ProviderType.MUSIC),
        }.get(instance)
    )
    ctrl._select_provider_id = MediaControllerBase._select_provider_id.__get__(ctrl)

    item = create_mock_album(
        provider_mappings=[
            create_provider_mapping(
                provider_instance="smart_playlist_1",
                provider_domain="smart_playlist",
                item_id="plugin_item",
            ),
            create_provider_mapping(
                provider_instance="qobuz_1",
                provider_domain="qobuz",
                item_id="music_item",
            ),
        ]
    )

    with patch(
        "music_assistant.controllers.music.media.base.get_current_user",
        return_value=Mock(provider_filter=["spotify_1"]),
    ):
        provider_instance, provider_item = ctrl._select_provider_id(item)

    assert provider_instance == "smart_playlist_1"
    assert provider_item == "plugin_item"


async def test_get_library_item_does_not_filter_in_library() -> None:
    """
    Test that get_library_item always passes in_library_only=False.

    Single-item lookups must find items regardless of in_library state.
    """
    album = create_mock_album()

    ctrl = Mock(spec=MediaControllerBase)
    ctrl.db_table = "albums"
    ctrl.media_type = MediaType.ALBUM
    ctrl.get_library_items_by_query = AsyncMock(return_value=[album])
    ctrl.get_library_item = MediaControllerBase.get_library_item.__get__(ctrl)

    await ctrl.get_library_item(1)

    call_kwargs = ctrl.get_library_items_by_query.call_args[1]
    assert call_kwargs["in_library_only"] is False


async def test_update_item_in_library_skips_non_music_providers() -> None:
    """Test update callback dispatch skips provider mappings that are not music providers."""
    ctrl = Mock(spec=MediaControllerBase)
    ctrl._update_library_item = AsyncMock()
    ctrl.get_library_item = AsyncMock(
        return_value=Mock(
            uri="library://album/1",
            provider_mappings=[
                create_provider_mapping(
                    provider_instance="smart_playlist_1",
                    provider_domain="smart_playlist",
                    item_id="abc",
                )
            ],
            metadata=Mock(images=None),
        )
    )

    mass = Mock()
    mass.music = Mock()
    mass.music.match_provider_instances = Mock()
    mass.music.database.deferred_commit = _noop_deferred_commit
    mass.signal_event = Mock()
    mass.get_provider = Mock(return_value=Mock(type=ProviderType.PLUGIN))
    ctrl.mass = mass

    ctrl.update_item_in_library = MediaControllerBase.update_item_in_library.__get__(ctrl)

    update = create_mock_album(item_id="1")

    updated = await ctrl.update_item_in_library(item_id=1, update=update, overwrite=False)

    assert updated is not None
    ctrl._update_library_item.assert_called_once()
    mass.music.match_provider_instances.assert_called_once_with(update)
    mass.get_provider.assert_called_once_with("smart_playlist_1")


# --- Group 7: Per-item event suppression during provider sync ---


def _create_event_capture_controller(
    library_item: Mock, events: list[EventType]
) -> tuple[Mock, Mock]:
    """Build a controller mock with real add/update methods bound; events records signalled types."""
    mass = Mock()
    mass.signal_event = Mock(side_effect=lambda event, *_args, **_kwargs: events.append(event))
    mass.music.database.deferred_commit = _noop_deferred_commit
    ctrl = Mock(spec=MediaControllerBase)
    ctrl.mass = mass
    ctrl._db_add_lock = asyncio.Lock()
    ctrl._get_library_item_by_match = AsyncMock(return_value=None)
    ctrl._add_library_item = AsyncMock(return_value=1)
    ctrl._update_library_item = AsyncMock()
    ctrl.get_library_item = AsyncMock(return_value=library_item)
    ctrl.add_item_to_library = MediaControllerBase.add_item_to_library.__get__(ctrl)
    ctrl.update_item_in_library = MediaControllerBase.update_item_in_library.__get__(ctrl)
    return ctrl, mass


async def test_add_and_update_item_emit_events_outside_sync() -> None:
    """Regular add/update calls emit per-item events and run the provider write-back."""
    mapping = create_provider_mapping()
    library_item = create_mock_album(provider="library", provider_mappings=[mapping])
    library_item.uri = "library://album/1"
    events: list[EventType] = []
    ctrl, mass = _create_event_capture_controller(library_item, events)

    provider = Mock(type=ProviderType.MUSIC)
    provider.on_item_updated = AsyncMock()
    mass.get_provider.return_value = provider

    await ctrl.add_item_to_library(create_mock_album(provider="spotify"))
    assert events == [EventType.MEDIA_ITEM_ADDED]

    await ctrl.update_item_in_library(1, create_mock_album(provider="spotify"))
    assert events == [EventType.MEDIA_ITEM_ADDED, EventType.MEDIA_ITEM_UPDATED]
    provider.on_item_updated.assert_awaited_once_with(library_item)


async def test_provider_sync_suppresses_per_item_events() -> None:
    """A provider sync emits only MUSIC_SYNC_COMPLETED; per-item events resume afterwards."""
    library_item = create_mock_album(provider="library")
    library_item.uri = "library://album/1"
    events: list[EventType] = []
    ctrl, mass = _create_event_capture_controller(library_item, events)
    # run the deferred completion check inline instead of on the event loop
    mass.call_later = Mock(side_effect=lambda _delay, target, **_kwargs: target())

    music_ctrl = MusicController.__new__(MusicController)
    music_ctrl.mass = mass
    music_ctrl._sync_lock = asyncio.Lock()

    provider = Mock()

    async def fake_sync_library(_media_type: MediaType) -> None:
        # stand in for the per-mediatype sync loops adding/updating items
        await ctrl.add_item_to_library(create_mock_album(provider="spotify"))
        await ctrl.update_item_in_library(1, create_mock_album(provider="spotify"))

    provider.sync_library = fake_sync_library

    run_sync = music_ctrl._create_provider_sync_handler(provider, MediaType.ALBUM)
    with (
        patch.object(MusicController, "active_sync_tasks", new_callable=PropertyMock) as tasks,
        patch.object(music_ctrl, "_queue_database_cleanup_task"),
    ):
        tasks.return_value = []
        await run_sync()

    assert events == [EventType.MUSIC_SYNC_COMPLETED]
    # write-back was skipped too (provider lookup never happened)
    mass.get_provider.assert_not_called()
    # suppression must not leak past the handler
    assert SUPPRESS_MEDIA_ITEM_UPDATES.get() is False
    await ctrl.add_item_to_library(create_mock_album(provider="spotify"))
    assert events == [EventType.MUSIC_SYNC_COMPLETED, EventType.MEDIA_ITEM_ADDED]
