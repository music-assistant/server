"""Tests for the color palette helper."""

from __future__ import annotations

import asyncio
import io
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from aiohttp.client_exceptions import ClientError
from PIL import Image, ImageDraw

from music_assistant.helpers import images
from music_assistant.helpers.colors import (
    _adjust_until_contrast,
    _contrast_ratio,
    _derive_palette,
    _pick_accent,
    _pick_on_color,
    _relative_luminance,
    extract_palette,
    get_palette,
)
from music_assistant.helpers.images import invalidate_cached_image
from tests.common import collect_loop_errors

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

_MIN_CONTRAST = 4.5


def _make_image_bytes(colors: list[tuple[int, int, int]]) -> bytes:
    """Build a PNG composed of rectangles for each input color."""
    width = 60
    height = 60
    img = Image.new("RGB", (width * len(colors), height), colors[0])
    draw = ImageDraw.Draw(img)
    for idx, color in enumerate(colors):
        draw.rectangle([idx * width, 0, (idx + 1) * width, height], fill=color)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_relative_luminance_extremes() -> None:
    """Black is 0, white is 1."""
    assert _relative_luminance((0, 0, 0)) == 0.0
    assert abs(_relative_luminance((255, 255, 255)) - 1.0) < 1e-9


def test_contrast_ratio_black_vs_white() -> None:
    """Black vs white is the maximum 21:1 ratio."""
    assert abs(_contrast_ratio((0, 0, 0), (255, 255, 255)) - 21.0) < 1e-9


def test_adjust_until_contrast_darkens_when_needed() -> None:
    """Mixing a mid-gray toward black yields a darker color that clears MIN vs white."""
    result = _adjust_until_contrast((180, 180, 180), (0, 0, 0), ((255, 255, 255),))
    assert result is not None
    assert _contrast_ratio(result, (255, 255, 255)) >= _MIN_CONTRAST


def test_adjust_until_contrast_returns_none_for_unsolvable() -> None:
    """Mixing toward white can never satisfy contrast vs white."""
    assert _adjust_until_contrast((128, 128, 128), (255, 255, 255), ((255, 255, 255),)) is None


def test_pick_accent_skips_similar_to_primary() -> None:
    """_pick_accent returns the first candidate far enough from primary."""
    primary = (200, 30, 30)
    assert _pick_accent(primary, [primary, (205, 35, 35), (10, 50, 200)]) == (10, 50, 200)
    assert _pick_accent(primary, [primary, (205, 35, 35)]) is None


def test_pick_on_color_requires_min_contrast() -> None:
    """Only candidates clearing MIN_CONTRAST vs the target are considered."""
    candidates = [(255, 255, 255), (128, 128, 128), (20, 20, 20)]
    # vs near-black target, white wins, gray fails MIN_CONTRAST
    assert _pick_on_color(candidates, (20, 20, 20)) == (255, 255, 255)


def test_derive_palette_synthesizes_on_colors_when_no_candidate() -> None:
    """When no image candidate clears contrast, on_dark/on_light are synthesized."""
    # All candidates near mid-gray — none can clear 4.5 vs pure black or pure white.
    candidates = [(120, 120, 120), (130, 125, 128), (115, 118, 122)]
    palette = _derive_palette(candidates)
    assert palette.on_dark is not None
    assert palette.on_light is not None
    assert _contrast_ratio(palette.on_dark, (0, 0, 0)) >= _MIN_CONTRAST
    assert _contrast_ratio(palette.on_light, (255, 255, 255)) >= _MIN_CONTRAST


def test_derive_palette_empty() -> None:
    """Empty candidates yield a fully-empty palette, not an exception."""
    palette = _derive_palette([])
    assert palette.primary is None
    assert palette.background_dark is None


def test_palette_invariants_over_random_candidates() -> None:
    """
    Across many synthetic candidate sets, every emitted contrast pair holds.

    Sweeps a fixed-seed RNG over candidate counts and RGB values to catch
    regressions in the contrast invariants. Calibrate the iteration count so
    the test stays cheap in CI.
    """
    import random  # noqa: PLC0415

    rng = random.Random(42)
    # Calibrated for CI: 50 iterations runs in a few ms. Bump if invariants
    # regress and we need denser coverage.
    for _ in range(50):
        n = rng.randint(1, 8)
        cands = [(rng.randrange(256), rng.randrange(256), rng.randrange(256)) for _ in range(n)]
        p = _derive_palette(cands)
        if p.background_dark is not None:
            assert _contrast_ratio(p.background_dark, (255, 255, 255)) >= _MIN_CONTRAST
        if p.background_light is not None:
            assert _contrast_ratio(p.background_light, (0, 0, 0)) >= _MIN_CONTRAST
        if p.on_dark is not None:
            assert _contrast_ratio(p.on_dark, (0, 0, 0)) >= _MIN_CONTRAST
        if p.on_light is not None:
            assert _contrast_ratio(p.on_light, (255, 255, 255)) >= _MIN_CONTRAST


def test_extract_palette_handles_invalid_bytes() -> None:
    """Invalid image bytes return an empty palette."""
    palette = extract_palette(b"not an image")
    assert palette.primary is None


def test_extract_palette_end_to_end() -> None:
    """An image with multiple distinct colors yields a palette that meets WCAG."""
    image_bytes = _make_image_bytes([(60, 30, 90), (220, 200, 150), (250, 250, 245)])
    palette = extract_palette(image_bytes)
    assert palette.primary is not None
    if palette.background_dark is not None:
        assert _contrast_ratio(palette.background_dark, (255, 255, 255)) >= _MIN_CONTRAST


async def test_cancelled_caller_of_a_failing_extraction_logs_no_loop_error(
    mass_minimal: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Palette extraction failing after its caller gave up is not reported to the loop handler."""
    # mass_minimal constructs the cache controller without initializing it
    cache_config = await mass_minimal.config.get_core_config(mass_minimal.cache.domain)
    await mass_minimal.cache.setup(cache_config)
    entered = asyncio.Event()
    release = asyncio.Event()
    extraction: list[asyncio.Task[Any]] = []

    async def failing_source(_mass: MusicAssistant, path_or_url: str, _provider: str) -> bytes:
        current = asyncio.current_task()
        assert current is not None
        extraction.append(current)
        entered.set()
        await release.wait()
        raise PermissionError(f"Permission denied: {path_or_url}")

    monkeypatch.setattr("music_assistant.helpers.colors.get_image_data", failing_source)
    with collect_loop_errors() as reported:
        caller = asyncio.create_task(get_palette(mass_minimal, "/some/image.png", "builtin"))
        await entered.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        # only fail the extraction once the cancellation is fully processed
        release.set()
        await asyncio.wait(extraction)

    assert isinstance(extraction[0].exception(), PermissionError)
    assert reported == []


async def test_get_palette_tolerates_unavailable_image(
    mass_minimal: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unfetchable image yields an empty palette; extraction retries once it is back."""
    mass_minimal.webserver = MagicMock(base_url="http://127.0.0.1:8095")
    mass_minimal.streams = MagicMock(base_url="http://127.0.0.1:8097")
    # mass_minimal constructs the cache controller without initializing it
    cache_config = await mass_minimal.config.get_core_config(mass_minimal.cache.domain)
    await mass_minimal.cache.setup(cache_config)
    remote_url = "http://sonos.example.com:1400/getaa?u=gone.flac"

    async def failing_remote_fetch(_mass: MusicAssistant, _url: str) -> bytes:
        raise ClientError("404, message='Not Found'")

    monkeypatch.setattr(images, "_fetch_remote_image", failing_remote_fetch)
    try:
        palette = await get_palette(mass_minimal, remote_url, "builtin")
        assert palette is not None
        assert palette.primary is None  # empty palette, but no exception raised

        # nothing was cached for the failure, so once the artwork is reachable
        # again the real palette is extracted
        async def ok_remote_fetch(_mass: MusicAssistant, _url: str) -> bytes:
            return _make_image_bytes([(200, 30, 30), (30, 30, 200)])

        monkeypatch.setattr(images, "_fetch_remote_image", ok_remote_fetch)
        await invalidate_cached_image(mass_minimal, "builtin", remote_url)
        palette = await get_palette(mass_minimal, remote_url, "builtin")
        assert palette is not None
        assert palette.primary is not None
    finally:
        # drop the module-global image cache entries this test created
        await invalidate_cached_image(mass_minimal, "builtin", remote_url)
