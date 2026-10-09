"""
Ranking of the sources a media item can be streamed from.

A library item often has several copies: a local file, the same track on one or more
streaming services, another account of the same service. This module decides the order those
copies are tried in. The caller expands the mappings to the provider instances that may serve
them and applies the access rules, this module only sorts.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass, replace
from enum import IntEnum, StrEnum
from operator import itemgetter
from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import ContentType
from music_assistant_models.helpers import get_global_cache_value

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat, ProviderMapping

    from music_assistant.models.provider import Provider

# a lossy copy counts as high quality from this bit rate (kbps) on
LOSSY_HIGH_MIN_BIT_RATE = 256
# a lossy copy without a known bit rate counts as this one, like its quality score does
_ASSUMED_BIT_RATE = 320


class QualityTier(IntEnum):
    """Coarse quality classes of an audio format, best last."""

    UNKNOWN = 0
    LOSSY_LOW = 1
    LOSSY_HIGH = 2
    LOSSLESS = 3
    HIRES = 4


class StreamSourceMode(StrEnum):
    """Which copy of a media item plays when it is available from several sources."""

    BEST_QUALITY = "best_quality"
    PREFER_LOCAL = "prefer_local"
    BEST_QUALITY_PER_TRACK = "best_quality_per_track"


@dataclass(frozen=True, slots=True)
class StreamSourcePolicy:
    """The source selection settings a ranking runs under."""

    mode: StreamSourceMode = StreamSourceMode.BEST_QUALITY


@dataclass(frozen=True, slots=True)
class SourceCandidate:
    """One copy of a media item on one provider instance that may serve it."""

    mapping: ProviderMapping
    provider: Provider
    # an account of a streaming service, as opposed to a library of its own
    is_streaming: bool
    # why the candidate ranks where it does, filled in by the ranking
    reason: str = ""


DEFAULT_POLICY = StreamSourcePolicy()

_SortKey = tuple[bool, bool, bool, int, int, bool, str, str]

# what each level of the sort key stands for; the quality levels name what decided
_LEVEL_REASONS: tuple[str | None, ...] = (
    "pinned",
    "local source",
    "own account",
    None,
    None,
    "mapped instance",
    "tie-break",
    "tie-break",
)
_SCORE_LEVEL = 4


def quality_tier(audio_format: AudioFormat) -> QualityTier:
    """
    Return the quality tier of an audio format.

    A copy is lossless when its container or its codec is, and hi-res when it is lossless
    above 48 kHz or above 16 bit. A lossy copy without a known bit rate counts as 320 kbps.

    :param audio_format: The audio format to classify.
    """
    if audio_format.content_type.is_lossless() or audio_format.codec_type.is_lossless():
        if audio_format.sample_rate > 48000 or audio_format.bit_depth > 16:
            return QualityTier.HIRES
        return QualityTier.LOSSLESS
    if (
        audio_format.content_type == ContentType.UNKNOWN
        and audio_format.codec_type == ContentType.UNKNOWN
    ):
        return QualityTier.UNKNOWN
    if (audio_format.bit_rate or _ASSUMED_BIT_RATE) >= LOSSY_HIGH_MIN_BIT_RATE:
        return QualityTier.LOSSY_HIGH
    return QualityTier.LOSSY_LOW


def rank_stream_sources(
    candidates: Iterable[SourceCandidate],
    *,
    pinned: tuple[str, str] | None = None,
    preferred: Collection[str] = (),
    policy: StreamSourcePolicy = DEFAULT_POLICY,
) -> list[SourceCandidate]:
    """
    Return the candidates in the order they are to be tried, each with its reason.

    The pinned copy comes first (unless the policy ranks every track on its own), then local
    sources when the policy prefers them, then the playback user's own accounts, then the
    best quality by tier and by the mapping's quality score, which favours local and
    in-library copies. Ties fall to the candidate's own instance and item id, so equal copies
    are tried in the same order at every selection.

    :param candidates: The copies that may serve the item, already narrowed to what the
        playback user may use.
    :param pinned: The (provider instance, item id) the item is to be played from, if any.
    :param preferred: The provider instances the playback user owns.
    :param policy: The source selection settings to rank under.
    """
    keyed = sorted(
        (
            (
                _sort_key(
                    candidate.provider.instance_id,
                    candidate.mapping,
                    candidate.is_streaming,
                    pinned,
                    preferred,
                    policy,
                ),
                candidate,
            )
            for candidate in candidates
        ),
        key=itemgetter(0),
    )
    ranked: list[SourceCandidate] = []
    for index, (key, candidate) in enumerate(keyed):
        following = keyed[index + 1] if index + 1 < len(keyed) else None
        ranked.append(replace(candidate, reason=_reason(candidate, key, following)))
    return ranked


def rank_provider_mappings(
    mappings: Iterable[ProviderMapping],
    *,
    pinned: tuple[str, str] | None = None,
    policy: StreamSourcePolicy = DEFAULT_POLICY,
) -> list[ProviderMapping]:
    """
    Return a media item's own mappings in the order the policy ranks them.

    For callers that look up data per copy outside a playback: there is no playback user to
    steer to, only the copy actually streamed, when known, comes first. Whether a mapping's
    instance is a streaming service is read off the loaded providers.

    :param mappings: The media item's provider mappings.
    :param pinned: The (provider instance, item id) the item is streamed from, if known.
    :param policy: The source selection settings to rank under.
    """
    non_streaming = cast("set[str]", get_global_cache_value("non_streaming_providers") or set())
    return sorted(
        mappings,
        key=lambda mapping: _sort_key(
            mapping.provider_instance,
            mapping,
            mapping.provider_instance not in non_streaming,
            pinned,
            (),
            policy,
        ),
    )


def _sort_key(
    instance_id: str,
    mapping: ProviderMapping,
    is_streaming: bool,
    pinned: tuple[str, str] | None,
    preferred: Collection[str],
    policy: StreamSourcePolicy,
) -> _SortKey:
    """Return the ranking key of one copy on one instance; the lowest key is tried first."""
    is_pinned = (instance_id, mapping.item_id) == pinned
    return (
        policy.mode != StreamSourceMode.BEST_QUALITY_PER_TRACK and not is_pinned,
        policy.mode == StreamSourceMode.PREFER_LOCAL and is_streaming,
        instance_id not in preferred,
        -quality_tier(mapping.audio_format),
        # the mapping's score carries the local and in-library bonus
        -mapping.quality,
        # the instance the item is mapped on, before an account standing in for it
        instance_id != mapping.provider_instance,
        instance_id,
        mapping.item_id,
    )


def _reason(
    candidate: SourceCandidate,
    key: _SortKey,
    following: tuple[_SortKey, SourceCandidate] | None,
) -> str:
    """Return what puts a candidate ahead of the next one; the last one states its quality."""
    audio_format = candidate.mapping.audio_format
    if following is None:
        return _quality_label(audio_format)
    next_key, next_candidate = following
    for level, (own, other) in enumerate(zip(key, next_key, strict=True)):
        if own == other:
            continue
        # a copy that ranks ahead on score without a better format did so on the local or
        # in-library bonus the mapping's score carries
        if (
            level == _SCORE_LEVEL
            and audio_format.quality <= next_candidate.mapping.audio_format.quality
        ):
            return _bonus_reason(candidate, next_candidate)
        return _LEVEL_REASONS[level] or _quality_label(audio_format)
    return _quality_label(audio_format)


def _bonus_reason(candidate: SourceCandidate, next_candidate: SourceCandidate) -> str:
    """Return which bonus put a candidate ahead of the next one at an equal format."""
    if not candidate.is_streaming and next_candidate.is_streaming:
        return "local source"
    if candidate.mapping.in_library and not next_candidate.mapping.in_library:
        return "in library"
    return "local source"


def _quality_label(audio_format: AudioFormat) -> str:
    """Return a short description of an audio format's quality, such as ``hi-res 96/24``."""
    tier = quality_tier(audio_format)
    if tier == QualityTier.UNKNOWN:
        return "unknown quality"
    if tier < QualityTier.LOSSLESS:
        return f"lossy {audio_format.bit_rate} kbps" if audio_format.bit_rate else "lossy"
    label = "hi-res" if tier == QualityTier.HIRES else "lossless"
    return f"{label} {audio_format.sample_rate / 1000:g}/{audio_format.bit_depth}"
