"""Tests for sync deletion logic: hard-delete vs soft-delete behavior."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import ProviderMapping, Track

from music_assistant.controllers.music import MusicController


def _make_track(
    db_id: int,
    provider_mappings: set[ProviderMapping],
    favorite: bool = False,
) -> Track:
    """Create a Track with the given provider mappings for testing."""
    return Track(
        item_id=str(db_id),
        provider="library",
        name=f"Track {db_id}",
        provider_mappings=provider_mappings,
        favorite=favorite,
    )


def _make_prov_mapping(
    item_id: str,
    provider_domain: str,
    provider_instance: str,
    in_library: bool | None = None,
) -> ProviderMapping:
    """Create a ProviderMapping for testing."""
    return ProviderMapping(
        item_id=item_id,
        provider_domain=provider_domain,
        provider_instance=provider_instance,
        in_library=in_library,
    )


@pytest.fixture
def media_controller() -> AsyncMock:
    """Return a mock media type controller (e.g. TracksController)."""
    return AsyncMock()


@pytest.fixture
def music_controller(media_controller: AsyncMock) -> MusicController:
    """Return a MusicController wired to use the mock media_controller."""
    mass = MagicMock()
    mass.config.get_raw_core_config_value.return_value = "GLOBAL"
    ctrl = MusicController(mass)
    ctrl.get_controller = lambda _media_type: media_controller  # type: ignore[assignment]
    return ctrl


async def test_sync_removes_synced_item_hard_delete(
    music_controller: MusicController,
    media_controller: AsyncMock,
) -> None:
    """Provider sync removes a synced item -> hard-deleted from DB.

    Item was synced (prov_map.in_library=True), then removed from provider.
    After soft-delete sets in_library=False, _process_sync_deletions should
    call remove_item_from_library().
    """
    track = _make_track(
        db_id=1,
        provider_mappings={
            _make_prov_mapping("sp_track_1", "spotify", "spotify_1", in_library=False),
        },
    )
    # spotify_1 no longer has this item after sync
    music_controller.mass.cache.get = AsyncMock(return_value=None)  # type: ignore[method-assign]
    # DB still has the item with its (now soft-deleted) provider mapping
    media_controller.get_library_item = AsyncMock(return_value=track)

    await music_controller._process_sync_deletions(
        provider_instance_id="spotify_1",
        media_type=MediaType.TRACK,
        prev_library_items=[1],
    )

    media_controller.remove_item_from_library.assert_called_once_with(1)


async def test_manually_added_item_not_in_prev_ids(
    music_controller: MusicController,
    media_controller: AsyncMock,
) -> None:
    """Manually-added items (in_library=False) are never in prev_library_items.

    Items added via MA search have in_library=0. They're never in the provider's
    library, so they never appear in cur_db_ids or prev_library_items.
    The sync deletion logic never processes them.
    """
    # spotify_1 has no items after sync
    music_controller.mass.cache.get = AsyncMock(return_value=None)  # type: ignore[method-assign]

    await music_controller._process_sync_deletions(
        provider_instance_id="spotify_1",
        media_type=MediaType.TRACK,
        prev_library_items=[],
    )

    media_controller.get_library_item.assert_not_called()
    media_controller.remove_item_from_library.assert_not_called()


async def test_manually_added_item_none_in_library(
    music_controller: MusicController,
    media_controller: AsyncMock,
) -> None:
    """Same as above but with in_library=None (default for new ProviderMapping)."""
    # spotify_1 has no items after sync
    music_controller.mass.cache.get = AsyncMock(return_value=None)  # type: ignore[method-assign]

    await music_controller._process_sync_deletions(
        provider_instance_id="spotify_1",
        media_type=MediaType.TRACK,
        prev_library_items=[],
    )

    media_controller.remove_item_from_library.assert_not_called()


async def test_multi_provider_removal_from_one_keeps_item(
    music_controller: MusicController,
    media_controller: AsyncMock,
) -> None:
    """Multi-provider: removal from one provider, still in another -> item stays.

    Item has mappings from spotify_1 (already soft-deleted) and tidal_1 (in_library=True).
    _process_sync_deletions should NOT hard-delete because tidal_1 still has it.
    """
    track = _make_track(
        db_id=1,
        provider_mappings={
            _make_prov_mapping("sp_track_1", "spotify", "spotify_1", in_library=False),
            _make_prov_mapping("td_track_1", "tidal", "tidal_1", in_library=True),
        },
    )
    # spotify_1 removed this item from its library after sync
    music_controller.mass.cache.get = AsyncMock(return_value=None)  # type: ignore[method-assign]
    # DB has the item with mappings from both providers
    media_controller.get_library_item = AsyncMock(return_value=track)

    await music_controller._process_sync_deletions(
        provider_instance_id="spotify_1",
        media_type=MediaType.TRACK,
        prev_library_items=[1],
    )

    media_controller.remove_item_from_library.assert_not_called()


async def test_multi_provider_removal_from_last_deletes_item(
    music_controller: MusicController,
    media_controller: AsyncMock,
) -> None:
    """Multi-provider: removal from last provider -> hard-delete.

    Item had both providers, but spotify already set in_library=False.
    Now tidal also removes it (soft-deleted to in_library=False) -> should hard-delete.
    """
    track = _make_track(
        db_id=1,
        provider_mappings={
            _make_prov_mapping("sp_track_1", "spotify", "spotify_1", in_library=False),
            _make_prov_mapping("td_track_1", "tidal", "tidal_1", in_library=False),
        },
    )
    # tidal_1 removed this item from its library after sync
    music_controller.mass.cache.get = AsyncMock(return_value=None)  # type: ignore[method-assign]
    # DB has the item, but both providers have in_library=False
    media_controller.get_library_item = AsyncMock(return_value=track)

    await music_controller._process_sync_deletions(
        provider_instance_id="tidal_1",
        media_type=MediaType.TRACK,
        prev_library_items=[1],
    )

    media_controller.remove_item_from_library.assert_called_once_with(1)


async def test_item_already_deleted_from_db_is_skipped(
    music_controller: MusicController,
    media_controller: AsyncMock,
) -> None:
    """If the item is already deleted from the DB, skip it gracefully."""
    # spotify_1 no longer has this item after sync
    music_controller.mass.cache.get = AsyncMock(return_value=None)  # type: ignore[method-assign]
    # Item is already gone from the DB
    media_controller.get_library_item = AsyncMock(side_effect=MediaNotFoundError("not found"))

    await music_controller._process_sync_deletions(
        provider_instance_id="spotify_1",
        media_type=MediaType.TRACK,
        prev_library_items=[1],
    )

    media_controller.remove_item_from_library.assert_not_called()


async def test_items_still_in_provider_not_processed(
    music_controller: MusicController,
    media_controller: AsyncMock,
) -> None:
    """Items still in the provider (in cur_db_ids) are not processed for deletion."""
    track = _make_track(
        db_id=1,
        provider_mappings={
            _make_prov_mapping("sp_track_1", "spotify", "spotify_1", in_library=True),
        },
    )
    # spotify_1 still has item 1 in its library after sync
    music_controller.mass.cache.get = AsyncMock(return_value=[1])  # type: ignore[method-assign]
    # DB has the item (but it should never be looked up)
    media_controller.get_library_item = AsyncMock(return_value=track)

    await music_controller._process_sync_deletions(
        provider_instance_id="spotify_1",
        media_type=MediaType.TRACK,
        prev_library_items=[1],
    )

    media_controller.get_library_item.assert_not_called()
    media_controller.remove_item_from_library.assert_not_called()
