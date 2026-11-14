"""Test 24-7 (247e) playback reporting."""

from __future__ import annotations

from base64 import b64encode
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import ContentType, StreamType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.helpers.datetime import iso_from_utc_timestamp
from music_assistant.providers.music247e.streaming import Music247eStreamingManager

if TYPE_CHECKING:
    from music_assistant.providers.music247e.provider import Music247eProvider


@pytest.mark.parametrize(
    ("elapsed", "streamed", "expected_seconds"),
    [
        (60.0, 0.0, None),
        (60.0, 0.5, None),
        (60.0, 0.999, None),
        (0.5, 60.0, None),
        (60.0, 1.0, 1),
        (60.0, 12.75, 12),
        (12.75, 60.0, 12),
    ],
)
async def test_report_playback(
    provider: Music247eProvider,
    monkeypatch: pytest.MonkeyPatch,
    elapsed: float,
    streamed: float,
    expected_seconds: int | None,
) -> None:
    """Skip sub-second playback and report integer seconds limited by elapsed time."""
    now = 1700000060
    monkeypatch.setattr("music_assistant.providers.music247e.streaming.utc_timestamp", lambda: now)
    api = Mock()
    api.post_graphql = AsyncMock(return_value={"data": {"reportPlayback": {"ok": True}}})
    provider.api = api
    manager = Music247eStreamingManager(provider)
    streamdetails = StreamDetails(
        provider=provider.instance_id,
        item_id="track1",
        audio_format=AudioFormat(content_type=ContentType.MP4),
        stream_type=StreamType.HLS,
        path="https://example.invalid/stream.m3u8",
        data={"start_ts": now - elapsed},
        seconds_streamed=streamed,
    )

    await manager.report_playback(streamdetails)

    if expected_seconds is None:
        api.post_graphql.assert_not_called()
        return

    api.post_graphql.assert_awaited_once()
    query, variables = api.post_graphql.call_args.args
    assert "reportPlayback(report: $report)" in query
    assert variables == {
        "report": {
            "playbackUrl": streamdetails.path,
            "playbackContext": b64encode(b"catalog:track;track1").decode(),
            "playedSeconds": expected_seconds,
            "playedAt": iso_from_utc_timestamp(now),
        }
    }
    assert type(variables["report"]["playedSeconds"]) is int
