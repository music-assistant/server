"""
Tests for the stream source ranking.

The ranking orders the copies of a media item for playback. Every level of its sort key is
exercised on its own, with the levels above it held equal, and one set of candidates is
ranked from shuffled input to show the order does not depend on how they come in.
"""

from __future__ import annotations

import random
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from music_assistant_models.enums import ContentType
from music_assistant_models.helpers import get_global_cache_value, set_global_cache_values
from music_assistant_models.media_items import AudioFormat, ProviderMapping

from music_assistant.controllers.streams.stream_sources import (
    QualityTier,
    SourceCandidate,
    StreamSourceMode,
    StreamSourcePolicy,
    quality_tier,
    rank_provider_mappings,
    rank_stream_sources,
)

if TYPE_CHECKING:
    from music_assistant.models.provider import Provider

LOCAL = "filesystem_local--a"
# sorts before the local instance, so the id tie-break cannot mask a local-first level
DEEZER = "deezer--a"
# a local source that is not the filesystem: its local bonus comes from the provider cache
PLEX = "plex--a"
TIDAL = "tidal--a"
OTHER_TIDAL = "tidal--b"
SPOTIFY = "spotify--a"
ITEM_ID = "1"

PREFER_LOCAL = StreamSourcePolicy(mode=StreamSourceMode.PREFER_LOCAL)
PER_TRACK = StreamSourcePolicy(mode=StreamSourceMode.BEST_QUALITY_PER_TRACK)


def _format(
    content_type: ContentType = ContentType.FLAC,
    *,
    sample_rate: int = 44100,
    bit_depth: int = 16,
    bit_rate: int | None = None,
    codec_type: ContentType = ContentType.UNKNOWN,
    channels: int = 2,
) -> AudioFormat:
    """Build an audio format; the default is CD quality FLAC."""
    return AudioFormat(
        content_type=content_type,
        codec_type=codec_type,
        sample_rate=sample_rate,
        bit_depth=bit_depth,
        bit_rate=bit_rate,
        channels=channels,
    )


def _mapping(
    instance: str,
    *,
    item_id: str = ITEM_ID,
    audio_format: AudioFormat | None = None,
    in_library: bool | None = None,
) -> ProviderMapping:
    """Build a mapping of the item on the given provider instance."""
    return ProviderMapping(
        item_id=item_id,
        provider_domain=instance.split("--", maxsplit=1)[0],
        provider_instance=instance,
        in_library=in_library,
        audio_format=audio_format or _format(),
    )


def _candidate(
    instance: str,
    *,
    mapping: ProviderMapping | None = None,
    is_streaming: bool = True,
    **mapping_kwargs: Any,
) -> SourceCandidate:
    """
    Build a candidate of a copy served by the given provider instance.

    :param instance: The instance that would serve the copy.
    :param mapping: The copy's mapping; by default one on that same instance.
    :param is_streaming: Whether the instance is an account of a streaming service.
    :param mapping_kwargs: Passed on to the default mapping.
    """
    return SourceCandidate(
        mapping=mapping or _mapping(instance, **mapping_kwargs),
        provider=cast("Provider", SimpleNamespace(instance_id=instance)),
        is_streaming=is_streaming,
    )


def _ids(ranked: list[SourceCandidate]) -> list[str]:
    """Return the ranked candidates as ``instance/item id`` strings."""
    return [f"{candidate.provider.instance_id}/{candidate.mapping.item_id}" for candidate in ranked]


@pytest.mark.parametrize(
    ("audio_format", "tier"),
    [
        (AudioFormat(), QualityTier.UNKNOWN),
        (_format(ContentType.MP3, bit_rate=128), QualityTier.LOSSY_LOW),
        (_format(ContentType.MP3, bit_rate=256), QualityTier.LOSSY_HIGH),
        (_format(ContentType.OGG), QualityTier.LOSSY_HIGH),
        (_format(ContentType.MP3, bit_rate=320, channels=0), QualityTier.LOSSY_HIGH),
        (
            _format(ContentType.M4A, codec_type=ContentType.AAC, bit_rate=256),
            QualityTier.LOSSY_HIGH,
        ),
        (_format(ContentType.FLAC), QualityTier.LOSSLESS),
        (_format(ContentType.FLAC, sample_rate=48000), QualityTier.LOSSLESS),
        (_format(ContentType.M4A, codec_type=ContentType.ALAC), QualityTier.LOSSLESS),
        (_format(ContentType.FLAC, sample_rate=96000, bit_depth=24), QualityTier.HIRES),
        (_format(ContentType.FLAC, bit_depth=24), QualityTier.HIRES),
    ],
    ids=[
        "unknown format",
        "mp3 128 kbps is low lossy",
        "mp3 256 kbps is high lossy",
        "unknown bit rate counts as 320 kbps",
        "zero channels do not break the tier",
        "aac in m4a is lossy",
        "cd quality flac is lossless",
        "48 kHz is still lossless",
        "alac in m4a is lossless",
        "96/24 is hi-res",
        "24 bit at 44.1 kHz is hi-res",
    ],
)
def test_quality_tier(audio_format: AudioFormat, tier: QualityTier) -> None:
    """An audio format lands in the tier its container, codec, bit rate and resolution give."""
    assert quality_tier(audio_format) == tier


@pytest.mark.parametrize(
    ("candidates", "options", "expected", "reason"),
    [
        (
            [
                _candidate(LOCAL, is_streaming=False),
                _candidate(TIDAL, audio_format=_format(sample_rate=96000, bit_depth=24)),
                _candidate(SPOTIFY, audio_format=_format(ContentType.MP3, bit_rate=320)),
            ],
            {"pinned": (SPOTIFY, ITEM_ID), "preferred": [TIDAL]},
            [f"{SPOTIFY}/1", f"{TIDAL}/1", f"{LOCAL}/1"],
            "pinned",
        ),
        (
            [
                _candidate(LOCAL, is_streaming=False),
                _candidate(TIDAL, audio_format=_format(sample_rate=96000, bit_depth=24)),
                _candidate(SPOTIFY, audio_format=_format(ContentType.MP3, bit_rate=320)),
            ],
            {"pinned": (SPOTIFY, ITEM_ID), "policy": PER_TRACK},
            [f"{TIDAL}/1", f"{LOCAL}/1", f"{SPOTIFY}/1"],
            "hi-res 96/24",
        ),
        (
            [_candidate(LOCAL, is_streaming=False), _candidate(TIDAL)],
            {"pinned": (TIDAL, ITEM_ID), "policy": PREFER_LOCAL},
            [f"{TIDAL}/1", f"{LOCAL}/1"],
            "pinned",
        ),
        (
            [
                _candidate(
                    LOCAL, is_streaming=False, audio_format=_format(ContentType.MP3, bit_rate=320)
                ),
                _candidate(TIDAL),
            ],
            {"policy": PREFER_LOCAL},
            [f"{LOCAL}/1", f"{TIDAL}/1"],
            "local source",
        ),
        (
            [
                _candidate(
                    LOCAL, is_streaming=False, audio_format=_format(ContentType.MP3, bit_rate=320)
                ),
                _candidate(TIDAL),
            ],
            {},
            [f"{TIDAL}/1", f"{LOCAL}/1"],
            "lossless 44.1/16",
        ),
        (
            [
                _candidate(SPOTIFY, audio_format=_format(ContentType.MP3, bit_rate=320)),
                _candidate(TIDAL),
            ],
            {"preferred": [SPOTIFY]},
            [f"{SPOTIFY}/1", f"{TIDAL}/1"],
            "own account",
        ),
        (
            [
                _candidate(TIDAL, audio_format=AudioFormat()),
                _candidate(SPOTIFY, audio_format=_format(ContentType.MP3, bit_rate=128)),
            ],
            {},
            [f"{SPOTIFY}/1", f"{TIDAL}/1"],
            "lossy 128 kbps",
        ),
        (
            [
                _candidate(TIDAL),
                _candidate(OTHER_TIDAL, audio_format=_format(sample_rate=48000)),
            ],
            {},
            [f"{OTHER_TIDAL}/1", f"{TIDAL}/1"],
            "lossless 48/16",
        ),
        (
            [_candidate(DEEZER), _candidate(LOCAL, is_streaming=False)],
            {},
            [f"{LOCAL}/1", f"{DEEZER}/1"],
            "local source",
        ),
        (
            [
                _candidate(SPOTIFY, audio_format=_format(ContentType.OGG, bit_rate=320)),
                _candidate(
                    LOCAL, is_streaming=False, audio_format=_format(ContentType.MP3, bit_rate=320)
                ),
            ],
            {},
            [f"{LOCAL}/1", f"{SPOTIFY}/1"],
            "local source",
        ),
        (
            [_candidate(TIDAL), _candidate(OTHER_TIDAL, in_library=True)],
            {},
            [f"{OTHER_TIDAL}/1", f"{TIDAL}/1"],
            "in library",
        ),
        (
            [
                _candidate(TIDAL, mapping=_mapping(OTHER_TIDAL)),
                _candidate(OTHER_TIDAL),
            ],
            {},
            [f"{OTHER_TIDAL}/1", f"{TIDAL}/1"],
            "mapped instance",
        ),
        (
            [
                _candidate(OTHER_TIDAL),
                _candidate(TIDAL, item_id="2"),
                _candidate(TIDAL),
            ],
            {},
            [f"{TIDAL}/1", f"{TIDAL}/2", f"{OTHER_TIDAL}/1"],
            "tie-break",
        ),
    ],
    ids=[
        "the pin comes before the own account and the better quality",
        "the pin is ignored when every track is ranked on its own",
        "the pin comes before a local source when preferring local",
        "a local source comes first when preferring local",
        "quality decides over a local source by default",
        "the own account comes before a better quality elsewhere",
        "the quality tier decides before the quality score",
        "the quality score decides within a tier",
        "the local bonus decides between equal formats",
        "the local bonus outweighs a slightly better lossy format",
        "the in-library bonus decides between equal formats",
        "the mapped instance comes before an account standing in for it",
        "equal copies are tried by instance id, then item id",
    ],
)
def test_rank_stream_sources_level(
    candidates: list[SourceCandidate],
    options: dict[str, Any],
    expected: list[str],
    reason: str,
) -> None:
    """One level of the sort key decides, with every level above it equal."""
    ranked = rank_stream_sources(candidates, **options)

    assert _ids(ranked) == expected
    assert ranked[0].reason == reason


def test_the_order_does_not_depend_on_the_input_order() -> None:
    """Shuffling the candidates never changes the ranking, ties included."""
    candidates = [
        _candidate(LOCAL, is_streaming=False),
        _candidate(TIDAL),
        _candidate(TIDAL, item_id="2"),
        _candidate(OTHER_TIDAL),
        _candidate(SPOTIFY, audio_format=_format(ContentType.OGG, bit_rate=320)),
    ]
    expected = [f"{LOCAL}/1", f"{TIDAL}/1", f"{TIDAL}/2", f"{OTHER_TIDAL}/1", f"{SPOTIFY}/1"]
    rng = random.Random(1)

    for _ in range(20):
        shuffled = list(candidates)
        rng.shuffle(shuffled)
        assert _ids(rank_stream_sources(shuffled)) == expected


def test_reasons_name_the_decisive_level_and_the_last_candidates_quality() -> None:
    """Each candidate says what puts it ahead of the next; the last states its own quality."""
    ranked = rank_stream_sources(
        [
            _candidate(SPOTIFY, audio_format=_format(ContentType.OGG, bit_rate=320)),
            _candidate(TIDAL, audio_format=_format(sample_rate=96000, bit_depth=24)),
            _candidate(LOCAL, is_streaming=False),
            _candidate(OTHER_TIDAL, audio_format=AudioFormat()),
        ],
        preferred=[LOCAL],
    )

    assert [candidate.reason for candidate in ranked] == [
        "own account",
        "hi-res 96/24",
        "lossy 320 kbps",
        "unknown quality",
    ]
    assert rank_stream_sources([_candidate(TIDAL)])[0].reason == "lossless 44.1/16"
    assert (
        rank_stream_sources([_candidate(SPOTIFY, audio_format=_format(ContentType.MP3))])[0].reason
        == "lossy"
    )


async def test_rank_provider_mappings_pins_the_streamed_copy_and_knows_local_sources() -> None:
    """Bare mappings rank under the given policy, with the streamed copy first when known."""
    plex = _mapping(PLEX, audio_format=_format(ContentType.MP3, bit_rate=320))
    deezer = _mapping(DEEZER)
    previous = get_global_cache_value("non_streaming_providers")
    await set_global_cache_values({"non_streaming_providers": {PLEX}})
    try:
        assert rank_provider_mappings({plex, deezer}) == [deezer, plex]
        assert rank_provider_mappings({plex, deezer}, pinned=(PLEX, ITEM_ID)) == [plex, deezer]
        assert rank_provider_mappings({plex, deezer}, policy=PREFER_LOCAL) == [plex, deezer]
    finally:
        await set_global_cache_values({"non_streaming_providers": previous})
