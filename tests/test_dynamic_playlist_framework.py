"""Tests for dynamic-playlist handling in the metadata refresh scan and provider sync."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock, patch

from music_assistant_models.enums import ImageType, MediaType
from music_assistant_models.media_items import (
    MediaItemImage,
    Playlist,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.constants import DB_TABLE_PLAYLISTS
from music_assistant.controllers.metadata import MetaDataController
from music_assistant.controllers.metadata.enrichment import MetadataEnrichmentMixin
from music_assistant.models.music_provider import MusicProvider


def _controller() -> MetaDataController:
    """Create a bare MetaDataController without running __init__."""
    return MetaDataController.__new__(MetaDataController)


def _provider() -> MusicProvider:
    """Create a bare MusicProvider without running __init__."""
    return MusicProvider.__new__(MusicProvider)


@asynccontextmanager
async def _noop_deferred_commit() -> AsyncGenerator[None]:
    """Stand-in for DatabaseConnection.deferred_commit on mocked databases."""
    yield


def _image(path: str) -> MediaItemImage:
    """Build a MediaItemImage for the given path, as the pandora instance would supply it."""
    return MediaItemImage(
        type=ImageType.THUMB,
        path=path,
        provider="pandora_1",
        remotely_accessible=True,
    )


def _provider_mapping(item_id: str = "station_1") -> ProviderMapping:
    """Build a minimal ProviderMapping for a Pandora-style station."""
    return ProviderMapping(
        item_id=item_id,
        provider_domain="pandora",
        provider_instance="pandora_1",
        in_library=True,
    )


# --------------------------------------------------------------------------- #
#  _update_playlist_metadata processes all playlists when called directly      #
# --------------------------------------------------------------------------- #


async def test_update_playlist_metadata_processes_dynamic_playlists() -> None:
    """_update_playlist_metadata processes dynamic playlists; providers decide filtering."""
    mixin = MetadataEnrichmentMixin()
    mixin.mass = Mock()
    mixin.logger = Mock()
    mixin.providers = []  # type: ignore[misc]

    async def mock_tracks(
        item_id: str,  # noqa: ARG001
        provider: str,  # noqa: ARG001
    ) -> AsyncIterator[Track]:
        """Empty async generator."""
        if False:
            yield  # type: ignore[unreachable]  # pragma: no cover

    mixin.mass.music.playlists.tracks = mock_tracks
    mixin.mass.music.playlists.update_item_in_library = AsyncMock()

    playlist = Mock()
    playlist.is_dynamic = True
    playlist.provider_mappings = {_provider_mapping()}
    playlist.provider = "pandora"
    playlist.item_id = "test_dynamic"
    playlist.name = "Test Dynamic Playlist"
    playlist.metadata = Mock()
    playlist.metadata.last_refresh = 0
    playlist.metadata.genres = set()

    await mixin._update_playlist_metadata(playlist, force_refresh=True)

    # Track scanning should happen (providers decide whether to use this data)
    mixin.logger.debug.assert_called()


async def test_refresh_playlist_metadata_batch_query_excludes_dynamic_playlists() -> None:
    """Batch query filters is_dynamic playlists; single updates process all playlists."""
    ctrl = _controller()
    mass = Mock()
    mass.music.playlists.get_library_items_by_query = AsyncMock(return_value=[])
    ctrl.mass = mass

    await ctrl._refresh_playlist_metadata_batch()

    _, kwargs = mass.music.playlists.get_library_items_by_query.call_args
    query_parts = kwargs["extra_query_parts"]
    # Batch query should still filter dynamic playlists (automated refresh)
    assert any(f"{DB_TABLE_PLAYLISTS}.is_dynamic = 0" in part for part in query_parts)


# --------------------------------------------------------------------------- #
#  Provider sync overwrites name/images for non-editable *dynamic* playlists  #
# --------------------------------------------------------------------------- #


def _build_sync_fixture(
    *,
    library_is_editable: bool,
    library_name: str,
    prov_name: str,
    library_images: list[str],
    prov_images: list[str],
    is_dynamic: bool = True,
) -> tuple[MusicProvider, AsyncMock, Mock]:
    """Build a MusicProvider + mocked playlists controller wired for one sync pass."""
    mapping = _provider_mapping()

    library_item = Mock(spec=Playlist)
    library_item.item_id = "1"
    library_item.is_editable = library_is_editable
    library_item.name = library_name
    library_item.metadata = Mock(images=UniqueList([_image(path) for path in library_images]))
    library_item.date_added = None
    library_item.supported_mediatypes = [MediaType.TRACK]
    library_item.favorite = True
    library_item.is_dynamic = is_dynamic

    prov_item = Mock()
    prov_item.item_id = "station_1"
    prov_item.name = prov_name
    prov_item.metadata = Mock(images=UniqueList([_image(path) for path in prov_images]))
    prov_item.date_added = None
    prov_item.supported_mediatypes = [MediaType.TRACK]
    prov_item.favorite = True
    prov_item.provider_mappings = UniqueList([mapping])
    prov_item.uri = "pandora://station_1"
    prov_item.is_dynamic = is_dynamic

    updated_item = Mock()
    updated_item.item_id = "1"
    updated_item.favorite = True

    playlists_ctrl = AsyncMock()
    playlists_ctrl.get_library_item_by_prov_mappings = AsyncMock(return_value=library_item)
    playlists_ctrl.update_item_in_library = AsyncMock(return_value=updated_item)

    mass = Mock()
    mass.music.playlists = playlists_ctrl
    mass.music.database.deferred_commit = _noop_deferred_commit

    provider = _provider()
    provider.mass = mass
    provider.config = Mock(get_value=Mock(return_value=[]))
    provider.logger = Mock()

    return provider, playlists_ctrl, prov_item


async def _get_single_playlist(prov_item: Mock) -> AsyncIterator[Mock]:
    """Yield a single provider playlist item, mirroring get_library_playlists()."""
    yield prov_item


async def _run_sync(provider: MusicProvider, prov_item: Mock) -> None:
    """Run _sync_library_playlists() with its provider-side hooks patched out."""
    with patch.multiple(
        provider,
        get_library_playlists=Mock(return_value=_get_single_playlist(prov_item)),
        _check_provider_mappings=Mock(return_value=True),
        _update_sync_task_item_status=Mock(),
        _report_sync_task_failure=Mock(),
    ):
        await provider._sync_library_playlists()


async def test_sync_overwrites_name_for_noneditable_dynamic_playlist() -> None:
    """A non-editable playlist whose name changed on the provider gets overwritten."""
    provider, playlists_ctrl, prov_item = _build_sync_fixture(
        library_is_editable=False,
        library_name="Old Station Name",
        prov_name="New Station Name",
        library_images=["same.jpg"],
        prov_images=["same.jpg"],
    )

    await _run_sync(provider, prov_item)

    playlists_ctrl.update_item_in_library.assert_called_once_with("1", prov_item, overwrite=True)


async def test_sync_overwrites_images_for_noneditable_dynamic_playlist() -> None:
    """A non-editable playlist whose images changed on the provider gets overwritten."""
    provider, playlists_ctrl, prov_item = _build_sync_fixture(
        library_is_editable=False,
        library_name="Same Name",
        prov_name="Same Name",
        library_images=["old.jpg"],
        prov_images=["new.jpg"],
    )

    await _run_sync(provider, prov_item)

    playlists_ctrl.update_item_in_library.assert_called_once_with("1", prov_item, overwrite=True)


async def test_sync_does_not_overwrite_editable_playlist_on_name_change() -> None:
    """User-editable playlists are never force-overwritten, even if names diverge."""
    provider, playlists_ctrl, prov_item = _build_sync_fixture(
        library_is_editable=True,
        library_name="Old Name",
        prov_name="New Name",
        library_images=["same.jpg"],
        prov_images=["same.jpg"],
    )

    await _run_sync(provider, prov_item)

    playlists_ctrl.update_item_in_library.assert_not_called()


async def test_sync_does_not_overwrite_noneditable_static_playlist_on_name_change() -> None:
    """
    Non-editable, non-dynamic playlists (e.g. a provider's "Favorites") are spared.

    Only non-editable *dynamic* playlists are treated as provider-owned; a static
    non-editable playlist keeps its locally-enriched metadata/images.
    """
    provider, playlists_ctrl, prov_item = _build_sync_fixture(
        library_is_editable=False,
        library_name="Old Name",
        prov_name="New Name",
        library_images=["same.jpg"],
        prov_images=["same.jpg"],
        is_dynamic=False,
    )

    await _run_sync(provider, prov_item)

    playlists_ctrl.update_item_in_library.assert_not_called()


async def test_sync_skips_update_when_noneditable_playlist_unchanged() -> None:
    """No spurious update is issued when name/images already match."""
    provider, playlists_ctrl, prov_item = _build_sync_fixture(
        library_is_editable=False,
        library_name="Same Name",
        prov_name="Same Name",
        library_images=["same.jpg"],
        prov_images=["same.jpg"],
    )

    await _run_sync(provider, prov_item)

    playlists_ctrl.update_item_in_library.assert_not_called()
