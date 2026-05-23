"""Tests for the imageproxy id system on the MetaDataController."""

import asyncio
import hashlib

import pytest

from music_assistant.controllers.metadata import (
    CACHE_CATEGORY_IMAGE_IDS,
    MetaDataController,
    _is_safe_imageproxy_request_path,
)
from music_assistant.mass import MusicAssistant


@pytest.fixture
async def metadata_controller(mass_minimal: MusicAssistant) -> MetaDataController:
    """Construct a MetaDataController with the minimal MA fixture."""
    await mass_minimal.cache._setup_database()
    controller = MetaDataController(mass_minimal)
    mass_minimal.metadata = controller
    return controller


async def test_compute_image_id_is_deterministic(metadata_controller: MetaDataController) -> None:
    """The same (provider, path) must always produce the same hex id."""
    image_id_a = metadata_controller.compute_image_id("filesystem", "/local/cover.jpg")
    image_id_b = metadata_controller.compute_image_id("filesystem", "/local/cover.jpg")
    assert image_id_a == image_id_b
    expected = hashlib.sha256(b"filesystem//local/cover.jpg").hexdigest()
    assert image_id_a == expected


async def test_compute_image_id_differs_per_input(metadata_controller: MetaDataController) -> None:
    """Different (provider, path) pairs must produce different ids."""
    assert metadata_controller.compute_image_id("x", "/a") != metadata_controller.compute_image_id(
        "x", "/b"
    )
    assert metadata_controller.compute_image_id("x", "/a") != metadata_controller.compute_image_id(
        "y", "/a"
    )


async def test_resolve_image_id_via_in_memory_lru(
    metadata_controller: MetaDataController,
) -> None:
    """A freshly computed id resolves from the in-memory LRU without hitting DB."""
    image_id = metadata_controller.compute_image_id("filesystem", "/local/cover.jpg")
    resolved = await metadata_controller.resolve_image_id(image_id)
    assert resolved == ("filesystem", "/local/cover.jpg")


async def test_resolve_image_id_via_cache_db(metadata_controller: MetaDataController) -> None:
    """After the async persist runs, the mapping resolves from cache even with empty LRU."""
    image_id = metadata_controller.compute_image_id("filesystem", "/local/cover.jpg")
    # let the scheduled persist task drain
    for _ in range(20):
        await asyncio.sleep(0)
        raw = await metadata_controller.cache.get(key=image_id, category=CACHE_CATEGORY_IMAGE_IDS)
        if raw is not None:
            break
    assert raw == {"provider": "filesystem", "path": "/local/cover.jpg"}
    # wipe the in-memory layer so we hit the cache db
    metadata_controller._image_id_lru.clear()
    resolved = await metadata_controller.resolve_image_id(image_id)
    assert resolved == ("filesystem", "/local/cover.jpg")


async def test_resolve_image_id_returns_none_for_unknown(
    metadata_controller: MetaDataController,
) -> None:
    """An id that was never registered must resolve to None (→ 404)."""
    resolved = await metadata_controller.resolve_image_id("0" * 64)
    assert resolved is None


async def test_image_id_persists_with_persistent_flag(
    metadata_controller: MetaDataController,
) -> None:
    """Mappings must survive a `clear()` without include_persistent."""
    image_id = metadata_controller.compute_image_id("filesystem", "/persistent.jpg")
    # wait for the scheduled persist task to land in the cache db
    for _ in range(20):
        await asyncio.sleep(0)
        raw = await metadata_controller.cache.get(key=image_id, category=CACHE_CATEGORY_IMAGE_IDS)
        if raw is not None:
            break
    assert raw is not None
    # simulate the user-facing "Reset cache" action: clear() without include_persistent
    await metadata_controller.cache.clear()
    metadata_controller._image_id_lru.clear()
    resolved = await metadata_controller.resolve_image_id(image_id)
    assert resolved == ("filesystem", "/persistent.jpg")


def test_is_safe_imageproxy_request_path_allows_safe_inputs() -> None:
    """http(s) and bare-path inputs are accepted."""
    assert _is_safe_imageproxy_request_path("https://cdn.example.com/a.jpg")
    assert _is_safe_imageproxy_request_path("http://cdn.example.com/a.jpg")
    assert _is_safe_imageproxy_request_path("/local/cover.jpg")
    assert _is_safe_imageproxy_request_path("cover.jpg")


def test_is_safe_imageproxy_request_path_rejects_dangerous_schemes() -> None:
    """file://, data:, gopher://, javascript: and friends are rejected."""
    assert not _is_safe_imageproxy_request_path("file:///etc/passwd")
    assert not _is_safe_imageproxy_request_path("FILE:///etc/passwd")
    assert not _is_safe_imageproxy_request_path("data:image/png;base64,iVBORw0KG")
    assert not _is_safe_imageproxy_request_path("gopher://example.com/")
    assert not _is_safe_imageproxy_request_path("javascript:alert(1)")
    assert not _is_safe_imageproxy_request_path("ftp://example.com/a.jpg")
