"""Tests for the LoudnessAnalysisProvider._finalize return-value contract."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType

from music_assistant.constants import CONF_LOG_LEVEL
from music_assistant.models.audio_analysis import AudioAnalysisData, AudioAnalysisError
from music_assistant.models.audio_analysis_provider import AnalysisSessionData
from music_assistant.providers.loudness_analysis.provider import (
    CONF_WRITE_REPLAYGAIN_TAGS,
    MIN_DURATION_SECONDS,
    LoudnessAnalysisProvider,
    LoudnessSessionData,
    _parse_ebur128_metrics,
)


def _make_provider() -> LoudnessAnalysisProvider:
    """Construct a LoudnessAnalysisProvider with mocked MA infrastructure."""
    mass = MagicMock()
    mass.streams.audio_analysis.get_audio_analysis_version = AsyncMock(return_value=None)
    mass.streams.audio_analysis.set_audio_analysis = AsyncMock()
    manifest = MagicMock()
    manifest.domain = "loudness_analysis"
    config = MagicMock()
    config.instance_id = "loudness_analysis_test"
    config.get_value = MagicMock(return_value="GLOBAL")
    config.values = {}
    return LoudnessAnalysisProvider(mass, manifest, config, set())


def _make_session_data() -> tuple[LoudnessSessionData, MagicMock]:
    """Return (LoudnessSessionData with mocked ffmpeg, streamdetails mock)."""
    streamdetails = MagicMock()
    streamdetails.item_id = "track-1"
    streamdetails.provider = "test_provider"
    streamdetails.uri = "test://track-1"
    streamdetails.media_type = MediaType.TRACK

    ffmpeg = MagicMock()
    ffmpeg.wait = AsyncMock()
    ffmpeg.close = AsyncMock()
    ffmpeg.write_eof = AsyncMock()
    ffmpeg.log_history = []

    session_data = LoudnessSessionData(ffmpeg=ffmpeg)
    return session_data, streamdetails


@pytest.mark.asyncio
async def test_finalize_returns_analysis_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """_finalize must return AudioAnalysisData with the parsed metrics when analysis succeeds."""
    provider = _make_provider()
    session_id = "test-session-success"

    session_data, streamdetails = _make_session_data()
    session_data.chunks_received = MIN_DURATION_SECONDS + 1
    session_data.eof_sent = True  # already sent, _send_eof will be a no-op

    provider._data[session_id] = session_data
    provider._sessions[session_id] = AnalysisSessionData(
        streamdetails=streamdetails,
        audio_format=MagicMock(),
    )

    # Patch _parse_ebur128_metrics to return a valid result above the threshold
    monkeypatch.setattr(
        "music_assistant.providers.loudness_analysis.provider._parse_ebur128_metrics",
        lambda _log: (-14.5, 7.2, -1.2),
    )

    result = await provider._finalize(session_id)

    assert isinstance(result, AudioAnalysisData)
    assert result.loudness_integrated == -14.5


@pytest.mark.asyncio
async def test_finalize_raises_when_below_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """_finalize must raise AudioAnalysisError when measured loudness is below the reliability floor."""
    provider = _make_provider()
    session_id = "test-session-quiet"

    session_data, streamdetails = _make_session_data()
    session_data.chunks_received = MIN_DURATION_SECONDS + 1
    session_data.eof_sent = True

    provider._data[session_id] = session_data
    provider._sessions[session_id] = AnalysisSessionData(
        streamdetails=streamdetails,
        audio_format=MagicMock(),
    )

    # ebur128 reports ~-70 LUFS on near-silent tracks, below the reliability floor.
    monkeypatch.setattr(
        "music_assistant.providers.loudness_analysis.provider._parse_ebur128_metrics",
        lambda _log: (-70.0, 5.0, -1.0),
    )

    with pytest.raises(AudioAnalysisError, match="quiet"):
        await provider._finalize(session_id)


@pytest.mark.asyncio
async def test_finalize_raises_when_insufficient_duration() -> None:
    """_finalize must raise AudioAnalysisError when chunks_received is below the minimum."""
    provider = _make_provider()
    session_id = "test-session-short"

    session_data, streamdetails = _make_session_data()
    session_data.chunks_received = MIN_DURATION_SECONDS - 1
    session_data.eof_sent = True

    provider._data[session_id] = session_data
    provider._sessions[session_id] = AnalysisSessionData(
        streamdetails=streamdetails,
        audio_format=MagicMock(),
    )

    with pytest.raises(AudioAnalysisError, match="too short"):
        await provider._finalize(session_id)


# ---------------------------------------------------------------------------
# post_analysis tests
# ---------------------------------------------------------------------------


def _make_loudness_provider(*, write_replaygain_tags: bool) -> LoudnessAnalysisProvider:
    """Construct a LoudnessAnalysisProvider with a config gated on write_replaygain_tags."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "loudness_analysis"
    config = MagicMock()
    config.instance_id = "loudness_analysis_test"
    config.values = {}
    config.get_value = MagicMock(
        side_effect=lambda key: {
            CONF_LOG_LEVEL: "GLOBAL",
            CONF_WRITE_REPLAYGAIN_TAGS: write_replaygain_tags,
        }.get(key, "GLOBAL")
    )
    return LoudnessAnalysisProvider(mass, manifest, config, supported_features=set())


@pytest.mark.asyncio
async def test_post_analysis_writes_tag_when_path_writable_and_config_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """post_analysis writes ReplayGain tag when path is filesystem-writable AND config is on."""
    provider = _make_loudness_provider(write_replaygain_tags=True)
    streamdetails = MagicMock()
    streamdetails.path = "/music/test.flac"
    analysis = AudioAnalysisData(loudness_integrated=-14.0)

    write_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "music_assistant.providers.loudness_analysis.provider.write_replaygain_track_gain",
        write_mock,
    )

    await provider.post_analysis(streamdetails, analysis)

    # ReplayGain 2.0: track_gain_db = -18 - loudness_lufs = -18 - (-14) = -4
    write_mock.assert_awaited_once_with("/music/test.flac", -4.0)


@pytest.mark.asyncio
async def test_post_analysis_skips_when_path_not_writable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """post_analysis is a no-op when streamdetails.path is None or non-string."""
    provider = _make_loudness_provider(write_replaygain_tags=True)
    streamdetails = MagicMock()
    streamdetails.path = None
    analysis = AudioAnalysisData(loudness_integrated=-14.0)

    write_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "music_assistant.providers.loudness_analysis.provider.write_replaygain_track_gain",
        write_mock,
    )

    await provider.post_analysis(streamdetails, analysis)

    write_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_analysis_skips_when_config_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """post_analysis is a no-op when write_replaygain_tags config is False."""
    provider = _make_loudness_provider(write_replaygain_tags=False)
    streamdetails = MagicMock()
    streamdetails.path = "/music/test.flac"
    analysis = AudioAnalysisData(loudness_integrated=-14.0)

    write_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "music_assistant.providers.loudness_analysis.provider.write_replaygain_track_gain",
        write_mock,
    )

    await provider.post_analysis(streamdetails, analysis)

    write_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_analysis_skips_when_loudness_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """post_analysis is a no-op when analysis.loudness_integrated is None."""
    provider = _make_loudness_provider(write_replaygain_tags=True)
    streamdetails = MagicMock()
    streamdetails.path = "/music/test.flac"
    analysis = AudioAnalysisData(loudness_integrated=None)

    write_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "music_assistant.providers.loudness_analysis.provider.write_replaygain_track_gain",
        write_mock,
    )

    await provider.post_analysis(streamdetails, analysis)

    write_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# true peak measurement tests
# ---------------------------------------------------------------------------

# verbatim ffmpeg output, do not hand-edit
_FFMPEG_SUMMARY_WITH_PEAK = [
    "[Parsed_ebur128_0 @ 0x8b8c05080] Summary:",
    "",
    "  Integrated loudness:",
    "    I:         -21.8 LUFS",
    "    Threshold: -31.8 LUFS",
    "",
    "  Loudness range:",
    "    LRA:         0.0 LU",
    "    Threshold: -41.8 LUFS",
    "    LRA low:   -21.8 LUFS",
    "    LRA high:  -21.8 LUFS",
    "",
    "  True peak:",
    "    Peak:      -18.1 dBFS",
]


def test_parse_metrics_extracts_true_peak_from_ffmpeg_summary() -> None:
    """All three metrics must be parsed from a real ebur128 summary."""
    integrated, lra, true_peak = _parse_ebur128_metrics(_FFMPEG_SUMMARY_WITH_PEAK)

    assert integrated == -21.8
    assert lra == 0.0
    assert true_peak == -18.1


@pytest.mark.asyncio
async def test_start_analysis_requests_peak_measurement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ebur128 only reports true peak when explicitly asked, so the filter must request it."""
    provider = _make_provider()
    streamdetails = MagicMock()
    streamdetails.volume_normalization_mode = None

    fake_ffmpeg = MagicMock()
    fake_ffmpeg.start = AsyncMock()
    ffmpeg_cls = MagicMock(return_value=fake_ffmpeg)
    monkeypatch.setattr("music_assistant.providers.loudness_analysis.provider.FFMpeg", ffmpeg_cls)

    assert await provider._start_analysis("session-peak", streamdetails, MagicMock()) is True

    filter_params = ffmpeg_cls.call_args.kwargs["filter_params"]
    assert any("peak=true" in param for param in filter_params)
