"""Tests for storing and deleting custom images on the MetaDataController."""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.enums import ImageType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import MediaItemImage

from music_assistant.constants import CUSTOM_IMAGES_DIRNAME
from music_assistant.controllers.metadata import MetaDataController
from music_assistant.helpers.images import MAX_CUSTOM_IMAGE_BYTES
from tests.controllers.music.helpers import png_base64


def _image(path: str, provider: str = "builtin") -> MediaItemImage:
    """Build a thumb MediaItemImage for the given path."""
    return MediaItemImage(
        type=ImageType.THUMB, path=path, provider=provider, remotely_accessible=False
    )


@pytest.fixture
async def metadata_controller(
    cache_database: None,  # noqa: ARG001
    metadata_controller: MetaDataController,
) -> MetaDataController:
    """Construct a MetaDataController backed by the cache database."""
    return metadata_controller


@pytest.fixture
def images_dir(metadata_controller: MetaDataController) -> Path:
    """Return the custom images directory of the controller (not created yet)."""
    return Path(metadata_controller.mass.storage_path) / CUSTOM_IMAGES_DIRNAME


async def test_store_custom_image_writes_file(
    metadata_controller: MetaDataController, images_dir: Path
) -> None:
    """A valid upload lands on disk and the returned image references it."""
    image = await metadata_controller.store_custom_image(png_base64(), "genre.7")
    assert image.type == ImageType.THUMB
    assert image.provider == "builtin"
    assert not image.remotely_accessible
    assert image.path.startswith(f"{CUSTOM_IMAGES_DIRNAME}/genre.7.")
    assert image.path.endswith(".png")
    assert (images_dir.parent / image.path).is_file()


async def test_store_custom_image_accepts_data_url(
    metadata_controller: MetaDataController, images_dir: Path
) -> None:
    """A browser-style data-URL prefix is stripped before decoding."""
    image = await metadata_controller.store_custom_image(
        f"data:image/png;base64,{png_base64()}", "genre.7"
    )
    assert (images_dir.parent / image.path).is_file()


@pytest.mark.parametrize(
    "data",
    [
        pytest.param("not-valid-base64!!!", id="bad-base64"),
        pytest.param("Zm9vé", id="non-ascii"),
        pytest.param("A" * (MAX_CUSTOM_IMAGE_BYTES * 4 // 3 + 100), id="oversized"),
        pytest.param(
            base64.b64encode(b"<svg xmlns='http://www.w3.org/2000/svg'/>").decode(), id="svg"
        ),
    ],
)
async def test_store_custom_image_rejects_invalid_data(
    metadata_controller: MetaDataController, images_dir: Path, data: str
) -> None:
    """Invalid uploads raise InvalidDataError and leave no file behind."""
    with pytest.raises(InvalidDataError):
        await metadata_controller.store_custom_image(data, "genre.7")
    assert not images_dir.exists()


async def test_store_custom_image_gets_unique_path(
    metadata_controller: MetaDataController,
) -> None:
    """Two uploads with the same prefix get distinct paths."""
    first = await metadata_controller.store_custom_image(png_base64(), "genre.7")
    second = await metadata_controller.store_custom_image(png_base64(), "genre.7")
    assert first.path != second.path


async def test_delete_custom_image_removes_file_and_invalidates_cache(
    metadata_controller: MetaDataController, images_dir: Path
) -> None:
    """Deleting a custom image removes its file and drops its cached variants."""
    image = await metadata_controller.store_custom_image(png_base64(), "genre.7")
    invalidate = AsyncMock()
    metadata_controller.invalidate_image_cache = invalidate  # type: ignore[method-assign]
    await metadata_controller.delete_custom_image(image)
    assert not (images_dir.parent / image.path).exists()
    invalidate.assert_awaited_once_with("builtin", image.path)


async def test_delete_custom_image_ignores_non_custom_image(
    metadata_controller: MetaDataController, images_dir: Path
) -> None:
    """An image that is not a stored custom image is left alone."""
    other = images_dir.parent / "other.png"
    other.write_bytes(b"img")
    invalidate = AsyncMock()
    metadata_controller.invalidate_image_cache = invalidate  # type: ignore[method-assign]
    await metadata_controller.delete_custom_image(_image("other.png"))
    await metadata_controller.delete_custom_image(
        _image(f"{CUSTOM_IMAGES_DIRNAME}/x.png", provider="test--1")
    )
    assert other.is_file()
    invalidate.assert_not_awaited()


async def test_delete_custom_image_blocks_path_traversal(
    metadata_controller: MetaDataController, images_dir: Path
) -> None:
    """A crafted path cannot delete files outside the custom images directory."""
    outside = images_dir.parent / "outside.png"
    outside.write_bytes(b"img")
    await metadata_controller.delete_custom_image(_image(f"{CUSTOM_IMAGES_DIRNAME}/../outside.png"))
    assert outside.is_file()


async def test_delete_custom_image_tolerates_missing_file(
    metadata_controller: MetaDataController,
) -> None:
    """Deleting an already removed file does not raise."""
    await metadata_controller.delete_custom_image(_image(f"{CUSTOM_IMAGES_DIRNAME}/gone.png"))
