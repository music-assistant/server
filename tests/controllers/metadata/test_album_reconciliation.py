"""Tests for the hourly album reconciliation maintenance task."""

from __future__ import annotations

from time import time
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

import aiohttp
import pytest
from music_assistant_models.enums import AlbumType, ProviderFeature
from music_assistant_models.errors import MediaNotFoundError, MusicAssistantError
from music_assistant_models.helpers import set_global_cache_values
from music_assistant_models.media_items import (
    Album,
    Artist,
    MediaItemMetadata,
    ProviderMapping,
    UniqueList,
)

from music_assistant.constants import DB_TABLE_ALBUMS
from music_assistant.controllers.metadata import MetaDataController
from music_assistant.controllers.metadata.constants import (
    METADATA_SCAN_BATCH_SIZE,
    REFRESH_INTERVAL,
)
from music_assistant.controllers.metadata.controller import _duplicate_album_sibling_guard
from music_assistant.mass import MusicAssistant

_REPORT_FAILURE = "music_assistant.controllers.metadata.controller.report_current_task_failure"
_CONTROLLER_TIME = "music_assistant.controllers.metadata.controller.time"


def _controller() -> MetaDataController:
    """Create a bare MetaDataController without running __init__."""
    ctrl = MetaDataController.__new__(MetaDataController)
    ctrl._corrupt_metadata_rows = {}
    ctrl.logger = Mock()
    return ctrl


def _album_stub(item_id: str = "1", name: str = "Test Album") -> Mock:
    """Build a lightweight stand-in for a library Album."""
    album = Mock()
    album.item_id = item_id
    album.name = name
    return album


# --------------------------------------------------------------------------- #
#  candidate query                                                             #
# --------------------------------------------------------------------------- #


async def test_reconcile_duplicate_albums_query_matches_unknown_or_duplicate_and_stale() -> None:
    """The candidate query selects unknown-typed or possibly-duplicated stale albums."""
    ctrl = _controller()
    mass = Mock()
    mass.music.albums.get_library_items_by_query = AsyncMock(return_value=[])
    ctrl.mass = mass

    with patch(_CONTROLLER_TIME, return_value=1_700_000_000.0):
        await ctrl._reconcile_duplicate_albums()
    refresh_before = int(1_700_000_000.0 - REFRESH_INTERVAL)

    _, kwargs = mass.music.albums.get_library_items_by_query.call_args
    assert kwargs["extra_query_parts"] == [
        f"({DB_TABLE_ALBUMS}.album_type = 'unknown' "
        f"OR {_duplicate_album_sibling_guard()}) AND ("
        f"json_extract({DB_TABLE_ALBUMS}.metadata,'$.last_refresh') ISNULL "
        f"OR json_extract({DB_TABLE_ALBUMS}.metadata,'$.last_refresh') < {refresh_before})"
    ]
    assert kwargs["limit"] == METADATA_SCAN_BATCH_SIZE
    assert kwargs["order_by"] == "random"


async def test_reconcile_duplicate_albums_retries_stale_but_not_fresh_refresh(
    mass: MusicAssistant,
) -> None:
    """An unknown album retries past REFRESH_INTERVAL; a recently-refreshed one does not."""
    now = int(time())
    stale_album = await mass.music.albums.add_item_to_library(
        Album(
            item_id="0",
            provider="library",
            name="Stale Album",
            album_type=AlbumType.UNKNOWN,
            artists=UniqueList(),
            provider_mappings={
                ProviderMapping(
                    item_id="stale-item", provider_domain="qobuz", provider_instance="qobuz_1"
                )
            },
            metadata=MediaItemMetadata(last_refresh=now - REFRESH_INTERVAL - 1),
        )
    )
    await mass.music.albums.add_item_to_library(
        Album(
            item_id="0",
            provider="library",
            name="Fresh Album",
            album_type=AlbumType.UNKNOWN,
            artists=UniqueList(),
            provider_mappings={
                ProviderMapping(
                    item_id="fresh-item", provider_domain="qobuz", provider_instance="qobuz_1"
                )
            },
            metadata=MediaItemMetadata(last_refresh=now - 1),
        )
    )

    with (
        patch.object(mass.metadata, "_update_album_metadata", AsyncMock()) as update_metadata,
        patch.object(mass.music.albums, "match_providers", AsyncMock()),
    ):
        await mass.metadata._reconcile_duplicate_albums()

    processed_ids = {call.args[0].item_id for call in update_metadata.await_args_list}
    assert processed_ids == {stale_album.item_id}


async def _add_album(
    mass: MusicAssistant,
    name: str,
    artist: Artist,
    *,
    version: str = "",
    album_type: AlbumType = AlbumType.ALBUM,
    provider_instance: str = "qobuz_1",
) -> Album:
    """Add a never-refreshed library album for the given artist."""
    return await mass.music.albums.add_item_to_library(
        Album(
            item_id="0",
            provider="library",
            name=name,
            version=version,
            album_type=album_type,
            artists=UniqueList([artist]),
            provider_mappings={
                ProviderMapping(
                    item_id=f"{provider_instance}-{name}-{version}",
                    provider_domain=provider_instance.rsplit("_", 1)[0],
                    provider_instance=provider_instance,
                )
            },
        )
    )


async def _reconciled_ids(mass: MusicAssistant) -> set[str]:
    """Run the reconciliation task and return the item ids it picked up."""
    with (
        patch.object(mass.metadata, "_update_album_metadata", AsyncMock()) as update_metadata,
        patch.object(mass.music.albums, "match_providers", AsyncMock()),
    ):
        await mass.metadata._reconcile_duplicate_albums()
    return {call.args[0].item_id for call in update_metadata.await_args_list}


async def test_reconcile_duplicate_albums_selects_typed_album_with_duplicate_sibling(
    mass: MusicAssistant,
) -> None:
    """A fully-typed album is reconciled when another row shares its name, artist and version."""
    artist = await mass.music.artists.add_item_to_library(
        Artist(
            item_id="0",
            provider="library",
            name="Phil Collins",
            provider_mappings={
                ProviderMapping(
                    item_id="artist", provider_domain="test", provider_instance="library"
                )
            },
        )
    )
    first = await _add_album(
        mass, "...But Seriously", artist, version="2016 Remaster", provider_instance="spotify_1"
    )
    second = await _add_album(
        mass, "...But Seriously", artist, version="2016 Remaster", provider_instance="qobuz_1"
    )
    # neither row is unknown-typed, so only the duplicate-sibling clause can select them
    assert first.item_id != second.item_id

    assert await _reconciled_ids(mass) == {first.item_id, second.item_id}


async def test_reconcile_duplicate_albums_ignores_distinct_editions(
    mass: MusicAssistant,
) -> None:
    """Rows whose version wording differs are separate editions and are left alone."""
    artist = await mass.music.artists.add_item_to_library(
        Artist(
            item_id="0",
            provider="library",
            name="Antoine Clamaran",
            provider_mappings={
                ProviderMapping(
                    item_id="artist", provider_domain="test", provider_instance="library"
                )
            },
        )
    )
    await _add_album(
        mass, "200 Years", artist, version="Remixes Pt. 1", provider_instance="spotify_1"
    )
    await _add_album(
        mass, "200 Years", artist, version="Remixes Pt. 2", provider_instance="qobuz_1"
    )

    assert await _reconciled_ids(mass) == set()


async def test_reconcile_duplicate_albums_selects_subset_version_wording(
    mass: MusicAssistant,
) -> None:
    """A version whose wording is a whole-word subset of its sibling's stays a candidate."""
    artist = await mass.music.artists.add_item_to_library(
        Artist(
            item_id="0",
            provider="library",
            name="Queen",
            provider_mappings={
                ProviderMapping(
                    item_id="artist", provider_domain="test", provider_instance="library"
                )
            },
        )
    )
    plain = await _add_album(
        mass, "Innuendo", artist, version="2011 Remaster", provider_instance="spotify_1"
    )
    deluxe = await _add_album(
        mass,
        "Innuendo",
        artist,
        version="Deluxe Edition 2011 Remaster",
        provider_instance="qobuz_1",
    )

    assert await _reconciled_ids(mass) == {plain.item_id, deluxe.item_id}


async def test_reconcile_duplicate_albums_skips_row_merged_away_earlier_in_the_batch() -> None:
    """A row already merged into its duplicate is skipped silently, not reported as a failure."""
    ctrl = _controller()
    mass = Mock()
    merged_away = _album_stub("1", "Merged Away")
    healthy = _album_stub("2", "Healthy Album")
    reloaded_healthy = _album_stub("2", "Healthy Album")
    mass.music.albums.get_library_items_by_query = AsyncMock(return_value=[merged_away, healthy])
    mass.music.albums.get_library_item = AsyncMock(return_value=reloaded_healthy)
    mass.music.albums.match_providers = AsyncMock()
    ctrl.mass = mass
    ctrl._update_album_metadata = AsyncMock(  # type: ignore[method-assign]
        side_effect=[MediaNotFoundError("album not found in library: 1"), None]
    )

    with patch(_REPORT_FAILURE) as report_failure:
        await ctrl._reconcile_duplicate_albums()

    report_failure.assert_not_called()
    mass.music.albums.match_providers.assert_awaited_once_with(reloaded_healthy)


async def test_reconcile_duplicate_albums_empty_queue_is_a_noop() -> None:
    """An empty candidate batch does not touch any album."""
    ctrl = _controller()
    mass = Mock()
    mass.music.albums.get_library_items_by_query = AsyncMock(return_value=[])
    ctrl.mass = mass

    await ctrl._reconcile_duplicate_albums()

    mass.music.albums.get_library_item.assert_not_called()
    mass.music.albums.match_providers.assert_not_called()


# --------------------------------------------------------------------------- #
#  enrich -> reload -> re-match flow                                          #
# --------------------------------------------------------------------------- #


async def test_reconcile_duplicate_albums_enriches_then_reloads_before_matching() -> None:
    """Each album is enriched, the library row reloaded, then re-matched with fresh data."""
    ctrl = _controller()
    mass = Mock()
    album = _album_stub("1", "Original Name")
    reloaded_album = _album_stub("1", "Enriched Name")
    mass.music.albums.get_library_items_by_query = AsyncMock(return_value=[album])
    mass.music.albums.get_library_item = AsyncMock(return_value=reloaded_album)
    mass.music.albums.match_providers = AsyncMock()
    ctrl.mass = mass
    ctrl._update_album_metadata = AsyncMock()  # type: ignore[method-assign]

    await ctrl._reconcile_duplicate_albums()

    ctrl._update_album_metadata.assert_awaited_once_with(album, force_refresh=False)
    mass.music.albums.get_library_item.assert_awaited_once_with("1")
    # match_providers must see the reloaded (enriched) object, not the stale pre-update one
    mass.music.albums.match_providers.assert_awaited_once_with(reloaded_album)


async def test_reconcile_duplicate_albums_never_adds_or_deletes_directly() -> None:
    """The task never adds a new library item or deletes one outside the safe merge path."""
    ctrl = _controller()
    mass = Mock()
    album = _album_stub()
    mass.music.albums.get_library_items_by_query = AsyncMock(return_value=[album])
    mass.music.albums.get_library_item = AsyncMock(return_value=album)
    mass.music.albums.match_providers = AsyncMock()
    ctrl.mass = mass
    ctrl._update_album_metadata = AsyncMock()  # type: ignore[method-assign]

    await ctrl._reconcile_duplicate_albums()

    mass.music.albums.add_item_to_library.assert_not_called()
    mass.music.albums.remove_item_from_library.assert_not_called()
    mass.music.albums.merge_library_items.assert_not_called()


async def test_reconcile_duplicate_albums_batch_size_bound() -> None:
    """The batch never exceeds METADATA_SCAN_BATCH_SIZE, even when more items match."""
    ctrl = _controller()
    mass = Mock()
    albums = [_album_stub(str(i), f"Album {i}") for i in range(METADATA_SCAN_BATCH_SIZE)]
    mass.music.albums.get_library_items_by_query = AsyncMock(return_value=albums)
    mass.music.albums.get_library_item = AsyncMock(side_effect=lambda item_id: _album_stub(item_id))
    mass.music.albums.match_providers = AsyncMock()
    ctrl.mass = mass
    ctrl._update_album_metadata = AsyncMock()  # type: ignore[method-assign]

    await ctrl._reconcile_duplicate_albums()

    assert ctrl._update_album_metadata.await_count == METADATA_SCAN_BATCH_SIZE
    assert mass.music.albums.match_providers.await_count == METADATA_SCAN_BATCH_SIZE


# --------------------------------------------------------------------------- #
#  per-item failure isolation                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "error",
    [
        MusicAssistantError("boom"),
        aiohttp.ClientError("connection reset"),
        TimeoutError("timed out"),
    ],
)
async def test_reconcile_duplicate_albums_isolates_metadata_failure(error: Exception) -> None:
    """An expected per-item metadata failure is reported and does not raise."""
    ctrl = _controller()
    mass = Mock()
    album = _album_stub("1", "Failing Album")
    mass.music.albums.get_library_items_by_query = AsyncMock(return_value=[album])
    mass.music.albums.get_library_item = AsyncMock()
    mass.music.albums.match_providers = AsyncMock()
    ctrl.mass = mass
    ctrl._update_album_metadata = AsyncMock(side_effect=error)  # type: ignore[method-assign]

    with patch(_REPORT_FAILURE) as report_failure:
        await ctrl._reconcile_duplicate_albums()  # must not raise

    report_failure.assert_called_once_with(f"Failing Album: {error}")
    # the failed item never reaches reload/re-match
    mass.music.albums.get_library_item.assert_not_called()
    mass.music.albums.match_providers.assert_not_called()


async def test_reconcile_duplicate_albums_isolates_match_providers_failure() -> None:
    """A provider search failure during re-matching is caught and reported, not raised."""
    ctrl = _controller()
    mass = Mock()
    album = _album_stub("1", "Flaky Album")
    reloaded = _album_stub("1", "Flaky Album")
    mass.music.albums.get_library_items_by_query = AsyncMock(return_value=[album])
    mass.music.albums.get_library_item = AsyncMock(return_value=reloaded)
    mass.music.albums.match_providers = AsyncMock(
        side_effect=aiohttp.ClientError("connection reset")
    )
    ctrl.mass = mass
    ctrl._update_album_metadata = AsyncMock()  # type: ignore[method-assign]

    with patch(_REPORT_FAILURE) as report_failure:
        await ctrl._reconcile_duplicate_albums()  # must not raise

    report_failure.assert_called_once_with("Flaky Album: connection reset")


async def test_reconcile_duplicate_albums_failure_does_not_abort_the_batch() -> None:
    """A failing album is isolated; the remaining albums in the batch still get processed."""
    ctrl = _controller()
    mass = Mock()
    failing_album = _album_stub("1", "Failing Album")
    healthy_album = _album_stub("2", "Healthy Album")
    reloaded_healthy = _album_stub("2", "Healthy Album")
    mass.music.albums.get_library_items_by_query = AsyncMock(
        return_value=[failing_album, healthy_album]
    )
    mass.music.albums.get_library_item = AsyncMock(return_value=reloaded_healthy)
    mass.music.albums.match_providers = AsyncMock()
    ctrl.mass = mass
    ctrl._update_album_metadata = AsyncMock(  # type: ignore[method-assign]
        side_effect=[MusicAssistantError("boom"), None]
    )

    with patch(_REPORT_FAILURE) as report_failure:
        await ctrl._reconcile_duplicate_albums()

    report_failure.assert_called_once_with("Failing Album: boom")
    mass.music.albums.get_library_item.assert_awaited_once_with("2")
    mass.music.albums.match_providers.assert_awaited_once_with(reloaded_healthy)


async def test_reconcile_duplicate_albums_no_match_completes_without_failure() -> None:
    """A normal no-match completion is not treated as a failure; the album stays attempted."""
    ctrl = _controller()
    mass = Mock()
    album = _album_stub("1", "No Match Album")
    reloaded = _album_stub("1", "No Match Album")
    mass.music.albums.get_library_items_by_query = AsyncMock(return_value=[album])
    mass.music.albums.get_library_item = AsyncMock(return_value=reloaded)
    mass.music.albums.match_providers = AsyncMock(return_value=None)
    ctrl.mass = mass
    ctrl._update_album_metadata = AsyncMock()  # type: ignore[method-assign]

    with patch(_REPORT_FAILURE) as report_failure:
        await ctrl._reconcile_duplicate_albums()

    report_failure.assert_not_called()
    ctrl._update_album_metadata.assert_awaited_once_with(album, force_refresh=False)
    mass.music.albums.match_providers.assert_awaited_once_with(reloaded)


# --------------------------------------------------------------------------- #
#  integration: a confirmed match owned by a duplicate merges via the safe path #
# --------------------------------------------------------------------------- #


async def test_reconcile_duplicate_albums_merges_conflicting_mapping_via_safe_path(
    mass: MusicAssistant,
) -> None:
    """
    A confirmed re-match already owned by another library album merges, not duplicates.

    Drives the real `AlbumsController.match_providers` -> `add_provider_mappings` path
    end to end against a real (test) database, only stubbing the provider IO boundary
    (search/full-item fetch), to prove the reconciliation task relies on the existing
    safe merge primitive instead of reimplementing conflict handling.
    """
    artist = await mass.music.artists.add_item_to_library(
        Artist(
            item_id="0",
            provider="library",
            name="Sigur Rós",
            provider_mappings={
                ProviderMapping(
                    item_id="artist", provider_domain="test", provider_instance="library"
                )
            },
        )
    )
    target = await mass.music.albums.add_item_to_library(
        Album(
            item_id="0",
            provider="library",
            name="( )",
            album_type=AlbumType.UNKNOWN,
            artists=UniqueList([artist]),
            provider_mappings={
                ProviderMapping(
                    item_id="qobuz-item", provider_domain="qobuz", provider_instance="qobuz_1"
                )
            },
        )
    )
    duplicate = await mass.music.albums.add_item_to_library(
        Album(
            item_id="0",
            provider="library",
            name="( )",
            album_type=AlbumType.ALBUM,
            artists=UniqueList([artist]),
            provider_mappings={
                ProviderMapping(
                    item_id="spotify-item",
                    provider_domain="spotify",
                    provider_instance="spotify_1",
                )
            },
        )
    )

    provider = Mock()
    provider.domain = "spotify"
    provider.instance_id = "spotify_1"
    provider.supported_features = {ProviderFeature.SEARCH}
    provider.is_streaming_provider = True

    search_result = Album(
        item_id="spotify-item",
        provider="spotify_1",
        name=target.name,
        artists=UniqueList([artist]),
        provider_mappings={
            ProviderMapping(
                item_id="spotify-item", provider_domain="spotify", provider_instance="spotify_1"
            )
        },
    )

    # both providers must be "available" for the match to be considered, and for
    # add_provider_mappings' subsequent uniqueness check to see qobuz_1's own mapping
    await set_global_cache_values({"available_providers": {"qobuz_1", "spotify_1"}})

    with (
        patch.object(mass.music.albums, "search", AsyncMock(return_value=[search_result])),
        patch.object(mass.music.albums, "get_provider_item", AsyncMock(return_value=search_result)),
        patch.object(mass.music, "library_supported", Mock(return_value=True)),
        patch.object(type(mass.music), "providers", new_callable=PropertyMock) as providers_mock,
    ):
        providers_mock.return_value = [provider]
        await mass.music.albums.match_providers(target)

    # the duplicate row is gone: its mapping was transferred, not recreated
    with pytest.raises(MediaNotFoundError):
        await mass.music.albums.get_library_item(duplicate.item_id)
    merged = await mass.music.albums.get_library_item(target.item_id)
    assert {m.provider_instance for m in merged.provider_mappings} == {"qobuz_1", "spotify_1"}
