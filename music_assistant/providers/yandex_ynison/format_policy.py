"""Pure format policy for dynamic AudioSource sessions."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from music_assistant_models.enums import ContentType

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat

type PcmSignature = tuple[ContentType, int, int, int]

_PCM_CONTENT_TYPES: dict[int, ContentType] = {
    16: ContentType.PCM_S16LE,
    24: ContentType.PCM_S24LE,
    32: ContentType.PCM_S32LE,
}


def select_effective_pcm(
    source_format: AudioFormat,
    supported_sample_rates: Iterable[tuple[int, int]],
) -> PcmSignature:
    """
    Select the effective native PCM signature for a source and consumer.

    :param source_format: Real format returned by the linked music provider.
    :param supported_sample_rates: Consumer ``(sample_rate, bit_depth)`` pairs. Music
        Assistant treats only their sample-rate component as a constraint for realtime
        AudioSource PCM and preserves the source bit depth.
    :return: Content type, sample rate, bit depth, and channel count.
    :raises ValueError: If the real source format is outside dynamic-mode bounds.
    """
    source_rate = source_format.sample_rate
    if not 8_000 <= source_rate <= 384_000:
        raise ValueError("source sample rate must be within 8000..384000 Hz")
    if not 1 <= source_format.bit_depth <= 32:
        raise ValueError("source bit depth must be within 1..32")

    source_depth = _pcm_container_depth(source_format.bit_depth)
    supported = [(rate, depth) for rate, depth in supported_sample_rates if rate > 0 and depth > 0]
    if not supported:
        return (
            _PCM_CONTENT_TYPES[source_depth],
            source_rate,
            source_depth,
            source_format.channels,
        )

    rates = {rate for rate, _depth in supported}
    rates_not_above_source = [rate for rate in rates if rate <= source_rate]
    selected_rate = max(rates_not_above_source) if rates_not_above_source else min(rates)
    return (
        _PCM_CONTENT_TYPES[source_depth],
        selected_rate,
        source_depth,
        source_format.channels,
    )


def _pcm_container_depth(source_depth: int) -> int:
    """Map source precision to its PCM container depth."""
    if source_depth <= 16:
        return 16
    if source_depth <= 24:
        return 24
    return 32
