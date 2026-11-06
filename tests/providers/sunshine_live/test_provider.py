"""Tests for the Sunshine Live music provider."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from types import TracebackType
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import (
    MediaNotFoundError,
    ProviderUnavailableError,
    UnplayableMediaError,
)
from music_assistant_models.media_items import Radio

from music_assistant.providers.sunshine_live import SUPPORTED_FEATURES
from music_assistant.providers.sunshine_live.constants import CHANNELS_URL, FALLBACK_IMAGE_URL
from music_assistant.providers.sunshine_live.helpers import parse_channels
from music_assistant.providers.sunshine_live.provider import SunshineLiveProvider

LOGO = "https://images.sunshine-live.de/files/2023-05/classics.jpg?width=360&height=360"


def _entry(station_id: int, name: str, url: str, logo: str = LOGO) -> dict[str, Any]:
    """Return one entry of the answer of the station."""
    return {"stream": name, "station_id": station_id, "url_high": url, "stream_logo": logo}


# the entries hold what the station really answers, with all the quirks of its data
ANSWER: dict[str, Any] = {
    "1": _entry(3, "SUNSHINE LIVE", "//stream.sunshine-live.de/live/mp3-192/homepage/"),
    "2": _entry(87, "SUNSHINE LIVE CH", "//stream.sunshine-live.de/schweiz/mp3-192/homepage"),
    "3": _entry(6, "CLASSICS", "//stream.sunshine-live.de/classics/mp3-192/homepage/"),
    "4": _entry(5, "EDM", "//stream.sunshine-live.de/edm/mp3-192/homepage/"),
    "5": _entry(101, "BOUNCE", "http://stream.sunshine-live.de/bounce/mp3-192/homepage/"),
    "6": _entry(
        97,
        "CALM FLOW BY KARMALOFT",
        " http://stream.sunshine-live.de/calmflow/mp3-192/homepage",
    ),
    "7": _entry(83, "MELODIC BEATS", "//stream.sunshine-live.de/MelodicB/mp3-192/homepage/"),
    "8": _entry(92, "ÉLECTRIQUE CAFÉ", "//stream.sunshine-live.de/electriquecafe/mp3-192/homepage"),
    "9": _entry(11, "90s", "//stream.sunshine-live.de/90er/mp3-192/homepage/"),
    "11": _entry(48, "WOMEN OF TECHNO", "//stream.sunshine-live.de/technoqueens/mp3-192/homepage/"),
    "10": _entry(96, "", "//stream.sunshine-live.de/xmasbeats/mp3-192/homepage", logo=""),
}


class _FakeResponse:
    """Stand-in for the aiohttp response of the list of channels."""

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


def _provider(answer: Any = ANSWER) -> SunshineLiveProvider:
    """Return a provider wired to mocks, whose station gives the answer."""
    manifest = MagicMock()
    manifest.domain = "sunshine_live"
    manifest.name = "Sunshine Live"

    config = MagicMock()
    config.instance_id = "sunshine_live_test"
    config.values = {}

    def _get_value(key: str, default: Any = None) -> Any:
        return "GLOBAL" if key == "log_level" else default

    config.get_value.side_effect = _get_value
    mass = MagicMock()
    mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    mass.cache.set = AsyncMock()
    mass.http_session.get = MagicMock(return_value=_FakeResponse(answer))

    def _create_task(coro: Coroutine[Any, Any, Any], **_kwargs: Any) -> asyncio.Task[Any]:
        return asyncio.create_task(coro)

    mass.create_task = _create_task
    return SunshineLiveProvider(mass, manifest, config, SUPPORTED_FEATURES)


@pytest.fixture
def provider() -> SunshineLiveProvider:
    """Return a provider on the recorded answer of the station."""
    return _provider()


@pytest.mark.parametrize(
    ("channel_id", "name"),
    [
        ("3", "Sunshine Live"),
        ("87", "Sunshine Live CH"),
        ("6", "Sunshine Live - Classics"),
        ("5", "Sunshine Live - EDM"),
        ("97", "Sunshine Live - Calm Flow by Karmaloft"),
        ("92", "Sunshine Live - Électrique Café"),
        ("11", "Sunshine Live - 90s"),
        ("48", "Sunshine Live - Women of Techno"),
        ("96", "Sunshine Live - Xmasbeats"),
    ],
)
def test_channel_names_are_readable(channel_id: str, name: str) -> None:
    """The capitals of the station become a normal name, an empty one comes from the stream."""
    assert parse_channels(ANSWER)[channel_id].name == name


@pytest.mark.parametrize(
    ("channel_id", "url"),
    [
        ("6", "https://stream.sunshine-live.de/classics/mp3-192/homepage/"),
        ("101", "https://stream.sunshine-live.de/bounce/mp3-192/homepage/"),
        ("97", "https://stream.sunshine-live.de/calmflow/mp3-192/homepage"),
        ("83", "https://stream.sunshine-live.de/MelodicB/mp3-192/homepage/"),
    ],
    ids=["no-scheme", "http", "leading-space", "case-sensitive"],
)
def test_stream_urls_are_cleaned_up(channel_id: str, url: str) -> None:
    """Every stream ends up as a full https url, with its path untouched."""
    assert parse_channels(ANSWER)[channel_id].stream_url == url


def test_a_channel_has_the_format_of_its_stream() -> None:
    """The format of a stream is in its url."""
    channel = parse_channels(ANSWER)["6"]

    assert channel.content_type == ContentType.MP3
    assert channel.bit_rate == 192


def test_a_stream_without_a_known_format_still_plays() -> None:
    """An unknown format is left for the stream handling to find out."""
    answer = {"1": _entry(1, "NEW", "//stream.sunshine-live.de/new/opus/homepage/")}
    channel = parse_channels(answer)["1"]

    assert channel.content_type == ContentType.UNKNOWN
    assert channel.bit_rate is None


def test_the_lower_quality_stream_is_used_when_it_is_the_only_one() -> None:
    """A channel without a high quality stream stays available."""
    answer = {"1": {"stream": "NEW", "station_id": 1, "url_low": "//stream.example/new/mp3-128/"}}

    assert parse_channels(answer)["1"].bit_rate == 128


def test_a_channel_image_comes_from_the_station_with_a_fallback() -> None:
    """A new channel without a picture gets the picture of the station."""
    channels = parse_channels(ANSWER)

    assert channels["6"].image == LOGO
    assert channels["96"].image == FALLBACK_IMAGE_URL


@pytest.mark.parametrize(
    "entry",
    [
        {"stream": "NO ID", "url_high": "//stream.sunshine-live.de/a/mp3-192/"},
        {"stream": "NO STREAM", "station_id": 1},
        {"stream": "BAD STREAM", "station_id": 1, "url_high": "not a url"},
        "not a channel",
    ],
    ids=["no-id", "no-stream", "bad-stream", "no-mapping"],
)
def test_an_unusable_entry_is_left_out(entry: Any) -> None:
    """One broken entry doesn't cost the other channels."""
    channels = parse_channels({"1": entry, "2": ANSWER["3"]})

    assert list(channels) == ["6"]


@pytest.mark.parametrize("answer", [None, [], "error", {}])
def test_an_answer_without_channels_gives_no_channels(answer: Any) -> None:
    """An unexpected answer is not an error here, the provider decides what it means."""
    assert parse_channels(answer) == {}


async def test_browse_follows_the_list_of_the_station(provider: SunshineLiveProvider) -> None:
    """Every channel shows with its picture, in the order of the station."""
    items = await provider.browse("sunshine_live://")

    assert [item.item_id for item in items] == [str(x["station_id"]) for x in ANSWER.values()]
    assert items[0].name == "Sunshine Live"
    assert all(isinstance(item, Radio) and item.metadata.images for item in items)


async def test_a_new_channel_of_the_station_shows_up() -> None:
    """A channel that nobody listed in the code is there, this is why the list is dynamic."""
    answer = {"1": _entry(200, "BRAND NEW", "//stream.sunshine-live.de/new/mp3-192/")}

    radio = await _provider(answer).get_radio("200")

    assert radio.name == "Sunshine Live - Brand New"


async def test_get_radio_returns_the_channel(provider: SunshineLiveProvider) -> None:
    """A known channel has its own name, picture and mapping on this instance."""
    radio = await provider.get_radio("6")

    assert radio.name == "Sunshine Live - Classics"
    assert radio.provider == "sunshine_live_test"
    assert radio.metadata.images
    assert radio.metadata.images[0].path == LOGO
    assert radio.metadata.images[0].remotely_accessible
    mapping = next(iter(radio.provider_mappings))
    assert mapping.item_id == "6"
    assert mapping.provider_domain == "sunshine_live"
    assert mapping.provider_instance == "sunshine_live_test"


async def test_get_radio_refuses_an_unknown_channel(provider: SunshineLiveProvider) -> None:
    """A channel that left the list is reported as not found."""
    with pytest.raises(MediaNotFoundError):
        await provider.get_radio("unknown")


async def test_search_finds_a_channel_by_part_of_its_name(
    provider: SunshineLiveProvider,
) -> None:
    """Case and spaces around the search text do not matter."""
    results = await provider.search("  CLASSICS ", [MediaType.RADIO])

    assert [radio.item_id for radio in results.radio] == ["6"]


async def test_search_keeps_to_the_limit(provider: SunshineLiveProvider) -> None:
    """All channels hold the station name, so the limit has to apply."""
    results = await provider.search("sunshine", [MediaType.RADIO], limit=3)

    assert [radio.item_id for radio in results.radio] == ["3", "87", "6"]


@pytest.mark.parametrize(
    ("query", "media_types"),
    [("classics", [MediaType.TRACK]), ("   ", [MediaType.RADIO]), ("nothing", [MediaType.RADIO])],
    ids=["other-media-type", "empty-query", "no-match"],
)
async def test_search_without_a_result(
    provider: SunshineLiveProvider, query: str, media_types: list[MediaType]
) -> None:
    """A search that cannot match returns nothing."""
    results = await provider.search(query, media_types)

    assert list(results.radio) == []


async def test_stream_details_use_the_stream_of_the_channel(
    provider: SunshineLiveProvider,
) -> None:
    """The stream comes from the list of the station, cleaned up."""
    details = await provider.get_stream_details("101", MediaType.RADIO)

    assert details.path == "https://stream.sunshine-live.de/bounce/mp3-192/homepage/"
    assert details.provider == "sunshine_live_test"
    assert details.media_type == MediaType.RADIO
    assert details.stream_type == StreamType.HTTP
    assert details.audio_format.content_type == ContentType.MP3
    assert details.audio_format.bit_rate == 192
    assert details.allow_seek is False
    assert details.can_seek is False


async def test_stream_details_refuse_other_requests(provider: SunshineLiveProvider) -> None:
    """Only the known channels can play, and only as radio."""
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details("unknown", MediaType.RADIO)
    with pytest.raises(UnplayableMediaError):
        await provider.get_stream_details("6", MediaType.TRACK)


async def test_the_list_is_asked_from_the_station_and_kept(
    provider: SunshineLiveProvider,
) -> None:
    """The station gets the request once, the answer goes in the cache for later calls."""
    await provider.browse("sunshine_live://")

    session: Any = provider.mass.http_session
    assert session.get.call_args.args == (CHANNELS_URL,)
    cache: Any = provider.mass.cache
    assert cache.get_with_freshness.await_args.kwargs["include_expired"] is True


async def test_a_cached_list_saves_the_request() -> None:
    """A fresh list in the cache is used without asking the station."""
    provider = _provider()
    cache: Any = provider.mass.cache
    cache.get_with_freshness = AsyncMock(return_value=(ANSWER, True, True))

    channels = await provider.browse("sunshine_live://")

    assert len(channels) == len(ANSWER)
    session: Any = provider.mass.http_session
    session.get.assert_not_called()


async def test_an_expired_list_is_used_while_the_station_is_down() -> None:
    """A station that can't be reached doesn't empty the provider."""
    provider = _provider()
    cache: Any = provider.mass.cache
    cache.get_with_freshness = AsyncMock(return_value=(ANSWER, False, True))
    session: Any = provider.mass.http_session
    session.get = MagicMock(side_effect=aiohttp.ClientError())

    channels = await provider.browse("sunshine_live://")
    await asyncio.sleep(0)

    assert len(channels) == len(ANSWER)


@pytest.mark.parametrize(
    "error", [aiohttp.ClientError(), TimeoutError(), ValueError()], ids=["client", "time", "json"]
)
async def test_an_unreachable_station_is_reported(error: Exception) -> None:
    """Without a list in the cache a failed request makes the provider unavailable."""
    provider = _provider()
    session: Any = provider.mass.http_session
    session.get = MagicMock(side_effect=error)

    with pytest.raises(ProviderUnavailableError):
        await provider.browse("sunshine_live://")


async def test_an_empty_list_is_reported() -> None:
    """A station without channels is a problem, not an empty provider."""
    with pytest.raises(ProviderUnavailableError):
        await _provider({}).browse("sunshine_live://")
