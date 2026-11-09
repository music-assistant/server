"""Shared fixtures for the DR LYD provider tests."""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.helpers.json import json_dumps, json_loads
from music_assistant.helpers.throttle_retry import Throttler
from music_assistant.providers.dr_lyd import SUPPORTED_FEATURES
from music_assistant.providers.dr_lyd.constants import IMAGE_URL
from music_assistant.providers.dr_lyd.models import Channel, Episode, Series
from music_assistant.providers.dr_lyd.parsers import (
    parse_channels,
    parse_programmes,
    parse_series,
    parse_tracks,
)
from music_assistant.providers.dr_lyd.provider import DRLydProvider
from tests.common import use_real_create_task

FIXTURES = Path(__file__).parent / "fixtures"
DOMAIN = "dr_lyd"
INSTANCE_ID = "dr_lyd--test"

API = "https://api.dr.dk/radio/v5"
CATALOGUE_URL = f"{API}/series?limit=500"
CATALOGUE_NEXT_URL = f"{API}/series?limit=500&offset={{}}"
SERIES_URL = f"{API}/series/{{}}"
GROUPS_URL = f"{API}/series/{{}}/episodes/groups?limit=500"
GROUP_URL = f"{API}/series/{{}}/episodes/groups/{{}}?limit=500&offset={{}}"
EPISODE_URL = f"{API}/episodes/{{}}"
CHANNELS_URL = f"{API}/channels"
PROGRAMMES_URL = f"{API}/schedules/all/now"
TRACKS_URL = f"{API}/indexpoints/live/{{}}"

DEN_SIDSTE_TIME = "6641303469000"
GENSTART = "2642056922000"
SARA_OG_MONOPOLET = "1090358387000"
MUSIKQUIZZEN = "757024345000"
TAETTERE_PAA_HIMLEN = "2553137358000"
DE_LOVLOESE = "3337857881000"
RADIOAVISEN = "2467664383000"
BARNELIV = "5711523608000"
UMBRELLA_EPISODES = [
    "16122593411",
    "16122593412",
    "16122593413",
    "16122293321",
    "16122293322",
    "16122293323",
    "16122293324",
]
NOT_AN_ID = [
    "https://example.com/feed.xml",
    "https://example.com/feed.xml https://example.com/guid/6427350",
    "abc",
    "",
    " 12",
    "12 34",
    "12/34",
    "../12",
    "-12",
]


def load_fixture(name: str) -> Any:
    """Return a document that was recorded from the radio API of DR."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def epoch(time_of_day: str, day: str = "2026-11-05") -> float:
    """Return the epoch seconds of a UTC time, by default on the day of the recorded documents."""
    return datetime.fromisoformat(f"{day}T{time_of_day}+00:00").timestamp()


SERIES_DOCUMENT = load_fixture("series.json")
EPISODES_DOCUMENT = load_fixture("episodes.json")
CHANNELS_DOCUMENT = load_fixture("channels.json")
TRACKS_P3 = load_fixture("tracks_p3.json")
TRACKS_P2 = load_fixture("tracks_p2.json")
PROGRAMMES = load_fixture("programmes.json")
SERIES = {
    series.id: series
    for series in map(parse_series, SERIES_DOCUMENT["items"])
    if series is not None
}
CHANNEL_LIST = parse_channels(CHANNELS_DOCUMENT)
CHANNELS = {channel.slug: channel for channel in CHANNEL_LIST}
TRACKS = parse_tracks(TRACKS_P3)
PROGRAMME_LIST = parse_programmes(PROGRAMMES)
ASSET = "https://api.dr.dk/radio/v1/assetlinks/urn:dr:radio:episode:{}/{}"
STREAM = "https://drliveradio2.akamaized.net/hls/live/2118698/{}/masterab.m3u8"
ICY_STREAM = "https://live-icy.dr.dk/A/{}.mp3"
P3_LOGO = IMAGE_URL.format("urn:dr:radio:image:624c02604e30a95b58edee96")
P3_PICTURE = IMAGE_URL.format("urn:dr:radio:image:6ad5c1091068ba26db3ae6da")


def series_item(number: str) -> dict[str, Any]:
    """Return the item of the series document that has the production number."""
    return next(item for item in SERIES_DOCUMENT["items"] if item["productionNumber"] == number)


def episode_items(series_number: str) -> list[dict[str, Any]]:
    """Return the items of all the groups of episodes of a series."""
    return [item for group in EPISODES_DOCUMENT[series_number] for item in group["items"]]


def episode_item(number: str) -> dict[str, Any]:
    """Return the item of the episodes document that has the production number."""
    return next(
        item
        for groups in EPISODES_DOCUMENT.values()
        for group in groups
        for item in group["items"]
        if item["productionNumber"] == number
    )


def asset(target: str, format_: str, bitrate: int | None, url: str | None) -> dict[str, Any]:
    """Return an audio asset."""
    return {"target": target, "format": format_, "bitrate": bitrate, "url": url}


def channel_item(slug: str = "p9", **changes: Any) -> dict[str, Any]:
    """Return an item of the channels document, by default a flow channel with an HLS stream."""
    item = {
        "slug": slug,
        "title": "P9",
        "id": f"urn:dr:radio:channel:{slug}",
        "isFlowChannel": True,
        "audioAssets": [{"format": "HLS", "url": STREAM.format(slug)}],
    }
    return {**item, **changes}


def channels_document_with(slug: str, **changes: Any) -> list[dict[str, Any]]:
    """Return the channels document with changes made to the channel of the slug."""
    document = copy.deepcopy(CHANNELS_DOCUMENT)
    for entry in document:
        for channel in [entry, *entry.get("districts", [])]:
            if channel["slug"] == slug:
                channel.update(changes)
    return list(document)


def track_item(**changes: Any) -> dict[str, Any]:
    """Return an item of the live index points document, by default a track."""
    item = {
        "type": "Track",
        "title": "Raindance",
        "playedTime": "2026-11-05T22:03:09+00:00",
        "durationMilliseconds": 205000,
        "classical": False,
        "description": "Dave feat. Tems",
        "roles": [],
    }
    return {**item, **changes}


def without_picture(channel: Channel) -> Channel:
    """Return the channel without a picture."""
    return Channel.from_dict({**channel.to_dict(), "image_url": None})


def make_series(**changes: Any) -> Series:
    """Return a series, by default one without any extras."""
    fields: dict[str, Any] = {
        "id": "1",
        "title": "Title",
        "description": None,
        "episodes": 1,
        "latest": None,
        "image_url": None,
        "genres": (),
        "explicit": False,
        "page_url": None,
    }
    return Series(**{**fields, **changes})


def make_episode(**changes: Any) -> Episode:
    """Return an episode, by default one without any extras."""
    fields: dict[str, Any] = {
        "id": "2",
        "title": "Episode",
        "description": None,
        "duration": 60,
        "published": None,
        "audio_url": "https://example.com/audio.mp3",
        "hls": False,
        "image_url": None,
        "explicit": False,
        "page_url": None,
        "series_id": "1",
        "series_title": "Title",
    }
    return Episode(**{**fields, **changes})


class FakeApi:
    """Stands in for the radio API of DR: serves the documents it was given by url."""

    def __init__(self, documents: dict[str, Any] | None = None) -> None:
        """Initialize with the documents to serve."""
        self.documents = dict(documents or {})
        self.errors: dict[str, Exception] = {}
        self.requests: list[str] = []

    async def __call__(self, url: str) -> Any:
        """Return the document at the url, or raise like the API does."""
        self.requests.append(url)
        if url in self.errors:
            raise self.errors[url]
        if url not in self.documents:
            raise MediaNotFoundError(f"DR has nothing at {url}")
        return self.documents[url]


class FakeCache:
    """
    Stands in for the cache controller, serializing like the real one.

    What the cache decorator stores is written as JSON and read back through the ``from_dict`` of
    the base class, so a model that does not survive that round trip fails here.
    """

    def __init__(self) -> None:
        """Initialize an empty cache."""
        self.entries: dict[str, tuple[str, int]] = {}

    async def get_with_freshness(
        self, key: str, base_class: Any = None, **_kwargs: Any
    ) -> tuple[Any, bool, bool]:
        """Return the stored value of the key, as fresh."""
        if key not in self.entries:
            return None, False, False
        data = json_loads(self.entries[key][0])
        if base_class is not None:
            if isinstance(data, list):
                data = [base_class.from_dict(item) for item in data]
            else:
                data = base_class.from_dict(data)
        return data, True, True

    async def set(self, key: str, data: Any, expiration: int, **_kwargs: Any) -> None:
        """Store the value as JSON."""
        self.entries[key] = (json_dumps(data), expiration)


def cache_entries(provider: DRLydProvider) -> dict[str, tuple[str, int]]:
    """Return what the provider has stored in its cache, with the seconds each entry is kept."""
    return cast("FakeCache", provider.mass.cache).entries


def make_provider(mass: MagicMock | None = None) -> DRLydProvider:
    """Return a provider wired to mocks, on a cache that serializes like the real one."""
    if mass is None:
        mass = MagicMock()
        mass.cache = FakeCache()
        use_real_create_task(mass)
    manifest = MagicMock()
    manifest.domain = DOMAIN
    manifest.name = "DR LYD"
    config = MagicMock()
    config.instance_id = INSTANCE_ID
    config.values = {}
    config.get_value.side_effect = lambda key, default=None: (
        "GLOBAL" if key == "log_level" else default
    )
    provider = DRLydProvider(mass, manifest, config, SUPPORTED_FEATURES)
    provider._throttler = Throttler(rate_limit=1000, period=1)
    return provider


@pytest.fixture
def api() -> FakeApi:
    """Return the stand-in for the radio API of DR."""
    return FakeApi()


@pytest.fixture
def provider(api: FakeApi) -> DRLydProvider:
    """Return a provider that reads its documents from the stand-in for the radio API."""
    provider = make_provider()
    provider._get_json = api  # type: ignore[method-assign]
    return provider
