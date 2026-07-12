"""Tests for the imageproxy id system on the MetaDataController."""

import asyncio
import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from music_assistant_models.enums import ImageType
from music_assistant_models.media_items import (
    MediaItemImage,
    MediaItemMetadata,
    ProviderMapping,
    Track,
)
from music_assistant_models.media_items.metadata import IMAGE_PROXY_ID_RESOLVER
from music_assistant_models.unique_list import UniqueList
from PIL import Image

from music_assistant.controllers.metadata import MetaDataController
from music_assistant.controllers.metadata.constants import (
    _IMAGE_ID_CACHE_TTL,
    _IMAGEPROXY_CONTENT_TYPES,
    CACHE_CATEGORY_IMAGE_IDS,
)
from music_assistant.controllers.metadata.helpers import (
    _normalize_imageproxy_format,
)
from music_assistant.helpers import images as images_helper
from music_assistant.helpers.colors import get_palette
from music_assistant.helpers.images import (
    _THUMB_CACHE_VERSION,
    _THUMB_FILENAME_RE,
    _extract_imageproxy_id,
    _has_alpha,
    _thumb_cache_filename,
    create_thumb_hash,
    detect_image_content_format,
    get_image_thumb,
    is_svg_data,
    player_image_url,
)
from music_assistant.mass import MusicAssistant


@pytest.fixture
async def metadata_controller(mass_minimal: MusicAssistant) -> MetaDataController:
    """Construct a MetaDataController with the minimal MA fixture."""
    await mass_minimal.cache._setup_database()
    controller = MetaDataController(mass_minimal)
    mass_minimal.metadata = controller
    return controller


async def _wait_for_persisted_image_id(
    controller: MetaDataController, image_id: str, timeout: float = 2.0
) -> dict[str, str]:
    """
    Wait until `_persist_image_id` has written its row, or fail the test.

    `_persist_image_id` runs on the loop but does real work (json dump on a
    thread + sqlite write), so a `sleep(0)` busy-loop can race on slow CI.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        raw = await controller.cache.get(
            key=image_id,
            category=CACHE_CATEGORY_IMAGE_IDS,
            provider=controller.domain,
        )
        if raw is not None:
            assert isinstance(raw, dict)
            return raw
        await asyncio.sleep(0.01)
    raise AssertionError(f"image_id {image_id!r} not persisted within {timeout}s")


async def test_compute_image_id_is_deterministic(metadata_controller: MetaDataController) -> None:
    """The same (provider, path) must always produce the same hex id."""
    image_id_a = metadata_controller.compute_image_id("filesystem", "/local/cover.jpg")
    image_id_b = metadata_controller.compute_image_id("filesystem", "/local/cover.jpg")
    assert image_id_a == image_id_b
    expected = hashlib.sha256(b"filesystem//local/cover.jpg").hexdigest()
    assert image_id_a == expected


async def test_image_id_matches_thumb_and_palette_cache_key(
    metadata_controller: MetaDataController,
) -> None:
    """
    image_id must equal create_thumb_hash(provider, path).

    The thumbnail and color-palette caches both key off
    `create_thumb_hash(provider, path)`. If `compute_image_id` ever diverged
    from that, palette lookups for /imageproxy/<id> URLs would miss and the
    on-disk thumbnail cache would split into two buckets.
    """
    image_id = metadata_controller.compute_image_id("filesystem", "/local/cover.jpg")
    assert image_id == create_thumb_hash("filesystem", "/local/cover.jpg")


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
    raw = await _wait_for_persisted_image_id(metadata_controller, image_id)
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
    await _wait_for_persisted_image_id(metadata_controller, image_id)
    # simulate the user-facing "Reset cache" action: clear() without include_persistent
    await metadata_controller.cache.clear()
    metadata_controller._image_id_lru.clear()
    resolved = await metadata_controller.resolve_image_id(image_id)
    assert resolved == ("filesystem", "/persistent.jpg")


def _track_with_image(item_id: str, image: MediaItemImage) -> Track:
    """Build a minimal serializable Track carrying the given image."""
    return Track(
        item_id=item_id,
        provider="library",
        name=f"Track {item_id}",
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain="filesystem",
                provider_instance="filesystem--test",
            )
        },
        metadata=MediaItemMetadata(images=UniqueList([image])),
    )


async def _wait_for_expiration_refresh(
    controller: MetaDataController, image_id: str, old_expires: int, timeout: float = 2.0
) -> int:
    """Wait until the stored row's expiration moved past `old_expires`, or fail."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        expires = await controller.cache.get_expiration(
            key=image_id,
            category=CACHE_CATEGORY_IMAGE_IDS,
            provider=controller.domain,
        )
        if expires is not None and expires > old_expires:
            return expires
        await asyncio.sleep(0.01)
    raise AssertionError(f"expiration for {image_id!r} not refreshed within {timeout}s")


async def test_serialize_twice_hashes_once_and_persists_once(
    metadata_controller: MetaDataController, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Serializing the same item list twice hashes and persists each unique image only once.

    The proxy_id injection runs per image occurrence per outbound message, so
    repeat serializations must be served from the forward memo: no hashing at
    all on the second pass and exactly one cache write per unique image.
    """
    hash_calls: list[tuple[str, str]] = []

    def counting_thumb_hash(provider: str, path: str) -> str:
        hash_calls.append((provider, path))
        return create_thumb_hash(provider, path)

    monkeypatch.setattr(
        "music_assistant.controllers.metadata.images.create_thumb_hash", counting_thumb_hash
    )
    persist_calls: list[str] = []
    real_cache_set = metadata_controller.cache.set

    async def counting_cache_set(*args: Any, **kwargs: Any) -> None:
        if kwargs.get("category") == CACHE_CATEGORY_IMAGE_IDS:
            persist_calls.append(kwargs["key"])
        await real_cache_set(*args, **kwargs)

    monkeypatch.setattr(metadata_controller.cache, "set", counting_cache_set)

    # three image occurrences per pass, but only two unique images
    image_a = MediaItemImage(type=ImageType.THUMB, path="/album_a.jpg", provider="filesystem")
    image_b = MediaItemImage(type=ImageType.THUMB, path="/album_b.jpg", provider="filesystem")
    tracks = [
        _track_with_image("1", image_a),
        _track_with_image("2", image_a),
        _track_with_image("3", image_b),
    ]
    token = IMAGE_PROXY_ID_RESOLVER.set(metadata_controller.compute_image_id)
    try:
        first_pass = [track.to_dict() for track in tracks]
        assert len(hash_calls) == 2
        second_pass = [track.to_dict() for track in tracks]
    finally:
        IMAGE_PROXY_ID_RESOLVER.reset(token)
    # zero hashing on the second pass: everything came from the forward memo
    assert len(hash_calls) == 2
    assert first_pass == second_pass
    expected_ids = sorted(
        create_thumb_hash("filesystem", path) for path in ("/album_a.jpg", "/album_b.jpg")
    )
    for track_dict in first_pass:
        assert track_dict["metadata"]["images"][0]["proxy_id"] in expected_ids
    # exactly one persist per unique image across both passes
    for image_id in expected_ids:
        await _wait_for_persisted_image_id(metadata_controller, image_id)
    assert sorted(persist_calls) == expected_ids


async def test_persist_skipped_when_row_already_fresh(
    metadata_controller: MetaDataController, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mapping already stored with plenty of TTL left must not be rewritten."""
    provider, path = "filesystem", "/prestored.jpg"
    image_id = create_thumb_hash(provider, path)
    # simulate a previous process run that already stored the mapping
    await metadata_controller.cache.set(
        key=image_id,
        data={"provider": provider, "path": path},
        category=CACHE_CATEGORY_IMAGE_IDS,
        provider=metadata_controller.domain,
        expiration=_IMAGE_ID_CACHE_TTL,
        persistent=True,
    )
    probes: list[str] = []
    real_get_expiration = metadata_controller.cache.get_expiration

    async def recording_get_expiration(*args: Any, **kwargs: Any) -> int | None:
        result = await real_get_expiration(*args, **kwargs)
        probes.append(kwargs.get("key") or args[0])
        return result

    writes: list[str] = []
    real_cache_set = metadata_controller.cache.set

    async def recording_cache_set(*args: Any, **kwargs: Any) -> None:
        writes.append(kwargs.get("key") or args[0])
        await real_cache_set(*args, **kwargs)

    monkeypatch.setattr(metadata_controller.cache, "get_expiration", recording_get_expiration)
    monkeypatch.setattr(metadata_controller.cache, "set", recording_cache_set)

    assert metadata_controller.compute_image_id(provider, path) == image_id
    # wait for the freshness probe, then give the persist task time to (not) write
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 2.0
    while not probes and loop.time() < deadline:
        await asyncio.sleep(0.01)
    assert probes == [image_id]
    await asyncio.sleep(0.05)
    assert writes == []
    # the pre-stored row keeps the id resolvable, also without the in-memory layer
    metadata_controller._image_id_lru.clear()
    assert await metadata_controller.resolve_image_id(image_id) == (provider, path)


async def test_persist_refreshes_stale_row(metadata_controller: MetaDataController) -> None:
    """A stored row past half its TTL is rewritten so the id stays resolvable."""
    provider, path = "filesystem", "/stale.jpg"
    image_id = create_thumb_hash(provider, path)
    # a row whose remaining TTL is way below half of _IMAGE_ID_CACHE_TTL
    await metadata_controller.cache.set(
        key=image_id,
        data={"provider": provider, "path": path},
        category=CACHE_CATEGORY_IMAGE_IDS,
        provider=metadata_controller.domain,
        expiration=1000,
        persistent=True,
    )
    old_expires = await metadata_controller.cache.get_expiration(
        key=image_id,
        category=CACHE_CATEGORY_IMAGE_IDS,
        provider=metadata_controller.domain,
    )
    assert old_expires is not None
    assert metadata_controller.compute_image_id(provider, path) == image_id
    await _wait_for_expiration_refresh(metadata_controller, image_id, old_expires)


async def test_imageproxy_resolves_after_restart(
    metadata_controller: MetaDataController, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    An id persisted by one process run must resolve on the imageproxy of the next.

    Simulates a restart with a second controller instance on the same cache
    database: cold in-memory maps, so resolution can only come from the
    persisted row.
    """
    provider, path = "filesystem", "/restart/cover.jpg"
    image_id = metadata_controller.compute_image_id(provider, path)
    await _wait_for_persisted_image_id(metadata_controller, image_id)

    restarted = MetaDataController(metadata_controller.mass)
    assert not restarted._image_id_forward
    assert not restarted._image_id_lru
    served: list[tuple[str, str]] = []

    async def fake_serve_thumbnail(
        path_arg: str, provider_arg: str, _size: int, _image_format: str
    ) -> web.Response:
        served.append((provider_arg, path_arg))
        return web.Response(status=200)

    monkeypatch.setattr(restarted, "_serve_thumbnail", fake_serve_thumbnail)
    request = MagicMock()
    request.path = f"/imageproxy/{image_id}"
    request.query = {}
    response = await restarted.handle_imageproxy(request)
    assert response.status == 200
    assert served == [(provider, path)]


async def test_no_repersist_after_in_memory_eviction(
    metadata_controller: MetaDataController, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-encountering an image after memo/LRU eviction must not hit the cache db again."""
    provider, path = "filesystem", "/evicted.jpg"
    image_id = metadata_controller.compute_image_id(provider, path)
    await _wait_for_persisted_image_id(metadata_controller, image_id)
    # simulate LRU pressure evicting the in-memory maps (markers survive)
    metadata_controller._image_id_forward.clear()
    metadata_controller._image_id_lru.clear()

    calls: list[str] = []

    async def recording_get_expiration(*_args: object, **_kwargs: object) -> int | None:
        calls.append("probe")
        return None

    async def recording_cache_set(*_args: object, **_kwargs: object) -> None:
        calls.append("write")

    monkeypatch.setattr(metadata_controller.cache, "get_expiration", recording_get_expiration)
    monkeypatch.setattr(metadata_controller.cache, "set", recording_cache_set)

    # re-encounter re-hashes (memo was evicted) but the persist marker is fresh,
    # so no persist work is scheduled at all
    assert metadata_controller.compute_image_id(provider, path) == image_id
    await asyncio.sleep(0.05)
    assert calls == []


def test_normalize_imageproxy_format() -> None:
    """Client `fmt` values are lowercased / trimmed and validated."""
    assert _normalize_imageproxy_format("jpg") == "jpg"
    assert _normalize_imageproxy_format("JPG") == "jpg"
    assert _normalize_imageproxy_format("jpeg") == "jpeg"
    assert _normalize_imageproxy_format("PNG") == "png"
    assert _normalize_imageproxy_format("svg") == "svg"
    assert _normalize_imageproxy_format("  png  ") == "png"
    # invalid / empty input falls through so caller can detect from path
    assert _normalize_imageproxy_format("") is None
    assert _normalize_imageproxy_format(None) is None
    assert _normalize_imageproxy_format("gif") is None
    assert _normalize_imageproxy_format("bmp") is None


def test_imageproxy_content_types_are_standards_compliant() -> None:
    """Every accepted fmt must produce a standards-compliant MIME type."""
    assert _IMAGEPROXY_CONTENT_TYPES["jpg"] == "image/jpeg"
    assert _IMAGEPROXY_CONTENT_TYPES["jpeg"] == "image/jpeg"
    assert _IMAGEPROXY_CONTENT_TYPES["png"] == "image/png"
    assert _IMAGEPROXY_CONTENT_TYPES["svg"] == "image/svg+xml"


def test_extract_imageproxy_id_matches_path_only() -> None:
    """Only URLs whose path begins with /imageproxy/ should yield an id."""
    valid = "http://mass.local/imageproxy/" + "a" * 64 + "?size=256"
    assert _extract_imageproxy_id(valid) == "a" * 64
    # an id-shaped substring inside a query string must not match
    decoy = "http://other.example.com/foo?next=/imageproxy/" + "b" * 64
    assert _extract_imageproxy_id(decoy) is None
    # invalid id length / charset
    assert _extract_imageproxy_id("http://mass.local/imageproxy/short") is None
    assert _extract_imageproxy_id("http://mass.local/imageproxy/" + "g" * 64) is None
    # extra path segments must not be accepted (matches handle_imageproxy)
    assert _extract_imageproxy_id("http://mass.local/imageproxy/" + "a" * 64 + "/extra") is None
    assert _extract_imageproxy_id("http://mass.local/imageproxy/extra/" + "a" * 64) is None
    # canonical form with trailing slash is fine
    assert _extract_imageproxy_id("http://mass.local/imageproxy/" + "a" * 64 + "/") == "a" * 64


async def test_handle_imageproxy_rejects_extra_path_segments(
    metadata_controller: MetaDataController,
) -> None:
    """`/imageproxy/<id>` is the only shape that should validate."""
    image_id = metadata_controller.compute_image_id("filesystem", "/local/cover.jpg")

    def _fake_request(path: str) -> MagicMock:
        request = MagicMock()
        request.path = path
        request.query = {}
        return request

    # canonical form passes the path check and reaches resolve (404 because
    # the (provider, path) is not actually fetchable in this minimal fixture)
    assert (
        await metadata_controller.handle_imageproxy(_fake_request(f"/imageproxy/{image_id}"))
    ).status in (200, 404)
    # extra leading segment must not be accepted
    bad = await metadata_controller.handle_imageproxy(
        _fake_request(f"/imageproxy/extra/{image_id}")
    )
    assert bad.status == 400
    # trailing extra segment likewise
    bad = await metadata_controller.handle_imageproxy(
        _fake_request(f"/imageproxy/{image_id}/extra")
    )
    assert bad.status == 400
    # double slash after the prefix must not be accepted either
    bad = await metadata_controller.handle_imageproxy(_fake_request(f"/imageproxy//{image_id}"))
    assert bad.status == 400


def test_is_svg_data() -> None:
    """SVG bytes are detected by content, regardless of file extension."""
    # plain root element
    assert is_svg_data(b'<svg xmlns="http://www.w3.org/2000/svg"></svg>')
    # XML declaration before the root element
    assert is_svg_data(b'<?xml version="1.0"?>\n<svg viewBox="0 0 10 10"></svg>')
    # leading whitespace and a comment before the root element
    assert is_svg_data(b"  \n<!-- a logo -->\n<svg></svg>")
    # doctype before the root element
    assert is_svg_data(b'<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN">\n<svg></svg>')
    # raster formats and arbitrary data are not SVG
    assert not is_svg_data(b"\xff\xd8\xff\xe0JFIF")  # jpeg magic
    assert not is_svg_data(b"\x89PNG\r\n\x1a\n")  # png magic
    assert not is_svg_data(b"")
    assert not is_svg_data(b"<html><body>not svg</body></html>")
    # an XML document that never declares an <svg> element is not SVG
    assert not is_svg_data(b'<?xml version="1.0"?><rss></rss>')


def test_detect_image_content_format() -> None:
    """Image format is sniffed from leading magic bytes (png/jpg/svg)."""
    assert detect_image_content_format(b"\x89PNG\r\n\x1a\n....") == "png"
    assert detect_image_content_format(b"\xff\xd8\xff\xe0JFIF") == "jpg"
    assert detect_image_content_format(b'<svg xmlns="http://www.w3.org/2000/svg"></svg>') == "svg"
    assert detect_image_content_format(b"") is None
    assert detect_image_content_format(b"GIF89a") is None
    # every sniffed value must map to a known content type
    for fmt in ("png", "jpg", "svg"):
        assert fmt in _IMAGEPROXY_CONTENT_TYPES


def test_thumb_cache_filename_separates_flatten_variants() -> None:
    """Flattened (player-compat) and transparency-preserving variants differ."""
    thumb_hash = "a" * 64
    plain = _thumb_cache_filename(thumb_hash, 512, "jpeg", flatten_transparency=False)
    flat = _thumb_cache_filename(thumb_hash, 512, "jpeg", flatten_transparency=True)
    assert plain == f"{thumb_hash}_512_v{_THUMB_CACHE_VERSION}.jpg"
    assert flat == f"{thumb_hash}_512_v{_THUMB_CACHE_VERSION}_flat.jpg"
    assert plain != flat
    # png requests keep their own extension/bucket
    png_name = _thumb_cache_filename(thumb_hash, 0, "png")
    assert png_name == f"{thumb_hash}_0_v{_THUMB_CACHE_VERSION}.png"


def test_has_alpha() -> None:
    """Only images that actually use transparency are detected."""
    assert _has_alpha(Image.new("RGBA", (4, 4), (0, 0, 0, 0)))
    assert _has_alpha(Image.new("LA", (4, 4)))
    palette_img = Image.new("P", (4, 4))
    palette_img.info["transparency"] = 0
    assert _has_alpha(palette_img)
    # a fully-opaque alpha channel does not count as transparent
    assert not _has_alpha(Image.new("RGBA", (4, 4), (1, 2, 3, 255)))
    # opaque modes have no alpha
    assert not _has_alpha(Image.new("RGB", (4, 4), (10, 20, 30)))
    assert not _has_alpha(Image.new("L", (4, 4)))
    assert not _has_alpha(Image.new("P", (4, 4)))


def test_thumb_cache_filename_includes_cache_version() -> None:
    """The version is in the filename so pre-upgrade thumbnails can't be reused."""
    thumb_hash = "a" * 64
    name = _thumb_cache_filename(thumb_hash, 512, "jpeg")
    assert f"_v{_THUMB_CACHE_VERSION}" in name
    # the unversioned (pre-PR) filename must not collide with the current one
    assert name != f"{thumb_hash}_512.jpg"
    # the sanitizer must accept the versioned shape it now produces
    assert _THUMB_FILENAME_RE.fullmatch(name)
    assert _THUMB_FILENAME_RE.fullmatch(
        _thumb_cache_filename(thumb_hash, 0, "png", flatten_transparency=True)
    )


async def test_serve_thumbnail_sets_csp_for_svg(
    metadata_controller: MetaDataController, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SVG responses carry a script-blocking CSP; raster responses do not."""
    served: dict[str, tuple[bytes, str]] = {"value": (b"<svg></svg>", "svg")}

    async def _fake_resolve(*_args: object, **_kwargs: object) -> tuple[bytes, str]:
        return served["value"]

    monkeypatch.setattr(metadata_controller, "_resolve_thumbnail", _fake_resolve)

    svg_resp = await metadata_controller._serve_thumbnail("p", "builtin", 0, "svg")
    assert svg_resp.headers["Content-Security-Policy"] == (
        "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    )
    assert svg_resp.headers["X-Content-Type-Options"] == "nosniff"

    served["value"] = (b"\xff\xd8\xff", "jpg")
    jpg_resp = await metadata_controller._serve_thumbnail("p", "builtin", 256, "jpeg")
    assert "Content-Security-Policy" not in jpg_resp.headers
    assert "X-Content-Type-Options" not in jpg_resp.headers


async def test_invalidate_image_cache_end_to_end(
    metadata_controller: MetaDataController, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Invalidation drops thumbs, source bytes and palette; the next request re-fetches.

    This is the retag scenario: a local file's artwork is replaced while its
    (provider, path) identity stays the same, so every derived artifact keyed
    on that identity must be regenerated.
    """
    image_path = str(tmp_path / "cover.png")
    Image.new("RGB", (300, 300), (200, 30, 30)).save(image_path, "PNG")
    mass = metadata_controller.mass
    fetches = 0
    real_fetch = images_helper._fetch_source_image

    async def counting_fetch(*args: Any, **kwargs: Any) -> tuple[bytes, bool]:
        nonlocal fetches
        fetches += 1
        return await real_fetch(*args, **kwargs)

    monkeypatch.setattr(images_helper, "_fetch_source_image", counting_fetch)

    # derive two thumb variants and a palette from one single source fetch
    await get_image_thumb(mass, image_path, 80, "builtin")
    await get_image_thumb(mass, image_path, 256, "builtin")
    palette = await get_palette(mass, image_path, "builtin")
    assert palette is not None
    assert palette.primary is not None
    assert fetches == 1

    thumb_hash = create_thumb_hash("builtin", image_path)
    thumb_dir = Path(mass.cache_path, "thumbnails")
    assert [f for f in thumb_dir.iterdir() if f.name.startswith(thumb_hash)]
    assert await mass.cache.get(thumb_hash, provider="palette") is not None

    await metadata_controller.invalidate_image_cache("builtin", image_path)

    assert not [f for f in thumb_dir.iterdir() if f.name.startswith(thumb_hash)]
    assert thumb_hash not in images_helper._source_memory_cache.entries
    assert not any(key.startswith(f"{thumb_hash}_") for key in images_helper._thumb_memory_cache)
    assert await mass.cache.get(thumb_hash, provider="palette") is None

    # replace the artwork on disk: the next request serves the new content
    Image.new("RGB", (300, 300), (30, 30, 200)).save(image_path, "PNG")
    new_palette = await get_palette(mass, image_path, "builtin")
    assert new_palette is not None
    assert new_palette.primary != palette.primary


def test_player_image_url_forces_jpeg_on_imageproxy_urls() -> None:
    """Player-bound imageproxy URLs move to the streams server and force fmt=jpeg."""
    mass = MagicMock()
    mass.webserver.base_url = "http://192.168.1.2:8095"
    mass.streams.base_url = "http://192.168.1.2:8097"
    image_id = "a" * 64

    url = f"http://192.168.1.2:8095/imageproxy/{image_id}?size=512&fmt=png"
    result = player_image_url(mass, url)
    assert result == f"http://192.168.1.2:8097/imageproxy/{image_id}?size=512&fmt=jpeg"

    # non-imageproxy urls (e.g. remote radio artwork) pass through unchanged
    remote = "https://example.com/art.png"
    assert player_image_url(mass, remote) == remote
    assert player_image_url(mass, None) is None
