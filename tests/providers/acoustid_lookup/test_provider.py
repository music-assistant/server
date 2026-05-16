"""Tests for the AcoustidLookupProvider."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from music_assistant_models.enums import MediaType, StreamType

from music_assistant.constants import CONF_LOG_LEVEL
from music_assistant.models.audio_analysis import AudioAnalysisData
from music_assistant.models.audio_analysis_provider import AnalysisSessionData
from music_assistant.providers.acoustid_lookup.provider import (
    CONF_API_KEY,
    CONF_MIN_SCORE,
    CONF_WRITE_TAGS_BACK,
    AcoustidLookupProvider,
    _parse_response,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_provider(
    *,
    api_key: str | None = "TESTKEY",
    min_score: float = 0.85,
    write_tags_back: bool = False,
) -> AcoustidLookupProvider:
    """Build a provider with a mocked MusicAssistant infrastructure."""
    mass = MagicMock()
    mass.get_provider = MagicMock(return_value=None)
    mass.streams.audio_analysis.get_audio_analysis_version = AsyncMock(return_value=None)
    mass.streams.audio_analysis.set_audio_analysis = AsyncMock()
    mass.metadata.set_track_identifiers = AsyncMock()
    mass.metadata.set_album_release_group = AsyncMock()
    mass.streams.audio_analysis.get_extra_data_for_album_tracks = AsyncMock(return_value=[])
    mass.music.tracks.get_library_item_by_prov_id = AsyncMock(return_value=None)
    mass.music.albums.get_library_item = AsyncMock(return_value=None)
    mass.music.albums.get_library_album_tracks = AsyncMock(return_value=[])
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.set = AsyncMock()
    mass.http_session = MagicMock()

    manifest = MagicMock()
    manifest.domain = "acoustid_lookup"

    config = MagicMock()
    config.instance_id = "acoustid_lookup_test"
    config.values = {}
    config_lookup: dict[str, Any] = {
        CONF_LOG_LEVEL: "GLOBAL",
        CONF_API_KEY: api_key,
        CONF_MIN_SCORE: min_score,
        CONF_WRITE_TAGS_BACK: write_tags_back,
    }
    config.get_value = MagicMock(side_effect=lambda key: config_lookup.get(key, "GLOBAL"))

    return AcoustidLookupProvider(mass, manifest, config, set())


def _make_streamdetails(
    *,
    stream_type: StreamType = StreamType.LOCAL_FILE,
    media_type: MediaType = MediaType.TRACK,
    path: str | None = "/music/track.flac",
    duration: int | None = 180,
) -> MagicMock:
    sd = MagicMock()
    sd.item_id = "track-1"
    sd.provider = "filesystem_local_test"
    sd.uri = "library://track/track-1"
    sd.media_type = media_type
    sd.stream_type = stream_type
    sd.path = path
    sd.duration = duration
    return sd


def _make_audio_format(
    *, bit_depth: int = 16, sample_rate: int = 44100, channels: int = 2
) -> MagicMock:
    fmt = MagicMock()
    fmt.bit_depth = bit_depth
    fmt.sample_rate = sample_rate
    fmt.channels = channels
    return fmt


class _FakeFingerprinter:
    """Stand-in for acoustid.chromaprint.Fingerprinter."""

    def __init__(self) -> None:
        self.start_args: tuple[int, int] | None = None
        self.fed: list[bytes] = []
        self.finished = False

    def start(self, sample_rate: int, channels: int) -> None:
        self.start_args = (sample_rate, channels)

    def feed(self, data: bytes) -> None:
        self.fed.append(bytes(data))

    def finish(self) -> bytes:
        self.finished = True
        return b"FAKEFINGERPRINT"


def _install_fake_chromaprint(monkeypatch: pytest.MonkeyPatch, fp: _FakeFingerprinter) -> None:
    """Patch the lazy chromaprint import so _create_fingerprinter returns our fake."""

    def fake_create(
        _self: AcoustidLookupProvider, sample_rate: int, channels: int
    ) -> _FakeFingerprinter:
        fp.start(int(sample_rate), int(channels))
        return fp

    monkeypatch.setattr(AcoustidLookupProvider, "_create_fingerprinter", fake_create)


def _install_lookup_response(
    monkeypatch: pytest.MonkeyPatch, response: dict[str, Any] | None
) -> None:
    """Patch the cached/throttled _lookup to return a fixed dict (or None)."""

    async def fake_lookup(
        _self: AcoustidLookupProvider, *_args: Any, **_kwargs: Any
    ) -> dict[str, Any] | None:
        return response

    monkeypatch.setattr(AcoustidLookupProvider, "_lookup", fake_lookup)


def _make_library_album(
    *,
    name: str = "Silver Thunderbird",
    existing_rg: str | None = None,
) -> MagicMock:
    """Build a library Album mock with get/add_external_id wiring."""
    album = MagicMock()
    album.name = name
    album.item_id = "42"
    storage: dict[str, str] = {}
    if existing_rg:
        storage["musicbrainz_releasegroupid"] = existing_rg

    def get_ext(key: Any) -> str | None:
        return storage.get(getattr(key, "value", key))

    def add_ext(key: Any, value: str) -> None:
        storage[getattr(key, "value", key)] = value

    album.get_external_id.side_effect = get_ext
    album.add_external_id.side_effect = add_ext
    return album


def _make_album_tracks(count: int, provider_instance: str = "filesystem_local_test") -> list[Any]:
    """Build N Track mocks each with one provider mapping in our provider."""
    tracks = []
    for i in range(count):
        pm = MagicMock()
        pm.provider_instance = provider_instance
        pm.provider_domain = "filesystem_local"
        pm.item_id = f"native-{i}"
        t = MagicMock()
        t.provider_mappings = [pm]
        tracks.append(t)
    return tracks


def _wire_album_for_consensus(
    provider: AcoustidLookupProvider,
    *,
    album: MagicMock,
    album_tracks: list[Any],
    extras: list[dict[str, Any]],
) -> None:
    """Glue an album + tracks + AcoustID extras into the provider's mass mock."""
    library_track = MagicMock()
    library_track.album = MagicMock(item_id="42")
    cast("MagicMock", provider.mass.music.tracks).get_library_item_by_prov_id = AsyncMock(
        return_value=library_track
    )
    cast("MagicMock", provider.mass.music.albums).get_library_item = AsyncMock(return_value=album)
    cast("MagicMock", provider.mass.music.albums).get_library_album_tracks = AsyncMock(
        return_value=album_tracks
    )
    cast(
        "MagicMock", provider.mass.streams.audio_analysis
    ).get_extra_data_for_album_tracks = AsyncMock(return_value=extras)


def _rg(
    rg_id: str, title: str = "Silver Thunderbird", primary_type: str = "Album"
) -> dict[str, Any]:
    """Shortcut for a release-group entry inside ``extras``."""
    return {"id": rg_id, "title": title, "primary_type": primary_type}


# ---------------------------------------------------------------------------
# Provider core
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skip_when_mbid_already_present() -> None:
    """A track that already has an MBID short-circuits without persisting anything."""
    provider = _make_provider()
    track = MagicMock()
    track.mbid = "00000000-0000-0000-0000-000000000001"
    cast("MagicMock", provider.mass.music.tracks).get_library_item_by_prov_id = AsyncMock(
        return_value=track
    )

    accepted = await provider.start_analysis(
        "session-1", _make_streamdetails(), _make_audio_format()
    )

    assert accepted is False
    # No sentinel row — re-evaluated every call so clearing the MBID re-enables analysis.
    cast("AsyncMock", provider.mass.streams.audio_analysis.set_audio_analysis).assert_not_awaited()
    # MBID check short-circuits before the base class consults the version-gate.
    cast(
        "AsyncMock", provider.mass.streams.audio_analysis.get_audio_analysis_version
    ).assert_not_awaited()


@pytest.mark.asyncio
async def test_skip_when_no_api_key() -> None:
    """No API key configured → bail before touching the library or the version-gate."""
    provider = _make_provider(api_key=None)

    accepted = await provider.start_analysis(
        "session-no-key", _make_streamdetails(), _make_audio_format()
    )

    assert accepted is False
    cast("AsyncMock", provider.mass.music.tracks.get_library_item_by_prov_id).assert_not_awaited()
    cast(
        "AsyncMock", provider.mass.streams.audio_analysis.get_audio_analysis_version
    ).assert_not_awaited()


@pytest.mark.asyncio
async def test_proceeds_when_mbid_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A track with no MBID falls through to the base version-gate and arms the session."""
    provider = _make_provider()
    track = MagicMock()
    track.mbid = None
    cast("MagicMock", provider.mass.music.tracks).get_library_item_by_prov_id = AsyncMock(
        return_value=track
    )
    _install_fake_chromaprint(monkeypatch, _FakeFingerprinter())

    accepted = await provider.start_analysis(
        "session-2", _make_streamdetails(), _make_audio_format()
    )

    assert accepted is True
    cast(
        "AsyncMock", provider.mass.streams.audio_analysis.get_audio_analysis_version
    ).assert_awaited_once()


@pytest.mark.asyncio
async def test_finalize_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A high-score match yields mbid + acoustid + candidates + release_groups."""
    provider = _make_provider()
    _install_fake_chromaprint(monkeypatch, _FakeFingerprinter())

    session_id = "session-final"
    await provider._start_analysis(session_id, _make_streamdetails(), _make_audio_format())
    provider._sessions[session_id] = AnalysisSessionData(
        streamdetails=_make_streamdetails(),
        audio_format=_make_audio_format(),
    )
    provider._data[session_id].pcm_seconds_fed = 30.0

    _install_lookup_response(
        monkeypatch,
        {
            "status": "ok",
            "results": [
                {
                    "id": "acoustid-1",
                    "score": 0.97,
                    "recordings": [
                        {
                            "id": "mbid-rich",
                            "title": "Song",
                            "artists": [{}],
                            "releases": [{"id": "rel-a"}],
                            "releasegroups": [
                                {"id": "rg-a", "title": "Album", "type": "Album"},
                            ],
                        },
                    ],
                },
            ],
        },
    )

    result = await provider._finalize(session_id)

    assert result is not None
    assert result.extra_data is not None
    assert result.extra_data["mbid"] == "mbid-rich"
    assert result.extra_data["acoustid"] == "acoustid-1"
    assert result.extra_data["match_score"] == pytest.approx(0.97)
    assert result.extra_data["release_groups"] == [
        {"id": "rg-a", "title": "Album", "primary_type": "Album", "secondary_types": []}
    ]
    assert result.extra_data["candidates"][0]["acoustid"] == "acoustid-1"


@pytest.mark.asyncio
async def test_finalize_rejects_low_score(monkeypatch: pytest.MonkeyPatch) -> None:
    """A best-result score below the threshold yields no analysis."""
    provider = _make_provider(min_score=0.85)
    _install_fake_chromaprint(monkeypatch, _FakeFingerprinter())

    session_id = "session-lowscore"
    await provider._start_analysis(session_id, _make_streamdetails(), _make_audio_format())
    provider._sessions[session_id] = AnalysisSessionData(
        streamdetails=_make_streamdetails(),
        audio_format=_make_audio_format(),
    )
    provider._data[session_id].pcm_seconds_fed = 10.0

    _install_lookup_response(
        monkeypatch,
        {
            "status": "ok",
            "results": [
                {
                    "id": "acoustid-low",
                    "score": 0.5,
                    "recordings": [{"id": "mbid-low", "title": "Song"}],
                }
            ],
        },
    )

    assert await provider._finalize(session_id) is None


# ---------------------------------------------------------------------------
# post_analysis side effects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("write_tags_back", "expect_tag_writes"),
    [(False, False), (True, True)],
    ids=["tags_disabled", "tags_enabled"],
)
async def test_post_analysis_persists_and_writes_tags(
    monkeypatch: pytest.MonkeyPatch,
    *,
    write_tags_back: bool,
    expect_tag_writes: bool,
) -> None:
    """post_analysis must always persist to library, and write tags only when enabled."""
    write_mbid = AsyncMock(return_value=True)
    write_acoustid = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "music_assistant.providers.acoustid_lookup.provider.write_musicbrainz_recording_id_tag",
        write_mbid,
    )
    monkeypatch.setattr(
        "music_assistant.providers.acoustid_lookup.provider.write_acoustid_tag",
        write_acoustid,
    )

    provider = _make_provider(write_tags_back=write_tags_back)
    streamdetails = _make_streamdetails()
    analysis = AudioAnalysisData(extra_data={"mbid": "mbid-x", "acoustid": "acoustid-x"})

    consensus_calls: list[Any] = []

    async def fake_consensus(_self: AcoustidLookupProvider, sd: Any) -> None:
        consensus_calls.append(sd)

    monkeypatch.setattr(AcoustidLookupProvider, "_maybe_set_album_release_group", fake_consensus)

    await provider.post_analysis(streamdetails, analysis)

    cast("AsyncMock", provider.mass.metadata.set_track_identifiers).assert_awaited_once_with(
        item_id=streamdetails.item_id,
        provider_instance_id_or_domain=streamdetails.provider,
        mbid="mbid-x",
        acoustid="acoustid-x",
        isrcs=[],
    )
    # Album consensus is a pure DB write and must run regardless of write_tags_back.
    assert consensus_calls == [streamdetails]
    if expect_tag_writes:
        write_mbid.assert_awaited_once_with("/music/track.flac", "mbid-x")
        write_acoustid.assert_awaited_once_with("/music/track.flac", "acoustid-x")
    else:
        write_mbid.assert_not_awaited()
        write_acoustid.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "expected_isrcs", "expected_artist_mbids"),
    [
        pytest.param(
            "returns",
            ["USAB12345678", "USCD87654321"],
            ["artist-mbid-1", "artist-mbid-2"],
            id="mb_returns_extras",
        ),
        pytest.param("missing", [], [], id="mb_provider_missing"),
        pytest.param("raises", [], [], id="mb_raises"),
    ],
)
async def test_post_analysis_mb_enrichment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    scenario: str,
    expected_isrcs: list[str],
    expected_artist_mbids: list[str],
) -> None:
    """ISRCs and artist MBIDs from MusicBrainz get applied as DB/file tags when available."""
    write_mbid = AsyncMock(return_value=True)
    write_acoustid = AsyncMock(return_value=True)
    write_isrc = AsyncMock(return_value=True)
    write_artist_mbid = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "music_assistant.providers.acoustid_lookup.provider.write_musicbrainz_recording_id_tag",
        write_mbid,
    )
    monkeypatch.setattr(
        "music_assistant.providers.acoustid_lookup.provider.write_acoustid_tag",
        write_acoustid,
    )
    monkeypatch.setattr(
        "music_assistant.providers.acoustid_lookup.provider.write_isrc_tag",
        write_isrc,
    )
    monkeypatch.setattr(
        "music_assistant.providers.acoustid_lookup.provider.write_musicbrainz_artist_id_tag",
        write_artist_mbid,
    )

    provider = _make_provider(write_tags_back=True)
    if scenario == "returns":
        artist_credit = [MagicMock(artist=MagicMock(id=mbid)) for mbid in expected_artist_mbids]
        mb_recording = MagicMock(isrcs=expected_isrcs, artist_credit=artist_credit)
        mb_provider = MagicMock()
        mb_provider.get_recording_details = AsyncMock(return_value=mb_recording)
        provider.mass.get_provider = MagicMock(return_value=mb_provider)  # type: ignore[method-assign]
    elif scenario == "missing":
        provider.mass.get_provider = MagicMock(return_value=None)  # type: ignore[method-assign]
    else:
        mb_provider = MagicMock()
        mb_provider.get_recording_details = AsyncMock(side_effect=aiohttp.ClientError("synthetic"))
        provider.mass.get_provider = MagicMock(return_value=mb_provider)  # type: ignore[method-assign]

    await provider.post_analysis(
        _make_streamdetails(),
        AudioAnalysisData(extra_data={"mbid": "mbid-x", "acoustid": "acoustid-x"}),
    )

    set_ids = cast("AsyncMock", provider.mass.metadata.set_track_identifiers)
    assert set_ids.await_args is not None
    assert set_ids.await_args.kwargs["isrcs"] == expected_isrcs
    if expected_isrcs:
        write_isrc.assert_awaited_once_with("/music/track.flac", expected_isrcs)
    else:
        write_isrc.assert_not_awaited()
    if expected_artist_mbids:
        write_artist_mbid.assert_awaited_once_with("/music/track.flac", expected_artist_mbids)
    else:
        write_artist_mbid.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_analysis_consensus_silent_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A crash in the consensus helper must not lose the per-track persistence."""
    provider = _make_provider()

    async def boom(_self: AcoustidLookupProvider, _streamdetails: Any) -> None:
        raise RuntimeError("synthetic")

    monkeypatch.setattr(AcoustidLookupProvider, "_maybe_set_album_release_group", boom)

    await provider.post_analysis(
        _make_streamdetails(),
        AudioAnalysisData(
            extra_data={"mbid": "mbid-x", "acoustid": "acoustid-x", "release_groups": []}
        ),
    )

    cast("AsyncMock", provider.mass.metadata.set_track_identifiers).assert_awaited_once()


# ---------------------------------------------------------------------------
# Album-level release-group consensus
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("track_count", "extras"),
    [
        # full coverage: 10 tracks all vote for rg-st, name-matching
        pytest.param(
            10, [{"release_groups": [_rg("rg-st")]} for _ in range(10)], id="full_coverage"
        ),
        # singleton: 1 track with a name-matching RG
        pytest.param(1, [{"release_groups": [_rg("rg-st")]}], id="singleton_with_match"),
        # tolerance: 5 of 6 tracks vote for rg-st; one outlier with a different RG
        pytest.param(
            6,
            [{"release_groups": [_rg("rg-st")]} for _ in range(5)]
            + [{"release_groups": [_rg("rg-other", title="Other")]}],
            id="tolerates_one_missing",
        ),
    ],
)
async def test_consensus_writes_release_group(
    *,
    track_count: int,
    extras: list[dict[str, Any]],
) -> None:
    """Coverage reaches the floor with a name-matching survivor → album row gets MB_RELEASEGROUP."""
    provider = _make_provider()
    album = _make_library_album(name="Silver Thunderbird")
    album_tracks = _make_album_tracks(track_count)
    _wire_album_for_consensus(provider, album=album, album_tracks=album_tracks, extras=extras)

    await provider._maybe_set_album_release_group(_make_streamdetails())

    cast("AsyncMock", provider.mass.metadata.set_album_release_group).assert_awaited_once_with(
        42, "rg-st"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("album_kwargs", "track_count", "extras"),
    [
        # 1) album already has a release-group — idempotent skip before tally.
        pytest.param(
            {"existing_rg": "rg-pre-existing"},
            6,
            [{"release_groups": [_rg("rg-st")]} for _ in range(6)],
            id="album_already_has_releasegroup",
        ),
        # 2) below 50% quorum — 4 of 12 voted.
        pytest.param(
            {},
            12,
            [{"release_groups": [_rg("rg-st")]} for _ in range(4)],
            id="below_50pct_quorum",
        ),
        # 3) top coverage below required — 8 voters split 2/2/2/2 across distinct RGs.
        pytest.param(
            {},
            8,
            [{"release_groups": [_rg(f"rg-{i // 2}", title=f"Title {i // 2}")]} for i in range(8)],
            id="top_coverage_below_required",
        ),
        # 4) no survivor title matches the album — voting tracks share RGs, all wrong names.
        pytest.param(
            {},
            6,
            [
                {
                    "release_groups": [
                        _rg("rg-here-now", title="Here & Now"),
                        _rg("rg-box", title="Box Set"),
                    ]
                }
                for _ in range(6)
            ],
            id="no_survivor_title_matches",
        ),
    ],
)
async def test_consensus_abstains(
    *,
    album_kwargs: dict[str, Any],
    track_count: int,
    extras: list[dict[str, Any]],
) -> None:
    """Each guard refuses to write the release-group for the right reason."""
    provider = _make_provider()
    album = _make_library_album(name="Silver Thunderbird", **album_kwargs)
    album_tracks = _make_album_tracks(track_count)
    _wire_album_for_consensus(provider, album=album, album_tracks=album_tracks, extras=extras)

    await provider._maybe_set_album_release_group(_make_streamdetails())

    cast("AsyncMock", provider.mass.metadata.set_album_release_group).assert_not_awaited()


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parse_response_aggregates_release_groups() -> None:
    """release_groups is the deduped union across recordings, filtered by min_score."""
    payload = {
        "status": "ok",
        "results": [
            {
                "id": "acoustid-high",
                "score": 0.9,
                "recordings": [
                    {
                        "id": "rec-a",
                        "title": "Song",
                        "releasegroups": [
                            {"id": "rg-shared", "title": "Album", "type": "Album"},
                            {"id": "rg-a-only", "title": "Sampler", "type": "Compilation"},
                            # duplicate of rg-shared must be deduped
                            {"id": "rg-shared", "title": "Album", "type": "Album"},
                        ],
                    },
                    {
                        "id": "rec-b",
                        "releasegroups": [
                            {"id": "rg-shared", "title": "Album", "type": "Album"},
                            {"id": "rg-b-only", "title": "Reissue", "type": "Album"},
                        ],
                    },
                ],
            },
            {
                "id": "acoustid-low",
                "score": 0.3,
                "recordings": [
                    {
                        "id": "rec-low",
                        "title": "Song",
                        "releasegroups": [
                            {"id": "rg-low-score", "title": "Other", "type": "Album"}
                        ],
                    }
                ],
            },
        ],
    }
    _score, _acoustid, _mbid, _candidates, _matched, release_groups = _parse_response(
        payload, min_score=0.5
    )
    assert {rg["id"] for rg in release_groups} == {"rg-shared", "rg-a-only", "rg-b-only"}


@pytest.mark.parametrize(
    (
        "expected_track_title",
        "expected_album_title",
        "payload",
        "expected_mbid",
        "expected_acoustid",
    ),
    [
        # No track title supplied — filter is a no-op, score picks the winner.
        pytest.param(
            None,
            None,
            {
                "status": "ok",
                "results": [
                    {
                        "id": "acoustid-x",
                        "score": 0.9,
                        "recordings": [
                            {"id": "rec-x", "title": "Anything", "releases": [{"id": "r"}]}
                        ],
                    }
                ],
            },
            "rec-x",
            "acoustid-x",
            id="no_hint_falls_back_to_score",
        ),
        # Hint matches one recording — that recording wins.
        pytest.param(
            "Ventura Highway",
            None,
            {
                "status": "ok",
                "results": [
                    {
                        "id": "acoustid-x",
                        "score": 0.95,
                        "recordings": [
                            {"id": "rec-match", "title": "Ventura Highway"},
                            {"id": "rec-other", "title": "Other"},
                        ],
                    }
                ],
            },
            "rec-match",
            "acoustid-x",
            id="match_picks_correct_recording",
        ),
        # No recording matches the hint anywhere — refuse the match.
        pytest.param(
            "Ventura Highway",
            None,
            {
                "status": "ok",
                "results": [
                    {
                        "id": "acoustid-x",
                        "score": 0.97,
                        "recordings": [{"id": "rec-wrong", "title": "Trouble"}],
                    }
                ],
            },
            None,
            None,
            id="no_match_refuses",
        ),
        # Lower-score result whose recording title matches must beat higher-score non-match.
        pytest.param(
            "Ventura Highway",
            None,
            {
                "status": "ok",
                "results": [
                    {
                        "id": "acoustid-high",
                        "score": 0.99,
                        "recordings": [{"id": "rec-wrong", "title": "Trouble"}],
                    },
                    {
                        "id": "acoustid-low",
                        "score": 0.85,
                        "recordings": [{"id": "rec-right", "title": "Ventura Highway"}],
                    },
                ],
            },
            "rec-right",
            "acoustid-low",
            id="prefers_title_match_over_score",
        ),
        # MB has the canonical title; user has a medley/prefix that contains it.
        pytest.param(
            "Water Song / Janie's Got a Gun",
            None,
            {
                "status": "ok",
                "results": [
                    {
                        "id": "acoustid-x",
                        "score": 0.95,
                        "recordings": [{"id": "rec-jgg", "title": "Janie's Got a Gun"}],
                    }
                ],
            },
            "rec-jgg",
            "acoustid-x",
            id="substring_fallback_matches_subtitle",
        ),
        # Single-word title must not match a longer phrase containing it (trivial-collision guard).
        pytest.param(
            "Trouble",
            None,
            {
                "status": "ok",
                "results": [
                    {
                        "id": "acoustid-x",
                        "score": 0.97,
                        "recordings": [{"id": "rec-im-in-trouble", "title": "I'm In Trouble"}],
                    }
                ],
            },
            None,
            None,
            id="substring_fallback_rejects_single_word",
        ),
        # Two results both title-match the track; the album hint picks the right release.
        pytest.param(
            "Ventura Highway",
            "History: America's Greatest Hits",
            {
                "status": "ok",
                "results": [
                    {
                        "id": "acoustid-5.1mix",
                        "score": 0.99,
                        "recordings": [
                            {
                                "id": "rec-homecoming",
                                "title": "Ventura Highway",
                                "releases": [{"id": "rel-h", "title": "Homecoming"}],
                            }
                        ],
                    },
                    {
                        "id": "acoustid-history",
                        "score": 0.85,
                        "recordings": [
                            {
                                "id": "rec-history",
                                "title": "Ventura Highway",
                                "releases": [
                                    {"id": "rel-hist", "title": "History: America's Greatest Hits"}
                                ],
                            }
                        ],
                    },
                ],
            },
            "rec-history",
            "acoustid-history",
            id="prefers_album_match_when_track_matches_tie",
        ),
    ],
)
def test_parse_response_track_name_behaviour(
    *,
    expected_track_title: str | None,
    expected_album_title: str | None,
    payload: dict[str, Any],
    expected_mbid: str | None,
    expected_acoustid: str | None,
) -> None:
    """expected_track_title is a hard filter; expected_album_title disambiguates ties."""
    _score, acoustid, mbid, _candidates, _matched, _rgs = _parse_response(
        payload,
        expected_track_title=expected_track_title,
        expected_album_title=expected_album_title,
    )
    assert mbid == expected_mbid
    assert acoustid == expected_acoustid
