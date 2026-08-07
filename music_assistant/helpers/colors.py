"""
Color palette extraction from artwork.

Derives a 6-field MediaItemPalette per the Sendspin color@v1 spec from an
image: a `modern_colorthief` MMCQ quantizer produces image candidates, then `primary`,
`accent`, `on_dark`, `on_light`, `background_dark`, and `background_light` are
chosen (and adjusted where needed) so that every spec-mandated contrast pair
clears the WCAG AA 4.5:1 threshold.

Extracted palettes are stored in the cache controller (sqlite), keyed on a
hash of provider and image path, so results persist across restarts and are
shared process-wide.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from modern_colorthief import get_palette as _mmcq_palette
from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.media_items import MediaItemPalette

from music_assistant.helpers.images import (
    _extract_imageproxy_id,
    create_thumb_hash,
    get_image_data,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


_PALETTE_QUANTIZE_COLORS = 5
_COLORTHIEF_QUALITY = 10
# Minimum contrast ratio between colors (Sendspin color@v1 requires WCAG AA ≥ 4.5:1).
_MIN_CONTRAST = 4.5
# Preferred contrast (we try this first for richer, more vivid picks)
_PREFERRED_CONTRAST = 7.0
# Upper cap when picking the dark on-light color. Without this, near-black
# image regions (e.g. text outlines) win and the picked color looks like pure
# black instead of a vibrant darker shade from the artwork.
_MAX_DARK_PICK_CONTRAST = 17.35
_BACKGROUND_ADJUST_STEPS = 20
_SIMILARITY_THRESHOLD = 60  # squared euclidean RGB distance for accent picking

# Cache controller namespace for extracted palettes. Palettes are
# content-addressed (hash of provider+path) and deterministic, so a long
# expiration is safe: a palette only changes if the underlying image changes.
_CACHE_PROVIDER = "palette"
_CACHE_EXPIRATION = 90 * 24 * 3600  # 90 days

_RGB = tuple[int, int, int]


def _relative_luminance(rgb: _RGB) -> float:
    """Compute WCAG relative luminance for an sRGB color."""

    def channel(c: int) -> float:
        s = c / 255.0
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast_ratio(a: _RGB, b: _RGB) -> float:
    """Compute WCAG contrast ratio between two RGB colors."""
    la = _relative_luminance(a)
    lb = _relative_luminance(b)
    lighter, darker = (la, lb) if la >= lb else (lb, la)
    return (lighter + 0.05) / (darker + 0.05)


def _color_distance_sq(a: _RGB, b: _RGB) -> int:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2


def _mix(rgb: _RGB, target: _RGB, factor: float) -> _RGB:
    """Blend rgb toward target. factor=0 returns rgb, factor=1 returns target."""
    return (
        round(rgb[0] + (target[0] - rgb[0]) * factor),
        round(rgb[1] + (target[1] - rgb[1]) * factor),
        round(rgb[2] + (target[2] - rgb[2]) * factor),
    )


def _adjust_until_contrast(
    color: _RGB,
    mix_toward: _RGB,
    refs: tuple[_RGB, ...],
    min_contrast: float = _MIN_CONTRAST,
) -> _RGB | None:
    """
    Mix color toward mix_toward until contrast >= min_contrast vs all refs.

    :param color: Starting color.
    :param mix_toward: Direction to blend (e.g. black to darken, white to lighten).
    :param refs: Colors that the result must contrast against.
    :param min_contrast: Required minimum contrast ratio against every ref.
    """
    for step in range(_BACKGROUND_ADJUST_STEPS + 1):
        factor = step / _BACKGROUND_ADJUST_STEPS
        candidate = _mix(color, mix_toward, factor)
        if all(_contrast_ratio(candidate, ref) >= min_contrast for ref in refs):
            return candidate
    return None


def _adjust_with_fallback(color: _RGB, mix_toward: _RGB, refs: tuple[_RGB, ...]) -> _RGB | None:
    """Try _PREFERRED_CONTRAST first, fall back to _MIN_CONTRAST if needed."""
    return _adjust_until_contrast(
        color, mix_toward, refs, _PREFERRED_CONTRAST
    ) or _adjust_until_contrast(color, mix_toward, refs, _MIN_CONTRAST)


def _extract_candidates(image_bytes: bytes) -> list[_RGB]:
    """Extract a dominant-color palette via MMCQ (matches the colorthief JS lib)."""
    palette = _mmcq_palette(
        image_bytes, color_count=_PALETTE_QUANTIZE_COLORS, quality=_COLORTHIEF_QUALITY
    )
    return [(r, g, b) for r, g, b in palette]


def _pick_on_color(
    candidates: list[_RGB],
    target: _RGB,
    min_contrast: float = _MIN_CONTRAST,
    max_contrast: float = float("inf"),
) -> _RGB | None:
    """Pick the candidate with highest contrast vs target in [min, max]."""
    best: _RGB | None = None
    best_ratio = 0.0
    for rgb in candidates:
        ratio = _contrast_ratio(rgb, target)
        if min_contrast <= ratio <= max_contrast and ratio > best_ratio:
            best = rgb
            best_ratio = ratio
    return best


def _pick_on_color_with_fallback(
    candidates: list[_RGB], target: _RGB, max_contrast: float = float("inf")
) -> _RGB | None:
    """Try _PREFERRED_CONTRAST first, fall back to _MIN_CONTRAST if needed."""
    return _pick_on_color(candidates, target, _PREFERRED_CONTRAST, max_contrast) or _pick_on_color(
        candidates, target, _MIN_CONTRAST, max_contrast
    )


def _pick_accent(primary: _RGB, candidates: list[_RGB]) -> _RGB | None:
    """Return the first candidate that is hue-distant from primary, or None."""
    for rgb in candidates:
        if rgb == primary:
            continue
        if _color_distance_sq(rgb, primary) >= _SIMILARITY_THRESHOLD**2:
            return rgb
    return None


def _derive_palette(candidates: list[_RGB]) -> MediaItemPalette:
    if not candidates:
        return MediaItemPalette()
    primary = candidates[0]

    # On-colors are picked from image candidates against pure black/white (the
    # natural reference for a "light color" or "dark color"), this matches the previous algorithm from the frontend.
    # on_light caps at _MAX_DARK_PICK_CONTRAST to avoid near-black picks (e.g. text outlines) winning over genuinely dark
    # image colors.
    on_dark = _pick_on_color_with_fallback(candidates, (0, 0, 0))
    on_light = _pick_on_color_with_fallback(candidates, (255, 255, 255), _MAX_DARK_PICK_CONTRAST)

    # Synthesize a fallback when no candidate cleared the contrast bar so the
    # field is always emitted. Spec dual-use: on_dark must clear 4.5:1 vs black
    # text (so it can also act as a light bg), on_light must clear vs white.
    if on_dark is None:
        on_dark = _adjust_until_contrast(primary, (255, 255, 255), ((0, 0, 0),))
    if on_light is None:
        on_light = _adjust_until_contrast(primary, (0, 0, 0), ((255, 255, 255),))

    # Adjust backgrounds so they clear MIN_CONTRAST vs both the matching text
    # color (white/black) and the chosen on-color, per spec.
    bg_dark_refs: tuple[_RGB, ...] = ((255, 255, 255),)
    if on_dark is not None:
        bg_dark_refs = ((255, 255, 255), on_dark)
    background_dark = _adjust_with_fallback(primary, (0, 0, 0), bg_dark_refs)

    bg_light_refs: tuple[_RGB, ...] = ((0, 0, 0),)
    if on_light is not None:
        bg_light_refs = ((0, 0, 0), on_light)
    background_light = _adjust_with_fallback(primary, (255, 255, 255), bg_light_refs)

    # Accent: a hue-distant secondary from the image. This is not adjusted for contrast.
    accent = _pick_accent(primary, candidates)

    return MediaItemPalette(
        background_dark=background_dark,
        background_light=background_light,
        primary=primary,
        accent=accent,
        on_dark=on_dark,
        on_light=on_light,
    )


def extract_palette(image_bytes: bytes) -> MediaItemPalette:
    """
    Extract a MediaItemPalette from raw image bytes.

    :param image_bytes: Raw image data (PNG, JPEG, etc.).
    """
    try:
        candidates = _extract_candidates(image_bytes)
    except ValueError, OSError:
        return MediaItemPalette()
    return _derive_palette(candidates)


async def _extract_and_cache(
    mass: MusicAssistant, path_or_url: str, provider: str, key: str
) -> MediaItemPalette:
    try:
        img_data = await get_image_data(mass, path_or_url, provider)
    except FileNotFoundError, MusicAssistantError:
        # the image is unavailable (e.g. a stale artwork URL that 404s); the
        # empty palette is not cached below, so extraction is retried once the
        # image becomes available again
        return MediaItemPalette()
    palette = await asyncio.to_thread(extract_palette, img_data)
    # Only persist a palette that actually yielded colors; an empty result is
    # usually a transient decode/download failure that should be retried rather
    # than cached for weeks.
    if palette.primary is not None:
        await mass.cache.set(
            key,
            palette.to_dict(),
            provider=_CACHE_PROVIDER,
            expiration=_CACHE_EXPIRATION,
        )
    return palette


async def get_palette(
    mass: MusicAssistant, path_or_url: str, provider: str
) -> MediaItemPalette | None:
    """
    Get the color palette for an image, backed by the cache controller.

    :param mass: The MusicAssistant instance.
    :param path_or_url: Image path or URL (same format as get_image_data).
    :param provider: Provider identifier for the image source.
    """
    if not path_or_url:
        return None
    key = create_thumb_hash(provider, path_or_url)

    cached: MediaItemPalette | None = await mass.cache.get(
        key, provider=_CACHE_PROVIDER, base_class=MediaItemPalette
    )
    if cached is not None:
        return cached

    # Dedupe concurrent extraction (e.g. now-playing + prefetch) for the same image.
    task: asyncio.Task[MediaItemPalette] = mass.create_task(
        _extract_and_cache,
        mass,
        path_or_url,
        provider,
        key,
        task_id=f"palette.{key}",
        abort_existing=False,
    )
    # wait for the shared extraction instead of awaiting it directly: a caller awaiting a
    # task holds it as its fut_waiter, so cancelling that caller would cancel the
    # extraction for every other caller too. asyncio.shield achieves the same, but as of
    # Python 3.14 a cancelled caller makes it report the extraction's exception through
    # loop.call_exception_handler, even when another caller already handled it.
    if not task.done():
        await asyncio.wait((task,))
    return task.result()


async def invalidate_cached_palette(mass: MusicAssistant, provider: str, path_or_url: str) -> None:
    """
    Remove the cached palette for an image so the next request re-extracts it.

    :param mass: The MusicAssistant instance.
    :param provider: Provider identifier for the image source.
    :param path_or_url: Image path or URL (same format as get_palette).
    """
    await mass.cache.delete(key=create_thumb_hash(provider, path_or_url), provider=_CACHE_PROVIDER)


async def get_palette_for_url(
    mass: MusicAssistant, image_url: str | None
) -> MediaItemPalette | None:
    """Resolve an imageproxy URL to (path, provider) and return its palette."""
    if not image_url:
        return None
    # /imageproxy/<id> form: async-resolve the id back to (provider, path).
    if image_id := _extract_imageproxy_id(image_url):
        resolved = await mass.metadata.resolve_image_id(image_id)
        if resolved is None:
            return None
        provider, path = resolved
    else:
        path, provider = image_url, "builtin"
    try:
        return await get_palette(mass, path, provider)
    except FileNotFoundError, OSError:
        return None
