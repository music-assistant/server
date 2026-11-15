"""Tests for the CBC Listen music provider."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from multidict import CIMultiDict
from music_assistant_models.enums import (
    ContentType,
    ImageType,
    LinkType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    MediaNotFoundError,
    ProviderUnavailableError,
    RetriesExhausted,
)
from music_assistant_models.media_items import BrowseFolder, ItemMapping, Podcast, Radio

from music_assistant.constants import (
    CONF_ENTRY_UNOFFICIAL_PROVIDER,
    DEFAULT_AUDIOBOOK_PODCAST_GENRE,
)
from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers.cbc_listen import SUPPORTED_FEATURES, setup
from music_assistant.providers.cbc_listen.constants import (
    API_HEADERS,
    API_RATE_LIMIT,
    API_RATE_PERIOD,
    API_TIMEOUT,
    CLIPS_URL,
    DEFAULT_IMAGE_URL,
    NETWORK_LOGOS,
    SHOW_URL,
    SHOWS_URL,
)
from music_assistant.providers.cbc_listen.helpers import (
    format_image_url,
    get_stream_metadata,
    search_channels,
    search_shows,
)
from music_assistant.providers.cbc_listen.models import (
    CBCClip,
    CBCNetwork,
    CBCProgram,
    CBCShow,
    CBCTrack,
)
from music_assistant.providers.cbc_listen.parsers import (
    parse_channels,
    parse_podcast,
    parse_podcast_episode,
    parse_radio,
)
from music_assistant.providers.cbc_listen.provider import CBCListenProvider
from tests.common import use_real_create_task

if TYPE_CHECKING:
    from types import TracebackType

DOMAIN = "cbc_listen"
INSTANCE_ID = "cbc_listen_test"
SLEEP = "music_assistant.helpers.throttle_retry.asyncio.sleep"

SAMPLE_NETWORKS: list[dict[str, Any]] = [
    {
        "networkID": 1,
        "liveTitle": "Radio One",
        "streams": [
            {
                "id": 41,
                "title": "Toronto",
                "fullTitle": "CBC Radio One:  Toronto",
                "province": "Ontario",
                "streamURL": "https://cbcradiolive.akamaized.net/hls/live/2041036/ES_R1ETR/master.m3u8",
                "callSign": "CBC_R1_TOR",
                "programGuideLocationKey": "toronto",
            },
            {
                "id": 99,
                "title": "Broken",
                "fullTitle": "CBC Radio One: Broken",
                "province": "Ontario",
                "streamURL": "",
                "callSign": "CBC_R1_BRK",
                "programGuideLocationKey": "broken",
            },
        ],
    },
    {
        "networkID": 2,
        "liveTitle": "CBC Music",
        "streams": [
            {
                "id": 37,
                "title": "Eastern",
                "fullTitle": "CBC Music: Eastern",
                "province": "Ontario",
                "streamURL": "https://cbcradiolive.akamaized.net/hls/live/2041057/ES_R2ETR/master.m3u8",
                "callSign": "CBC_R2_TOR",
                "programGuideLocationKey": "toronto",
            }
        ],
    },
]

SAMPLE_SHOWS: list[dict[str, Any]] = [
    {
        "id": 209,
        "title": "Front Burner",
        "description": "Daily news podcast from CBC.",
        "hosts": "Jayme Poisson",
        "image": "https://www.cbc.ca/radio/podcasts/images/frontburner.jpg",
        "webURL": "https://www.cbc.ca/listen/cbc-podcasts/209-front-burner",
        "originalPodcast": True,
        "isPodcast": True,
        "episodeCount": 200,
        "segmentCount": 0,
        "itunesCategoryDescription": "News",
    },
    {
        "id": 23,
        "title": "Ideas",
        "description": "Deep conversation on big topics.",
        "hosts": "Nahlah Ayed",
        "image": "https://images.radio-canada.ca/c_scale,w_${width},q_auto/v1/cbc-music/programs/${ratio}/ideas.png",
        "webURL": "https://www.cbc.ca/listen/live-radio/1-23-ideas",
        "originalPodcast": False,
        "isPodcast": False,
        "episodeCount": 100,
        "segmentCount": 0,
        "itunesCategoryDescription": None,
    },
    {
        "id": 39,
        "title": "Metro Morning",
        "description": "Toronto morning radio show featuring daily interviews.",
        "hosts": "",
        "image": "",
        "webURL": "https://www.cbc.ca/listen/live-radio/1-39-metro-morning",
        "originalPodcast": False,
        "isPodcast": False,
        "episodeCount": 0,
        "segmentCount": 50,
        "itunesCategoryDescription": "Society & Culture",
    },
    {
        "id": 500,
        "title": "Empty Show",
        "description": "Show with no episodes.",
        "hosts": "",
        "image": "",
        "webURL": "",
        "originalPodcast": True,
        "isPodcast": True,
        "episodeCount": 0,
        "segmentCount": 0,
        "itunesCategoryDescription": None,
    },
]

SAMPLE_CLIPS: dict[str, Any] = {
    "clipCount": 3,
    "clips": [
        {
            "clipID": 1002,
            "showID": 209,
            "title": "Newer Episode",
            "description": "Second episode.",
            "src": "https://podcast-a.akamaihd.net/mp3/podcasts/ep2.mp3",
            "duration": 1800,
            "releasedAt": 1700100000000,
            "imageURL": "https://www.cbc.ca/radio/podcasts/images/ep2.jpg",
            "webURL": "https://www.cbc.ca/listen/cbc-podcasts/209-front-burner/episode/1002",
            "contentTier": "free",
        },
        {
            "clipID": 1001,
            "showID": 209,
            "title": "Older Episode",
            "description": "First episode.",
            "src": "https://podcast-a.akamaihd.net/mp3/podcasts/ep1.mp3",
            "duration": 1500,
            "releasedAt": 1700000000000,
            "imageURL": "",
            "webURL": "https://www.cbc.ca/listen/cbc-podcasts/209-front-burner/episode/1001",
            "contentTier": "free",
        },
        {
            "clipID": 1003,
            "showID": 209,
            "title": "Locked Episode",
            "description": "Requires login.",
            "src": "https://podcast-a.akamaihd.net/mp3/podcasts/ep3.mp3",
            "duration": 1200,
            "releasedAt": 1700200000000,
            "imageURL": "",
            "webURL": "",
            "contentTier": "authenticated",
        },
    ],
}


class FakeResponse:
    """Mock aiohttp response for CBC Listen API requests."""

    def __init__(
        self,
        status: int = HTTPStatus.OK,
        data: Any = None,
        headers: dict[str, str] | None = None,
        raw_payload: Any = None,
    ) -> None:
        """Initialize the fake response."""
        self.status = status
        self.headers: CIMultiDict[str] = CIMultiDict(headers or {})
        payload = {"status": "success", "data": data} if raw_payload is None else raw_payload
        self._body = json.dumps(payload)

    async def __aenter__(self) -> Self:
        """Enter the response context."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit the response context."""

    async def json(self) -> Any:
        """Parse and return the JSON payload."""
        return json.loads(self._body)

    async def read(self) -> bytes:
        """Return the response body as raw bytes."""
        return self._body.encode()


def make_provider(*responses: FakeResponse | Exception) -> tuple[CBCListenProvider, MagicMock]:
    """Create a configured CBCListenProvider and mock HTTP getter."""
    mass = MagicMock()
    get = MagicMock(side_effect=list(responses))
    mass.http_session.get = get
    mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    mass.cache.set = AsyncMock()
    use_real_create_task(mass)

    manifest = MagicMock(domain=DOMAIN, name="CBC Listen")
    config = MagicMock(instance_id=INSTANCE_ID, get_value=lambda _key, default=None: default)

    provider = CBCListenProvider(mass, manifest, config, SUPPORTED_FEATURES)
    provider.throttler = ThrottlerManager(rate_limit=API_RATE_LIMIT, period=API_RATE_PERIOD)
    provider._playlog_dates = {}
    return provider, get


async def test_setup_and_properties() -> None:
    """Test provider setup, config entries, and basic properties."""
    provider, _ = make_provider()
    instance = await setup(provider.mass, provider.manifest, provider.config)
    assert isinstance(instance, CBCListenProvider)
    await instance.handle_async_init()
    assert isinstance(instance.throttler, ThrottlerManager)
    assert instance.max_concurrent_streams is None
    assert instance.supported_media_types == {MediaType.RADIO, MediaType.PODCAST}
    assert instance.supported_features == {ProviderFeature.BROWSE, ProviderFeature.SEARCH}
    assert await instance.get_config_entries() == (CONF_ENTRY_UNOFFICIAL_PROVIDER,)


def test_parsers() -> None:
    """Test model round-trips and media item parsers."""
    networks = [CBCNetwork.from_dict(item) for item in SAMPLE_NETWORKS]
    channels = parse_channels(networks)
    assert [c.station_id for c in channels] == ["41", "37"]
    assert channels[0].name == "CBC Radio One: Toronto"
    shows = [CBCShow.from_dict(item) for item in SAMPLE_SHOWS]
    assert CBCShow.from_dict(shows[0].to_dict()) == shows[0]

    radio = parse_radio(channels[0], INSTANCE_ID, DOMAIN)
    assert radio.item_id == "41"
    assert radio.name == "CBC Radio One: Toronto"
    assert radio.metadata.images
    assert radio.metadata.images[0].path == NETWORK_LOGOS[1]
    assert not radio.metadata.images[0].remotely_accessible

    podcast_23 = parse_podcast(shows[1], INSTANCE_ID, DOMAIN)
    assert podcast_23.publisher == "CBC"
    assert (
        podcast_23.metadata.description
        == "Hosted by Nahlah Ayed.\n\nDeep conversation on big topics."
    )
    assert podcast_23.metadata.genres == {DEFAULT_AUDIOBOOK_PODCAST_GENRE}
    assert podcast_23.metadata.images
    assert podcast_23.metadata.images[0].type == ImageType.THUMB
    assert {
        link.url for link in (podcast_23.metadata.links or set()) if link.type == LinkType.WEBSITE
    } == {"https://www.cbc.ca/listen/live-radio/1-23-ideas"}

    podcast_39 = parse_podcast(shows[2], INSTANCE_ID, DOMAIN)
    assert podcast_39.publisher == "CBC"
    assert (
        podcast_39.metadata.description == "Toronto morning radio show featuring daily interviews."
    )
    assert podcast_39.metadata.genres == {"Society & Culture"}
    assert podcast_39.metadata.images
    assert podcast_39.metadata.images[0].path == DEFAULT_IMAGE_URL

    clip = CBCClip.from_dict(SAMPLE_CLIPS["clips"][1])
    ep = parse_podcast_episode(clip, ItemMapping.from_item(podcast_23), 1, INSTANCE_ID, DOMAIN)
    assert ep.position == 1
    assert ep.duration == 1500
    assert ep.metadata.images
    assert ep.metadata.images[0].path == podcast_23.metadata.images[0].path

    assert CBCClip.from_dict({"clipID": 1, "showID": 1, "title": "Untitled"}).content_tier == ""


def test_helpers() -> None:
    """Test image formatting, search helpers, and live metadata builders."""
    assert format_image_url("") == ""
    assert (
        format_image_url("https://example.com/${width}/${ratio}/img.png")
        == "https://example.com/512/1x1/img.png"
    )

    channels = parse_channels([CBCNetwork.from_dict(item) for item in SAMPLE_NETWORKS])
    assert search_channels(channels, "", 5) == []
    assert [c.station_id for c in search_channels(channels, "tor", 1)] == ["41"]
    assert [c.station_id for c in search_channels(channels, "ontario r2", 5)] == ["37"]

    shows = [CBCShow.from_dict(item) for item in SAMPLE_SHOWS]
    assert search_shows(shows, "", 5) == []
    assert [s.id for s in search_shows(shows, "toronto", 5)] == [39]
    assert [s.id for s in search_shows(shows, "jayme", 5)] == [209]

    programs = [
        CBCProgram(
            show_id=1044,
            program_title="About Time",
            host_name="Tom Allen",
            program_image="https://example.com/${width}/${ratio}/about.png",
            epoch_start=1000,
            epoch_end=5000,
        )
    ]
    tracks = [
        CBCTrack(
            title="Pastoral Symphony",
            composers="Vaughan Williams",
            album="Symphonies",
            broadcasted_time=2000,
        )
    ]

    meta_track = get_stream_metadata(channels[1], programs, tracks, 3000)
    assert meta_track.title == "Pastoral Symphony"
    assert meta_track.artist == "Vaughan Williams"
    assert meta_track.album == "Symphonies"
    assert meta_track.image_url == "https://example.com/512/1x1/about.png"

    meta_prog = get_stream_metadata(channels[1], programs, [], 3000)
    assert meta_prog.title == "About Time"
    assert meta_prog.artist == "Tom Allen"

    meta_fallback = get_stream_metadata(channels[1], programs, tracks, 500)
    assert meta_fallback.title == "CBC Music: Eastern"
    assert meta_fallback.image_url == NETWORK_LOGOS[2]


async def test_browse_and_search() -> None:
    """Test browsing top-level folders, radios, podcasts, radio shows, and searching."""
    provider, _ = make_provider(
        FakeResponse(data=SAMPLE_NETWORKS),
        FakeResponse(data=SAMPLE_SHOWS),
        FakeResponse(data=SAMPLE_SHOWS),
        FakeResponse(data=SAMPLE_SHOWS),
        FakeResponse(data=SAMPLE_NETWORKS),
    )

    folders = await provider.browse(f"{INSTANCE_ID}://")
    assert [f.item_id for f in folders if isinstance(f, BrowseFolder)] == [
        "radios",
        "podcasts",
        "radio_shows",
    ]
    radios = await provider.browse(f"{INSTANCE_ID}://radios")
    assert [r.item_id for r in radios if isinstance(r, Radio)] == ["41", "37"]
    podcasts = await provider.browse(f"{INSTANCE_ID}://podcasts")
    assert [p.item_id for p in podcasts if isinstance(p, Podcast)] == ["209"]
    radio_shows = await provider.browse(f"{INSTANCE_ID}://radio_shows")
    assert [s.item_id for s in radio_shows if isinstance(s, Podcast)] == ["23", "39"]

    with pytest.raises(KeyError):
        await provider.browse(f"{INSTANCE_ID}://unknown")

    results = await provider.search("toronto", [MediaType.PODCAST, MediaType.RADIO], limit=5)
    assert [p.item_id for p in results.podcasts] == ["39"]
    assert [r.item_id for r in results.radio] == ["41"]


async def test_get_items_and_episodes() -> None:
    """Test fetching a single radio station, podcast, paginated episodes, and single episode."""
    page_1 = {"clipCount": 3, "clips": SAMPLE_CLIPS["clips"][:2]}
    page_2 = {"clipCount": 3, "clips": SAMPLE_CLIPS["clips"][2:]}
    provider, get = make_provider(
        FakeResponse(data=SAMPLE_NETWORKS),
        FakeResponse(data=SAMPLE_NETWORKS),
        FakeResponse(data=SAMPLE_SHOWS[0]),
        FakeResponse(data=SAMPLE_SHOWS[3]),
        FakeResponse(data=SAMPLE_SHOWS[0]),
        FakeResponse(data=page_1),
        FakeResponse(data=page_2),
        FakeResponse(data=SAMPLE_CLIPS["clips"][0]),
        FakeResponse(data=SAMPLE_SHOWS[0]),
    )

    assert (await provider.get_radio("41")).name == "CBC Radio One: Toronto"
    with pytest.raises(MediaNotFoundError):
        await provider.get_radio("999")

    assert (await provider.get_podcast("209")).name == "Front Burner"
    with pytest.raises(MediaNotFoundError):
        await provider.get_podcast("500")

    with patch("music_assistant.providers.cbc_listen.provider.PAGE_SIZE", 2):
        episodes = [ep async for ep in provider.get_podcast_episodes("209")]
    assert [(ep.item_id, ep.position) for ep in episodes] == [("1002", 2), ("1001", 1)]

    single_ep = await provider.get_podcast_episode("1002")
    assert single_ep.item_id == "1002"
    assert single_ep.podcast.item_id == "209"
    called_urls = [call.args[0] for call in get.call_args_list]
    assert f"{SHOW_URL}/209/clips?pageNumber=1&pageSize=2" in called_urls
    assert f"{SHOW_URL}/209/clips?pageNumber=2&pageSize=2" in called_urls


async def test_stream_details_and_live_metadata() -> None:
    """Test stream details for radio and podcast episodes and live metadata updates."""
    now_dt = datetime(2026, 11, 12, 2, 0, 0, tzinfo=UTC)
    now_ms = int(now_dt.timestamp() * 1000)
    queue_music = [
        {
            "showID": 1044,
            "programTitle": "About Time",
            "hostName": "Tom Allen",
            "programImage": "https://example.com/about.png",
            "epochStart": now_ms - 60000,
            "epochEnd": now_ms + 60000,
        }
    ]
    queue_talk = [
        {
            "showID": 39,
            "programTitle": "Metro Morning",
            "hostName": "David Common",
            "programImage": "https://example.com/metro.png",
            "epochStart": now_ms - 30000,
            "epochEnd": now_ms + 90000,
        }
    ]
    queue_syndicated = [
        {
            "showID": "the_world",
            "programTitle": "The World",
            "hostName": "Marco Werman",
            "programImage": "https://example.com/world.png",
            "epochStart": now_ms - 30000,
            "epochEnd": now_ms + 90000,
        }
    ]

    provider, get = make_provider(
        FakeResponse(data=SAMPLE_NETWORKS),
        FakeResponse(data=SAMPLE_CLIPS["clips"][0]),
        FakeResponse(data=SAMPLE_CLIPS["clips"][2]),
        # Tick 1: show just started; previous broadcast's playlog date (20261110) has only
        # older tracks, so the date is not cached yet.
        FakeResponse(data=SAMPLE_NETWORKS),
        FakeResponse(data=queue_music),
        FakeResponse(data={"episodes": [{"episodeDateId": 20261110}]}),
        FakeResponse(
            data=[
                {
                    "title": "Yesterday's Finale",
                    "artists": "Earlier Artist",
                    "composers": "",
                    "album": "Archive",
                    "broadcastedTime": now_ms - 86_400_000,
                }
            ]
        ),
        # Tick 2: music show resolves playlog date 20261111, fetches tracks, and caches date.
        FakeResponse(data=SAMPLE_NETWORKS),
        FakeResponse(data=queue_music),
        FakeResponse(data={"episodes": [{"episodeDateId": 20261111}]}),
        FakeResponse(
            data=[
                {
                    "title": "Pastoral Symphony",
                    "artists": "Mark Elder",
                    "composers": "Vaughan Williams",
                    "album": "Pastoral",
                    "broadcastedTime": now_ms - 10000,
                }
            ]
        ),
        # Tick 3: same music slot reuses cached 20261111 without calling /playlogs again.
        FakeResponse(data=SAMPLE_NETWORKS),
        FakeResponse(data=queue_music),
        FakeResponse(
            data=[
                {
                    "title": "The Lark Ascending",
                    "artists": "Hilary Hahn",
                    "composers": "Vaughan Williams",
                    "album": "Lark",
                    "broadcastedTime": now_ms - 5000,
                }
            ]
        ),
        # Tick 4: unrecoverable error leaves existing metadata untouched.
        FakeResponse(status=HTTPStatus.FORBIDDEN),
        # Tick 5 & 6: talk show with empty playlogs caches None and skips further playlog calls.
        FakeResponse(data=SAMPLE_NETWORKS),
        FakeResponse(data=queue_talk),
        FakeResponse(data={"episodes": []}),
        FakeResponse(data=SAMPLE_NETWORKS),
        FakeResponse(data=queue_talk),
        # Tick 7: syndicated show with string showID skips playlog lookups entirely.
        FakeResponse(data=SAMPLE_NETWORKS),
        FakeResponse(data=queue_syndicated),
    )

    radio_stream = await provider.get_stream_details("37", MediaType.RADIO)
    assert radio_stream.stream_type == StreamType.HLS
    assert radio_stream.audio_format.content_type == ContentType.AAC
    assert not radio_stream.can_seek

    ep_stream = await provider.get_stream_details("1002", MediaType.PODCAST_EPISODE)
    assert ep_stream.stream_type == StreamType.HTTP
    assert ep_stream.audio_format.content_type == ContentType.MP3
    assert ep_stream.can_seek
    assert ep_stream.path == "https://podcast-a.akamaihd.net/mp3/podcasts/ep2.mp3"

    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details("1003", MediaType.PODCAST_EPISODE)

    with patch("music_assistant.providers.cbc_listen.provider.utc", return_value=now_dt):
        await provider._update_stream_metadata(radio_stream, 0)
        assert radio_stream.stream_metadata is not None
        assert radio_stream.stream_metadata.title == "About Time"
        assert radio_stream.stream_metadata.artist == "Tom Allen"
        await provider._update_stream_metadata(radio_stream, 0)
        assert radio_stream.stream_metadata is not None
        assert radio_stream.stream_metadata.title == "Pastoral Symphony"
        assert radio_stream.stream_metadata.artist == "Mark Elder"

        await provider._update_stream_metadata(radio_stream, 30)
        assert radio_stream.stream_metadata.title == "The Lark Ascending"

        await provider._update_stream_metadata(radio_stream, 60)
        assert radio_stream.stream_metadata.title == "The Lark Ascending"

        await provider._update_stream_metadata(radio_stream, 90)
        assert radio_stream.stream_metadata.title == "Metro Morning"
        assert radio_stream.stream_metadata.artist == "David Common"

        await provider._update_stream_metadata(radio_stream, 120)
        assert radio_stream.stream_metadata.title == "Metro Morning"

        await provider._update_stream_metadata(radio_stream, 150)
        assert radio_stream.stream_metadata.title == "The World"
        assert radio_stream.stream_metadata.artist == "Marco Werman"

    called_urls = [call.args[0] for call in get.call_args_list]
    assert (
        called_urls.count(f"{SHOW_URL}/1044/playlogs?pageNumber=1&locationKey=toronto&pageSize=1")
        == 2
    )
    assert called_urls.count(f"{SHOW_URL}/1044/playlogs/day/20261110?locationKey=toronto") == 1
    assert called_urls.count(f"{SHOW_URL}/1044/playlogs/day/20261111?locationKey=toronto") == 2
    assert (
        called_urls.count(f"{SHOW_URL}/39/playlogs?pageNumber=1&locationKey=toronto&pageSize=1")
        == 1
    )
    assert any(
        call.kwargs == {"headers": API_HEADERS, "timeout": API_TIMEOUT}
        for call in get.call_args_list
    )


async def test_http_errors_and_rate_limiting() -> None:
    """Test 4xx/5xx handling, transient retries, 429 backoff, and invalid payload errors."""
    bad_json_response = FakeResponse()
    bad_json_response._body = "not-json"
    provider, _ = make_provider(
        FakeResponse(status=HTTPStatus.NOT_FOUND),
        FakeResponse(status=HTTPStatus.BAD_REQUEST),
        FakeResponse(status=HTTPStatus.FORBIDDEN),
        bad_json_response,
        FakeResponse(raw_payload={"status": "error"}),
        FakeResponse(data=[]),
        FakeResponse(data=[]),
    )

    with pytest.raises(MediaNotFoundError):
        await provider._get_data(SHOWS_URL)
    with pytest.raises(MediaNotFoundError):
        await provider._get_data(f"{CLIPS_URL}/999999")
    with pytest.raises(ProviderUnavailableError, match="status 403"):
        await provider._get_data(SHOWS_URL)
    with pytest.raises(ProviderUnavailableError, match="Could not parse"):
        await provider._get_data(SHOWS_URL)
    with pytest.raises(ProviderUnavailableError, match="Unexpected response payload"):
        await provider._get_data(SHOWS_URL)
    with pytest.raises(ProviderUnavailableError, match="no live radio stations"):
        await provider._get_channels()
    with pytest.raises(ProviderUnavailableError, match="no shows"):
        await provider._get_shows()

    retry_provider, _ = make_provider(
        FakeResponse(status=HTTPStatus.INTERNAL_SERVER_ERROR),
        aiohttp.ClientError("connection failed"),
        TimeoutError(),
        FakeResponse(data=SAMPLE_SHOWS),
    )
    with patch(SLEEP, new_callable=AsyncMock) as sleep:
        data = await retry_provider._get_data(SHOWS_URL)
        assert len(data) == 4
        assert sleep.await_count == 3

    rate_limit_provider, _ = make_provider(
        FakeResponse(status=HTTPStatus.TOO_MANY_REQUESTS, headers={"Retry-After": "3"}),
        FakeResponse(data=SAMPLE_SHOWS),
    )
    with patch(SLEEP, new_callable=AsyncMock) as sleep:
        data = await rate_limit_provider._get_data(SHOWS_URL)
        assert len(data) == 4
        assert sleep.await_count == 1

    exhausted_provider, _ = make_provider(
        *[FakeResponse(status=HTTPStatus.TOO_MANY_REQUESTS)] * 5,
    )
    with (
        patch(SLEEP, new_callable=AsyncMock),
        pytest.raises(RetriesExhausted),
    ):
        await exhausted_provider._get_data(SHOWS_URL)


async def test_resolve_image() -> None:
    """Test fetching image bytes, 404 fallback to default artwork, and network errors."""
    provider, _ = make_provider(
        FakeResponse(raw_payload="image"),
        FakeResponse(status=HTTPStatus.NOT_FOUND),
        FakeResponse(raw_payload="default"),
        FakeResponse(status=HTTPStatus.NOT_FOUND),
        FakeResponse(status=HTTPStatus.NOT_FOUND),
        FakeResponse(status=HTTPStatus.NOT_FOUND),
        aiohttp.ClientError("connection failed"),
    )

    assert await provider.resolve_image("https://www.cbc.ca/img.jpg") == b'"image"'
    assert await provider.resolve_image("https://www.cbc.ca/missing.jpg") == b'"default"'
    assert await provider.resolve_image(DEFAULT_IMAGE_URL) is None
    assert await provider.resolve_image("https://www.cbc.ca/missing-2.jpg") is None
    with pytest.raises(MediaNotFoundError, match="connection failed"):
        await provider.resolve_image("https://www.cbc.ca/broken.jpg")
