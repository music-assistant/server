"""Tests for Soundcloud stream URL selection and stream details."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from music_assistant_models.enums import MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError

if TYPE_CHECKING:
    from music_assistant.providers.soundcloud import SoundcloudMusicProvider

# transcoding endpoints as advertised by the Soundcloud API, and the CDN urls they resolve to
PROGRESSIVE_API = "https://api-v2.soundcloud.com/media/soundcloud:tracks:1/abc/stream/progressive"
HLS_API = "https://api-v2.soundcloud.com/media/soundcloud:tracks:1/abc/stream/hls"
AAC_API = "https://api-v2.soundcloud.com/media/soundcloud:tracks:1/abc/stream/aac"
PROGRESSIVE_CDN = "https://cf-media.sndcdn.com/gBClm0KpoWnf.128.mp3"
HLS_CDN = "https://cf-hls-media.sndcdn.com/playlist/gBClm0KpoWnf.128.mp3/playlist.m3u8"

NOW = 1700000000


def _transcoding(preset: str, protocol: str, url: str) -> dict[str, Any]:
    """Build a single transcoding entry."""
    return {"preset": preset, "url": url, "format": {"protocol": protocol}}


def _track_info(
    transcodings: list[dict[str, Any]], *, track_authorization: str | None = "auth-token"
) -> dict[str, Any]:
    """Build a Soundcloud API track object carrying the given transcodings."""
    track_info: dict[str, Any] = {"id": 1, "media": {"transcodings": transcodings}}
    if track_authorization is not None:
        track_info["track_authorization"] = track_authorization
    return track_info


def _resolver(responses: dict[str, dict[str, Any]]) -> AsyncMock:
    """Return an API stub resolving transcoding urls to their CDN response."""

    async def _get(url: str, **_kwargs: Any) -> dict[str, Any]:
        for api_url, response in responses.items():
            if url.startswith(api_url):
                return response
        # a transcoding that no longer exists answers with an empty body
        return {}

    return AsyncMock(side_effect=_get)


async def test_stream_url_needs_track_authorization(provider: SoundcloudMusicProvider) -> None:
    """Without a track authorization token no stream url can be built."""
    provider._soundcloud.get = _resolver({PROGRESSIVE_API: {"url": PROGRESSIVE_CDN}})

    result = await provider._get_stream_url(
        _track_info(
            [_transcoding("mp3_0_1", "progressive", PROGRESSIVE_API)], track_authorization=None
        )
    )

    assert result is None
    provider._soundcloud.get.assert_not_awaited()


async def test_stream_url_prefers_progressive_over_hls(provider: SoundcloudMusicProvider) -> None:
    """Progressive is picked over HLS, which only covers a limited content window."""
    provider._soundcloud.get = _resolver(
        {PROGRESSIVE_API: {"url": PROGRESSIVE_CDN}, HLS_API: {"url": HLS_CDN}}
    )

    # hls is listed first, as the Soundcloud API does
    result = await provider._get_stream_url(
        _track_info(
            [
                _transcoding("mp3_0_1", "hls", HLS_API),
                _transcoding("mp3_0_1", "progressive", PROGRESSIVE_API),
            ]
        )
    )

    assert result == PROGRESSIVE_CDN
    requested = provider._soundcloud.get.await_args_list[0].args[0]
    assert requested.startswith(PROGRESSIVE_API)
    assert "client_id=test-client-id" in requested
    assert "track_authorization=auth-token" in requested


async def test_stream_url_falls_back_to_hls(provider: SoundcloudMusicProvider) -> None:
    """An mp3 HLS transcoding is used when the track has no progressive variant."""
    provider._soundcloud.get = _resolver({HLS_API: {"url": HLS_CDN}})

    result = await provider._get_stream_url(_track_info([_transcoding("mp3_0_1", "hls", HLS_API)]))

    assert result == HLS_CDN


async def test_stream_url_ignores_non_mp3_presets(provider: SoundcloudMusicProvider) -> None:
    """Presets we cannot play are never requested."""
    provider._soundcloud.get = _resolver({AAC_API: {"url": "https://example.com/aac"}})

    result = await provider._get_stream_url(_track_info([_transcoding("aac_160k", "hls", AAC_API)]))

    assert result is None
    provider._soundcloud.get.assert_not_awaited()


async def test_stream_url_none_when_transcoding_is_gone(provider: SoundcloudMusicProvider) -> None:
    """A transcoding that no longer resolves yields no stream url."""
    # empty response body, as returned for the phantom mp3 entries of DRM protected tracks
    provider._soundcloud.get = _resolver({})

    result = await provider._get_stream_url(
        _track_info([_transcoding("mp3_1_0", "progressive", PROGRESSIVE_API)])
    )

    assert result is None


async def _stream_details(provider: SoundcloudMusicProvider, cdn_url: str) -> Any:
    """Return stream details for a track resolving to the given CDN url."""
    provider._soundcloud.get_track_details.return_value = [
        _track_info([_transcoding("mp3_0_1", "progressive", PROGRESSIVE_API)])
    ]
    provider._soundcloud.get = _resolver({PROGRESSIVE_API: {"url": cdn_url}})
    with patch("music_assistant.providers.soundcloud.time.time", return_value=NOW):
        return await provider.get_stream_details("1", MediaType.TRACK)


async def test_stream_details_for_progressive_url(provider: SoundcloudMusicProvider) -> None:
    """A progressive CDN url is streamed over HTTP and can be seeked."""
    streamdetails = await _stream_details(provider, PROGRESSIVE_CDN)

    assert streamdetails.item_id == "1"
    assert streamdetails.provider == provider.instance_id
    assert streamdetails.path == PROGRESSIVE_CDN
    assert streamdetails.stream_type == StreamType.HTTP
    assert streamdetails.can_seek
    assert streamdetails.allow_seek


async def test_stream_details_for_hls_url(provider: SoundcloudMusicProvider) -> None:
    """An HLS CDN url is streamed as HLS."""
    streamdetails = await _stream_details(provider, HLS_CDN)

    assert streamdetails.stream_type == StreamType.HLS


@pytest.mark.parametrize("param", ["Expires", "expire"])
async def test_stream_details_expiry_from_url(
    provider: SoundcloudMusicProvider, param: str
) -> None:
    """The expiry is taken from the CDN url so a seek never uses an expired url."""
    streamdetails = await _stream_details(provider, f"{PROGRESSIVE_CDN}?{param}={NOW + 3600}")

    # ten seconds of headroom are kept before the url actually expires
    assert streamdetails.expiration == 3590


async def test_stream_details_expiry_without_query(provider: SoundcloudMusicProvider) -> None:
    """A url without expiry information falls back to a conservative default."""
    streamdetails = await _stream_details(provider, PROGRESSIVE_CDN)

    assert streamdetails.expiration == 30


async def test_stream_details_expiry_clamped_for_stale_url(
    provider: SoundcloudMusicProvider,
) -> None:
    """An already expired url never results in a negative expiry."""
    streamdetails = await _stream_details(provider, f"{PROGRESSIVE_CDN}?Expires={NOW - 3600}")

    assert streamdetails.expiration == 30


async def test_stream_details_without_stream_url(provider: SoundcloudMusicProvider) -> None:
    """A track without any usable transcoding reports that no stream url is available."""
    provider._soundcloud.get_track_details.return_value = [_track_info([])]
    provider._soundcloud.get = _resolver({})

    with pytest.raises(MediaNotFoundError, match="No stream URL available"):
        await provider.get_stream_details("1", MediaType.TRACK)
