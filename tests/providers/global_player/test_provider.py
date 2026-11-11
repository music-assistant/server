"""Tests for the Global Player music provider."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from music_assistant_models.enums import (
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    MediaNotFoundError,
    ProviderUnavailableError,
    UnplayableMediaError,
)
from music_assistant_models.media_items import Radio

from music_assistant.providers.global_player import SUPPORTED_FEATURES, GlobalPlayerProvider, setup
from music_assistant.providers.global_player.helpers import parse_stream_url
from tests.common import use_real_create_task

SAMPLE_BRANDS: list[dict[str, Any]] = [
    {
        "id": "2mwx3",
        "slug": "uk",
        "name": "Capital UK",
        "heraldId": 128,
        "tagline": "The UK's No.1 Hit Music Station",
        "brandLogo": "https://herald.musicradio.com/media/0d3d891d.png",
        "brandName": "Capital",
        "brandSlug": "capital",
    },
    {
        "id": "2mwx4",
        "slug": "uk",
        "name": "Heart UK",
        "heraldId": 129,
        "tagline": "Turn up the feel good!",
        "brandLogo": "https://herald.musicradio.com/media/heart.png",
        "brandName": "Heart",
        "brandSlug": "heart",
    },
]

SAMPLE_PLAYABLE: dict[str, Any] = {
    "id": "2mwx3",
    "contentType": "Station",
    "title": "Capital UK",
    "playback": [
        {
            "url": "https://hls.thisisdax.com/hls/CapitalUK-plus/master.m3u8",
            "canUse": "auth.license",
            "flags": ["format.hls", "auth.license"],
        },
        {
            "url": "https://media-ssl.musicradio.com/CapitalUK",
            "canUse": "true",
            "flags": ["format.icecast", "GlobalAdSupported", "live"],
        },
    ],
}


def _make_http_response_ctx(status: int = 200, json_data: Any = None) -> MagicMock:
    """Build an async context manager mock yielding an aiohttp response."""
    response = MagicMock()
    response.status = status
    response.json = AsyncMock(return_value=json_data)
    if status >= 400:
        response.raise_for_status.side_effect = aiohttp.ClientResponseError(
            MagicMock(), (), status=status
        )
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


@pytest.fixture
def provider() -> GlobalPlayerProvider:
    """Create a test GlobalPlayerProvider instance with mocked mass dependencies."""
    mass = MagicMock()
    mass.http_session = MagicMock()
    mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    mass.cache.set = AsyncMock()
    use_real_create_task(mass)

    manifest = MagicMock()
    manifest.domain = "global_player"
    manifest.name = "Global Player"

    config = MagicMock()
    config.instance_id = "global_player_test"
    # the base Provider class reads the log-level config entry on init;
    # fall through to its own default rather than hardcoding a value here
    config.get_value = lambda _key, default=None: default

    return GlobalPlayerProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def test_setup() -> None:
    """Test setup function initializes provider instance."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "global_player"
    config = MagicMock()
    config.instance_id = "global_player_test"
    config.get_value = lambda _key, default=None: default

    instance = await setup(mass, manifest, config)
    assert isinstance(instance, GlobalPlayerProvider)
    assert ProviderFeature.BROWSE in instance.supported_features
    assert ProviderFeature.SEARCH in instance.supported_features


def test_provider_properties(provider: GlobalPlayerProvider) -> None:
    """Test provider properties."""
    assert provider.is_streaming_provider is True
    assert provider.max_concurrent_streams is None


async def test_browse(provider: GlobalPlayerProvider) -> None:
    """Test browsing all radio stations."""
    cast("MagicMock", provider.mass.http_session.get).return_value = _make_http_response_ctx(
        status=200, json_data=SAMPLE_BRANDS
    )

    items = await provider.browse("root")
    assert len(items) == 2

    capital = items[0]
    assert isinstance(capital, Radio)
    assert capital.item_id == "2mwx3"
    assert capital.name == "Capital UK"
    assert capital.metadata.description == "The UK's No.1 Hit Music Station"
    images = capital.metadata.images
    assert images is not None
    assert len(images) == 1
    assert images[0].path == "https://herald.musicradio.com/media/0d3d891d.png"
    assert images[0].type == ImageType.THUMB


async def test_get_radio(provider: GlobalPlayerProvider) -> None:
    """Test get_radio for a valid and invalid station."""
    cast("MagicMock", provider.mass.http_session.get).return_value = _make_http_response_ctx(
        status=200, json_data=SAMPLE_BRANDS
    )

    radio = await provider.get_radio("2mwx4")
    assert radio.item_id == "2mwx4"
    assert radio.name == "Heart UK"

    with pytest.raises(MediaNotFoundError):
        await provider.get_radio("nonexistent_id")


async def test_search(provider: GlobalPlayerProvider) -> None:
    """Test search functionality across names and taglines."""
    cast("MagicMock", provider.mass.http_session.get).return_value = _make_http_response_ctx(
        status=200, json_data=SAMPLE_BRANDS
    )

    # Search for Heart
    res = await provider.search("Heart", [MediaType.RADIO])
    assert len(res.radio) == 1
    assert res.radio[0].name == "Heart UK"

    # Search by tagline
    res = await provider.search("Hit Music", [MediaType.RADIO])
    assert len(res.radio) == 1
    assert res.radio[0].name == "Capital UK"

    # Search with limit
    res = await provider.search("UK", [MediaType.RADIO], limit=1)
    assert len(res.radio) == 1

    # Search for non-matching query
    res = await provider.search("NonExistentStation", [MediaType.RADIO])
    assert len(res.radio) == 0

    # Search ignoring non-radio media types
    res = await provider.search("Capital", [MediaType.TRACK])
    assert len(res.radio) == 0


async def test_search_ranks_name_matches_before_tagline_matches(
    provider: GlobalPlayerProvider,
) -> None:
    """Test name matches come before tagline matches and survive the limit."""
    brands = [
        {
            "id": "heartdance",
            "name": "Heart Dance",
            "tagline": "Non-Stop Club Classics",
            "brandName": "Heart",
        },
        {
            "id": "classicfm",
            "name": "Classic FM",
            "tagline": "The World's Greatest Music",
            "brandName": "Classic FM",
        },
    ]
    cast("MagicMock", provider.mass.http_session.get).return_value = _make_http_response_ctx(
        status=200, json_data=brands
    )

    res = await provider.search("classic", [MediaType.RADIO])
    assert [radio.name for radio in res.radio] == ["Classic FM", "Heart Dance"]

    res = await provider.search("classic", [MediaType.RADIO], limit=1)
    assert [radio.name for radio in res.radio] == ["Classic FM"]


async def test_get_stream_details(provider: GlobalPlayerProvider) -> None:
    """Test resolving stream details for a radio station."""
    cast("MagicMock", provider.mass.http_session.get).return_value = _make_http_response_ctx(
        status=200, json_data=SAMPLE_PLAYABLE
    )

    stream_details = await provider.get_stream_details("2mwx3", MediaType.RADIO)
    assert stream_details.item_id == "2mwx3"
    assert stream_details.provider == "global_player_test"
    assert stream_details.media_type == MediaType.RADIO
    assert stream_details.stream_type == StreamType.HTTP
    assert stream_details.audio_format.content_type == ContentType.UNKNOWN
    assert stream_details.path == "https://media-ssl.musicradio.com/CapitalUK"
    assert stream_details.can_seek is False
    assert stream_details.allow_seek is False


async def test_get_stations_api_failure(provider: GlobalPlayerProvider) -> None:
    """Test get_stations handles API failure with ProviderUnavailableError."""
    cast("MagicMock", provider.mass.http_session.get).return_value = _make_http_response_ctx(
        status=500
    )
    with pytest.raises(ProviderUnavailableError):
        await provider.browse("root")


async def test_get_stations_transport_failure(provider: GlobalPlayerProvider) -> None:
    """Test get_stations handles transport and connection errors with ProviderUnavailableError."""
    cast("MagicMock", provider.mass.http_session.get).side_effect = aiohttp.ClientError(
        "Connection refused"
    )
    with pytest.raises(ProviderUnavailableError):
        await provider.browse("root")


async def test_get_playable_api_failure(provider: GlobalPlayerProvider) -> None:
    """Test _get_playable handles API failure with ProviderUnavailableError."""
    cast("MagicMock", provider.mass.http_session.get).return_value = _make_http_response_ctx(
        status=500
    )
    with pytest.raises(ProviderUnavailableError):
        await provider.get_stream_details("2mwx3", MediaType.RADIO)


async def test_get_playable_not_found(provider: GlobalPlayerProvider) -> None:
    """Test _get_playable maps a missing station to MediaNotFoundError."""
    cast("MagicMock", provider.mass.http_session.get).return_value = _make_http_response_ctx(
        status=404
    )
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details("missing", MediaType.RADIO)


async def test_get_playable_transport_failure(provider: GlobalPlayerProvider) -> None:
    """Test _get_playable handles transport errors with ProviderUnavailableError."""
    cast("MagicMock", provider.mass.http_session.get).side_effect = aiohttp.ClientError(
        "Network unreachable"
    )
    with pytest.raises(ProviderUnavailableError):
        await provider.get_stream_details("2mwx3", MediaType.RADIO)


def test_parse_stream_url_cannot_use() -> None:
    """Test parse_stream_url raises UnplayableMediaError when canUse is not true."""
    playable = {
        "id": "test_id",
        "playback": [
            {
                "url": "https://licensed.example.com",
                "canUse": "false",
                "flags": ["auth.license"],
            },
            {
                "url": "https://fallback.example.com/stream",
                "canUse": "false",
                "flags": ["format.icecast"],
            },
        ],
    }
    with pytest.raises(UnplayableMediaError, match="test_id"):
        parse_stream_url(playable, "test_id")


def test_parse_stream_url_not_found() -> None:
    """Test parse_stream_url raises UnplayableMediaError without a playable stream."""
    with pytest.raises(UnplayableMediaError, match="empty"):
        parse_stream_url({"playback": []}, "empty")
