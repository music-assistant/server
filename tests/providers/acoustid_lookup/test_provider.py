"""Tests for the AcoustidLookupProvider."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType, StreamType
from music_assistant_models.errors import ResourceTemporarilyUnavailable

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


def _make_provider(
    *,
    api_key: str | None = "TESTKEY",
    min_score: float = 0.85,
    write_tags_back: bool = False,
) -> AcoustidLookupProvider:
    """Build a provider with a mocked MusicAssistant infrastructure."""
    mass = MagicMock()
    mass.streams.audio_analysis.get_audio_analysis_version = AsyncMock(return_value=None)
    mass.streams.audio_analysis.set_audio_analysis = AsyncMock()
    mass.streams.audio_analysis.set_track_identifiers = AsyncMock()
    mass.music.tracks.get_library_item_by_prov_id = AsyncMock(return_value=None)
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


@pytest.mark.asyncio
async def test_skip_when_mbid_already_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """_start_analysis must return False and persist a sentinel when the track has an MBID."""
    provider = _make_provider()
    track = MagicMock()
    track.mbid = "00000000-0000-0000-0000-000000000001"
    cast("MagicMock", provider.mass.music.tracks).get_library_item_by_prov_id = AsyncMock(
        return_value=track
    )
    fp = _FakeFingerprinter()
    _install_fake_chromaprint(monkeypatch, fp)

    accepted = await provider._start_analysis(
        "session-1", _make_streamdetails(), _make_audio_format()
    )

    assert accepted is False
    set_aa = cast("AsyncMock", provider.mass.streams.audio_analysis.set_audio_analysis)
    set_aa.assert_awaited_once()
    call = set_aa.await_args
    assert call is not None
    assert call.kwargs["analysis"].extra_data == {"skipped": "mbid_present"}
    assert call.kwargs["analysis_version"] == provider.analysis_version


@pytest.mark.asyncio
async def test_skip_non_local_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """_start_analysis must reject streams whose stream_type isn't LOCAL_FILE."""
    provider = _make_provider()
    fp = _FakeFingerprinter()
    _install_fake_chromaprint(monkeypatch, fp)

    sd = _make_streamdetails(stream_type=StreamType.HTTP)
    accepted = await provider._start_analysis("session-2", sd, _make_audio_format())

    assert accepted is False
    get_track = cast("AsyncMock", provider.mass.music.tracks.get_library_item_by_prov_id)
    get_track.assert_not_called()


@pytest.mark.asyncio
async def test_fingerprint_pcm_accumulation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Feeding PCM chunks must accumulate into the session's fingerprinter."""
    provider = _make_provider()
    fp = _FakeFingerprinter()
    _install_fake_chromaprint(monkeypatch, fp)

    session_id = "session-fp"
    accepted = await provider._start_analysis(
        session_id, _make_streamdetails(), _make_audio_format()
    )
    assert accepted is True
    # populate _sessions like the base class would have
    provider._sessions[session_id] = AnalysisSessionData(
        streamdetails=_make_streamdetails(),
        audio_format=_make_audio_format(),
    )

    chunk = b"\x00\x00" * 1024
    for _ in range(3):
        await provider.process_pcm_chunk(session_id, chunk)

    assert len(fp.fed) == 3
    # 3 chunks of 2048 bytes s16le stereo @44.1kHz → 3 * 2048 / (4 * 44100) seconds
    expected_seconds = 3 * 2048 / (4 * 44100)
    assert provider._data[session_id].pcm_seconds_fed == pytest.approx(expected_seconds)


@pytest.mark.asyncio
async def test_finalize_persists_mbid_and_acoustid(monkeypatch: pytest.MonkeyPatch) -> None:
    """A high-score AcoustID match must yield the expected extra_data plus candidates."""
    provider = _make_provider()
    fp = _FakeFingerprinter()
    _install_fake_chromaprint(monkeypatch, fp)

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
                    "id": "acoustid-uuid-1",
                    "score": 0.97,
                    "recordings": [
                        {"id": "mbid-rich", "title": "Song", "artists": [{}], "releases": [{}]},
                        {"id": "mbid-sparse"},
                    ],
                },
                {
                    "id": "acoustid-uuid-2",
                    "score": 0.40,
                    "recordings": [{"id": "mbid-other"}],
                },
            ],
        },
    )

    result = await provider._finalize(session_id)

    assert result is not None
    assert result.extra_data is not None
    assert result.extra_data["mbid"] == "mbid-rich"
    assert result.extra_data["acoustid"] == "acoustid-uuid-1"
    assert result.extra_data["match_score"] == pytest.approx(0.97)
    candidates = result.extra_data["candidates"]
    assert len(candidates) == 2
    assert candidates[0]["acoustid"] == "acoustid-uuid-1"
    assert "mbid-rich" in candidates[0]["recordings"]


@pytest.mark.asyncio
async def test_low_score_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A best-result score below the threshold must produce no analysis."""
    provider = _make_provider(min_score=0.85)
    fp = _FakeFingerprinter()
    _install_fake_chromaprint(monkeypatch, fp)

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
                    "id": "acoustid-uuid-low",
                    "score": 0.5,
                    "recordings": [{"id": "mbid-low"}],
                }
            ],
        },
    )

    result = await provider._finalize(session_id)
    assert result is None


@pytest.mark.asyncio
async def test_rate_limit_retry() -> None:
    """A 429 from AcoustID must raise ResourceTemporarilyUnavailable so the throttler retries."""
    provider = _make_provider()

    @asynccontextmanager
    async def fake_get(*_args: Any, **_kwargs: Any) -> Any:
        response = MagicMock()
        response.status = 429
        response.headers = {"Retry-After": "7"}
        response.json = AsyncMock(return_value={})
        yield response

    provider.mass.http_session.get = fake_get  # type: ignore[method-assign,assignment]

    with pytest.raises(ResourceTemporarilyUnavailable) as excinfo:
        # bypass cache/throttler decorators to test the raw HTTP handling
        await AcoustidLookupProvider._lookup.__wrapped__.__wrapped__(  # type: ignore[attr-defined]
            provider, "key", "FAKEFINGERPRINT", 30
        )
    assert excinfo.value.backoff_time == 7


@pytest.mark.asyncio
async def test_post_analysis_writes_mbid_to_library_track() -> None:
    """post_analysis must hand mbid + acoustid to the audio_analysis controller helper."""
    provider = _make_provider(write_tags_back=False)
    analysis = AudioAnalysisData(extra_data={"mbid": "mbid-final", "acoustid": "acoustid-final"})
    streamdetails = _make_streamdetails()

    await provider.post_analysis(streamdetails, analysis)

    set_ids = cast("AsyncMock", provider.mass.streams.audio_analysis.set_track_identifiers)
    set_ids.assert_awaited_once_with(
        item_id=streamdetails.item_id,
        provider_instance_id_or_domain=streamdetails.provider,
        mbid="mbid-final",
        acoustid="acoustid-final",
    )


@pytest.mark.asyncio
async def test_post_analysis_writes_tags_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tag writeback must fire only when write_tags_back=True."""
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

    # disabled — no writes
    provider_off = _make_provider(write_tags_back=False)
    await provider_off.post_analysis(
        _make_streamdetails(),
        AudioAnalysisData(extra_data={"mbid": "mbid-x", "acoustid": "acoustid-x"}),
    )
    write_mbid.assert_not_awaited()
    write_acoustid.assert_not_awaited()

    # enabled — both writes fire
    provider_on = _make_provider(write_tags_back=True)
    await provider_on.post_analysis(
        _make_streamdetails(),
        AudioAnalysisData(extra_data={"mbid": "mbid-x", "acoustid": "acoustid-x"}),
    )
    write_mbid.assert_awaited_once_with("/music/track.flac", "mbid-x")
    write_acoustid.assert_awaited_once_with("/music/track.flac", "acoustid-x")


def test_parse_response_prefers_recording_with_more_data() -> None:
    """Within a single result, the recording with artists+releases must win."""
    payload = {
        "status": "ok",
        "results": [
            {
                "id": "acoustid-x",
                "score": 0.92,
                "recordings": [
                    {"id": "sparse"},
                    {"id": "rich", "artists": [{}], "releases": [{}], "title": "T"},
                ],
            }
        ],
    }
    score, acoustid, mbid, candidates, album_matched = _parse_response(payload)
    assert score == pytest.approx(0.92)
    assert acoustid == "acoustid-x"
    assert mbid == "rich"
    assert candidates[0]["recordings"] == ["sparse", "rich"]
    assert album_matched is False


def test_parse_response_prefers_release_title_match_over_richness() -> None:
    """When the expected album title matches a release on a sparser recording, it wins."""
    payload = {
        "status": "ok",
        "results": [
            {
                "id": "acoustid-x",
                "score": 0.95,
                "recordings": [
                    {
                        "id": "compilation",
                        "title": "Angelsong",
                        "artists": [{}],
                        "releases": [{"id": "rel-ghost", "title": "Ghost Train"}],
                    },
                    {
                        "id": "original",
                        "releases": [{"id": "rel-st", "title": "Silver Thunderbird"}],
                    },
                ],
            }
        ],
    }
    score, _acoustid, mbid, _candidates, album_matched = _parse_response(
        payload, expected_album_title="Silver Thunderbird"
    )
    assert score == pytest.approx(0.95)
    assert mbid == "original"
    assert album_matched is True


def test_parse_response_falls_back_to_richness_when_album_unknown() -> None:
    """Without an album hint, the existing richness heuristic still picks the rich entry."""
    payload = {
        "status": "ok",
        "results": [
            {
                "id": "acoustid-x",
                "score": 0.9,
                "recordings": [
                    {"id": "sparse"},
                    {"id": "rich", "title": "T", "artists": [{}], "releases": [{}]},
                ],
            }
        ],
    }
    _score, _acoustid, mbid, _candidates, album_matched = _parse_response(payload)
    assert mbid == "rich"
    assert album_matched is False
