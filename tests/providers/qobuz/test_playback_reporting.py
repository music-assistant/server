"""Tests for the playback reports the Qobuz provider sends."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import MediaType

from music_assistant.providers.qobuz import SUPPORTED_FEATURES, QobuzProvider

FILE_URL_RESPONSE = {
    "track_id": 42,
    "url": "https://streaming.qobuz.com/file/42.flac",
    "mime_type": "audio/flac",
    "sampling_rate": 44.1,
    "bit_depth": 16,
    "duration": 180,
    "format_id": 27,
}


@pytest.fixture
def provider() -> QobuzProvider:
    """Create a QobuzProvider whose API calls are mocked."""
    mass = Mock()
    manifest = Mock()
    manifest.domain = "qobuz"
    config = Mock()
    config.instance_id = "qobuz_test"
    config.name = "Qobuz Test"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {
        "quality": "27",
        "log_level": "GLOBAL",
    }.get(key, default)
    provider = QobuzProvider(mass, manifest, config, SUPPORTED_FEATURES)
    provider._user_auth_info = {"user": {"id": 123, "device": {"id": 7}, "credential": {"id": 9}}}
    provider._get_data = AsyncMock(return_value=FILE_URL_RESPONSE)  # type: ignore[method-assign]
    provider._post_data = AsyncMock(return_value={})  # type: ignore[method-assign]
    return provider


async def test_stream_details_send_no_report(provider: QobuzProvider) -> None:
    """Resolving stream details (also done for a preload) reports nothing to Qobuz."""
    streamdetails = await provider.get_stream_details("42", MediaType.TRACK)

    assert streamdetails.data == FILE_URL_RESPONSE
    provider._post_data.assert_not_awaited()  # type: ignore[attr-defined]
    provider.mass.create_task.assert_not_called()  # type: ignore[attr-defined]


async def test_playback_reports_one_start_and_one_end(provider: QobuzProvider) -> None:
    """A playback is reported as one start and one matching end."""
    streamdetails = await provider.get_stream_details("42", MediaType.TRACK)
    provider._get_data.reset_mock()  # type: ignore[attr-defined]

    await provider.on_stream_started(streamdetails)
    streamdetails.seconds_streamed = 179.6
    await provider.on_streamed(streamdetails)

    provider._post_data.assert_awaited_once()  # type: ignore[attr-defined]
    endpoint, *_ = provider._post_data.await_args.args  # type: ignore[attr-defined]
    (event,) = provider._post_data.await_args.kwargs["data"]  # type: ignore[attr-defined]
    assert endpoint == "track/reportStreamingStart"
    assert event["track_id"] == 42
    assert event["format_id"] == 27
    assert event["user_id"] == 123
    provider._get_data.assert_awaited_once_with(  # type: ignore[attr-defined]
        "track/reportStreamingEnd", user_id=123, track_id="42", duration=179
    )
