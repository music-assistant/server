"""Tests for the nicovideo stream details."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import StreamType

from music_assistant.providers.nicovideo.constants import NICOVIDEO_USER_AGENT
from music_assistant.providers.nicovideo.services.video import NicovideoVideoService
from tests.providers.nicovideo.fixture_data.shared_types import StreamFixtureData
from tests.providers.nicovideo.helpers import create_converter_manager

if TYPE_CHECKING:
    from tests.providers.nicovideo.fixtures.fixture_loader import FixtureLoader

HLS_URL = "https://delivery.example.com/hlsbid/123/master.m3u8?session=abc"
MEDIA_PLAYLIST_URL = "https://delivery.example.com/hlsbid/123/media/audio.m3u8?session=abc"
MASTER_PLAYLIST = (
    "#EXTM3U\n"
    "#EXT-X-VERSION:6\n"
    '#EXT-X-STREAM-INF:BANDWIDTH=112053,CODECS="mp4a.40.2"\n'
    f"{MEDIA_PLAYLIST_URL}\n"
)
MEDIA_PLAYLIST = "#EXTM3U\n#EXT-X-VERSION:6\n#EXTINF:6.0,\nsegment1.m4s\n#EXT-X-ENDLIST\n"


def _create_video_service(
    fixture: StreamFixtureData, playlist_text: str
) -> tuple[NicovideoVideoService, MagicMock]:
    """Create a video service whose niconico client and HTTP session return canned data."""
    service_manager = Mock()
    service_manager.converter_manager = create_converter_manager()
    service_manager._call_with_throttler = AsyncMock(
        side_effect=lambda func, *args, **kwargs: func(*args, **kwargs)
    )
    watch = service_manager.niconico_py_client.video.watch
    watch.get_watch_data.return_value = fixture.watch_data
    watch.get_hls_content_url.return_value = HLS_URL
    service_manager.niconico_py_client.session.cookies.get.return_value = "test_bid"
    response = Mock()
    response.text = AsyncMock(return_value=playlist_text)
    http_session = MagicMock()
    http_session.get.return_value.__aenter__.return_value = response
    service_manager.provider.mass.http_session = http_session
    return NicovideoVideoService(service_manager), http_session


@pytest.mark.parametrize(
    ("playlist_text", "expected_path"),
    [(MASTER_PLAYLIST, MEDIA_PLAYLIST_URL), (MEDIA_PLAYLIST, HLS_URL)],
    ids=["master_playlist", "media_playlist"],
)
async def test_stream_details_point_ffmpeg_at_media_playlist(
    fixture_loader: FixtureLoader, playlist_text: str, expected_path: str
) -> None:
    """The stream details hand ffmpeg the media playlist URL with the domand_bid cookie."""
    fixture = fixture_loader.load_fixture(Path("stream/stream_data.json"))
    assert isinstance(fixture, StreamFixtureData)
    service, http_session = _create_video_service(fixture, playlist_text)
    track = service.converter_manager.track.convert_by_watch_data(
        fixture.watch_data, fixture.watch_lazy_data
    )
    assert track is not None

    streamdetails = await service.get_stream_details(fixture.watch_data.video.id_, track)

    assert streamdetails.stream_type == StreamType.HTTP
    assert streamdetails.path == expected_path
    assert streamdetails.extra_input_args == [
        "-headers",
        f"User-Agent: {NICOVIDEO_USER_AGENT}\r\nCookie: domand_bid=test_bid\r\n",
    ]
    assert streamdetails.allow_seek
    assert streamdetails.duration == fixture.watch_data.video.duration
    http_session.get.assert_called_once_with(
        HLS_URL, headers={"User-Agent": NICOVIDEO_USER_AGENT, "Cookie": "domand_bid=test_bid"}
    )
