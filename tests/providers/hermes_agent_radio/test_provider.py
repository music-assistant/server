"""
Tests for the Hermes Agent Radio music provider.

All hermetic: the station's JSON feeds are canned, no network I/O happens.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self, cast
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from music_assistant_models.enums import ContentType, MediaType, ProviderFeature, StreamType
from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

if TYPE_CHECKING:
    from music_assistant_models.media_items import Radio

from music_assistant.providers.hermes_agent_radio import (
    NOW_PLAYING_ENDPOINT,
    SUPPORTED_FEATURES,
    HermesAgentRadio,
)

STATIONS_ANSWER: dict[str, Any] = {
    "stations": [
        {
            "id": "main",
            "name": "Hermes Agent Radio",
            "tagline": "Everything we play, one stream",
            "stream": "/stream",
            "songs": 60,
            "plays_main": False,
        },
        {
            "id": "hiphop",
            "name": "Hermes Agent Radio Hip-Hop",
            "tagline": "Bars, beats and agents with something to say",
            "stream": "/stream/hiphop",
            "songs": 22,
            "plays_main": False,
        },
        {
            "id": "techno",
            "name": "Hermes Agent Radio Techno",
            "tagline": "Machines, bass and no brakes",
            "stream": "/stream/techno",
            "songs": 8,
            "plays_main": False,
        },
    ]
}

NOW_PLAYING_ANSWER: dict[str, Any] = {
    "now": {
        "kind": "music",
        "item_id": "ddace1f796ba45d3",
        "title": "Queue Jump Blues",
        "artist": "Night Shift Cipher",
        "duration_s": 195.0,
    },
    "up_next": [{"kind": "track", "title": "Barn Light Boogie", "artist": "Glitter Protocol"}],
}


class _FakeResponse:
    """Stand-in for the aiohttp response of one of the station feeds."""

    def __init__(self, data: Any) -> None:
        self._data = data

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        return None

    def raise_for_status(self) -> None:
        """Accept the response as a successful one."""

    async def json(self, content_type: str | None = None) -> Any:
        return self._data


def _provider(
    stations: Any = STATIONS_ANSWER,
    now_playing: Any = NOW_PLAYING_ANSWER,
    stations_error: Exception | None = None,
    now_playing_error: Exception | None = None,
) -> HermesAgentRadio:
    """Return a provider wired to mocks serving the canned feed answers."""

    def _get(url: str, **_kwargs: Any) -> _FakeResponse:
        if NOW_PLAYING_ENDPOINT in url:
            if now_playing_error:
                raise now_playing_error
            return _FakeResponse(now_playing)
        if stations_error:
            raise stations_error
        return _FakeResponse(stations)

    manifest = MagicMock()
    manifest.domain = "hermes_agent_radio"
    manifest.name = "Hermes Agent Radio"

    config = MagicMock()
    config.instance_id = "hermes_agent_radio_test"
    config.values = {}

    def _get_value(key: str, default: Any = None) -> Any:
        return "GLOBAL" if key == "log_level" else default

    config.get_value.side_effect = _get_value
    mass = MagicMock()
    mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    mass.cache.set = AsyncMock()
    mass.http_session.get = MagicMock(side_effect=_get)

    def _create_task(coro: Coroutine[Any, Any, Any], **_kwargs: Any) -> asyncio.Task[Any]:
        return asyncio.create_task(coro)

    mass.create_task = _create_task
    return HermesAgentRadio(mass, manifest, config, SUPPORTED_FEATURES)


@pytest.fixture(name="provider")
def provider_fixture() -> HermesAgentRadio:
    """Return a provider serving the canned station feeds."""
    return _provider()


async def test_features_and_streaming_flag(provider: HermesAgentRadio) -> None:
    """The provider declares browse/search and never caches audio."""
    assert ProviderFeature.BROWSE in provider.supported_features
    assert ProviderFeature.SEARCH in provider.supported_features
    assert provider.is_streaming_provider


async def test_browse_lists_the_station_lineup(provider: HermesAgentRadio) -> None:
    """Browse returns every station with its static description."""
    radios = list(await provider.browse("hermes_agent_radio://"))
    assert [r.item_id for r in radios] == ["main", "hiphop", "techno"]
    first = cast("Radio", next(r for r in radios if r.item_id == "main"))
    assert first.name == "Hermes Agent Radio"
    assert first.metadata.description == (
        "Everything we play, one stream\n60 community songs in rotation"
    )


async def test_browse_touches_only_the_stations_feed(provider: HermesAgentRadio) -> None:
    """The parser is static: one feed call per listing, no now-playing fetch."""
    await provider.browse("hermes_agent_radio://")
    session: Any = provider.mass.http_session
    calls = [call.args[0] for call in session.get.call_args_list]
    assert len(calls) == 1
    assert calls[0].endswith("stations.json")


async def test_browse_rejects_a_subpath(provider: HermesAgentRadio) -> None:
    """Unknown browse subpaths raise instead of returning wrong content."""
    with pytest.raises(KeyError):
        await provider.browse("hermes_agent_radio://nope")


async def test_get_radio_returns_the_station(provider: HermesAgentRadio) -> None:
    """A known id resolves to its radio item."""
    radio = await provider.get_radio("techno")
    assert radio.name == "Hermes Agent Radio Techno"
    assert radio.media_type == MediaType.RADIO


async def test_get_radio_refuses_an_unknown_id(provider: HermesAgentRadio) -> None:
    """An id the station does not list raises MediaNotFound."""
    with pytest.raises(MediaNotFoundError):
        await provider.get_radio("jazz")


async def test_search_matches_name_and_tagline(provider: HermesAgentRadio) -> None:
    """Search hits both the station name and its tagline text."""
    by_name = await provider.search("hip", [MediaType.RADIO])
    assert [r.item_id for r in by_name.radio] == ["hiphop"]
    by_tagline = await provider.search("no brakes", [MediaType.RADIO])
    assert [r.item_id for r in by_tagline.radio] == ["techno"]


async def test_search_keeps_to_requested_types_and_limit(provider: HermesAgentRadio) -> None:
    """Non-radio searches come back empty; the limit is honoured."""
    assert (await provider.search("hermes", [MediaType.ARTIST])).radio == []
    three = _provider(
        stations={
            "stations": [
                {"id": f"s{i}", "name": "Hermes", "stream": f"/stream/s{i}"} for i in range(9)
            ]
        }
    )
    results = await three.search("hermes", [MediaType.RADIO], limit=4)
    assert len(results.radio) == 4


async def test_stream_details_use_the_station_mounts(provider: HermesAgentRadio) -> None:
    """Each station plays its own mount, as a realtime MP3 stream."""
    main = await provider.get_stream_details("main", MediaType.RADIO)
    assert main.path == "https://hermesagentradio.com/stream"
    assert main.stream_type == StreamType.HTTP
    assert main.audio_format.content_type == ContentType.MP3
    assert main.is_realtime
    techno = await provider.get_stream_details("techno", MediaType.RADIO)
    assert techno.path == "https://hermesagentradio.com/stream/techno"


async def test_live_metadata_is_attached_to_every_station(
    provider: HermesAgentRadio,
) -> None:
    """Each mount plays with its own live-metadata callback."""
    main = await provider.get_stream_details("main", MediaType.RADIO)
    assert main.stream_metadata_update_callback == provider._update_stream_metadata
    assert main.stream_metadata_update_interval == 60
    techno = await provider.get_stream_details("techno", MediaType.RADIO)
    assert techno.stream_metadata_update_callback == provider._update_stream_metadata


async def test_stream_details_refuse_an_unknown_id(provider: HermesAgentRadio) -> None:
    """A station that vanished from the feed is not playable."""
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details("jazz", MediaType.RADIO)


async def test_an_unreachable_feed_is_reported() -> None:
    """A connection error surfaces as ProviderUnavailable, not a raw exception."""
    provider = _provider(stations_error=aiohttp.ClientError("down"))
    with pytest.raises(ProviderUnavailableError):
        await provider.browse("hermes_agent_radio://")


async def test_an_empty_station_list_is_reported() -> None:
    """A feed answering with no stations is treated as unavailable."""
    provider = _provider(stations={"stations": []})
    with pytest.raises(ProviderUnavailableError):
        await provider.browse("hermes_agent_radio://")


async def test_metadata_callback_updates_from_the_now_playing_feed(
    provider: HermesAgentRadio,
) -> None:
    """The callback maps the feed's current item onto stream metadata."""
    streamdetails = await provider.get_stream_details("main", MediaType.RADIO)
    await provider._update_stream_metadata(streamdetails, 0)
    meta = streamdetails.stream_metadata
    assert meta is not None
    assert meta.title == "Queue Jump Blues"
    assert meta.artist == "Night Shift Cipher"
    assert meta.duration == 195
    assert meta.description == "Up next: Barn Light Boogie"


async def test_metadata_callback_asks_for_the_playing_mount(provider: HermesAgentRadio) -> None:
    """A genre station's metadata comes from its own mount, not the main mix."""
    streamdetails = await provider.get_stream_details("techno", MediaType.RADIO)
    await provider._update_stream_metadata(streamdetails, 0)
    session: Any = provider.mass.http_session
    now_calls = [
        call for call in session.get.call_args_list if NOW_PLAYING_ENDPOINT in call.args[0]
    ]
    assert now_calls[-1].kwargs["params"] == {"station": "techno"}


async def test_metadata_callback_keeps_last_known_on_failure() -> None:
    """A failing now-playing fetch must not blank the display or raise."""
    provider = _provider(now_playing_error=aiohttp.ClientError("down"))
    streamdetails: StreamDetails = await provider.get_stream_details("main", MediaType.RADIO)
    sentinel = StreamMetadata(title="previous item", artist="previous artist")
    streamdetails.stream_metadata = sentinel
    await provider._update_stream_metadata(streamdetails, 0)
    assert streamdetails.stream_metadata is sentinel
