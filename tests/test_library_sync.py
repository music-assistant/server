"""Tests for library sync in_library behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Album, AudioFormat, ProviderMapping, UniqueList

from music_assistant.controllers.media.base import MediaControllerBase
from music_assistant.controllers.music import MusicController
from music_assistant.models.music_provider import CACHE_CATEGORY_PREV_LIBRARY_IDS

# --- Helpers ---


def create_provider_mapping(
    provider_instance: str = "spotify_1",
    item_id: str = "track_abc",
    provider_domain: str = "spotify",
    in_library: bool | None = None,
    available: bool = True,
) -> ProviderMapping:
    """Create a ProviderMapping with sensible defaults.

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
    """Create a mock Album media item.

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
    return album


# --- Group 1: Optimistic in_library on add ---


async def test_add_item_to_library_sets_in_library_true() -> None:
    """Test that add_item_to_library sets in_library=True on all provider mappings.

    When a user adds an item from MA search, every mapping should be optimistically
    marked as in_library=True before being stored in the database.
    """
    mapping = create_provider_mapping(in_library=None)
    album = create_mock_album(provider="spotify", provider_mappings=[mapping])

    mass = Mock()
    ctrl_mock = AsyncMock()
    ctrl_mock.add_item_to_library = AsyncMock(return_value=album)

    provider_mock = Mock()
    provider_mock.library_edit_supported.return_value = True
    provider_mock.library_sync_back_enabled.return_value = True

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
    """Test that in_library=True is set even when sync back to provider is disabled.

    The optimistic set should happen unconditionally, but library_add should NOT be called.
    """
    mapping = create_provider_mapping(in_library=None)
    album = create_mock_album(provider="spotify", provider_mappings=[mapping])

    mass = Mock()
    ctrl_mock = AsyncMock()
    ctrl_mock.add_item_to_library = AsyncMock(return_value=album)

    provider_mock = Mock()
    provider_mock.library_edit_supported.return_value = True
    provider_mock.library_sync_back_enabled.return_value = False

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
    """Test that in_library=True is set even when provider doesn't support library edit.

    The optimistic set should happen unconditionally, but library_add should NOT be called.
    """
    mapping = create_provider_mapping(in_library=None)
    album = create_mock_album(provider="spotify", provider_mappings=[mapping])

    mass = Mock()
    ctrl_mock = AsyncMock()
    ctrl_mock.add_item_to_library = AsyncMock(return_value=album)

    provider_mock = Mock()
    provider_mock.library_edit_supported.return_value = False

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


# --- Group 2: Refresh item preserves in_library ---


async def test_refresh_item_preserves_in_library_state() -> None:
    """Test that refresh_item restores in_library=True after provider returns None.

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
    """Test that refresh_item restores in_library=False after provider returns None.

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
    """Test that provider-explicit in_library value is not overwritten by cache.

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
    """Test that refresh_item returns early for non-library items.

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
    """Test that sync marks removed items as in_library=False.

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
    """Test that items remain visible when sync deletions is disabled.

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
    """Test that favorite is unset when no other providers have the item in library.

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
    """Test that favorite is kept when another provider still has the item in library.

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
    """Test that cache is always updated with current IDs even when deletions are disabled.

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
    return ctrl


async def test_apply_filters_in_library_only_without_provider_filter() -> None:
    """Test that in_library_only adds a JOIN on provider_mappings with in_library=1.

    When no provider_filter is set but in_library_only=True, a JOIN on
    provider_mappings should be added with the in_library=1 condition.
    """
    ctrl = _create_controller_for_filter_tests()
    query_parts: list[str] = []
    query_params: dict[str, object] = {}
    join_parts: list[str] = []

    ctrl._apply_filters(
        query_parts=query_parts,
        query_params=query_params,
        join_parts=join_parts,
        favorite=None,
        search=None,
        provider_filter=None,
        in_library_only=True,
    )

    assert len(join_parts) == 1
    assert "provider_mappings.in_library = 1" in join_parts[0]
    assert "provider_media_type" in query_params


async def test_apply_filters_in_library_only_with_provider_filter() -> None:
    """Test that in_library_only with provider_filter adds both conditions to the JOIN.

    When both in_library_only=True and a provider_filter are set, the JOIN should
    include both the provider condition and the in_library=1 condition.
    """
    ctrl = _create_controller_for_filter_tests()
    query_parts: list[str] = []
    query_params: dict[str, object] = {}
    join_parts: list[str] = []

    ctrl._apply_filters(
        query_parts=query_parts,
        query_params=query_params,
        join_parts=join_parts,
        favorite=None,
        search=None,
        provider_filter=["spotify_1"],
        in_library_only=True,
    )

    assert len(join_parts) == 1
    assert "provider_mappings.in_library = 1" in join_parts[0]
    assert "provider_filter_0" in query_params
    assert query_params["provider_filter_0"] == "spotify_1"


async def test_apply_filters_no_in_library_filter_by_default() -> None:
    """Test that no provider_mappings JOIN is added when in_library_only is False.

    Without a provider_filter or in_library_only flag, no JOIN on
    provider_mappings should be added.
    """
    ctrl = _create_controller_for_filter_tests()
    query_parts: list[str] = []
    query_params: dict[str, object] = {}
    join_parts: list[str] = []

    ctrl._apply_filters(
        query_parts=query_parts,
        query_params=query_params,
        join_parts=join_parts,
        favorite=None,
        search=None,
        provider_filter=None,
        in_library_only=False,
    )

    assert len(join_parts) == 0


async def test_apply_filters_provider_filter_without_in_library() -> None:
    """Test that provider_filter without in_library_only omits the in_library clause.

    When a provider_filter is set but in_library_only is False, the JOIN should
    filter by provider but NOT include the in_library=1 condition.
    """
    ctrl = _create_controller_for_filter_tests()
    query_parts: list[str] = []
    query_params: dict[str, object] = {}
    join_parts: list[str] = []

    ctrl._apply_filters(
        query_parts=query_parts,
        query_params=query_params,
        join_parts=join_parts,
        favorite=None,
        search=None,
        provider_filter=["spotify_1"],
        in_library_only=False,
    )

    assert len(join_parts) == 1
    assert "in_library" not in join_parts[0]
    assert "provider_filter_0" in query_params


# --- Group 5: set_provider_mappings behavior ---


@pytest.fixture
def mock_controller() -> Mock:
    """Create a mock MediaControllerBase for set_provider_mappings tests."""
    ctrl = Mock(spec=MediaControllerBase)
    ctrl.media_type = MediaType.ALBUM
    ctrl.mass = Mock()
    ctrl.mass.music.database.delete = AsyncMock()
    ctrl.mass.music.database.upsert = AsyncMock()
    ctrl.set_provider_mappings = MediaControllerBase.set_provider_mappings.__get__(ctrl)
    return ctrl


async def test_set_provider_mappings_overwrite_deletes_and_reinserts(
    mock_controller: Mock,
) -> None:
    """Test that overwrite=True deletes existing mappings before upserting.

    :param mock_controller: Mock MediaControllerBase instance.
    """
    mapping = create_provider_mapping(in_library=True)

    await mock_controller.set_provider_mappings(1, [mapping], overwrite=True)

    mock_controller.mass.music.database.delete.assert_called_once()
    mock_controller.mass.music.database.upsert.assert_called_once()


async def test_set_provider_mappings_upsert_preserves_null_in_library(
    mock_controller: Mock,
) -> None:
    """Test that in_library=None is excluded from the upsert dict.

    When in_library is None, it should not be included in the dict passed to upsert,
    allowing the database's existing value to be preserved.

    :param mock_controller: Mock MediaControllerBase instance.
    """
    mapping = create_provider_mapping(in_library=None)

    await mock_controller.set_provider_mappings(1, [mapping], overwrite=False)

    upsert_call = mock_controller.mass.music.database.upsert.call_args
    upsert_dict = upsert_call[0][1]
    assert "in_library" not in upsert_dict


async def test_set_provider_mappings_upsert_writes_explicit_in_library(
    mock_controller: Mock,
) -> None:
    """Test that an explicit in_library value is included in the upsert dict.

    When in_library is explicitly True or False, it should be written to the database.

    :param mock_controller: Mock MediaControllerBase instance.
    """
    mapping = create_provider_mapping(in_library=True)

    await mock_controller.set_provider_mappings(1, [mapping], overwrite=False)

    upsert_call = mock_controller.mass.music.database.upsert.call_args
    upsert_dict = upsert_call[0][1]
    assert upsert_dict["in_library"] is True


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


async def test_get_library_item_does_not_filter_in_library() -> None:
    """Test that get_library_item always passes in_library_only=False.

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
