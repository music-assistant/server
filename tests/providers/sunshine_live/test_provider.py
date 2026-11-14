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
from music_assistant_models.media_items import AudioFormat, Radio
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.controllers.streams.constants import (
    STREAMDETAILS_INBAND_TITLE_HANDOFF_KEY,
    STREAMDETAILS_INBAND_TITLE_KEY,
)
from music_assistant.helpers.throttle_retry import Throttler
from music_assistant.providers.sunshine_live import SUPPORTED_FEATURES
from music_assistant.providers.sunshine_live.constants import (
    API_RATE_LIMIT,
    API_RATE_PERIOD,
    CHANNELS_URL,
    FALLBACK_IMAGE_URL,
)
from music_assistant.providers.sunshine_live.helpers import is_same_song, parse_channels
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
    "12": _entry(104, "MOST WANTED", "//stream.sunshine-live.de/mostwanted/mp3-192/webstream"),
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
    mass.metadata.update_radio_stream_artwork = AsyncMock()
    mass.metadata.normalize_radio_artist_name = MagicMock(side_effect=lambda name: name)

    def _create_task(coro: Coroutine[Any, Any, Any], **_kwargs: Any) -> asyncio.Task[Any]:
        return asyncio.create_task(coro)

    mass.create_task = _create_task
    provider = SunshineLiveProvider(mass, manifest, config, SUPPORTED_FEATURES)
    provider._throttler = Throttler(rate_limit=1000, period=1)
    return provider


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
        ("6", "https://stream.sunshine-live.de/classics/mp3-192/"),
        ("101", "https://stream.sunshine-live.de/bounce/mp3-192/"),
        ("97", "https://stream.sunshine-live.de/calmflow/mp3-192/"),
        ("83", "https://stream.sunshine-live.de/MelodicB/mp3-192/"),
        ("87", "https://stream.sunshine-live.de/schweiz/mp3-192/"),
        ("104", "https://stream.sunshine-live.de/mostwanted/mp3-192/"),
    ],
    ids=["no-scheme", "http", "leading-space", "case-sensitive", "no-slash", "other-player"],
)
def test_stream_urls_are_cleaned_up(channel_id: str, url: str) -> None:
    """Every stream is a full https url of channel and format, without the name of a player."""
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

    assert details.path == "https://stream.sunshine-live.de/bounce/mp3-192/"
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


@pytest.mark.parametrize("answer", [{}, {"1": {"stream": "BROKEN"}}], ids=["empty", "unusable"])
async def test_a_bad_answer_does_not_replace_a_working_list(answer: dict[str, Any]) -> None:
    """A refresh that brings no channels keeps the old list, also for the next hour."""
    provider = _provider(answer)
    cache: Any = provider.mass.cache
    cache.get_with_freshness = AsyncMock(return_value=(ANSWER, False, True))

    channels = await provider.browse("sunshine_live://")
    for _ in range(3):
        await asyncio.sleep(0)

    assert len(channels) == len(ANSWER)
    cache.set.assert_not_awaited()


async def test_a_good_answer_is_kept() -> None:
    """A refresh that brings channels replaces the old list."""
    provider = _provider()
    cache: Any = provider.mass.cache
    cache.get_with_freshness = AsyncMock(return_value=(ANSWER, False, True))

    await provider.browse("sunshine_live://")
    for _ in range(3):
        await asyncio.sleep(0)

    cache.set.assert_awaited_once()


async def test_the_throttler_starts_with_the_provider(provider: SunshineLiveProvider) -> None:
    """The requests of a running provider share one limit."""
    await provider.handle_async_init()

    assert provider._throttler.rate_limit == API_RATE_LIMIT
    assert provider._throttler.period == API_RATE_PERIOD


async def test_a_request_to_the_station_is_throttled(provider: SunshineLiveProvider) -> None:
    """Failed requests aren't cached, so the throttler limits how often they come."""
    entered: list[bool] = []

    class _Throttler(Throttler):
        async def __aenter__(self) -> float:
            entered.append(True)
            return await super().__aenter__()

    provider._throttler = _Throttler(rate_limit=1000, period=1)

    await provider.browse("sunshine_live://")

    assert entered == [True]


COVER = "https://is1-ssl.mzstatic.com/image/cover.jpg"


def _details(stream_title: str | None = None, channel_id: str = "6") -> StreamDetails:
    """Return stream details of a playing channel, whose stream gave the title."""
    data: dict[str, Any] = {STREAMDETAILS_INBAND_TITLE_HANDOFF_KEY: True}
    if stream_title is not None:
        data[STREAMDETAILS_INBAND_TITLE_KEY] = stream_title
    return StreamDetails(
        provider="sunshine_live_test",
        item_id=channel_id,
        media_type=MediaType.RADIO,
        audio_format=AudioFormat(content_type=ContentType.MP3),
        data=data,
    )


def _plays(artist: str, title: str, cover: str = "") -> dict[str, Any]:
    """Return the answer of the station, whose channel 6 plays the given song."""
    entry = _entry(6, "CLASSICS", "//stream.sunshine-live.de/classics/mp3-192/homepage/")
    entry |= {"artist_name": artist, "song_title": title}
    entry["covers"] = {"cover_art_url_xxl": cover}
    return {"1": entry}


async def test_stream_details_take_over_the_title_of_the_stream(
    provider: SunshineLiveProvider,
) -> None:
    """The provider decides what shows, so the placeholder of the stream never does."""
    details = await provider.get_stream_details("6", MediaType.RADIO)

    assert details.stream_metadata_update_callback == provider._update_stream_metadata
    assert details.data[STREAMDETAILS_INBAND_TITLE_HANDOFF_KEY] is True


async def test_a_song_shows_the_title_of_the_stream() -> None:
    """The stream follows the audio, so it wins over the list of the station."""
    provider = _provider(_plays("Other", "Song"))
    details = _details("Bingo Boys - How to Dance")

    await provider._update_stream_metadata(details, 0)

    assert details.stream_metadata is not None
    assert details.stream_metadata.artist == "Bingo Boys"
    assert details.stream_metadata.title == "How to Dance"
    assert details.stream_metadata.image_url is None
    metadata: Any = provider.mass.metadata
    metadata.update_radio_stream_artwork.assert_awaited_once_with(details)


async def test_a_title_with_more_dashes_splits_at_the_first() -> None:
    """Like other radio stations, the artist ends at the first separator."""
    details = _details("Artist - Song - Part Two")

    await _provider(_plays("", ""))._update_stream_metadata(details, 0)

    assert details.stream_metadata is not None
    assert details.stream_metadata.artist == "Artist"
    assert details.stream_metadata.title == "Song - Part Two"


async def test_a_title_without_an_artist_shows_as_it_is() -> None:
    """A title that names no artist is not split."""
    details = _details("Just a title")

    await _provider(_plays("", ""))._update_stream_metadata(details, 0)

    assert details.stream_metadata is not None
    assert details.stream_metadata.artist is None
    assert details.stream_metadata.title == "Just a title"


async def test_a_song_uses_the_cover_of_the_station_when_it_is_the_same_song() -> None:
    """A matching cover needs no lookup, also with other capitals."""
    provider = _provider(_plays("Bingo Boys", "HOW TO DANCE", COVER))
    details = _details("Bingo Boys - How to Dance")

    await provider._update_stream_metadata(details, 0)

    assert details.stream_metadata is not None
    assert details.stream_metadata.image_url == COVER
    metadata: Any = provider.mass.metadata
    metadata.update_radio_stream_artwork.assert_not_awaited()


async def test_a_song_ignores_the_cover_of_another_song() -> None:
    """The list and the audio can be apart, this keeps a wrong cover away."""
    provider = _provider(_plays("Other", "Song", COVER))
    details = _details("Bingo Boys - How to Dance")

    await provider._update_stream_metadata(details, 0)

    assert details.stream_metadata is not None
    assert details.stream_metadata.image_url is None


async def test_a_cover_that_comes_later_is_added() -> None:
    """The song stays and needs no second lookup when the list catches up."""
    provider = _provider(_plays("Other", "Song"))
    details = _details("Bingo Boys - How to Dance")
    await provider._update_stream_metadata(details, 0)
    session: Any = provider.mass.http_session
    session.get = MagicMock(return_value=_FakeResponse(_plays("Bingo Boys", "How to Dance", COVER)))

    await provider._update_stream_metadata(details, 30)

    assert details.stream_metadata is not None
    assert details.stream_metadata.image_url == COVER
    metadata: Any = provider.mass.metadata
    metadata.update_radio_stream_artwork.assert_awaited_once()


async def test_the_same_song_is_not_looked_up_again() -> None:
    """A found artwork stays, and the lookup service isn't asked every time."""
    provider = _provider(_plays("Other", "Song"))
    details = _details("Bingo Boys - How to Dance")
    await provider._update_stream_metadata(details, 0)
    assert details.stream_metadata is not None
    details.stream_metadata.image_url = "https://found.example/art.jpg"

    await provider._update_stream_metadata(details, 30)

    assert details.stream_metadata.image_url == "https://found.example/art.jpg"
    metadata: Any = provider.mass.metadata
    metadata.update_radio_stream_artwork.assert_awaited_once()


@pytest.mark.parametrize(
    "stream_title",
    ["SUNSHINE LIVE - Simulcast", "sunshine live - Classics", "", None],
    ids=["placeholder", "lower-case", "empty", "none-yet"],
)
async def test_a_show_uses_the_list_of_the_station(stream_title: str | None) -> None:
    """During a show the stream only names the station, the list knows the show."""
    provider = _provider(_plays("DJ Falk", "DJs after Work - Club Legends"))
    details = _details(stream_title)

    await provider._update_stream_metadata(details, 0)

    assert details.stream_metadata is not None
    assert details.stream_metadata.artist == "DJ Falk"
    assert details.stream_metadata.title == "DJs after Work - Club Legends"


async def test_a_show_uses_its_cover() -> None:
    """A show has no song the cover could differ from."""
    details = _details("SUNSHINE LIVE - Simulcast")

    await _provider(_plays("DJ Falk", "Show", COVER))._update_stream_metadata(details, 0)

    assert details.stream_metadata is not None
    assert details.stream_metadata.image_url == COVER


async def test_nothing_known_shows_the_channel() -> None:
    """Without any information no made up title shows, the player falls back to the channel."""
    details = _details("SUNSHINE LIVE - Simulcast")

    await _provider(_plays("", ""))._update_stream_metadata(details, 0)

    assert details.stream_metadata is None


async def test_a_song_after_a_show_is_shown_again() -> None:
    """The same song can come back after a show."""
    provider = _provider(_plays("DJ Falk", "Show"))
    details = _details("Bingo Boys - How to Dance")
    await provider._update_stream_metadata(details, 0)
    details.data[STREAMDETAILS_INBAND_TITLE_KEY] = "SUNSHINE LIVE - Simulcast"
    await provider._update_stream_metadata(details, 30)
    details.data[STREAMDETAILS_INBAND_TITLE_KEY] = "Bingo Boys - How to Dance"

    await provider._update_stream_metadata(details, 60)

    assert details.stream_metadata is not None
    assert details.stream_metadata.artist == "Bingo Boys"


async def test_a_failed_request_keeps_what_shows() -> None:
    """A failed request says nothing about what plays."""
    provider = _provider()
    session: Any = provider.mass.http_session
    session.get = MagicMock(side_effect=aiohttp.ClientError())
    details = _details("SUNSHINE LIVE - Simulcast")
    details.stream_metadata = StreamMetadata(title="Show", artist="DJ Falk")

    await provider._update_stream_metadata(details, 0)

    assert details.stream_metadata.title == "Show"


def test_covers_in_another_format_are_ignored() -> None:
    """Some channels answer their covers in another format than the others."""
    entry = _entry(6, "CLASSICS", "//stream.sunshine-live.de/classics/mp3-192/") | {"covers": ""}

    assert parse_channels({"1": entry})["6"].cover is None


@pytest.mark.parametrize(
    ("stream_title", "artist", "title"),
    [
        (
            "Timmo, Stoked - Back to the Oldschool (Extended Mix)",
            "Timmo & Stoked",
            "Back to the Oldschool",
        ),
        ("Maite Dedecker - Cosmic Pulse (Extended Mix)", "Maite Dedecker", "Cosmic Pulse"),
        ("Pan-Pot - Kaltstrom  (Extended Mix)", "Pan-Pot", "Kaltstrom"),
        (
            "Double Vision - Knockin (Original Version)",
            "Double Vision",
            "Knockin (Original Version)",
        ),
    ],
    ids=["other-artist-separator", "version-only-in-stream", "double-space", "same"],
)
def test_the_same_song_is_found_in_both_spellings(
    stream_title: str, artist: str, title: str
) -> None:
    """These pairs were seen on the stream and in the list of the station at the same time."""
    answer = {"1": _entry(6, "X", "//stream.sunshine-live.de/x/mp3-192/")}
    answer["1"] |= {"artist_name": artist, "song_title": title}

    assert is_same_song(stream_title, parse_channels(answer)["6"])


@pytest.mark.parametrize(
    ("stream_title", "artist", "title"),
    [
        ("Pan-Pot - Kaltstrom", "Pan-Pot", "Other Song"),
        ("Pan-Pot - Kaltstrom", "Other Artist", "Kaltstrom"),
        ("Kaltstrom", "Pan-Pot", "Kaltstrom"),
        ("Pan-Pot - Kaltstrom", "", ""),
    ],
    ids=["other-title", "other-artist", "no-artist", "nothing-listed"],
)
def test_another_song_is_not_the_same(stream_title: str, artist: str, title: str) -> None:
    """A cover of another song must not show."""
    answer = {"1": _entry(6, "X", "//stream.sunshine-live.de/x/mp3-192/")}
    answer["1"] |= {"artist_name": artist, "song_title": title}

    assert not is_same_song(stream_title, parse_channels(answer)["6"])
