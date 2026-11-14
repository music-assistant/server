"""Tests for the DR LYD music provider."""

from __future__ import annotations

import asyncio
import copy
from datetime import timedelta
from typing import Any

import pytest
from music_assistant_models.enums import ContentType, MediaType, ProviderFeature, StreamType
from music_assistant_models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    ProviderUnavailableError,
    RateLimited,
    RetriesExhausted,
    UnplayableMediaError,
)
from music_assistant_models.media_items import BrowseFolder
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.providers.dr_lyd import SUPPORTED_FEATURES
from music_assistant.providers.dr_lyd.constants import (
    MAX_PAGES,
    STREAM_DELAY_SECONDS,
    STREAM_METADATA_UPDATE_INTERVAL,
)
from music_assistant.providers.dr_lyd.parsers import parse_podcast
from music_assistant.providers.dr_lyd.provider import DRLydProvider

from .conftest import (
    CATALOGUE_NEXT_URL,
    CATALOGUE_URL,
    CHANNEL_LIST,
    CHANNELS,
    CHANNELS_DOCUMENT,
    CHANNELS_URL,
    DE_LOVLOESE,
    DEN_SIDSTE_TIME,
    DEN_SIDSTE_TIME_COVER,
    DOMAIN,
    EPISODE_URL,
    EPISODES_DOCUMENT,
    GENSTART,
    GROUP_URL,
    GROUPS_URL,
    INSTANCE_ID,
    MUSIKQUIZZEN,
    ODDERHUSETS_HEMMELIGHED,
    P3_PICTURE,
    PROGRAMMES,
    PROGRAMMES_URL,
    SEARCH_URL,
    SERIES,
    SERIES_DOCUMENT,
    SERIES_URL,
    TRACKS_P2,
    TRACKS_P3,
    TRACKS_URL,
    UMBRELLA_EPISODES,
    FakeApi,
    at,
    cache_entries,
    channel_item,
    episode_item,
    episode_items,
    series_item,
)

PROVIDER_MODULE = "music_assistant.providers.dr_lyd.provider"


def catalogue_documents(pages: list[list[dict[str, Any]]]) -> dict[str, Any]:
    """Return the documents of a catalogue split in pages, served by the urls that link them."""
    documents: dict[str, Any] = {}
    offset = 0
    for index, items in enumerate(pages):
        page: dict[str, Any] = {
            "type": "List",
            "totalSize": sum(map(len, pages)),
            "items": items,
        }
        if index < len(pages) - 1:
            page["next"] = CATALOGUE_NEXT_URL.format(offset + len(items))
        documents[CATALOGUE_URL if offset == 0 else CATALOGUE_NEXT_URL.format(offset)] = page
        offset += len(items)
    return documents


def add_episode_documents(api: FakeApi) -> None:
    """Add the documents of the series and of their episodes to the stand-in for the API."""
    api.documents.update(
        {GROUPS_URL.format(number): groups for number, groups in EPISODES_DOCUMENT.items()}
    )
    api.documents.update(
        {
            EPISODE_URL.format(item["productionNumber"]): item
            for number in EPISODES_DOCUMENT
            for item in episode_items(number)
        }
    )
    api.documents.update(
        {SERIES_URL.format(item["productionNumber"]): item for item in SERIES_DOCUMENT["items"]}
    )


def add_now_playing_documents(api: FakeApi) -> None:
    """Add the documents of the channels and of what they play to the stand-in for the API."""
    api.documents.update(
        {
            CHANNELS_URL: CHANNELS_DOCUMENT,
            TRACKS_URL.format(CHANNELS["p3"].urn): TRACKS_P3,
            TRACKS_URL.format(CHANNELS["p2"].urn): TRACKS_P2,
            TRACKS_URL.format(CHANNELS["p8jazz"].urn): {"items": []},
            PROGRAMMES_URL: PROGRAMMES,
        }
    )


@pytest.fixture
def episodes_api(api: FakeApi) -> FakeApi:
    """Return the stand-in for the API, with the series and episodes of the fixtures."""
    add_episode_documents(api)
    return api


@pytest.fixture
def now_playing_api(api: FakeApi) -> FakeApi:
    """Return the stand-in for the API, with the channels and what they play."""
    add_now_playing_documents(api)
    return api


def listening_at(monkeypatch: pytest.MonkeyPatch, time_of_day: str) -> None:
    """Make the time so that what the listener hears is what DR played at the time of day."""
    monkeypatch.setattr(
        f"{PROVIDER_MODULE}.utc",
        lambda: at(time_of_day) + timedelta(seconds=STREAM_DELAY_SECONDS),
    )


async def settle() -> None:
    """Let the tasks that store what was read in the cache run."""
    for _ in range(3):
        await asyncio.sleep(0)


def stream_details(provider: DRLydProvider, slug: str = "p3") -> StreamDetails:
    """Return the stream details of a channel, as the player queue controller holds them."""
    return provider._get_radio_stream_details(CHANNELS[slug])


async def collect_episodes(provider: DRLydProvider, series_id: str) -> list[str]:
    """Return the ids of the episodes of a podcast, in the order the provider lists them."""
    return [episode.item_id async for episode in provider.get_podcast_episodes(series_id)]


def test_the_provider_has_search_and_browse() -> None:
    """The provider has no library, a radio or a podcast only gets into it when it is added."""
    assert {ProviderFeature.BROWSE, ProviderFeature.SEARCH} == SUPPORTED_FEATURES


async def test_the_config_has_the_unofficial_note(provider: DRLydProvider) -> None:
    """The user is told the API is unofficial."""
    entries = await provider.get_config_entries()
    assert [entry.key for entry in entries] == ["unofficial_provider_note"]


def test_the_provider_serves_radio_and_podcasts_without_stream_limit(
    provider: DRLydProvider,
) -> None:
    """Radio and podcasts take part in search based lookups, and streams are not limited."""
    assert provider.supported_media_types == {MediaType.RADIO, MediaType.PODCAST}
    assert provider.max_concurrent_streams is None


async def test_search_finds_podcasts_at_dr_and_radio_by_name(
    provider: DRLydProvider, api: FakeApi
) -> None:
    """The podcasts come from the search of DR, the channels from the cached list of channels."""
    api.documents = {
        SEARCH_URL.format("sidste+time", 5): {"items": [series_item(DEN_SIDSTE_TIME)]},
        SEARCH_URL.format("p3", 5): {"items": []},
        CHANNELS_URL: CHANNELS_DOCUMENT,
    }
    results = await provider.search("sidste time", [MediaType.PODCAST, MediaType.RADIO])
    assert [podcast.item_id for podcast in results.podcasts] == [DEN_SIDSTE_TIME]
    assert results.podcasts[0].provider == INSTANCE_ID
    assert results.radio == []
    assert api.requests == [SEARCH_URL.format("sidste+time", 5), CHANNELS_URL]
    results = await provider.search("p3", [MediaType.PODCAST, MediaType.RADIO])
    assert results.podcasts == []
    assert [radio.item_id for radio in results.radio] == ["p3"]


async def test_search_reads_only_what_the_media_types_ask_for(
    provider: DRLydProvider, api: FakeApi
) -> None:
    """Radio is not searched for podcasts, and podcasts are not searched for radio."""
    api.documents = {
        SEARCH_URL.format("genstart", 5): {"items": [series_item(GENSTART)]},
        CHANNELS_URL: CHANNELS_DOCUMENT,
    }
    await provider.search("genstart", [MediaType.PODCAST])
    assert api.requests == [SEARCH_URL.format("genstart", 5)]
    api.requests.clear()
    await provider.search("p3", [MediaType.RADIO])
    assert api.requests == [CHANNELS_URL]
    api.requests.clear()
    results = await provider.search("p3", [MediaType.ARTIST])
    assert (results.podcasts, results.radio, api.requests) == ([], [], [])


async def test_search_limit(provider: DRLydProvider, api: FakeApi) -> None:
    """A search asks DR for no more podcasts than the limit, and returns no more channels."""
    api.documents = {
        SEARCH_URL.format("p", 3): {"items": [series_item(GENSTART)]},
        CHANNELS_URL: CHANNELS_DOCUMENT,
    }
    results = await provider.search("p", [MediaType.PODCAST, MediaType.RADIO], limit=3)
    assert len(results.radio) == 3
    assert api.requests[0] == SEARCH_URL.format("p", 3)


@pytest.mark.parametrize(
    ("query", "encoded"),
    [("a&b=c d", "a%26b%3Dc+d"), ("æble & pære", "%C3%A6ble+%26+p%C3%A6re")],
)
async def test_the_words_of_a_search_stay_in_one_parameter_of_the_url(
    provider: DRLydProvider, api: FakeApi, query: str, encoded: str
) -> None:
    """Whatever is searched for is encoded, so it cannot add parameters to the url."""
    api.documents = {SEARCH_URL.format(encoded, 5): {"items": []}}
    await provider.search(query, [MediaType.PODCAST])
    assert api.requests == [SEARCH_URL.format(encoded, 5)]


async def test_search_leaves_out_a_podcast_that_is_not_available(
    provider: DRLydProvider, api: FakeApi
) -> None:
    """A series that cannot be listened to on demand is not a result."""
    api.documents = {
        SEARCH_URL.format("quiz", 5): {
            "items": [
                dict(series_item(GENSTART), isAvailableOnDemand=False),
                series_item(MUSIKQUIZZEN),
            ]
        }
    }
    results = await provider.search("quiz", [MediaType.PODCAST])
    assert [podcast.item_id for podcast in results.podcasts] == [MUSIKQUIZZEN]


async def test_browse_root_lists_the_folders(provider: DRLydProvider) -> None:
    """The folders are translated by the strings of the provider, or the shared ones."""
    folders = await provider.browse(f"{INSTANCE_ID}://")
    assert [
        (folder.item_id, folder.name) for folder in folders if isinstance(folder, BrowseFolder)
    ] == [
        ("recently_updated", "Recently updated"),
        ("podcasts", "Podcasts"),
        ("radios", "Radio"),
    ]
    for folder in folders:
        assert isinstance(folder, BrowseFolder)
        assert folder.translation_key == folder.item_id
        assert folder.path == f"{INSTANCE_ID}://{folder.item_id}"
        assert folder.provider == INSTANCE_ID


async def test_browse_radios_lists_the_channels(provider: DRLydProvider, api: FakeApi) -> None:
    """The radios folder lists every channel, in the order of DR."""
    api.documents = {CHANNELS_URL: CHANNELS_DOCUMENT}
    items = await provider.browse(f"{INSTANCE_ID}://radios")
    assert [item.item_id for item in items] == [channel.slug for channel in CHANNEL_LIST]
    assert all(item.media_type == MediaType.RADIO for item in items)


async def test_browse_recently_updated_lists_the_newest_podcasts_first(
    provider: DRLydProvider, api: FakeApi
) -> None:
    """The recently updated folder lists the podcasts by the time of their latest episode."""
    api.documents = catalogue_documents([SERIES_DOCUMENT["items"]])
    items = await provider.browse(f"{INSTANCE_ID}://recently_updated/")
    assert [item.name for item in items] == [
        "Radioavisen",
        "Genstart",
        "Musikquizzen",
        "Sara & Monopolet - podcast",
        "Tættere på himlen",
        "Den sidste time",
        "De Lovløse",
        "Barneliv gennem tiden",
    ]
    assert all(item.media_type == MediaType.PODCAST for item in items)


async def test_browse_recently_updated_is_cut_to_the_newest(
    provider: DRLydProvider, api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recently updated folder lists no more podcasts than it is meant to."""
    monkeypatch.setattr(f"{PROVIDER_MODULE}.BROWSE_RECENT_COUNT", 3)
    api.documents = catalogue_documents([SERIES_DOCUMENT["items"]])
    items = await provider.browse(f"{INSTANCE_ID}://recently_updated")
    assert [item.name for item in items] == ["Radioavisen", "Genstart", "Musikquizzen"]


async def test_browse_podcasts_lists_them_by_title(provider: DRLydProvider, api: FakeApi) -> None:
    """The podcasts folder lists every podcast ordered by title, ignoring case."""
    api.documents = catalogue_documents([SERIES_DOCUMENT["items"]])
    items = await provider.browse(f"{INSTANCE_ID}://podcasts")
    names = [item.name for item in items]
    assert len(names) == 8
    assert names == sorted(names, key=str.casefold)


async def test_browse_rejects_an_unknown_folder(provider: DRLydProvider, api: FakeApi) -> None:
    """A path that is no folder is invalid, without a request to DR."""
    with pytest.raises(KeyError):
        await provider.browse(f"{INSTANCE_ID}://nowhere")
    assert api.requests == []


async def test_get_radio(provider: DRLydProvider, api: FakeApi) -> None:
    """A radio is read by the slug of its channel."""
    api.documents = {CHANNELS_URL: CHANNELS_DOCUMENT}
    radio = await provider.get_radio("p6beat")
    assert (radio.item_id, radio.name) == ("p6beat", "P6")


@pytest.mark.parametrize("slug", ["p4", "nope", ""])
async def test_get_radio_of_a_channel_that_is_not_listed(
    provider: DRLydProvider, api: FakeApi, slug: str
) -> None:
    """A slug that is not listed, P4 itself included, is not found."""
    api.documents = {CHANNELS_URL: CHANNELS_DOCUMENT}
    with pytest.raises(MediaNotFoundError):
        await provider.get_radio(slug)


async def test_get_podcast_reads_the_series_by_its_production_number(
    provider: DRLydProvider, api: FakeApi
) -> None:
    """A podcast is read from the series document of its production number."""
    api.documents = {SERIES_URL.format(DEN_SIDSTE_TIME): series_item(DEN_SIDSTE_TIME)}
    podcast = await provider.get_podcast(DEN_SIDSTE_TIME)
    assert (podcast.item_id, podcast.name) == (DEN_SIDSTE_TIME, "Den sidste time")
    assert api.requests == ["https://api.dr.dk/radio/v5/series/6641303469000"]


async def test_get_podcast_is_not_found_when_dr_has_no_such_series(
    provider: DRLydProvider, api: FakeApi
) -> None:
    """A series DR does not know, or does not offer, is not found."""
    with pytest.raises(MediaNotFoundError):
        await provider.get_podcast("999999999999")
    with pytest.raises(MediaNotFoundError):
        await collect_episodes(provider, "999999999999")
    api.documents[SERIES_URL.format(GENSTART)] = dict(
        series_item(GENSTART), isAvailableOnDemand=False
    )
    with pytest.raises(MediaNotFoundError, match="to listen to"):
        await provider.get_podcast(GENSTART)


async def test_get_podcast_episodes_lists_the_episodes_of_a_podcast(
    provider: DRLydProvider, episodes_api: FakeApi
) -> None:
    """The episodes of a podcast are read from its groups, with their position."""
    episodes = [episode async for episode in provider.get_podcast_episodes(DEN_SIDSTE_TIME)]
    assert [episode.item_id for episode in episodes] == ["13332646601", "13332646424"]
    assert [episode.position for episode in episodes] == [2, 1]
    assert {episode.podcast.name for episode in episodes if episode.podcast} == {"Den sidste time"}
    assert {episode.podcast.item_id for episode in episodes if episode.podcast} == {DEN_SIDSTE_TIME}
    assert episodes_api.requests == [
        SERIES_URL.format(DEN_SIDSTE_TIME),
        GROUPS_URL.format(DEN_SIDSTE_TIME),
    ]


async def test_an_episode_without_a_picture_has_the_cover_of_its_podcast(
    provider: DRLydProvider, episodes_api: FakeApi
) -> None:
    """The picture of the podcast stands in for an episode without one."""
    groups = copy.deepcopy(EPISODES_DOCUMENT[DEN_SIDSTE_TIME])
    groups[0]["items"][0]["imageAssets"] = []
    episodes_api.documents[GROUPS_URL.format(DEN_SIDSTE_TIME)] = groups
    episodes = [episode async for episode in provider.get_podcast_episodes(DEN_SIDSTE_TIME)]
    assert [image.path for image in episodes[0].metadata.images or []] == [DEN_SIDSTE_TIME_COVER]


@pytest.mark.usefixtures("episodes_api")
async def test_a_podcast_made_of_other_podcasts_lists_every_group(provider: DRLydProvider) -> None:
    """The episodes of every group are listed, each named by the podcast it belongs to."""
    assert await collect_episodes(provider, DE_LOVLOESE) == UMBRELLA_EPISODES


async def test_the_pages_of_a_group_are_followed(
    provider: DRLydProvider, episodes_api: FakeApi
) -> None:
    """A group that has more episodes links to the next page."""
    items = episode_items(DE_LOVLOESE)
    first = GROUP_URL.format(DE_LOVLOESE, "all", 3)
    second = GROUP_URL.format(DE_LOVLOESE, "all", 5)
    episodes_api.documents[GROUPS_URL.format(DE_LOVLOESE)] = [
        {"groupId": "all", "items": items[:3], "next": first}
    ]
    episodes_api.documents[first] = {"groupId": "all", "items": items[3:5], "next": second}
    episodes_api.documents[second] = {"groupId": "all", "items": items[5:]}
    assert await collect_episodes(provider, DE_LOVLOESE) == UMBRELLA_EPISODES
    assert episodes_api.requests == [
        SERIES_URL.format(DE_LOVLOESE),
        GROUPS_URL.format(DE_LOVLOESE),
        first,
        second,
    ]


async def test_the_pages_of_a_group_stop_after_a_hundred(
    provider: DRLydProvider, episodes_api: FakeApi
) -> None:
    """A group that never ends is read for a hundred pages."""
    url = GROUP_URL.format(DEN_SIDSTE_TIME, "all", 2)
    group = dict(EPISODES_DOCUMENT[DEN_SIDSTE_TIME][0], next=url)
    episodes_api.documents[GROUPS_URL.format(DEN_SIDSTE_TIME)] = [group]
    episodes_api.documents[url] = group
    assert len(await collect_episodes(provider, DEN_SIDSTE_TIME)) == 2
    assert len(episodes_api.requests) == 2 + MAX_PAGES - 1


async def test_episodes_that_are_not_available_and_duplicates_are_left_out(
    provider: DRLydProvider, episodes_api: FakeApi
) -> None:
    """An episode that is not available, or is listed twice, is listed once at most."""
    groups = copy.deepcopy(EPISODES_DOCUMENT[DE_LOVLOESE])
    groups[0]["items"][0]["isAvailableOnDemand"] = False
    groups[1]["items"].append(copy.deepcopy(groups[0]["items"][2]))
    episodes_api.documents[GROUPS_URL.format(DE_LOVLOESE)] = groups
    assert await collect_episodes(provider, DE_LOVLOESE) == UMBRELLA_EPISODES[1:]


async def test_a_podcast_without_episodes(provider: DRLydProvider, episodes_api: FakeApi) -> None:
    """A podcast whose groups hold no episodes has none."""
    episodes_api.documents[GROUPS_URL.format(DEN_SIDSTE_TIME)] = []
    assert await collect_episodes(provider, DEN_SIDSTE_TIME) == []
    episodes_api.documents[GROUPS_URL.format(GENSTART)] = [{"groupId": "x", "items": []}]
    assert await collect_episodes(provider, GENSTART) == []


async def test_get_podcast_episode_takes_the_podcast_from_its_series(
    provider: DRLydProvider, episodes_api: FakeApi
) -> None:
    """A single episode is at position 0, in the podcast with the title and cover of its series."""
    episode = await provider.get_podcast_episode("13332646601")
    assert episode.item_id == "13332646601"
    assert episode.position == 0
    assert episode.podcast is not None
    assert (episode.podcast.item_id, episode.podcast.name) == (DEN_SIDSTE_TIME, "Den sidste time")
    assert episode.podcast.image is not None
    assert episode.podcast.image.path == DEN_SIDSTE_TIME_COVER
    assert episodes_api.requests == [
        EPISODE_URL.format("13332646601"),
        SERIES_URL.format(DEN_SIDSTE_TIME),
    ]


async def test_get_podcast_episode_of_an_umbrella_is_in_the_series_it_belongs_to(
    provider: DRLydProvider, episodes_api: FakeApi
) -> None:
    """An episode of a series made of other series names the series that DR gives for it."""
    episodes_api.documents[SERIES_URL.format(ODDERHUSETS_HEMMELIGHED)] = {
        **series_item(GENSTART),
        "productionNumber": ODDERHUSETS_HEMMELIGHED,
        "title": "Odderhusets hemmelighed",
    }
    episode = await provider.get_podcast_episode("16122593411")
    assert episode.name == "1:3 - Odderhusets hemmelighed: Familien i skoven"
    assert episode.podcast is not None
    assert (episode.podcast.item_id, episode.podcast.name) == (
        ODDERHUSETS_HEMMELIGHED,
        "Odderhusets hemmelighed",
    )


async def test_get_podcast_episode_is_not_found_without_its_series(
    provider: DRLydProvider, episodes_api: FakeApi
) -> None:
    """An episode of a series that DR does not know is not found."""
    del episodes_api.documents[SERIES_URL.format(DEN_SIDSTE_TIME)]
    with pytest.raises(MediaNotFoundError):
        await provider.get_podcast_episode("13332646601")


async def test_an_episode_is_read_again_every_time_but_its_series_is_cached(
    provider: DRLydProvider, episodes_api: FakeApi
) -> None:
    """What is played is what DR serves now, so an episode is not cached."""
    await provider.get_podcast_episode("13332646601")
    await settle()
    await provider.get_podcast_episode("13332646601")
    await provider.get_stream_details("13332646601", MediaType.PODCAST_EPISODE)
    assert episodes_api.requests.count(EPISODE_URL.format("13332646601")) == 3
    assert episodes_api.requests.count(SERIES_URL.format(DEN_SIDSTE_TIME)) == 1


async def test_an_episode_unknown_to_dr_is_not_found(
    provider: DRLydProvider, episodes_api: FakeApi
) -> None:
    """An episode DR does not know is not found."""
    with pytest.raises(MediaNotFoundError):
        await provider.get_podcast_episode("999999999999")
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details("999999999999", MediaType.PODCAST_EPISODE)
    assert episodes_api.requests == [EPISODE_URL.format("999999999999")] * 2


@pytest.mark.usefixtures("episodes_api")
async def test_the_stream_of_an_mp3_episode(provider: DRLydProvider) -> None:
    """An episode with an mp3 is streamed over http, and can be sought in."""
    details = await provider.get_stream_details("13332646601", MediaType.PODCAST_EPISODE)
    assert details.provider == INSTANCE_ID
    assert details.item_id == "13332646601"
    assert details.media_type == MediaType.PODCAST_EPISODE
    assert details.stream_type == StreamType.HTTP
    assert details.audio_format.content_type == ContentType.MP3
    assert isinstance(details.path, str)
    assert details.path.startswith("https://api.dr.dk/radio/v1/assetlinks/")
    assert details.can_seek
    assert details.allow_seek


@pytest.mark.usefixtures("episodes_api")
async def test_the_stream_of_an_hls_episode(provider: DRLydProvider) -> None:
    """An episode with only an HLS stream is streamed as HLS."""
    details = await provider.get_stream_details("13332646424", MediaType.PODCAST_EPISODE)
    assert details.stream_type == StreamType.HLS
    assert details.audio_format.content_type == ContentType.AAC


async def test_an_episode_without_audio_has_no_stream(
    provider: DRLydProvider, episodes_api: FakeApi
) -> None:
    """An episode DR has no audio for is not found."""
    item = dict(episode_item("13332646601"), audioAssets=[])
    episodes_api.documents[EPISODE_URL.format("13332646601")] = item
    with pytest.raises(MediaNotFoundError, match="no audio"):
        await provider.get_stream_details("13332646601", MediaType.PODCAST_EPISODE)


@pytest.mark.usefixtures("episodes_api")
async def test_the_audio_address_is_only_in_the_stream_details(provider: DRLydProvider) -> None:
    """The address of the audio is not part of any item the provider returns."""
    episode = await provider.get_podcast_episode("13332646601")
    listed = [item async for item in provider.get_podcast_episodes(DEN_SIDSTE_TIME)]
    details = await provider.get_stream_details("13332646601", MediaType.PODCAST_EPISODE)
    assert isinstance(details.path, str)
    for item in (episode, *listed):
        assert details.path not in str(item.to_dict())
        assert all(mapping.url is None for mapping in item.provider_mappings)


async def test_the_stream_of_a_channel(provider: DRLydProvider, api: FakeApi) -> None:
    """A channel is streamed as live HLS, which cannot be sought in."""
    api.documents = {CHANNELS_URL: CHANNELS_DOCUMENT}
    details = await provider.get_stream_details("p3", MediaType.RADIO)
    assert details.item_id == "p3"
    assert details.media_type == MediaType.RADIO
    assert details.stream_type == StreamType.HLS
    assert details.audio_format.content_type == ContentType.AAC
    assert details.path == CHANNELS["p3"].stream_url
    assert not details.can_seek
    assert not details.allow_seek
    assert details.stream_metadata_update_interval == STREAM_METADATA_UPDATE_INTERVAL
    assert details.stream_metadata_update_callback == provider._update_stream_metadata


async def test_the_stream_of_a_channel_without_hls_is_icy(
    provider: DRLydProvider, api: FakeApi
) -> None:
    """A channel without an HLS stream is streamed from its best ICY stream."""
    p3 = next(item for item in CHANNELS_DOCUMENT if item["slug"] == "p3")
    assets = [item for item in p3["audioAssets"] if item["format"] != "HLS"]
    api.documents = {CHANNELS_URL: [channel_item("p3", audioAssets=assets)]}
    details = await provider.get_stream_details("p3", MediaType.RADIO)
    assert details.stream_type == StreamType.ICY
    assert details.audio_format.content_type == ContentType.MP3
    assert details.path == "https://live-icy.dr.dk/A/A05H.mp3"


async def test_a_channel_that_is_not_listed_has_no_stream(
    provider: DRLydProvider, api: FakeApi
) -> None:
    """A slug that is not listed is not found."""
    api.documents = {CHANNELS_URL: CHANNELS_DOCUMENT}
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details("nope", MediaType.RADIO)


async def test_other_media_types_cannot_be_played(provider: DRLydProvider) -> None:
    """Only radio and episodes are played."""
    with pytest.raises(UnplayableMediaError):
        await provider.get_stream_details(DEN_SIDSTE_TIME, MediaType.PODCAST)


@pytest.mark.usefixtures("now_playing_api")
async def test_the_track_playing_is_shown_with_the_picture_of_the_programme(
    provider: DRLydProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stream metadata of a channel is the track it plays, with the programme's picture."""
    listening_at(monkeypatch, "22:30:00")
    details = stream_details(provider)
    await provider._update_stream_metadata(details, 0)
    assert details.stream_metadata == StreamMetadata(
        title="Min egen værste ven", artist="Friða Saiya", image_url=P3_PICTURE
    )


@pytest.mark.usefixtures("now_playing_api")
async def test_classical_music_is_shown_with_its_composer(
    provider: DRLydProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The artist of a classical track is its composer."""
    listening_at(monkeypatch, "22:20:00")
    details = stream_details(provider, "p2")
    await provider._update_stream_metadata(details, 0)
    assert details.stream_metadata is not None
    assert details.stream_metadata.title == "Suor Angelica - Intermezzo"
    assert details.stream_metadata.artist == "Giacomo Puccini"


@pytest.mark.usefixtures("now_playing_api")
async def test_the_track_follows_what_the_listener_hears(
    provider: DRLydProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The track shown is the one DR played a stream delay ago."""
    details = stream_details(provider)
    monkeypatch.setattr(
        f"{PROVIDER_MODULE}.utc",
        lambda: at("22:28:36") + timedelta(seconds=STREAM_DELAY_SECONDS - 1),
    )
    await provider._update_stream_metadata(details, 0)
    assert details.stream_metadata is not None
    assert details.stream_metadata.title == "DtMF"
    listening_at(monkeypatch, "22:28:36")
    await provider._update_stream_metadata(details, 0)
    assert details.stream_metadata.title == "Min egen værste ven"


@pytest.mark.usefixtures("now_playing_api")
async def test_the_programme_is_shown_between_tracks(
    provider: DRLydProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Between tracks the programme on air is shown."""
    listening_at(monkeypatch, "22:24:00")
    details = stream_details(provider)
    await provider._update_stream_metadata(details, 0)
    assert details.stream_metadata == StreamMetadata(
        title="Den sidste time", artist=None, image_url=P3_PICTURE
    )


@pytest.mark.usefixtures("now_playing_api")
async def test_the_channel_is_shown_when_it_plays_nothing_known(
    provider: DRLydProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A channel without tracks and a programme is shown by its name."""
    listening_at(monkeypatch, "22:30:00")
    details = stream_details(provider, "p8jazz")
    await provider._update_stream_metadata(details, 0)
    assert details.stream_metadata == StreamMetadata(title="P8", artist=None)


@pytest.mark.parametrize(
    "error",
    [
        ProviderUnavailableError("down"),
        MediaNotFoundError("gone"),
        RateLimited("slow down", backoff_time=30),
        RetriesExhausted("gave up"),
    ],
)
async def test_the_last_metadata_stays_when_dr_cannot_be_read(
    provider: DRLydProvider,
    now_playing_api: FakeApi,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    """When DR cannot say what plays, what was shown before stays."""
    listening_at(monkeypatch, "22:30:00")
    details = stream_details(provider)
    shown = StreamMetadata(title="Shown before")
    details.stream_metadata = shown
    now_playing_api.errors[TRACKS_URL.format(CHANNELS["p3"].urn)] = error
    await provider._update_stream_metadata(details, 0)
    assert details.stream_metadata is shown
    del now_playing_api.errors[TRACKS_URL.format(CHANNELS["p3"].urn)]
    now_playing_api.errors[PROGRAMMES_URL] = error
    await provider._update_stream_metadata(details, 0)
    assert details.stream_metadata is shown


async def test_any_other_error_is_not_swallowed(
    provider: DRLydProvider, now_playing_api: FakeApi
) -> None:
    """An error that is not about DR being out of reach is a bug, which is not hidden."""
    now_playing_api.errors[PROGRAMMES_URL] = InvalidDataError("unexpected")
    with pytest.raises(InvalidDataError):
        await provider._update_stream_metadata(stream_details(provider), 0)


async def test_the_last_metadata_stays_for_a_channel_that_is_no_longer_listed(
    provider: DRLydProvider, now_playing_api: FakeApi
) -> None:
    """A channel that vanished from the list leaves the metadata as it was."""
    details = stream_details(provider)
    now_playing_api.documents[CHANNELS_URL] = [
        item for item in CHANNELS_DOCUMENT if item["slug"] != "p3"
    ]
    await provider._update_stream_metadata(details, 0)
    assert details.stream_metadata is None


async def test_each_document_is_read_once_per_update_interval(
    provider: DRLydProvider, now_playing_api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documents are shared between listeners, and between channels for the programmes."""
    listening_at(monkeypatch, "22:30:00")
    for slug in ("p3", "p3", "p2"):
        await provider._update_stream_metadata(stream_details(provider, slug), 0)
    assert now_playing_api.requests == [
        CHANNELS_URL,
        TRACKS_URL.format(CHANNELS["p3"].urn),
        PROGRAMMES_URL,
        TRACKS_URL.format(CHANNELS["p2"].urn),
    ]


async def test_the_catalogue_follows_the_links_between_the_pages(
    provider: DRLydProvider, api: FakeApi
) -> None:
    """Every page of the series is read, one after the other."""
    items = SERIES_DOCUMENT["items"]
    api.documents = catalogue_documents([items[:3], items[3:4], items[4:]])
    assert len(await provider._get_catalogue()) == 8
    assert api.requests == [
        CATALOGUE_URL,
        CATALOGUE_NEXT_URL.format(3),
        CATALOGUE_NEXT_URL.format(4),
    ]


async def test_the_catalogue_is_one_request_when_everything_is_on_the_first_page(
    provider: DRLydProvider, api: FakeApi
) -> None:
    """The next page is only read when the page links to one."""
    api.documents = catalogue_documents([SERIES_DOCUMENT["items"]])
    assert len(await provider._get_catalogue()) == 8
    assert api.requests == [CATALOGUE_URL]


async def test_the_catalogue_stops_at_a_page_without_series(
    provider: DRLydProvider, api: FakeApi
) -> None:
    """A page without series ends the catalogue, even when it links to a next one."""
    api.documents = {
        CATALOGUE_URL: {
            "items": SERIES_DOCUMENT["items"][:2],
            "next": CATALOGUE_NEXT_URL.format(2),
        },
        CATALOGUE_NEXT_URL.format(2): {"items": [], "next": CATALOGUE_NEXT_URL.format(2)},
    }
    assert len(await provider._get_catalogue()) == 2
    assert len(api.requests) == 2


async def test_the_catalogue_stops_after_a_hundred_pages(
    provider: DRLydProvider, api: FakeApi
) -> None:
    """A catalogue that never ends is read for a hundred pages."""
    page = {"items": SERIES_DOCUMENT["items"][:1], "next": CATALOGUE_NEXT_URL.format(1)}
    api.documents = {CATALOGUE_URL: page, CATALOGUE_NEXT_URL.format(1): page}
    assert len(await provider._get_catalogue()) == 1
    assert len(api.requests) == MAX_PAGES


async def test_the_catalogue_leaves_out_series_that_are_not_available_and_duplicates(
    provider: DRLydProvider, api: FakeApi
) -> None:
    """A series that is not available, or listed twice, is listed once at most."""
    items = copy.deepcopy(SERIES_DOCUMENT["items"][:3])
    items[1]["isAvailableOnDemand"] = False
    items.append(dict(items[0], title="A copy"))
    api.documents = catalogue_documents([items])
    catalogue = await provider._get_catalogue()
    assert [podcast.name for podcast in catalogue] == [
        "Sara & Monopolet - podcast",
        "Den sidste time",
    ]


async def test_a_catalogue_without_series_is_an_error(
    provider: DRLydProvider, api: FakeApi
) -> None:
    """DR listing no podcasts is no catalogue, it is kept out of the cache."""
    api.documents = {CATALOGUE_URL: {"items": []}}
    with pytest.raises(ProviderUnavailableError, match="no podcasts"):
        await provider._get_catalogue()
    assert cache_entries(provider) == {}


@pytest.mark.parametrize(
    "document",
    [[], [channel_item("p9", isFlowChannel=False)], [channel_item("p9", audioAssets=[])]],
)
async def test_channels_without_a_channel_are_an_error(
    provider: DRLydProvider, api: FakeApi, document: list[dict[str, Any]]
) -> None:
    """A channels document without a channel is kept out of the cache."""
    api.documents = {CHANNELS_URL: document}
    with pytest.raises(ProviderUnavailableError, match="no radio channels"):
        await provider._get_channels()
    assert cache_entries(provider) == {}


async def test_what_is_read_is_cached_for_how_long_it_stays_the_same(
    provider: DRLydProvider, api: FakeApi
) -> None:
    """Every lookup that repeats is cached, for the time its content stays the same."""
    add_episode_documents(api)
    add_now_playing_documents(api)
    api.documents.update(catalogue_documents([SERIES_DOCUMENT["items"]]))
    await provider._get_catalogue()
    await provider._get_series(GENSTART)
    await provider._get_episodes(DEN_SIDSTE_TIME)
    await provider._get_channels()
    await provider._get_programmes()
    await provider._get_tracks(CHANNELS["p3"].urn)
    await settle()
    assert {key: seconds for key, (_, seconds) in cache_entries(provider).items()} == {
        "_get_catalogue": 3600,
        f"_get_series.{GENSTART}": 900,
        f"_get_series.{DEN_SIDSTE_TIME}": 900,
        f"_get_episodes.{DEN_SIDSTE_TIME}": 900,
        "_get_channels": 12 * 3600,
        "_get_programmes": 10,
        f"_get_tracks.{CHANNELS['p3'].urn}": 10,
    }


async def test_a_second_lookup_is_served_from_the_cache(
    provider: DRLydProvider, api: FakeApi
) -> None:
    """What was read is returned again from the cache, the way it came out of its models."""
    add_episode_documents(api)
    add_now_playing_documents(api)
    api.documents.update(catalogue_documents([SERIES_DOCUMENT["items"]]))
    urn = CHANNELS["p3"].urn
    catalogue = await provider._get_catalogue()
    series = await provider._get_series(GENSTART)
    episodes = await provider._get_episodes(DEN_SIDSTE_TIME)
    channels = await provider._get_channels()
    programmes = await provider._get_programmes()
    tracks = await provider._get_tracks(urn)
    await settle()
    requests = len(api.requests)
    assert await provider._get_catalogue() == catalogue
    assert await provider._get_series(GENSTART) == series
    assert await provider._get_episodes(DEN_SIDSTE_TIME) == episodes
    assert await provider._get_channels() == channels
    assert await provider._get_programmes() == programmes
    assert await provider._get_tracks(urn) == tracks
    assert len(api.requests) == requests


async def test_a_series_that_is_not_in_the_catalogue_can_still_be_read_alone(
    provider: DRLydProvider, api: FakeApi
) -> None:
    """A series is parsed the same, whether it comes from the catalogue or from its own url."""
    api.documents = {SERIES_URL.format(GENSTART): series_item(GENSTART)}
    assert await provider._get_series(GENSTART) == parse_podcast(
        SERIES[GENSTART], INSTANCE_ID, DOMAIN
    )
