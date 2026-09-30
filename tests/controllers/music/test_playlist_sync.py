"""Tests for the library sync of the playlists of a music provider."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from music_assistant_models.enums import ImageType, MediaType
from music_assistant_models.media_items import (
    MediaItemImage,
    MediaItemMetadata,
    Playlist,
    ProviderMapping,
    UniqueList,
)

from music_assistant.controllers.music.media.base import SUPPRESS_MEDIA_ITEM_UPDATES
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant.controllers.music.media.playlists import PlaylistController
    from music_assistant.mass import MusicAssistant

# the server fixture below is module scoped, so the tests must share its event loop
pytestmark = pytest.mark.asyncio(loop_scope="module")

INSTANCE_ID = "spotify--test"
COLLAGE = MediaItemImage(type=ImageType.THUMB, path="collage.jpg", provider="playlist_metadata")


@pytest.fixture
def playlists(music_mass_module: MusicAssistant) -> PlaylistController:
    """Return the playlist controller of a database-only server."""
    return music_mass_module.music.playlists


def _thumb(path: str, provider: str = INSTANCE_ID) -> MediaItemImage:
    """Build a thumb image."""
    return MediaItemImage(type=ImageType.THUMB, path=path, provider=provider)


def _playlist(item_id: str, name: str, *images: str, **details: Any) -> Playlist:
    """Build the playlist as the provider lists it, with a thumb for each given image path."""
    return Playlist(
        item_id=item_id,
        provider=INSTANCE_ID,
        name=name,
        provider_mappings={
            ProviderMapping(
                item_id=item_id, provider_domain="spotify", provider_instance=INSTANCE_ID
            )
        },
        metadata=MediaItemMetadata(images=UniqueList([_thumb(path) for path in images])),
        **details,
    )


def _raise(_media_type: MediaType, _item_ref: str | None, err: Exception) -> None:
    """Raise the error of a failed sync item, so it fails the test instead of being skipped."""
    raise err


async def _sync(playlists: PlaylistController, prov_item: Playlist) -> Playlist:
    """Sync the given provider playlist into the library and return the library item."""
    provider = MusicProvider.__new__(MusicProvider)
    provider.mass = playlists.mass
    provider.config = Mock(instance_id=INSTANCE_ID, get_value=Mock(return_value=[]))
    provider.manifest = Mock(domain="spotify")
    provider.logger = Mock()

    async def get_library_playlists() -> AsyncGenerator[Playlist]:
        yield prov_item

    # the library sync runs without per-item events and provider write-backs
    token = SUPPRESS_MEDIA_ITEM_UPDATES.set(True)
    try:
        with patch.multiple(
            provider,
            get_library_playlists=get_library_playlists,
            _update_sync_task_item_status=Mock(),
            _handle_sync_item_failure=Mock(side_effect=_raise),
        ):
            await provider._sync_library_playlists()
    finally:
        SUPPRESS_MEDIA_ITEM_UPDATES.reset(token)
    library_item = await playlists.get_library_item_by_prov_mappings(prov_item.provider_mappings)
    assert library_item is not None
    return library_item


async def _store(playlists: PlaylistController, library_item: Playlist) -> None:
    """Write the given library item back, as the metadata enrichment does."""
    await playlists.update_item_in_library(library_item.item_id, library_item, overwrite=True)


@pytest.mark.parametrize(
    ("is_editable", "is_dynamic"),
    [(False, False), (False, True), (True, False)],
    ids=["static", "dynamic", "editable"],
)
async def test_rename_and_new_cover_follow_the_provider(
    playlists: PlaylistController, is_editable: bool, is_dynamic: bool
) -> None:
    """A renamed playlist with a new cover gets the new name and cover in the library."""
    item_id = uuid4().hex
    flags = {"is_editable": is_editable, "is_dynamic": is_dynamic}
    await _sync(playlists, _playlist(item_id, "Old name", "old.jpg", **flags))
    renamed = _playlist(item_id, "The new name", "new.jpg", **flags)

    library_item = await _sync(playlists, renamed)

    assert library_item.name == "The new name"
    assert library_item.sort_name == renamed.sort_name
    assert library_item.image is not None
    assert library_item.image.path == "new.jpg"
    assert "old.jpg" not in {img.path for img in library_item.metadata.images or []}


async def test_locally_added_data_is_kept(playlists: PlaylistController) -> None:
    """A generated collage and the genres of the library item survive a rename and new cover."""
    item_id = uuid4().hex
    library_item = await _sync(playlists, _playlist(item_id, "Old name", "old.jpg"))
    library_item.metadata.images = UniqueList([*(library_item.metadata.images or []), COLLAGE])
    library_item.metadata.genres = {"rock"}
    await _store(playlists, library_item)

    library_item = await _sync(playlists, _playlist(item_id, "New name", "new.jpg"))

    assert library_item.name == "New name"
    assert library_item.metadata.images == [_thumb("new.jpg"), COLLAGE]
    assert library_item.metadata.genres == {"rock"}


async def test_provider_update_and_rename_in_one_pass(playlists: PlaylistController) -> None:
    """A changed date_added and a rename in the same sync both reach the library."""
    item_id = uuid4().hex
    await _sync(playlists, _playlist(item_id, "Old name", "cover.jpg"))
    date_added = datetime(2026, 1, 2, tzinfo=UTC)

    library_item = await _sync(
        playlists, _playlist(item_id, "New name", "cover.jpg", date_added=date_added)
    )

    assert (library_item.name, library_item.date_added) == ("New name", date_added)


async def test_localized_name_follows_the_provider(playlists: PlaylistController) -> None:
    """A playlist with a localized name takes the provider's new name parameters."""
    item_id = uuid4().hex

    def liked_songs(user: str) -> Playlist:
        return _playlist(
            item_id, f"Liked Songs {user}", translation_key="liked_songs", translation_params=[user]
        )

    await _sync(playlists, liked_songs("Old"))

    library_item = await _sync(playlists, liked_songs("New"))

    assert (library_item.name, library_item.translation_params) == ("Liked Songs New", ["New"])


async def test_image_tagged_with_the_domain_is_replaced(playlists: PlaylistController) -> None:
    """An image stored under the provider domain is dropped when the provider's images change."""
    item_id = uuid4().hex
    library_item = await _sync(playlists, _playlist(item_id, "Name", "old.jpg"))
    library_item.metadata.images = UniqueList([_thumb("legacy.jpg", provider="spotify")])
    await _store(playlists, library_item)

    library_item = await _sync(playlists, _playlist(item_id, "Name", "new.jpg"))

    assert library_item.metadata.images == [_thumb("new.jpg")]


async def test_image_type_the_provider_does_not_supply_is_kept(
    playlists: PlaylistController,
) -> None:
    """A stored fanart of the provider is kept when the provider only supplies a new thumb."""
    item_id = uuid4().hex
    library_item = await _sync(playlists, _playlist(item_id, "Name", "old.jpg"))
    fanart = MediaItemImage(type=ImageType.FANART, path="/collage/fanart.jpg", provider=INSTANCE_ID)
    library_item.metadata.images = UniqueList([*(library_item.metadata.images or []), fanart])
    await _store(playlists, library_item)

    library_item = await _sync(playlists, _playlist(item_id, "Name", "new.jpg"))

    assert library_item.metadata.images == [_thumb("new.jpg"), fanart]


async def test_stored_images_are_kept_without_provider_images(
    playlists: PlaylistController,
) -> None:
    """A provider without images for the playlist leaves the stored images while the name follows."""
    item_id = uuid4().hex
    await _sync(playlists, _playlist(item_id, "Old name", "cover.jpg"))

    library_item = await _sync(playlists, _playlist(item_id, "New name"))

    assert library_item.name == "New name"
    assert library_item.metadata.images == [_thumb("cover.jpg")]


async def test_unchanged_playlist_is_not_written(playlists: PlaylistController) -> None:
    """A playlist whose name and images already match the provider is not written again."""
    item_id = uuid4().hex
    library_item = await _sync(playlists, _playlist(item_id, "Name", "cover.jpg"))
    other_instance_image = _thumb("other.jpg", provider="spotify--other")
    library_item.metadata.images = UniqueList(
        [*(library_item.metadata.images or []), COLLAGE, other_instance_image]
    )
    await _store(playlists, library_item)

    with patch.object(
        playlists, "update_item_in_library", wraps=playlists.update_item_in_library
    ) as update_item_in_library:
        await _sync(playlists, _playlist(item_id, "Name", "cover.jpg"))

    update_item_in_library.assert_not_called()
