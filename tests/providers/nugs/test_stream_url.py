"""Test Nugs.net stream url building for regular, promo/trial and inactive subscriptions."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ContentType, MediaType
from music_assistant_models.errors import AudioError, InvalidDataError

from music_assistant.providers.nugs import CONF_QUALITY, NugsProvider

USER_DATA = {"userId": "user-1"}
SUBSCRIPTION_BASE = {
    "startedAt": "08/01/2026 00:00:00",
    "endsAt": "09/01/2026 00:00:00",
    "legacySubscriptionId": "legacy-1",
}


def _stub_get_data(provider: NugsProvider, subscription: dict[str, Any]) -> None:
    """Attach a _get_data stub returning the given subscription payload."""

    async def _fake(nugs_api: str, _endpoint: str, **_kwargs: Any) -> Any:
        if nugs_api == "subscription":
            return subscription
        return USER_DATA

    provider._get_data = AsyncMock(side_effect=_fake)  # type: ignore[method-assign]


def _stub_http_session(provider: NugsProvider, *bodies: str) -> MagicMock:
    """Stub the http session so subplayer calls return the given bodies in order."""
    bodies = bodies or ('{"streamLink": "https://stream.test/track.m3u8"}',)
    contexts = []
    for body in bodies:
        response = AsyncMock()
        response.raise_for_status = lambda: None
        response.text = AsyncMock(return_value=body)
        get_ctx = AsyncMock()
        get_ctx.__aenter__ = AsyncMock(return_value=response)
        get_ctx.__aexit__ = AsyncMock(return_value=False)
        contexts.append(get_ctx)
    session_get = MagicMock(side_effect=contexts)
    mass: Any = provider.mass
    mass.http_session.get = session_get
    return session_get


@pytest.mark.asyncio
async def test_stream_url_uses_regular_plan(provider: NugsProvider) -> None:
    """A regular subscription passes its own plan id as the cost plan access list."""
    _stub_get_data(provider, {**SUBSCRIPTION_BASE, "plan": {"id": "plan-42"}})
    session_get = _stub_http_session(provider)

    assert await provider._get_stream_url("123") == ("https://stream.test/track.m3u8", "lossy")
    assert session_get.call_args.kwargs["params"]["subCostplanIDAccessList"] == "plan-42"


@pytest.mark.asyncio
async def test_stream_url_falls_back_to_promo_plan(provider: NugsProvider) -> None:
    """A trial/promo subscription has plan set to None and its plan id under promo."""
    _stub_get_data(
        provider,
        {**SUBSCRIPTION_BASE, "plan": None, "promo": {"plan": {"id": "promo-7"}}},
    )
    session_get = _stub_http_session(provider)

    assert await provider._get_stream_url("123") == ("https://stream.test/track.m3u8", "lossy")
    assert session_get.call_args.kwargs["params"]["subCostplanIDAccessList"] == "promo-7"


@pytest.mark.asyncio
async def test_stream_url_without_any_plan_raises(provider: NugsProvider) -> None:
    """Without a regular or promo plan a clear audio error is raised."""
    _stub_get_data(provider, {**SUBSCRIPTION_BASE, "plan": None, "promo": None})

    with pytest.raises(AudioError):
        await provider._get_stream_url("123")


def _set_quality(provider: NugsProvider, quality: str) -> None:
    """Configure the stream quality setting on the provider."""
    config: Any = provider.config
    config.get_value.side_effect = lambda key, default=None: {
        "log_level": "GLOBAL",
        CONF_QUALITY: quality,
    }.get(key, default)


@pytest.mark.asyncio
@pytest.mark.parametrize(("quality", "platform_id"), [("lossless", 2), ("mqa", 5), ("lossy", -1)])
async def test_stream_url_uses_quality_on_high_quality_plan(
    provider: NugsProvider, quality: str, platform_id: int
) -> None:
    """A plan with high quality streaming requests the configured quality."""
    _set_quality(provider, quality)
    _stub_get_data(provider, {**SUBSCRIPTION_BASE, "plan": {"id": "p", "isHighQuality": True}})
    session_get = _stub_http_session(provider)

    await provider._get_stream_url("123")
    assert session_get.call_args.kwargs["params"]["platformID"] == platform_id


@pytest.mark.asyncio
async def test_stream_url_caps_quality_to_plan(provider: NugsProvider) -> None:
    """A plan without high quality streaming only requests the lossy stream."""
    _set_quality(provider, "mqa")
    _stub_get_data(provider, {**SUBSCRIPTION_BASE, "plan": {"id": "p", "isHighQuality": False}})
    session_get = _stub_http_session(provider)

    await provider._get_stream_url("123")
    assert session_get.call_count == 1
    assert session_get.call_args.kwargs["params"]["platformID"] == -1


@pytest.mark.asyncio
async def test_stream_url_falls_back_to_lossy(provider: NugsProvider) -> None:
    """A track without a stream in the requested quality falls back to lossy."""
    _stub_get_data(provider, {**SUBSCRIPTION_BASE, "plan": {"id": "p", "isHighQuality": True}})
    session_get = _stub_http_session(
        provider, '{"streamLink": ""}', '{"streamLink": "https://stream.test/lossy.m3u8"}'
    )

    assert await provider._get_stream_url("123") == ("https://stream.test/lossy.m3u8", "lossy")
    assert [c.kwargs["params"]["platformID"] for c in session_get.call_args_list] == [2, -1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("container", "codec", "sample_rate", "bit_depth", "content_type", "codec_type"),
    [
        ("flac", "flac", 48000, 24, ContentType.FLAC, ContentType.FLAC),
        ("hls", "flac", 48000, 24, ContentType.UNKNOWN, ContentType.FLAC),
        ("hls", "alac", 44100, 16, ContentType.UNKNOWN, ContentType.ALAC),
        ("mov,mp4,m4a,3gp,3g2,mj2", "alac", 44100, 16, ContentType.MP4, ContentType.ALAC),
    ],
)
async def test_stream_details_use_probed_format(
    provider: NugsProvider,
    monkeypatch: pytest.MonkeyPatch,
    container: str,
    codec: str,
    sample_rate: int,
    bit_depth: int,
    content_type: ContentType,
    codec_type: ContentType,
) -> None:
    """A lossless stream reports the codec, sample rate and bit depth the stream actually has."""
    tags = MagicMock(
        format=container, sample_rate=sample_rate, bits_per_sample=bit_depth, channels=2
    )
    tags.bit_rate = 2000
    tags.raw = {
        "streams": [
            {"codec_type": "video", "codec_name": "mjpeg"},
            {"codec_type": "audio", "codec_name": codec},
        ]
    }
    probe = AsyncMock(return_value=tags)
    monkeypatch.setattr("music_assistant.providers.nugs.async_parse_tags", probe)
    provider._get_stream_url = AsyncMock(  # type: ignore[method-assign]
        return_value=("https://stream.test/track.flac", "mqa")
    )

    details = await provider.get_stream_details("123", MediaType.TRACK)
    probe.assert_awaited_once_with("https://stream.test/track.flac", timeout=10)
    assert details.audio_format.content_type == content_type
    assert details.audio_format.codec_type == codec_type
    assert details.audio_format.sample_rate == sample_rate
    assert details.audio_format.bit_depth == bit_depth


@pytest.mark.asyncio
async def test_stream_details_skip_probe_for_lossy(
    provider: NugsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lossy stream is not probed."""
    probe = AsyncMock()
    monkeypatch.setattr("music_assistant.providers.nugs.async_parse_tags", probe)
    provider._get_stream_url = AsyncMock(  # type: ignore[method-assign]
        return_value=("https://stream.test/track.m3u8", "lossy")
    )

    details = await provider.get_stream_details("123", MediaType.TRACK)
    probe.assert_not_called()
    assert details.audio_format.content_type == ContentType.UNKNOWN


@pytest.mark.asyncio
async def test_stream_details_survive_probe_failure(
    provider: NugsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stream that cannot be probed still plays with an unknown format."""
    probe = AsyncMock(side_effect=InvalidDataError("boom"))
    monkeypatch.setattr("music_assistant.providers.nugs.async_parse_tags", probe)
    provider._get_stream_url = AsyncMock(  # type: ignore[method-assign]
        return_value=("https://stream.test/track.m4a", "lossless")
    )

    details = await provider.get_stream_details("123", MediaType.TRACK)
    assert details.audio_format.content_type == ContentType.UNKNOWN
    assert details.path == "https://stream.test/track.m4a"
