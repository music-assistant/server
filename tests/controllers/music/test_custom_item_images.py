"""Tests for the generic custom item image commands on the media controller base."""

from __future__ import annotations

import base64
import json
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ImageType
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError
from music_assistant_models.media_items import (
    MediaItemImage,
    MediaItemMetadata,
    ProviderMapping,
    Track,
)
from music_assistant_models.unique_list import UniqueList
from PIL import Image

from music_assistant.constants import CUSTOM_IMAGES_DIRNAME
from music_assistant.controllers.music.media.tracks import TracksController
from music_assistant.helpers.json import serialize_to_json


def _png_base64() -> str:
    """Return a minimal valid PNG as base64 string."""
    buf = BytesIO()
    Image.new("RGB", (4, 4), "red").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _image(
    path: str, image_type: ImageType = ImageType.THUMB, provider: str = "builtin"
) -> MediaItemImage:
    """Build a MediaItemImage for the given path."""
    return MediaItemImage(type=image_type, path=path, provider=provider, remotely_accessible=False)


def _track(images: list[MediaItemImage]) -> Track:
    """Build a minimal library Track carrying the given images."""
    return Track(
        item_id="1",
        provider="library",
        name="Test Track",
        provider_mappings={
            ProviderMapping(item_id="1", provider_domain="test", provider_instance="test--1")
        },
        metadata=MediaItemMetadata(images=UniqueList(images) or None),
    )


def _controller(tmp_path: Path, images: list[MediaItemImage]) -> tuple[TracksController, MagicMock]:
    """Build a TracksController with mocked mass whose db item carries the given images."""
    mass = MagicMock()
    mass.storage_path = str(tmp_path)
    mass.music.database = AsyncMock()
    mass.music.database.deferred_commit = MagicMock()
    metadata = MediaItemMetadata(images=UniqueList(images) or None)
    mass.music.database.get_row = AsyncMock(return_value={"metadata": serialize_to_json(metadata)})
    mass.metadata.invalidate_image_cache = AsyncMock()
    mass.get_provider.return_value = None
    controller = TracksController(mass)
    controller.get_library_item = AsyncMock(return_value=_track(images))  # type: ignore[method-assign]
    return controller, mass


def _stored_images(mass: MagicMock) -> list[dict[str, str]]:
    """Return the images list from the metadata persisted to the db."""
    update_call = mass.music.database.update.await_args
    metadata = json.loads(update_call.args[2]["metadata"])
    return metadata.get("images") or []


async def test_set_image_stores_file_and_updates_metadata(tmp_path: Path) -> None:
    """A valid upload lands on disk and replaces the thumb in the stored metadata."""
    controller, mass = _controller(tmp_path, [_image("Artist/cover.jpg", provider="test--1")])
    await controller.set_item_image("1", _png_base64())
    images = _stored_images(mass)
    assert len(images) == 1
    assert images[0]["path"].startswith(f"{CUSTOM_IMAGES_DIRNAME}/track.1.")
    assert images[0]["path"].endswith(".png")
    assert images[0]["provider"] == "builtin"
    stored_file = tmp_path / images[0]["path"]
    assert stored_file.is_file()
    # the replaced thumb got its cached variants invalidated
    mass.metadata.invalidate_image_cache.assert_awaited_once_with("test--1", "Artist/cover.jpg")


async def test_set_image_keeps_other_image_types(tmp_path: Path) -> None:
    """Only images of the uploaded type are replaced; other types are untouched."""
    fanart = _image("Artist/fanart.jpg", image_type=ImageType.FANART, provider="test--1")
    controller, mass = _controller(tmp_path, [fanart])
    await controller.set_item_image("1", _png_base64())
    images = _stored_images(mass)
    assert len(images) == 2
    paths_by_type = {img["type"]: img["path"] for img in images}
    assert paths_by_type["fanart"] == "Artist/fanart.jpg"
    assert paths_by_type["thumb"].startswith(f"{CUSTOM_IMAGES_DIRNAME}/")


async def test_set_image_replaces_previous_custom_image(tmp_path: Path) -> None:
    """Replacing a custom image deletes the previous file from disk."""
    images_dir = tmp_path / CUSTOM_IMAGES_DIRNAME
    images_dir.mkdir()
    old_file = images_dir / "track.1.oldsuffix.png"
    old_file.write_bytes(b"old")
    controller, mass = _controller(
        tmp_path, [_image(f"{CUSTOM_IMAGES_DIRNAME}/track.1.oldsuffix.png")]
    )
    await controller.set_item_image("1", _png_base64())
    assert not old_file.exists()
    mass.metadata.invalidate_image_cache.assert_awaited_once_with(
        "builtin", f"{CUSTOM_IMAGES_DIRNAME}/track.1.oldsuffix.png"
    )
    new_files = list(images_dir.iterdir())
    assert len(new_files) == 1
    assert new_files[0].name.startswith("track.1.")


async def test_set_image_accepts_data_url(tmp_path: Path) -> None:
    """A browser-style data-URL prefix is stripped before decoding."""
    controller, mass = _controller(tmp_path, [])
    await controller.set_item_image("1", f"data:image/png;base64,{_png_base64()}")
    assert _stored_images(mass)[0]["path"].startswith(f"{CUSTOM_IMAGES_DIRNAME}/")


async def test_set_image_invalid_base64_rejected(tmp_path: Path) -> None:
    """Garbage base64 raises InvalidDataError and leaves no file behind."""
    controller, _mass = _controller(tmp_path, [])
    with pytest.raises(InvalidDataError):
        await controller.set_item_image("1", "not-valid-base64!!!")
    assert not (tmp_path / CUSTOM_IMAGES_DIRNAME).exists()


async def test_set_image_oversized_rejected(tmp_path: Path) -> None:
    """An upload above the size limit is rejected before decoding."""
    controller, _mass = _controller(tmp_path, [])
    oversized = "A" * (10 * 1024 * 1024 * 4 // 3 + 100)
    with pytest.raises(InvalidDataError):
        await controller.set_item_image("1", oversized)
    assert not (tmp_path / CUSTOM_IMAGES_DIRNAME).exists()


async def test_set_image_unsupported_format_rejected(tmp_path: Path) -> None:
    """A non-raster upload (svg) is rejected and leaves no file behind."""
    controller, _mass = _controller(tmp_path, [])
    svg = base64.b64encode(b"<svg xmlns='http://www.w3.org/2000/svg'/>").decode()
    with pytest.raises(InvalidDataError):
        await controller.set_item_image("1", svg)
    assert not (tmp_path / CUSTOM_IMAGES_DIRNAME).exists()


async def test_set_image_unknown_item(tmp_path: Path) -> None:
    """An unknown item id raises MediaNotFoundError and leaves no file behind."""
    controller, mass = _controller(tmp_path, [])
    mass.music.database.get_row = AsyncMock(return_value=None)
    with pytest.raises(MediaNotFoundError):
        await controller.set_item_image("42", _png_base64())
    assert not (tmp_path / CUSTOM_IMAGES_DIRNAME).exists()


async def test_remove_image_without_custom_image_is_noop(tmp_path: Path) -> None:
    """Removing when no custom image exists changes nothing."""
    controller, mass = _controller(tmp_path, [_image("Artist/cover.jpg", provider="test--1")])
    await controller.remove_item_image("1")
    mass.music.database.update.assert_not_awaited()
    mass.metadata.invalidate_image_cache.assert_not_awaited()


async def test_remove_image_deletes_file(tmp_path: Path) -> None:
    """Removing a custom image deletes its file and drops it from metadata."""
    images_dir = tmp_path / CUSTOM_IMAGES_DIRNAME
    images_dir.mkdir()
    custom_file = images_dir / "track.1.somesuffix.png"
    custom_file.write_bytes(b"img")
    custom_img = _image(f"{CUSTOM_IMAGES_DIRNAME}/track.1.somesuffix.png")
    controller, mass = _controller(tmp_path, [custom_img])
    await controller.remove_item_image("1")
    assert not custom_file.exists()
    assert _stored_images(mass) == []
    mass.metadata.invalidate_image_cache.assert_awaited_once_with("builtin", custom_img.path)


async def test_hard_delete_removes_custom_image_file(tmp_path: Path) -> None:
    """Deleting a library item cleans up its custom image file."""
    images_dir = tmp_path / CUSTOM_IMAGES_DIRNAME
    images_dir.mkdir()
    custom_file = images_dir / "track.1.somesuffix.png"
    custom_file.write_bytes(b"img")
    custom_img = _image(f"{CUSTOM_IMAGES_DIRNAME}/track.1.somesuffix.png")
    controller, _mass = _controller(tmp_path, [custom_img])
    await controller.remove_item_from_library("1", recursive=False)
    assert not custom_file.exists()
