"""Tests for the daily scan collecting the metadata of items that never had theirs collected."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

from music_assistant_models.enums import ImageType
from music_assistant_models.media_items import (
    Album,
    Artist,
    MediaItemImage,
    MediaItemMetadata,
    ProviderMapping,
    UniqueList,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


def _mapping(item_id: str) -> set[ProviderMapping]:
    """Return a single qobuz provider mapping for a library item."""
    return {ProviderMapping(item_id=item_id, provider_domain="qobuz", provider_instance="qobuz_1")}


async def _add_album(
    mass: MusicAssistant, name: str, *, last_refresh: int | None = None, artwork: bool = False
) -> Album:
    """Add a library album with the given refresh marker, with or without artwork."""
    metadata = MediaItemMetadata(last_refresh=last_refresh)
    if artwork:
        metadata.images = UniqueList(
            [MediaItemImage(type=ImageType.THUMB, path=f"http://img/{name}", provider="qobuz_1")]
        )
    return await mass.music.albums.add_item_to_library(
        Album(
            item_id="0",
            provider="library",
            name=name,
            artists=UniqueList(),
            provider_mappings=_mapping(name),
            metadata=metadata,
        )
    )


async def test_scan_refreshes_albums_without_artwork_that_were_never_refreshed(
    mass: MusicAssistant,
) -> None:
    """An album without artwork is refreshed once; one refreshed before or with artwork is not."""
    bare = await _add_album(mass, "Bare")
    await _add_album(mass, "Refreshed", last_refresh=123)
    await _add_album(mass, "Covered", artwork=True)

    with (
        patch.object(mass.metadata, "_update_album_metadata", AsyncMock()) as update_album,
        patch.object(mass.metadata, "_update_artist_metadata", AsyncMock()) as update_artist,
    ):
        await mass.metadata._scan_missing_metadata()

    update_album.assert_awaited_once()
    assert update_album.await_args is not None
    assert update_album.await_args.args[0].item_id == bare.item_id
    assert update_album.await_args.kwargs == {"force_refresh": False}
    update_artist.assert_not_awaited()


async def test_scan_still_refreshes_artists_without_metadata(mass: MusicAssistant) -> None:
    """An artist without images or a description goes through the artist refresh."""
    artist = await mass.music.artists.add_item_to_library(
        Artist(item_id="0", provider="library", name="Radiohead", provider_mappings=_mapping("rh"))
    )

    with (
        patch.object(mass.metadata, "_update_album_metadata", AsyncMock()) as update_album,
        patch.object(mass.metadata, "_update_artist_metadata", AsyncMock()) as update_artist,
    ):
        await mass.metadata._scan_missing_metadata()

    update_artist.assert_awaited_once()
    assert update_artist.await_args is not None
    assert update_artist.await_args.args[0].item_id == artist.item_id
    assert update_artist.await_args.kwargs == {"force_refresh": False}
    update_album.assert_not_awaited()
