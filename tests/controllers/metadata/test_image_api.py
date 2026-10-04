"""Tests for fetching registered images through the API."""

import asyncio
import io
import threading
from base64 import b64decode
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.api import SuccessResultMessage
from music_assistant_models.auth import Scope
from music_assistant_models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    ProviderUnavailableError,
    ResourceTemporarilyUnavailable,
)
from PIL import Image

from music_assistant.controllers.metadata import MetaDataController, images
from music_assistant.controllers.metadata.constants import (
    _IMAGE_API_MAX_BYTES,
    _IMAGE_API_MAX_CONCURRENT,
)
from music_assistant.helpers import images as image_helpers
from music_assistant.helpers.util import join_task


@pytest.fixture
async def image_api(
    metadata_controller: MetaDataController, monkeypatch: pytest.MonkeyPatch
) -> MetaDataController:
    """Provide a registered image with mocked thumbnail rendering."""
    monkeypatch.setattr(
        metadata_controller, "resolve_image_id", AsyncMock(return_value=("library", "cover.png"))
    )
    monkeypatch.setattr(
        metadata_controller, "_resolve_thumbnail", AsyncMock(return_value=(b"image bytes", "png"))
    )
    return metadata_controller


async def test_image_api_result(image_api: MetaDataController) -> None:
    """The JSON result carries bytes, actual MIME type and cache policy."""
    result = await image_api.get_image("A" * 64)
    assert b64decode(result["data"], validate=True) == b"image bytes"
    assert result["content_type"] == "image/png"
    assert result["cache_control"] == "max-age=31536000"
    cast("AsyncMock", image_api.resolve_image_id).assert_awaited_once_with("a" * 64)
    cast("AsyncMock", image_api._resolve_thumbnail).assert_awaited_once_with(
        "cover.png", "library", 512, "png", False
    )
    assert (
        SuccessResultMessage.from_json(SuccessResultMessage("1", result).to_json()).result == result
    )


@pytest.mark.parametrize(
    "image_id", ["", "a" * 63, "g" * 64, "../cover.png", "https://example.com"]
)
async def test_image_api_invalid_id(image_api: MetaDataController, image_id: str) -> None:
    """Arbitrary paths and URLs are not accepted."""
    with pytest.raises(InvalidDataError):
        await image_api.get_image(image_id)
    cast("AsyncMock", image_api.resolve_image_id).assert_not_awaited()


@pytest.mark.parametrize("size", [-1, 1, 500, 2048])
async def test_image_api_invalid_size(image_api: MetaDataController, size: int) -> None:
    """Sizes outside the shared thumbnail set are rejected before fetching."""
    with pytest.raises(InvalidDataError):
        await image_api.get_image("a" * 64, size=size)
    cast("AsyncMock", image_api.resolve_image_id).assert_not_awaited()


@pytest.mark.parametrize("image_format", ["", "webp", "gif"])
async def test_image_api_invalid_format(image_api: MetaDataController, image_format: str) -> None:
    """Unsupported explicit formats are rejected."""
    with pytest.raises(InvalidDataError):
        await image_api.get_image("a" * 64, image_format=image_format)
    cast("AsyncMock", image_api.resolve_image_id).assert_not_awaited()


@pytest.mark.parametrize("size", [0, 80, 160, 256, 512, 1024])
async def test_image_api_format_and_size(image_api: MetaDataController, size: int) -> None:
    """Explicit JPEG requests preserve the HTTP transparency policy."""
    await image_api.get_image("a" * 64, size=size, image_format=" JPEG ")
    cast("AsyncMock", image_api._resolve_thumbnail).assert_awaited_once_with(
        "cover.png", "library", size, "jpeg", True
    )


async def test_image_api_unknown_id(image_api: MetaDataController) -> None:
    """Unknown ids produce a typed not-found result."""
    cast("AsyncMock", image_api.resolve_image_id).return_value = None
    with pytest.raises(MediaNotFoundError):
        await image_api.get_image("a" * 64)
    cast("AsyncMock", image_api._resolve_thumbnail).assert_not_awaited()


@pytest.mark.parametrize(
    "error", [FileNotFoundError(), ProviderUnavailableError(), MediaNotFoundError()]
)
async def test_image_api_unreadable(image_api: MetaDataController, error: Exception) -> None:
    """Missing files and unavailable providers produce the same operational error."""
    cast("AsyncMock", image_api._resolve_thumbnail).side_effect = error
    with pytest.raises(MediaNotFoundError):
        await image_api.get_image("a" * 64)


async def test_image_api_provider_timeout_releases_slots(
    metadata_controller: MetaDataController, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stalled provider resolution ends and unrelated artwork can still load."""
    monkeypatch.setattr(image_helpers, "_PROVIDER_IMAGE_TIMEOUT", 0.02)
    monkeypatch.setattr(images, "_IMAGE_API_TIMEOUT", 1)
    cancelled = 0

    async def stalled_resolver(_path: str) -> bytes:
        nonlocal cancelled
        try:
            await asyncio.Event().wait()
        finally:
            cancelled += 1
        return b""

    resolver = AsyncMock(side_effect=stalled_resolver)
    provider = MagicMock(resolve_image=resolver)
    monkeypatch.setattr(
        metadata_controller.mass,
        "get_provider",
        MagicMock(
            side_effect=lambda provider_id, **_kwargs: (
                provider if provider_id == "stalled" else None
            )
        ),
    )
    image_ids = [
        metadata_controller.compute_image_id("stalled", f"cover-{index}.png")
        for index in range(_IMAGE_API_MAX_CONCURRENT)
    ]
    results = await asyncio.gather(
        *(metadata_controller.get_image(image_id) for image_id in image_ids),
        return_exceptions=True,
    )
    assert all(isinstance(result, ResourceTemporarilyUnavailable) for result in results)
    assert cancelled == _IMAGE_API_MAX_CONCURRENT
    with pytest.raises(MediaNotFoundError):
        await metadata_controller.get_image(image_ids[0])
    assert resolver.await_count == _IMAGE_API_MAX_CONCURRENT
    source = tmp_path / "healthy-cover.png"
    Image.new("RGB", (20, 20), "blue").save(source)
    healthy_id = metadata_controller.compute_image_id("builtin", str(source))
    assert (await metadata_controller.get_image(healthy_id))["content_type"] == "image/png"


async def test_image_api_body_limit(image_api: MetaDataController) -> None:
    """Oversized images cannot fill a WebSocket response."""
    cast("AsyncMock", image_api._resolve_thumbnail).return_value = (
        b"x" * (_IMAGE_API_MAX_BYTES + 1),
        "png",
    )
    with pytest.raises(InvalidDataError, match="smaller thumbnail"):
        await image_api.get_image("a" * 64)


async def test_image_api_queue_timeout(
    image_api: MetaDataController, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Waiting for a fetch slot is included in the request timeout."""
    monkeypatch.setattr(images, "_IMAGE_API_TIMEOUT", 0.01)
    image_api._image_api_semaphore = asyncio.Semaphore(0)
    with pytest.raises(ResourceTemporarilyUnavailable):
        await image_api.get_image("a" * 64)
    cast("AsyncMock", image_api.resolve_image_id).assert_not_awaited()


async def test_image_api_scope(image_api: MetaDataController) -> None:
    """The command participates in the existing authenticated API dispatch."""
    attributes = vars(image_api.get_image)
    assert attributes["api_cmd"] == "metadata/get_image"
    assert attributes["api_authenticated"]
    assert attributes["api_required_scope"] == Scope.LIBRARY_READ


async def test_image_api_concurrency(image_api: MetaDataController) -> None:
    """A burst of requests renders at most six images concurrently."""
    slots_filled = asyncio.Event()
    release = asyncio.Event()
    active = 0
    maximum = 0

    async def render(*_args: object) -> tuple[bytes, str]:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        if active == _IMAGE_API_MAX_CONCURRENT:
            slots_filled.set()
        try:
            await release.wait()
            return b"image", "png"
        finally:
            active -= 1

    cast("AsyncMock", image_api._resolve_thumbnail).side_effect = render
    tasks = [asyncio.create_task(image_api.get_image("a" * 64)) for _ in range(12)]
    try:
        async with asyncio.timeout(2):
            await slots_filled.wait()
            assert active == _IMAGE_API_MAX_CONCURRENT
            release.set()
            await asyncio.gather(*tasks)
        assert maximum == _IMAGE_API_MAX_CONCURRENT
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_image_api_real_thumbnail(
    metadata_controller: MetaDataController, tmp_path: Path
) -> None:
    """Registered local artwork uses the same resized bytes as HTTP."""
    source = tmp_path / "cover.png"
    Image.new("RGBA", (300, 300), (10, 100, 200, 128)).save(source)
    image_id = metadata_controller.compute_image_id("library", str(source))
    result = await metadata_controller.get_image(image_id, size=160)
    decoded = b64decode(result["data"], validate=True)
    with Image.open(io.BytesIO(decoded)) as thumb:
        assert thumb.size == (160, 160)
        assert thumb.mode == "RGBA"
    request = AsyncMock()
    request.path = f"/imageproxy/{image_id}"
    request.query = {"size": "160"}
    response = await metadata_controller.handle_imageproxy(request)
    assert response.status == 200
    assert response.body == decoded
    assert response.content_type == result["content_type"]
    assert response.headers["Cache-Control"] == result["cache_control"]
    assert set(result) == {"data", "content_type", "cache_control"}


@pytest.mark.parametrize("cancel", [False, True])
async def test_image_api_abandoned_work_keeps_slots(
    image_api: MetaDataController, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    """Timeouts and disconnects do not turn over slots while shared work runs."""
    monkeypatch.setattr(images, "_IMAGE_API_TIMEOUT", 0.05)
    started = asyncio.Event()
    release = asyncio.Event()
    jobs: list[asyncio.Task[tuple[bytes, str]]] = []

    async def underlying() -> tuple[bytes, str]:
        await release.wait()
        return b"image", "png"

    async def render(*_args: object) -> tuple[bytes, str]:
        task = image_api.mass.create_task(underlying(), log_exceptions=False)
        jobs.append(task)
        if len(jobs) == _IMAGE_API_MAX_CONCURRENT:
            started.set()
        return await join_task(task)

    cast("AsyncMock", image_api._resolve_thumbnail).side_effect = render
    callers = [asyncio.create_task(image_api.get_image("a" * 64)) for _ in range(6)]
    try:
        async with asyncio.timeout(2):
            await started.wait()
        if cancel:
            for caller in callers:
                caller.cancel()
        results = await asyncio.gather(*callers, return_exceptions=True)
        expected = asyncio.CancelledError if cancel else ResourceTemporarilyUnavailable
        assert all(isinstance(result, expected) for result in results)
        assert all(not job.done() for job in jobs)
        # A second wave cannot create more resolver/shared tasks after abandonment.
        with pytest.raises(ResourceTemporarilyUnavailable):
            await image_api.get_image("b" * 64)
        assert len(jobs) == _IMAGE_API_MAX_CONCURRENT
        assert cast("AsyncMock", image_api.resolve_image_id).await_count == 6
        release.set()
        await asyncio.gather(*jobs)
        result = await image_api.get_image("c" * 64)
        assert result["content_type"] == "image/png"
    finally:
        release.set()
        await asyncio.gather(*callers, *jobs, return_exceptions=True)


async def test_image_api_pillow_thread_keeps_slot(
    metadata_controller: MetaDataController, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real shared thumbnail worker retains its slot until Pillow returns."""
    source = tmp_path / "thread-cover.png"
    Image.new("RGB", (40, 40), "blue").save(source)
    image_id = metadata_controller.compute_image_id("library", str(source))
    metadata_controller._image_api_semaphore = asyncio.Semaphore(1)
    monkeypatch.setattr(images, "_IMAGE_API_TIMEOUT", 0.1)
    started = asyncio.Event()
    finished = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_thumbnail = Image.Image.thumbnail

    def blocked_thumbnail(self: Image.Image, *args: Any, **kwargs: Any) -> None:
        loop.call_soon_threadsafe(started.set)
        try:
            if not release.wait(5):
                raise RuntimeError("Test did not release Pillow worker")
            original_thumbnail(self, *args, **kwargs)
        finally:
            loop.call_soon_threadsafe(finished.set)

    monkeypatch.setattr(Image.Image, "thumbnail", blocked_thumbnail)
    caller = asyncio.create_task(metadata_controller.get_image(image_id, size=80))
    try:
        async with asyncio.timeout(2):
            await started.wait()
        with pytest.raises(ResourceTemporarilyUnavailable):
            await caller
        with pytest.raises(ResourceTemporarilyUnavailable):
            await metadata_controller.get_image(image_id, size=160)
        assert not finished.is_set()
        release.set()
        result = await metadata_controller.get_image(image_id, size=80)
        assert result["content_type"] == "image/png"
        assert finished.is_set()
    finally:
        release.set()
        await asyncio.gather(caller, return_exceptions=True)


@pytest.mark.parametrize("source_format", ["png", "jpg", "svg"])
async def test_image_api_requested_svg_detects_real_content(
    metadata_controller: MetaDataController, tmp_path: Path, source_format: str
) -> None:
    """SVG requests report real MIME types; security headers remain HTTP-only."""
    source = tmp_path / f"original.{source_format}"
    if source_format == "svg":
        source.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>')
    else:
        Image.new("RGB", (20, 20), "red").save(source)
    image_id = metadata_controller.compute_image_id("library", str(source))
    result = await metadata_controller.get_image(image_id, image_format="svg")
    assert b64decode(result["data"]) == source.read_bytes()
    assert (
        result["content_type"]
        == {"png": "image/png", "jpg": "image/jpeg", "svg": "image/svg+xml"}[source_format]
    )
    request = AsyncMock()
    request.path = f"/imageproxy/{image_id}"
    request.query = {"fmt": "svg"}
    response = await metadata_controller.handle_imageproxy(request)
    assert response.status == 200
    assert response.body == b64decode(result["data"])
    assert response.content_type == result["content_type"]
    assert "headers" not in result
    assert ("Content-Security-Policy" in response.headers) == (source_format == "svg")
    assert ("X-Content-Type-Options" in response.headers) == (source_format == "svg")
