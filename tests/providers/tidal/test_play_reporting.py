"""Test Tidal play reporting."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, Mock

import pytest

from music_assistant.providers.tidal.play_reporting import (
    EVENT_BATCH_URL,
    TidalPlayReportingManager,
)

TRACK_ID = "123"


@pytest.fixture
def reporting_manager(provider_mock: Mock) -> TidalPlayReportingManager:
    """Return a TidalPlayReportingManager instance."""
    return TidalPlayReportingManager(provider_mock)


def _mock_post_response(provider_mock: Mock, status: int = 200) -> AsyncMock:
    """Wire mass.http_session.post to return a response with the given status."""
    response = AsyncMock()
    response.status = status
    provider_mock.mass.http_session.post.return_value.__aenter__.return_value = response
    return response


def test_register_stream_records_pending_session(
    reporting_manager: TidalPlayReportingManager,
) -> None:
    """Test register_stream records format info keyed by track id for on_played later."""
    reporting_manager.register_stream(
        TRACK_ID, quality="LOSSLESS", asset_presentation="FULL", audio_mode="STEREO"
    )

    session = reporting_manager._pending[TRACK_ID]
    assert session["tidal_quality"] == "LOSSLESS"
    assert session["tidal_asset_presentation"] == "FULL"
    assert session["tidal_audio_mode"] == "STEREO"
    assert "tidal_session_id" in session


async def test_report_played_skips_without_registered_stream(
    reporting_manager: TidalPlayReportingManager, provider_mock: Mock
) -> None:
    """Test report_played is a no-op when the track was never registered."""
    await reporting_manager.report_played(TRACK_ID, duration=180, position=180, fully_played=True)

    provider_mock.mass.http_session.post.assert_not_called()


async def test_report_played_skips_when_not_fully_played(
    reporting_manager: TidalPlayReportingManager, provider_mock: Mock
) -> None:
    """Test a stop/skip (fully_played=False) is silently not reported."""
    reporting_manager.register_stream(TRACK_ID, "LOSSLESS", "FULL", "STEREO")

    await reporting_manager.report_played(TRACK_ID, duration=180, position=30, fully_played=False)

    provider_mock.mass.http_session.post.assert_not_called()


async def test_report_played_skips_without_duration(
    reporting_manager: TidalPlayReportingManager, provider_mock: Mock
) -> None:
    """Test a track with no known duration is not reported (can't build a position)."""
    reporting_manager.register_stream(TRACK_ID, "LOSSLESS", "FULL", "STEREO")

    await reporting_manager.report_played(TRACK_ID, duration=None, position=180, fully_played=True)

    provider_mock.mass.http_session.post.assert_not_called()


async def test_report_played_skips_without_auth(
    reporting_manager: TidalPlayReportingManager, provider_mock: Mock
) -> None:
    """Test nothing is sent when the provider has no access token or user id."""
    provider_mock.auth.access_token = None
    reporting_manager.register_stream(TRACK_ID, "LOSSLESS", "FULL", "STEREO")

    await reporting_manager.report_played(TRACK_ID, duration=180, position=180, fully_played=True)

    provider_mock.mass.http_session.post.assert_not_called()


async def test_report_played_consumes_the_pending_entry(
    reporting_manager: TidalPlayReportingManager, provider_mock: Mock
) -> None:
    """Test a registered stream is popped once reported, so it can't be reported twice."""
    _mock_post_response(provider_mock, status=200)
    reporting_manager.register_stream(TRACK_ID, "LOSSLESS", "FULL", "STEREO")

    await reporting_manager.report_played(TRACK_ID, duration=180, position=175, fully_played=True)
    assert TRACK_ID not in reporting_manager._pending

    # a second callback for the same (now-unregistered) track id is a no-op
    await reporting_manager.report_played(TRACK_ID, duration=180, position=175, fully_played=True)
    assert provider_mock.mass.http_session.post.call_count == 1


async def test_report_played_sends_correlated_batch_on_completion(
    reporting_manager: TidalPlayReportingManager, provider_mock: Mock
) -> None:
    """Test a fully-played track sends one batch of 5 events sharing one session id."""
    _mock_post_response(provider_mock, status=200)
    reporting_manager.register_stream(TRACK_ID, "LOSSLESS", "FULL", "STEREO")
    session_id = reporting_manager._pending[TRACK_ID]["tidal_session_id"]

    await reporting_manager.report_played(TRACK_ID, duration=180, position=175, fully_played=True)

    provider_mock.mass.http_session.post.assert_called_once()
    call = provider_mock.mass.http_session.post.call_args
    assert call.args[0] == EVENT_BATCH_URL
    form = call.kwargs["data"]

    entry_names = {
        form[f"SendMessageBatchRequestEntry.{i}.MessageAttribute.1.Value.StringValue"]
        for i in range(1, 6)
    }
    assert entry_names == {
        "streaming_session_start",
        "playback_info_fetch",
        "playback_session",
        "playback_statistics",
        "streaming_session_end",
    }

    session_ids = set()
    for i in range(1, 6):
        body = json.loads(form[f"SendMessageBatchRequestEntry.{i}.MessageBody"])
        session_ids.add(body["payload"]["streamingSessionId"])
    assert session_ids == {session_id}

    play_log_entry = next(
        json.loads(form[f"SendMessageBatchRequestEntry.{i}.MessageBody"])
        for i in range(1, 6)
        if form[f"SendMessageBatchRequestEntry.{i}.MessageAttribute.1.Value.StringValue"]
        == "playback_session"
    )
    assert play_log_entry["group"] == "play_log"
    assert play_log_entry["payload"]["actions"] == []
    assert play_log_entry["payload"]["endAssetPosition"] == 175
    assert play_log_entry["payload"]["playbackSessionId"] == session_id

    auth_header = call.kwargs["headers"]["Authorization"]
    assert auth_header == f"Bearer {provider_mock.auth.access_token}"


async def test_report_played_derives_start_from_real_elapsed_position(
    reporting_manager: TidalPlayReportingManager, provider_mock: Mock
) -> None:
    """Test the session start timestamp is derived from position, not stream-fetch time."""
    _mock_post_response(provider_mock, status=200)
    reporting_manager.register_stream(TRACK_ID, "LOSSLESS", "FULL", "STEREO")

    await reporting_manager.report_played(TRACK_ID, duration=200, position=200, fully_played=True)

    form = provider_mock.mass.http_session.post.call_args.kwargs["data"]
    play_log_entry = next(
        json.loads(form[f"SendMessageBatchRequestEntry.{i}.MessageBody"])
        for i in range(1, 6)
        if form[f"SendMessageBatchRequestEntry.{i}.MessageAttribute.1.Value.StringValue"]
        == "playback_session"
    )
    payload = play_log_entry["payload"]
    # startTimestamp/endTimestamp must be ~position seconds apart (the real elapsed
    # listening time), regardless of how long the underlying stream fetch itself took.
    elapsed_ms = payload["endTimestamp"] - payload["startTimestamp"]
    assert elapsed_ms == pytest.approx(200_000, abs=1000)


async def test_report_played_caps_position_at_duration(
    reporting_manager: TidalPlayReportingManager, provider_mock: Mock
) -> None:
    """Test a position slightly over duration doesn't report a position past it."""
    _mock_post_response(provider_mock, status=200)
    reporting_manager.register_stream(TRACK_ID, "LOSSLESS", "FULL", "STEREO")

    await reporting_manager.report_played(TRACK_ID, duration=180, position=182, fully_played=True)

    form = provider_mock.mass.http_session.post.call_args.kwargs["data"]
    play_log_entry = next(
        json.loads(form[f"SendMessageBatchRequestEntry.{i}.MessageBody"])
        for i in range(1, 6)
        if form[f"SendMessageBatchRequestEntry.{i}.MessageAttribute.1.Value.StringValue"]
        == "playback_session"
    )
    assert play_log_entry["payload"]["endAssetPosition"] == 180


async def test_report_played_swallows_send_errors(
    reporting_manager: TidalPlayReportingManager, provider_mock: Mock
) -> None:
    """Test a network failure while reporting is logged, not raised."""
    provider_mock.mass.http_session.post.side_effect = TimeoutError("boom")
    reporting_manager.register_stream(TRACK_ID, "LOSSLESS", "FULL", "STEREO")

    # must not raise
    await reporting_manager.report_played(TRACK_ID, duration=180, position=180, fully_played=True)

    provider_mock.logger.debug.assert_called()
