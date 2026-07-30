"""
Parity test for the summary fast path of the library list endpoints.

``library_items(summary=True)`` returns slim summary variants of the media item
models, built directly from a slim SQL query instead of hydrating the full stored
item JSON. Every field a summary item carries must match the full item. Provider
mappings are carried in full (so clients keep the quality badge, availability,
provider icon and in-library state); the rest of the serialized (wire) form stays
sparse: no null values and none of the heavy metadata fields.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.enums import ImageType
from music_assistant_models.media_items import (
    Audiobook,
    ItemMapping,
    MediaItemImage,
    Playlist,
    ProviderMapping,
    Radio,
    UniqueList,
)
from music_assistant_models.media_items.metadata import IMAGE_PROXY_ID_RESOLVER
from music_assistant_models.translations import TRANSLATION_RESOLVER

from tests.common import wait_for_sync_completion
from tests.integration.conftest import wait_for

if TYPE_CHECKING:
    from music_assistant.controllers.music.media.base import MediaControllerBase
    from music_assistant.mass import MusicAssistant

# object-level fields a summary item may carry; compared against the full item when present
COMPARED_FIELDS = (
    "name",
    "sort_name",
    "favorite",
    "available",
    "version",
    "duration",
    "year",
    "album_type",
    "artist_type",
    "owner",
    "is_editable",
    "is_dynamic",
    "supported_mediatypes",
    "publisher",
    "total_episodes",
    "translation_key",
    "content_type",
    "fully_played",
    "resume_position_ms",
    "disc_number",
    "track_number",
)


def _assert_no_none_values(obj: Any, path: str) -> None:
    """Recursively assert a serialized summary dict contains no null values."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            assert value is not None, f"unexpected null value at {path}.{key}"
            _assert_no_none_values(value, f"{path}.{key}")
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            _assert_no_none_values(value, f"{path}[{index}]")


@contextmanager
def _api_serialization_context(mass: MusicAssistant) -> Iterator[None]:
    """Bind the per-request resolvers the API layer sets during serialization."""
    token = IMAGE_PROXY_ID_RESOLVER.set(mass.metadata.compute_image_id)
    token_loc = TRANSLATION_RESOLVER.set(partial(mass.translations.get_translation, locale="en_US"))
    try:
        yield
    finally:
        IMAGE_PROXY_ID_RESOLVER.reset(token)
        TRANSLATION_RESOLVER.reset(token_loc)


def _mapping_names(value: Any) -> list[tuple[str, str]]:
    """Normalize a list of artist mappings/strings to comparable (item_id, name) pairs."""
    result = []
    for entry in value or []:
        if isinstance(entry, str):
            result.append(("", entry))
        else:
            result.append((str(entry.item_id), entry.name))
    return result


def _first_thumb(item: Any) -> MediaItemImage | None:
    """Return the first thumb image of a media item (or None)."""
    for image in item.metadata.images or []:
        if image.type == ImageType.THUMB:
            return cast("MediaItemImage", image)
    return None


def _sorted_provider_mappings(item_dict: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a serialized item's provider mappings, sorted for stable comparison."""
    mappings = item_dict.get("provider_mappings") or []
    return sorted(mappings, key=lambda m: (m["provider_instance"], m["item_id"]))


async def _seed_playlist_and_radio(mass: MusicAssistant) -> None:
    """Add a playlist and radio station to the library (the test provider syncs neither)."""
    test_prov = mass.get_provider("test")
    assert test_prov is not None
    image = MediaItemImage(
        type=ImageType.THUMB,
        path="http://example.com/thumb.jpg",
        provider=test_prov.instance_id,
        remotely_accessible=True,
    )
    playlist = Playlist(
        item_id="pl_1",
        provider=test_prov.instance_id,
        name="Test Playlist",
        owner="tester",
        is_editable=True,
        provider_mappings={
            ProviderMapping(
                item_id="pl_1",
                provider_domain="test",
                provider_instance=test_prov.instance_id,
                in_library=True,
            )
        },
    )
    playlist.metadata.images = UniqueList([image])
    playlist.metadata.description = "A hand-curated test playlist."
    await mass.music.playlists.add_item_to_library(playlist)
    radio = Radio(
        item_id="radio_1",
        provider=test_prov.instance_id,
        name="Test Radio",
        provider_mappings={
            ProviderMapping(
                item_id="radio_1",
                provider_domain="test",
                provider_instance=test_prov.instance_id,
                in_library=True,
            )
        },
    )
    radio.metadata.images = UniqueList([image])
    radio.metadata.description = "A test radio station."
    await mass.music.radio.add_item_to_library(radio)


async def test_library_summary_items_parity(e2e_mass: MusicAssistant) -> None:
    """Summary items must match their full counterparts on every retained field."""
    mass = e2e_mass
    # wait for the initial (auto-scheduled) sync of the test provider and the
    # follow-up genre scan so the library is fully populated
    async with wait_for_sync_completion(mass):
        await mass.music.start_sync()
    await wait_for(lambda: not mass.music.active_sync_tasks)
    await _seed_playlist_and_radio(mass)

    controllers: tuple[MediaControllerBase[Any], ...] = (
        mass.music.artists,
        mass.music.albums,
        mass.music.tracks,
        mass.music.playlists,
        mass.music.radio,
        mass.music.audiobooks,
        mass.music.podcasts,
        mass.music.genres,
    )
    for ctrl in controllers:
        full_items = await ctrl.library_items(order_by="name")
        summary_items = await ctrl.library_items(order_by="name", summary=True)
        assert len(full_items) > 0, f"no library items for {ctrl.media_type.value}"
        assert [x.item_id for x in summary_items] == [x.item_id for x in full_items]

        for full, item in zip(full_items, summary_items, strict=True):
            # same model type (summary subclass), same identity
            assert isinstance(item, ctrl.item_cls)
            assert type(item) is ctrl.summary_item_cls
            assert item.provider == "library"
            assert item.uri == full.uri
            for field_name in COMPARED_FIELDS:
                if not hasattr(item, field_name):
                    continue
                assert getattr(item, field_name) == getattr(full, field_name), (
                    f"{ctrl.media_type.value} {item.item_id}: field {field_name} differs"
                )
            # artist/album/author mappings resolve to the same library items
            if hasattr(item, "artists"):
                assert _mapping_names(item.artists) == _mapping_names(full.artists)
            if hasattr(item, "album") and full.album:
                assert isinstance(item.album, ItemMapping)
                assert item.album.item_id == full.album.item_id
                assert item.album.name == full.album.name
                assert item.album.year == full.album.year
            if isinstance(item, Audiobook):
                assert isinstance(full, Audiobook)
                assert _mapping_names(item.authors) == _mapping_names(full.authors)
                assert _mapping_names(item.narrators) == _mapping_names(full.narrators)
            # the (single) summary image is the same image the full item shows first
            full_thumb = _first_thumb(full)
            summary_thumb = _first_thumb(item)
            if full_thumb:
                assert summary_thumb is not None
                assert summary_thumb.path == full_thumb.path
                assert summary_thumb.provider == full_thumb.provider

            # wire format: provider mappings are carried in full and must match the
            # full item exactly (the frontend reads audio_format for the quality badge,
            # availability, the provider icon and in-library state from here); the rest
            # of the wire form stays sparse (no nulls) and free of heavy metadata, and
            # the image dicts carry the same resolved proxy_id / localized names; bind
            # the same resolvers the API layer sets while serializing a response
            with _api_serialization_context(mass):
                full_dict = full.to_dict()
                summary_dict = item.to_dict()
            assert _sorted_provider_mappings(summary_dict) == _sorted_provider_mappings(full_dict)
            wire = {k: v for k, v in summary_dict.items() if k != "provider_mappings"}
            _assert_no_none_values(wire, ctrl.media_type.value)
            assert summary_dict["name"] == full_dict["name"]
            # summary metadata carries only the thumb image plus the few descriptive
            # fields the list rows render: the explicit flag and release date (tracks) and
            # the description (radio/playlist). A description may also be injected by the
            # translation resolver for localizable items; either way both modes must carry
            # the same value.
            assert set(summary_dict["metadata"]) <= {
                "images",
                "explicit",
                "release_date",
                "description",
            }
            for meta_field in ("description", "release_date"):
                if meta_field in summary_dict["metadata"]:
                    assert summary_dict["metadata"][meta_field] == full_dict["metadata"][meta_field]
            if full_thumb:
                assert summary_dict["metadata"]["images"][0] == full_dict["metadata"]["images"][0]
                assert summary_dict["metadata"]["images"][0]["proxy_id"]


async def test_library_summary_items_filters(e2e_mass: MusicAssistant) -> None:
    """The summary fast path composes with the regular list filters and paging."""
    mass = e2e_mass
    async with wait_for_sync_completion(mass):
        await mass.music.start_sync()
    await wait_for(lambda: not mass.music.active_sync_tasks)

    full_items = await mass.music.tracks.library_items(order_by="name")
    assert len(full_items) > 2
    # paging
    page = await mass.music.tracks.library_items(order_by="name", limit=2, offset=1, summary=True)
    assert [x.item_id for x in page] == [x.item_id for x in full_items[1:3]]
    # search
    needle = full_items[0].name
    full_search = await mass.music.tracks.library_items(search=needle)
    summary_search = await mass.music.tracks.library_items(search=needle, summary=True)
    assert sorted(x.item_id for x in summary_search) == sorted(x.item_id for x in full_search)
    # favorite filter
    await mass.music.tracks.set_favorite(full_items[0].item_id, True)
    favorites = await mass.music.tracks.library_items(favorite=True, summary=True)
    assert [x.item_id for x in favorites] == [full_items[0].item_id]
    assert favorites[0].favorite is True
