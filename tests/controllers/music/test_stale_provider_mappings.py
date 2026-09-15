"""Tests that a library sync drops mappings to items the provider no longer has."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import MediaType, ProviderType
from music_assistant_models.errors import InvalidDataError

from music_assistant.constants import CONF_ENTRY_LIBRARY_SYNC_DELETIONS, CONF_LOG_LEVEL
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

INSTANCE_ID = "test--1"
# provider album id -> library db id the sync hands out for it
LISTED_ALBUMS = {"album_1": 1, "album_2": 2}
# the provider's mappings in the library: album 1 still carries the id of the file it replaced
MAPPING_ROWS = [
    {"item_id": 1, "provider_item_id": "album_1"},
    {"item_id": 1, "provider_item_id": "replaced_1"},
    {"item_id": 2, "provider_item_id": "album_2"},
]


class LocalLibraryProvider(MusicProvider):
    """Provider whose catalog is its library, like a media server or local files."""

    #: provider album id to drop while listing the library, reported as skipped
    skip_item_id: str | None = None
    #: report the skipped album without an id, which makes the run incomplete
    skip_unidentified: bool = False

    @property
    def is_streaming_provider(self) -> bool:
        """Return whether the catalog is larger than the library."""
        return False

    async def get_library_albums(self) -> AsyncGenerator[Any]:
        """Yield the listed albums, dropping the one to skip."""
        if self.skip_unidentified:
            self.report_skipped_sync_item(MediaType.ALBUM, None, InvalidDataError("no id"))
        for item_id in (*LISTED_ALBUMS, self.skip_item_id):
            if item_id is None:
                continue
            if item_id == self.skip_item_id:
                self.report_skipped_sync_item(
                    MediaType.ALBUM, item_id, InvalidDataError("no artist")
                )
                continue
            album = MagicMock()
            album.item_id = item_id
            album.name = f"Album {item_id}"
            album.favorite = False
            album.metadata.genres = None
            album.provider_mappings = [MagicMock()]
            yield album


class StreamingProvider(LocalLibraryProvider):
    """Provider whose catalog is larger than the library it syncs."""

    @property
    def is_streaming_provider(self) -> bool:
        """Return whether the catalog is larger than the library."""
        return True


def _build_mass() -> MagicMock:
    """Return a mocked mass holding MAPPING_ROWS and the listed albums in its library."""
    mass = MagicMock()
    mass.cache.get = AsyncMock(return_value=list(LISTED_ALBUMS.values()))
    mass.cache.set = AsyncMock()
    mass.music.library_supported = MagicMock(return_value=True)
    mass.music.genres.sync_media_item_genres = AsyncMock()

    albums = mass.music.albums
    albums.get_library_item_sync_details = AsyncMock(return_value=None)

    async def add_item_to_library(prov_item: Any) -> Any:
        return MagicMock(item_id=LISTED_ALBUMS[prov_item.item_id], favorite=False)

    albums.add_item_to_library = AsyncMock(side_effect=add_item_to_library)

    async def iter_items(_table: str, match: dict[str, Any]) -> AsyncGenerator[dict[str, Any]]:
        assert match == {"media_type": "album", "provider_instance": INSTANCE_ID}
        for row in MAPPING_ROWS:
            yield row

    mass.music.database.iter_items = iter_items
    mass.music.get_controller = MagicMock(return_value=AsyncMock())
    return mass


def _build_provider(
    mass: MagicMock,
    cls: type[LocalLibraryProvider] = LocalLibraryProvider,
    sync_deletions: bool = True,
) -> LocalLibraryProvider:
    """Return a provider instance wired to the given (mocked) mass."""
    manifest = MagicMock()
    manifest.type = ProviderType.MUSIC
    manifest.domain = "test"
    config = MagicMock()
    config.instance_id = INSTANCE_ID
    config.domain = "test"
    values = {CONF_LOG_LEVEL: "GLOBAL", CONF_ENTRY_LIBRARY_SYNC_DELETIONS.key: sync_deletions}
    config.get_value.side_effect = lambda key, default=None: values.get(key, default)
    return cls(mass, manifest, config)


def _removed_mappings(mass: MagicMock) -> list[tuple[Any, ...]]:
    """Return the (db id, provider instance, provider item id) of each removed mapping."""
    controller = mass.music.get_controller.return_value
    return [call.args for call in controller.remove_provider_mapping.await_args_list]


async def test_mapping_to_an_item_the_provider_no_longer_lists_is_removed() -> None:
    """
    A replaced file leaves the library item with a mapping to the deleted provider item.

    That mapping still looks playable, so playback fails whenever it is the one picked.
    """
    mass = _build_mass()

    await _build_provider(mass).sync_library(MediaType.ALBUM)

    assert _removed_mappings(mass) == [(1, INSTANCE_ID, "replaced_1")]


async def test_streaming_provider_keeps_mappings_it_did_not_list() -> None:
    """An item that is not in a streaming library may still be in its catalog."""
    mass = _build_mass()

    await _build_provider(mass, cls=StreamingProvider).sync_library(MediaType.ALBUM)

    assert _removed_mappings(mass) == []


async def test_mappings_are_kept_when_sync_deletions_are_disabled() -> None:
    """Removing a mapping is a deletion, so it follows the library sync deletions option."""
    mass = _build_mass()

    await _build_provider(mass, sync_deletions=False).sync_library(MediaType.ALBUM)

    assert _removed_mappings(mass) == []


async def test_mappings_are_kept_when_the_run_is_incomplete() -> None:
    """A run that could not say what it dropped is no basis for removing anything."""
    mass = _build_mass()
    provider = _build_provider(mass)
    provider.skip_unidentified = True

    await provider.sync_library(MediaType.ALBUM)

    assert _removed_mappings(mass) == []


async def test_skipped_item_keeps_its_mapping() -> None:
    """An item the provider could not read is still on the provider."""
    mass = _build_mass()
    provider = _build_provider(mass)
    provider.skip_item_id = "replaced_1"

    await provider.sync_library(MediaType.ALBUM)

    assert _removed_mappings(mass) == []
