"""Tests for dynamic PCM format selection."""

from __future__ import annotations

import pytest
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

from music_assistant.providers.yandex_ynison.format_policy import select_effective_pcm


@pytest.mark.parametrize(
    ("source_rate", "source_depth", "supported", "expected"),
    [
        (44_100, 16, [(44_100, 16), (48_000, 24)], (ContentType.PCM_S16LE, 44_100, 16, 2)),
        (48_000, 24, [(44_100, 24), (48_000, 16)], (ContentType.PCM_S24LE, 48_000, 24, 2)),
        (88_200, 24, [(44_100, 16), (48_000, 24)], (ContentType.PCM_S24LE, 48_000, 24, 2)),
        (96_000, 24, [(48_000, 24), (96_000, 24)], (ContentType.PCM_S24LE, 96_000, 24, 2)),
        (176_400, 32, [(96_000, 24), (176_400, 32)], (ContentType.PCM_S32LE, 176_400, 32, 2)),
        (192_000, 24, [(96_000, 32), (192_000, 16)], (ContentType.PCM_S24LE, 192_000, 24, 2)),
    ],
)
def test_selects_highest_rate_not_above_source_and_preserves_source_depth(
    source_rate: int,
    source_depth: int,
    supported: list[tuple[int, int]],
    expected: tuple[ContentType, int, int, int],
) -> None:
    """The effective signature must constrain rate while retaining source precision."""
    source = AudioFormat(
        content_type=ContentType.FLAC,
        sample_rate=source_rate,
        bit_depth=source_depth,
        channels=2,
    )

    assert select_effective_pcm(source, supported) == expected


@pytest.mark.parametrize(
    ("source_depth", "expected_type", "expected_depth"),
    [
        (1, ContentType.PCM_S16LE, 16),
        (16, ContentType.PCM_S16LE, 16),
        (17, ContentType.PCM_S24LE, 24),
        (24, ContentType.PCM_S24LE, 24),
        (25, ContentType.PCM_S32LE, 32),
        (32, ContentType.PCM_S32LE, 32),
    ],
)
def test_maps_source_depth_to_pcm_container(
    source_depth: int, expected_type: ContentType, expected_depth: int
) -> None:
    """Source precision must map to the specified PCM container boundaries."""
    source = AudioFormat(
        content_type=ContentType.FLAC,
        sample_rate=384_000,
        bit_depth=source_depth,
        channels=1,
    )

    assert select_effective_pcm(source, []) == (
        expected_type,
        384_000,
        expected_depth,
        1,
    )


def test_uses_lowest_supported_rate_when_all_rates_exceed_source() -> None:
    """A player with no lower rate must use its lowest available rate."""
    source = AudioFormat(
        content_type=ContentType.FLAC,
        sample_rate=8_000,
        bit_depth=16,
        channels=2,
    )

    assert select_effective_pcm(source, [(44_100, 16), (48_000, 24)]) == (
        ContentType.PCM_S16LE,
        44_100,
        16,
        2,
    )


def test_preserves_source_depth_without_adding_precision() -> None:
    """A player's wider PCM support must not manufacture source precision."""
    source = AudioFormat(
        content_type=ContentType.FLAC,
        sample_rate=96_000,
        bit_depth=24,
        channels=2,
    )

    assert select_effective_pcm(
        source,
        [(48_000, 32), (96_000, 16), (96_000, 24), (96_000, 32)],
    ) == (ContentType.PCM_S24LE, 96_000, 24, 2)


def test_preserves_source_depth_when_player_pair_reports_safe_fallback_depth() -> None:
    """AudioSource PCM must retain source depth when MA only constrains its sample rate."""
    source = AudioFormat(
        content_type=ContentType.FLAC,
        sample_rate=48_000,
        bit_depth=24,
        channels=2,
    )

    assert select_effective_pcm(source, [(44_100, 16)]) == (
        ContentType.PCM_S24LE,
        44_100,
        24,
        2,
    )


@pytest.mark.parametrize("sample_rate", [7_999, 384_001])
def test_rejects_source_rates_outside_dynamic_range(sample_rate: int) -> None:
    """Invalid source rates must not be exposed as a dynamic AudioSource format."""
    source = AudioFormat(
        content_type=ContentType.FLAC,
        sample_rate=sample_rate,
        bit_depth=24,
        channels=2,
    )

    with pytest.raises(ValueError, match=r"8000\.\.384000"):
        select_effective_pcm(source, [])
