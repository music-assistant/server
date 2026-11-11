"""
Tests for the stream source ranking.

The ranking orders the copies of a media item for playback. Every level of its sort key is
exercised on its own, with the levels above it held equal, and one set of candidates is
ranked from shuffled input to show the order does not depend on how they come in. A copy
below the upgrade quality gives way to better ones. The settings the ranking runs under are read
from the streams settings.
"""

from __future__ import annotations

import random
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from music_assistant_models.enums import ContentType
from music_assistant_models.helpers import get_global_cache_value, set_global_cache_values
from music_assistant_models.media_items import AudioFormat, ProviderMapping

from music_assistant.controllers.streams.constants import (
    CONF_STREAM_SOURCE_MODE,
    CONF_UPGRADE_BELOW_QUALITY,
)
from music_assistant.controllers.streams.stream_sources import (
    DEFAULT_POLICY,
    ContainerListing,
    QualityTier,
    SourceCandidate,
    StreamSourceMode,
    StreamSourcePolicy,
    UpgradeQuality,
    quality_tier,
    rank_provider_mappings,
    rank_stream_sources,
    read_stream_source_policy,
    select_container_source,
)

if TYPE_CHECKING:
    from music_assistant.controllers.streams.controller import StreamsController
    from music_assistant.mass import MusicAssistant
    from music_assistant.models.provider import Provider

LOCAL = "filesystem_local--a"
# sorts before the local instance, so the id tie-break cannot mask a local-first level
DEEZER = "deezer--a"
# a local source that is not the filesystem: its local bonus comes from the provider cache
PLEX = "plex--a"
QOBUZ = "qobuz--a"
TIDAL = "tidal--a"
OTHER_TIDAL = "tidal--b"
SPOTIFY = "spotify--a"
ITEM_ID = "1"

PREFER_LOCAL = StreamSourcePolicy(mode=StreamSourceMode.PREFER_LOCAL)
PER_TRACK = StreamSourcePolicy(mode=StreamSourceMode.BEST_QUALITY_PER_TRACK)
UPGRADE_LOSSLESS = StreamSourcePolicy(upgrade_below=QualityTier.LOSSLESS)


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


MP3 = _format(ContentType.MP3, bit_rate=320)
HIRES = _format(sample_rate=96000, bit_depth=24)


def _ids(ranked: list[SourceCandidate]) -> list[str]:
    """Return the ranked candidates as ``instance/item id`` strings."""
    return [f"{candidate.provider.instance_id}/{candidate.mapping.item_id}" for candidate in ranked]


def _listing(
    instance: str,
    *formats: AudioFormat,
    is_streaming: bool = True,
    complete: bool = True,
) -> ContainerListing:
    """
    Build what an instance lists of an album: one copy per format given.

    :param instance: The instance listing the album.
    :param formats: The format of each copy it lists; one CD quality FLAC by default.
    :param is_streaming: Whether the instance is an account of a streaming service.
    :param complete: Whether it lists every track the album has in the library.
    """
    return ContainerListing(
        instance_id=instance,
        is_streaming=is_streaming,
        copies=[
            _mapping(instance, item_id=str(number), audio_format=audio_format)
            for number, audio_format in enumerate(formats or (_format(),), start=1)
        ],
        complete=complete,
    )


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
    rng = random.Random(1)  # noqa: S311 - test data

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


@pytest.mark.parametrize(
    ("candidates", "options", "expected", "reason"),
    [
        (
            [
                _candidate(LOCAL, is_streaming=False, audio_format=MP3),
                _candidate(TIDAL),
            ],
            {"pinned": (LOCAL, ITEM_ID), "policy": UPGRADE_LOSSLESS},
            [f"{TIDAL}/1", f"{LOCAL}/1"],
            "upgrade: lossless 44.1/16 over lossy 320 kbps",
        ),
        (
            [
                _candidate(LOCAL, is_streaming=False, audio_format=MP3),
                _candidate(TIDAL),
            ],
            {
                "policy": StreamSourcePolicy(
                    mode=StreamSourceMode.PREFER_LOCAL, upgrade_below=QualityTier.LOSSLESS
                )
            },
            [f"{TIDAL}/1", f"{LOCAL}/1"],
            "upgrade: lossless 44.1/16 over lossy 320 kbps",
        ),
        (
            [
                _candidate(SPOTIFY, audio_format=_format(ContentType.MP3, bit_rate=128)),
                _candidate(DEEZER, audio_format=_format(ContentType.OGG, bit_rate=320)),
            ],
            {"pinned": (SPOTIFY, ITEM_ID), "policy": UPGRADE_LOSSLESS},
            [f"{DEEZER}/1", f"{SPOTIFY}/1"],
            "upgrade: lossy 320 kbps over lossy 128 kbps",
        ),
        (
            [_candidate(LOCAL, is_streaming=False), _candidate(TIDAL, audio_format=HIRES)],
            {"pinned": (LOCAL, ITEM_ID), "policy": UPGRADE_LOSSLESS},
            [f"{LOCAL}/1", f"{TIDAL}/1"],
            "pinned",
        ),
        (
            [
                _candidate(LOCAL, is_streaming=False, audio_format=MP3),
                _candidate(SPOTIFY, audio_format=_format(ContentType.OGG, bit_rate=320)),
            ],
            {"pinned": (LOCAL, ITEM_ID), "policy": UPGRADE_LOSSLESS},
            [f"{LOCAL}/1", f"{SPOTIFY}/1"],
            "pinned",
        ),
        (
            [
                _candidate(LOCAL, is_streaming=False, audio_format=MP3),
                _candidate(TIDAL),
            ],
            {"pinned": (LOCAL, ITEM_ID)},
            [f"{LOCAL}/1", f"{TIDAL}/1"],
            "pinned",
        ),
        (
            [
                _candidate(TIDAL, audio_format=AudioFormat()),
                _candidate(
                    LOCAL,
                    is_streaming=False,
                    audio_format=_format(ContentType.MP3, bit_rate=128),
                ),
            ],
            {"pinned": (TIDAL, ITEM_ID), "policy": UPGRADE_LOSSLESS},
            [f"{TIDAL}/1", f"{LOCAL}/1"],
            "pinned",
        ),
    ],
    ids=[
        "a better copy jumps the pin",
        "a better copy jumps a local source when preferring local",
        "a better copy below the upgrade quality still jumps",
        "no jump when the first copy meets the upgrade quality",
        "no jump for a copy of the same tier",
        "no jump without an upgrade quality",
        "a copy of unknown quality is not taken to be below",
    ],
)
def test_rank_stream_sources_upgrade(
    candidates: list[SourceCandidate],
    options: dict[str, Any],
    expected: list[str],
    reason: str,
) -> None:
    """A first copy below the upgrade quality gives way to every copy of a better tier."""
    ranked = rank_stream_sources(candidates, **options)

    assert _ids(ranked) == expected
    assert ranked[0].reason == reason


def test_the_copies_reaching_the_upgrade_quality_go_first() -> None:
    """Better copies that reach the upgrade quality go first, alike ones in the usual order."""
    ranked = rank_stream_sources(
        [
            _candidate(LOCAL, is_streaming=False, audio_format=MP3),
            _candidate(TIDAL),
            _candidate(QOBUZ, audio_format=HIRES),
            _candidate(DEEZER),
            _candidate(SPOTIFY, audio_format=_format(ContentType.OGG, bit_rate=320)),
        ],
        pinned=(LOCAL, ITEM_ID),
        preferred=[DEEZER, SPOTIFY],
        policy=StreamSourcePolicy(upgrade_below=QualityTier.HIRES),
    )

    # the hi-res copy before the own lossless account, the own account before the other
    # lossless copy, and the own lossy account, no better than the pinned copy, behind it
    assert _ids(ranked) == [f"{QOBUZ}/1", f"{DEEZER}/1", f"{TIDAL}/1", f"{LOCAL}/1", f"{SPOTIFY}/1"]
    assert [candidate.reason for candidate in ranked] == [
        "upgrade: hi-res 96/24 over lossless 44.1/16",
        "own account",
        "upgrade: lossless 44.1/16 over lossy 320 kbps",
        "pinned",
        "lossy 320 kbps",
    ]


@pytest.mark.parametrize("pinned_mp3", [False, True], ids=["no pinned copy", "a pinned mp3"])
def test_a_poorer_pinned_copy_does_not_change_the_upgrade(pinned_mp3: bool) -> None:
    """The copy that reaches the upgrade quality plays, whether or not a poorer one is pinned."""
    candidates = [_candidate(DEEZER), _candidate(QOBUZ, audio_format=HIRES)]
    if pinned_mp3:
        candidates.append(_candidate(LOCAL, is_streaming=False, audio_format=MP3))

    ranked = rank_stream_sources(
        candidates,
        pinned=(LOCAL, ITEM_ID) if pinned_mp3 else None,
        preferred=[DEEZER],
        policy=StreamSourcePolicy(upgrade_below=QualityTier.HIRES),
    )

    assert _ids(ranked)[:2] == [f"{QOBUZ}/1", f"{DEEZER}/1"]


def test_without_a_copy_reaching_the_upgrade_quality_the_closest_goes_first() -> None:
    """Better copies that all fall short go first the closer they come to the upgrade quality."""
    ranked = rank_stream_sources(
        [
            _candidate(
                LOCAL, is_streaming=False, audio_format=_format(ContentType.MP3, bit_rate=128)
            ),
            _candidate(SPOTIFY, audio_format=_format(ContentType.OGG, bit_rate=320)),
            _candidate(TIDAL),
        ],
        pinned=(LOCAL, ITEM_ID),
        preferred=[SPOTIFY],
        policy=StreamSourcePolicy(upgrade_below=QualityTier.HIRES),
    )

    assert _ids(ranked) == [f"{TIDAL}/1", f"{SPOTIFY}/1", f"{LOCAL}/1"]


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


@pytest.mark.parametrize(
    ("listed", "options", "expected"),
    [
        (
            [_listing(TIDAL), _listing(SPOTIFY, HIRES, complete=False)],
            {},
            TIDAL,
        ),
        (
            [_listing(TIDAL), _listing(SPOTIFY, MP3, complete=False)],
            {"preferred": [SPOTIFY]},
            SPOTIFY,
        ),
        (
            [_listing(LOCAL, MP3, is_streaming=False), _listing(TIDAL, HIRES)],
            {"policy": PREFER_LOCAL},
            LOCAL,
        ),
        (
            [_listing(LOCAL, MP3, is_streaming=False, complete=False), _listing(SPOTIFY)],
            {"policy": PREFER_LOCAL, "preferred": [SPOTIFY]},
            LOCAL,
        ),
        (
            [_listing(LOCAL, MP3, is_streaming=False), _listing(TIDAL, HIRES)],
            {},
            TIDAL,
        ),
        (
            [_listing(TIDAL, _format(), _format()), _listing(OTHER_TIDAL, HIRES, MP3)],
            {},
            TIDAL,
        ),
        (
            [
                _listing(TIDAL, _format(), _format(), _format(sample_rate=48000)),
                _listing(OTHER_TIDAL, _format(), _format(sample_rate=48000), HIRES),
            ],
            {},
            OTHER_TIDAL,
        ),
        (
            [_listing(DEEZER), _listing(PLEX, is_streaming=False)],
            {},
            PLEX,
        ),
        (
            [_listing(OTHER_TIDAL), _listing(TIDAL)],
            {},
            TIDAL,
        ),
    ],
    ids=[
        "a complete listing comes before a better partial one",
        "the own account comes before a complete listing",
        "a local source comes first when preferring local",
        "a local source comes before the own account when preferring local",
        "quality decides over a local source by default",
        "the poorest copy decides the quality tier",
        "the median score decides within a tier",
        "a local source wins a tie",
        "equal listings are chosen by instance id",
    ],
)
def test_select_container_source_level(
    listed: list[ContainerListing], options: dict[str, Any], expected: str
) -> None:
    """One level of the album source key decides, with every level above it equal."""
    assert select_container_source(listed, **options) == expected
    assert select_container_source(list(reversed(listed)), **options) == expected


def test_no_container_source_when_every_track_is_ranked_on_its_own() -> None:
    """Ranking every track on its own chooses no source for the album."""
    assert select_container_source([_listing(LOCAL, is_streaming=False)], policy=PER_TRACK) is None


def test_no_container_source_without_a_listed_copy() -> None:
    """An instance that serves none of the album's tracks is no source of it."""
    assert select_container_source([]) is None
    empty = ContainerListing(instance_id=TIDAL, is_streaming=True, copies=[], complete=True)
    assert select_container_source([empty, _listing(SPOTIFY, MP3, complete=False)]) == SPOTIFY


@pytest.mark.parametrize(
    ("stored", "mode"),
    [
        ({CONF_STREAM_SOURCE_MODE: "best_quality"}, StreamSourceMode.BEST_QUALITY),
        ({CONF_STREAM_SOURCE_MODE: "prefer_local"}, StreamSourceMode.PREFER_LOCAL),
        (
            {CONF_STREAM_SOURCE_MODE: "best_quality_per_track"},
            StreamSourceMode.BEST_QUALITY_PER_TRACK,
        ),
        ({CONF_STREAM_SOURCE_MODE: "local_first"}, StreamSourceMode.BEST_QUALITY),
        ({}, StreamSourceMode.BEST_QUALITY),
    ],
    ids=["best quality", "prefer local", "best quality for every track", "unknown", "missing"],
)
def test_read_stream_source_policy(stored: dict[str, str], mode: StreamSourceMode) -> None:
    """Each mode the setting offers is read as such, an unknown or missing one as the default."""
    streams = SimpleNamespace(
        get_config_value=lambda key, default=None, **_kwargs: stored.get(key, default)
    )
    mass = cast("MusicAssistant", SimpleNamespace(streams=streams))

    assert read_stream_source_policy(mass, "q1") == StreamSourcePolicy(mode=mode)


async def test_the_setting_offers_every_mode_and_defaults_to_best_quality(
    streams_controller: StreamsController,
) -> None:
    """The streams settings offer the three modes: best quality until another one is saved."""
    mass = streams_controller.mass
    entry = next(
        entry
        for entry in await streams_controller.get_config_entries()
        if entry.key == CONF_STREAM_SOURCE_MODE
    )

    assert entry.category == "source_selection"
    assert entry.default_value == StreamSourceMode.BEST_QUALITY
    assert [option.value for option in entry.options] == [
        "best_quality",
        "prefer_local",
        "best_quality_per_track",
    ]
    streams_controller.config = await mass.config.get_core_config(streams_controller.domain)
    assert read_stream_source_policy(mass) == DEFAULT_POLICY
    await mass.config.save_core_config(
        streams_controller.domain, {CONF_STREAM_SOURCE_MODE: "prefer_local"}
    )
    assert read_stream_source_policy(mass) == PREFER_LOCAL


@pytest.mark.parametrize(
    ("stored", "policy"),
    [
        ({CONF_UPGRADE_BELOW_QUALITY: "off"}, DEFAULT_POLICY),
        (
            {CONF_UPGRADE_BELOW_QUALITY: "lossy_high"},
            StreamSourcePolicy(upgrade_below=QualityTier.LOSSY_HIGH),
        ),
        ({CONF_UPGRADE_BELOW_QUALITY: "lossless"}, UPGRADE_LOSSLESS),
        (
            {CONF_UPGRADE_BELOW_QUALITY: "hires"},
            StreamSourcePolicy(upgrade_below=QualityTier.HIRES),
        ),
        ({CONF_UPGRADE_BELOW_QUALITY: "studio"}, DEFAULT_POLICY),
        (
            {CONF_STREAM_SOURCE_MODE: "local_first", CONF_UPGRADE_BELOW_QUALITY: "lossless"},
            UPGRADE_LOSSLESS,
        ),
        (
            {CONF_STREAM_SOURCE_MODE: "prefer_local", CONF_UPGRADE_BELOW_QUALITY: "hires"},
            StreamSourcePolicy(mode=StreamSourceMode.PREFER_LOCAL, upgrade_below=QualityTier.HIRES),
        ),
    ],
    ids=[
        "off",
        "high-bitrate lossy",
        "lossless",
        "hi-res",
        "unknown",
        "an unknown mode keeps the upgrade",
        "with a mode",
    ],
)
def test_read_upgrade_quality(stored: dict[str, str], policy: StreamSourcePolicy) -> None:
    """Each upgrade quality the setting offers is read as its tier, an unknown one as off."""
    streams = SimpleNamespace(
        get_config_value=lambda key, default=None, **_kwargs: stored.get(key, default)
    )
    mass = cast("MusicAssistant", SimpleNamespace(streams=streams))

    assert read_stream_source_policy(mass, "q1") == policy


async def test_the_upgrade_setting_offers_every_quality_and_defaults_to_off(
    streams_controller: StreamsController,
) -> None:
    """The streams settings offer the upgrade qualities: off until another one is saved."""
    mass = streams_controller.mass
    entry = next(
        entry
        for entry in await streams_controller.get_config_entries()
        if entry.key == CONF_UPGRADE_BELOW_QUALITY
    )

    assert entry.category == "source_selection"
    assert entry.default_value == UpgradeQuality.OFF
    assert [option.value for option in entry.options] == ["off", "lossy_high", "lossless", "hires"]
    streams_controller.config = await mass.config.get_core_config(streams_controller.domain)
    assert read_stream_source_policy(mass).upgrade_below is None
    await mass.config.save_core_config(
        streams_controller.domain, {CONF_UPGRADE_BELOW_QUALITY: "lossless"}
    )
    assert read_stream_source_policy(mass) == UPGRADE_LOSSLESS
