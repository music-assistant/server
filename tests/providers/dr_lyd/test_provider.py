"""Tests for the DR LYD music provider."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import ContentType, MediaType, ProviderFeature, StreamType
from music_assistant_models.errors import (
    MediaNotFoundError,
    ProviderUnavailableError,
    UnplayableMediaError,
)
from music_assistant_models.media_items import BrowseFolder, Podcast
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.providers.dr_lyd import SUPPORTED_FEATURES
from music_assistant.providers.dr_lyd.constants import (
    MAX_PAGES,
    STREAM_DELAY_SECONDS,
    STREAM_METADATA_UPDATE_INTERVAL,
)
from music_assistant.providers.dr_lyd.parsers import parse_podcast, parse_series
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
    DOMAIN,
    EPISODE_URL,
    EPISODES_DOCUMENT,
    GENSTART,
    GROUP_URL,
    GROUPS_URL,
    INSTANCE_ID,
    MUSIKQUIZZEN,
    NOT_AN_ID,
    P3_PICTURE,
    PROGRAMMES,
    PROGRAMMES_URL,
    SERIES,
    SERIES_DOCUMENT,
    SERIES_URL,
    TRACKS_P2,
    TRACKS_P3,
    TRACKS_URL,
    UMBRELLA_EPISODES,
    FakeApi,
    cache_entries,
    episode_item,
    episode_items,
    epoch,
    series_item,
)


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


def listening_at(monkeypatch: pytest.MonkeyPatch, time_of_day: str) -> None:
    """Make the time so that what the listener hears is what DR played at the time of day."""
    monkeypatch.setattr(
        "music_assistant.providers.dr_lyd.provider.utc_timestamp",
        lambda: epoch(time_of_day) + STREAM_DELAY_SECONDS,
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


def test_the_provider_has_search_browse_and_a_podcast_library() -> None:
    """The provider has no radio library, a radio only gets into the library when it is added."""
    assert {ProviderFeature.BROWSE, ProviderFeature.SEARCH, ProviderFeature.LIBRARY_PODCASTS} == (
        SUPPORTED_FEATURES
    )
    assert ProviderFeature.LIBRARY_RADIOS not in SUPPORTED_FEATURES


async def test_the_config_has_the_unofficial_note_and_a_hidden_library_sync(
    provider: DRLydProvider,
) -> None:
    """The user is told the API is unofficial, and has no reason to switch the sync off."""
    entries = {entry.key: entry for entry in await provider.get_config_entries()}
    assert set(entries) == {"unofficial_provider_note", "library_sync_podcasts"}
    assert entries["library_sync_podcasts"].hidden
    assert entries["library_sync_podcasts"].default_value is True


def test_the_provider_serves_radio_and_podcasts_without_stream_limit(
    provider: DRLydProvider,
) -> None:
    """Radio and podcasts take part in search based lookups, and streams are not limited."""
    assert provider.supported_media_types == {MediaType.RADIO, MediaType.PODCAST}
    assert provider.max_concurrent_streams is None
    assert provider.is_streaming_provider


async def test_search_finds_podcasts_and_radio(provider: DRLydProvider, api: FakeApi) -> None:
    """A search finds the podcasts by title and description, and the channels by name."""
    api.documents = {
        **catalogue_documents([SERIES_DOCUMENT["items"]]),
        CHANNELS_URL: CHANNELS_DOCUMENT,
    }
    results = await provider.search("p3", [MediaType.PODCAST, MediaType.RADIO])
    assert [radio.item_id for radio in results.radio] == ["p3"]
    assert [podcast.name for podcast in results.podcasts] == []
    results = await provider.search("sidste time", [MediaType.PODCAST, MediaType.RADIO])
    assert [podcast.item_id for podcast in results.podcasts] == [DEN_SIDSTE_TIME]
    assert results.radio == []
    assert results.podcasts[0].provider == INSTANCE_ID


async def test_search_reads_only_what_the_media_types_ask_for(
    provider: DRLydProvider, api: FakeApi
) -> None:
    """Radio is not searched for podcasts, and podcasts are not searched for radio."""
    api.documents = {
        **catalogue_documents([SERIES_DOCUMENT["items"]]),
        CHANNELS_URL: CHANNELS_DOCUMENT,
    }
    await provider.search("genstart", [MediaType.PODCAST])
    assert api.requests == [CATALOGUE_URL]
    api.requests.clear()
    await provider.search("p3", [MediaType.RADIO])
    assert api.requests == [CHANNELS_URL]
    api.requests.clear()
    results = await provider.search("p3", [MediaType.ARTIST])
    assert (results.podcasts, results.radio, api.requests) == ([], [], [])


async def test_search_limit(provider: DRLydProvider, api: FakeApi) -> None:
    """A search returns no more of a media type than the limit."""
    api.documents = {CHANNELS_URL: CHANNELS_DOCUMENT}
    results = await provider.search("dr", [MediaType.RADIO], limit=3)
    assert len(results.radio) == 3


async def test_browse_root_lists_the_folders(provider: DRLydProvider) -> None:
    """The folders are translated by the strings of the provider."""
    folders = await provider.browse(f"{INSTANCE_ID}://")
    assert all(isinstance(folder, BrowseFolder) for folder in folders)
    assert [
        (folder.item_id, folder.name) for folder in folders if isinstance(folder, BrowseFolder)
    ] == [
        ("recently_updated", "Recently updated"),
        ("all_podcasts", "All podcasts"),
        ("live_radio", "Live radio"),
    ]
    for folder in folders:
        assert isinstance(folder, BrowseFolder)
        assert folder.translation_key == folder.item_id
        assert folder.path == f"{INSTANCE_ID}://{folder.item_id}"
        assert folder.provider == INSTANCE_ID


async def test_browse_live_radio_lists_the_channels(provider: DRLydProvider, api: FakeApi) -> None:
    """The live radio folder lists every channel, in the order of DR."""
    api.documents = {CHANNELS_URL: CHANNELS_DOCUMENT}
    items = await provider.browse(f"{INSTANCE_ID}://live_radio")
    assert [item.item_id for item in items] == [channel.slug for channel in CHANNEL_LIST]
    assert all(item.media_type == MediaType.RADIO for item in items)


async def test_browse_recently_updated_lists_the_newest_podcasts(
    provider: DRLydProvider, api: FakeApi
) -> None:
    """The recently updated folder lists the podcasts by the time of their latest episode."""
    api.documents = catalogue_documents([SERIES_DOCUMENT["items"]])
    items = await provider.browse(f"{INSTANCE_ID}://recently_updated/")
    assert [item.name for item in items][:3] == ["Radioavisen", "Genstart", "Musikquizzen"]
    assert all(item.media_type == MediaType.PODCAST for item in items)


async def test_browse_all_podcasts_lists_them_by_title(
    provider: DRLydProvider, api: FakeApi
) -> None:
    """The all podcasts folder lists every podcast ordered by title, ignoring case."""
    api.documents = catalogue_documents([SERIES_DOCUMENT["items"]])
    items = await provider.browse(f"{INSTANCE_ID}://all_podcasts")
    names = [item.name for item in items]
    assert len(names) == 8
    assert names == sorted(names, key=str.casefold)


async def test_browse_rejects_an_unknown_folder(provider: DRLydProvider) -> None:
    """A path that is no folder is invalid."""
    with pytest.raises(KeyError):
        await provider.browse(f"{INSTANCE_ID}://nowhere")


async def test_get_radio(provider: DRLydProvider, api: FakeApi) -> None:
    """A radio is read by the slug of its channel."""
    api.documents = {CHANNELS_URL: CHANNELS_DOCUMENT}
    radio = await provider.get_radio("p6beat")
    assert (radio.item_id, radio.name) == ("p6beat", "DR P6 Beat")


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
    document = dict(series_item(GENSTART), isAvailableOnDemand=False)
    api.documents[SERIES_URL.format(GENSTART)] = document
    with pytest.raises(MediaNotFoundError, match="to listen to"):
        await provider.get_podcast(GENSTART)


@pytest.mark.parametrize("item_id", NOT_AN_ID)
async def test_ids_that_are_no_production_numbers_are_not_found_without_a_request(
    provider: DRLydProvider, api: FakeApi, item_id: str
) -> None:
    """What cannot be a production number never reaches DR."""
    with pytest.raises(MediaNotFoundError):
        await provider.get_podcast(item_id)
    with pytest.raises(MediaNotFoundError):
        await collect_episodes(provider, item_id)
    with pytest.raises(MediaNotFoundError):
        await provider.get_podcast_episode(item_id)
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details(item_id, MediaType.PODCAST_EPISODE)
    assert api.requests == []


class EpisodeDocuments:
    """Adds the documents of the episodes of the fixtures to the stand-in for the API."""

    def __init__(self, api: FakeApi) -> None:
        """Initialize with the stand-in for the API."""
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


@pytest.fixture
def episodes_api(api: FakeApi) -> FakeApi:
    """Return the stand-in for the API, with the series and episodes of the fixtures."""
    EpisodeDocuments(api)
    return api


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
    cover = SERIES[DEN_SIDSTE_TIME].image_url
    assert cover is not None
    assert [image.path for image in episodes[0].metadata.images or []] == [cover]


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


async def test_a_page_that_is_not_on_the_api_of_dr_is_not_followed(
    provider: DRLydProvider, episodes_api: FakeApi
) -> None:
    """The key of the API is not sent to another host."""
    group = dict(EPISODES_DOCUMENT[DEN_SIDSTE_TIME][0], next="https://example.com/more")
    episodes_api.documents[GROUPS_URL.format(DEN_SIDSTE_TIME)] = [group]
    with pytest.raises(ProviderUnavailableError, match="not on its API"):
        await collect_episodes(provider, DEN_SIDSTE_TIME)
    assert "https://example.com/more" not in episodes_api.requests


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


async def test_episodes_that_cannot_be_played_and_duplicates_are_left_out(
    provider: DRLydProvider, episodes_api: FakeApi
) -> None:
    """An episode that is not available, has no audio or is listed twice is listed once."""
    groups = copy.deepcopy(EPISODES_DOCUMENT[DE_LOVLOESE])
    groups[0]["items"][0]["isAvailableOnDemand"] = False
    groups[0]["items"][1]["audioAssets"] = []
    groups[1]["items"].append(copy.deepcopy(groups[0]["items"][2]))
    episodes_api.documents[GROUPS_URL.format(DE_LOVLOESE)] = groups
    assert await collect_episodes(provider, DE_LOVLOESE) == ["16122593413", *UMBRELLA_EPISODES[3:]]


async def test_a_podcast_without_episodes(provider: DRLydProvider, episodes_api: FakeApi) -> None:
    """A podcast whose groups hold no episodes has none."""
    episodes_api.documents[GROUPS_URL.format(DEN_SIDSTE_TIME)] = []
    assert await collect_episodes(provider, DEN_SIDSTE_TIME) == []
    groups = [{"groupId": "x", "items": []}, {"items": None}, None, "text"]
    episodes_api.documents[GROUPS_URL.format(GENSTART)] = groups
    assert await collect_episodes(provider, GENSTART) == []


@pytest.mark.parametrize("document", [{"items": []}, "text", None, 5])
async def test_a_document_that_is_not_a_list_of_groups_is_an_error(
    provider: DRLydProvider, episodes_api: FakeApi, document: Any
) -> None:
    """DR sends a list of groups, anything else means DR is not working as it should."""
    episodes_api.documents[GROUPS_URL.format(DEN_SIDSTE_TIME)] = document
    with pytest.raises(ProviderUnavailableError):
        await collect_episodes(provider, DEN_SIDSTE_TIME)


async def test_get_podcast_episode_names_the_podcast_dr_gives_for_it(
    provider: DRLydProvider, episodes_api: FakeApi
) -> None:
    """A single episode is read from DR, at position 0, in the podcast DR names for it."""
    episode = await provider.get_podcast_episode("16122593411")
    assert episode.item_id == "16122593411"
    assert episode.position == 0
    assert episode.podcast is not None
    assert (episode.podcast.item_id, episode.podcast.name) == (
        "6640504587581",
        "Odderhusets hemmelighed",
    )
    assert episodes_api.requests == [EPISODE_URL.format("16122593411")]


async def test_get_podcast_episode_needs_a_podcast(
    provider: DRLydProvider, episodes_api: FakeApi
) -> None:
    """An episode DR names no podcast for is not found."""
    document = dict(episode_item("13332646601"), series=None)
    episodes_api.documents[EPISODE_URL.format("13332646601")] = document
    with pytest.raises(MediaNotFoundError, match="names no podcast"):
        await provider.get_podcast_episode("13332646601")


async def test_an_episode_is_read_again_every_time(
    provider: DRLydProvider, episodes_api: FakeApi
) -> None:
    """What is played is what DR serves now, so an episode is not cached."""
    await provider.get_podcast_episode("13332646601")
    await provider.get_stream_details("13332646601", MediaType.PODCAST_EPISODE)
    await provider.get_stream_details("13332646601", MediaType.PODCAST_EPISODE)
    assert episodes_api.requests == [EPISODE_URL.format("13332646601")] * 3


async def test_an_episode_without_audio_or_unknown_to_dr_is_not_found(
    provider: DRLydProvider, episodes_api: FakeApi
) -> None:
    """An episode DR has no audio for, or does not know, is not found."""
    item = dict(episode_item("13332646601"), audioAssets=[])
    episodes_api.documents[EPISODE_URL.format("13332646601")] = item
    with pytest.raises(MediaNotFoundError, match="no audio"):
        await provider.get_podcast_episode("13332646601")
    with pytest.raises(MediaNotFoundError):
        await provider.get_podcast_episode("999999999999")


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
    api.documents = {CHANNELS_URL: [{**p3, "audioAssets": assets}]}
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


def library_podcast(
    series_id: str, instance_id: str = INSTANCE_ID, in_library: bool | None = True
) -> Podcast:
    """Return a podcast of the library of Music Assistant, saved from this provider."""
    series = SERIES[series_id]
    podcast = parse_podcast(series, instance_id, DOMAIN)
    podcast.name = "Name when it was saved"
    for mapping in podcast.provider_mappings:
        mapping.in_library = in_library
    return podcast


def saved(provider: DRLydProvider, *podcasts: Podcast) -> None:
    """Make the podcasts the library items of the provider."""

    async def _iterate(_instance_id: str) -> AsyncGenerator[Podcast]:
        for podcast in podcasts:
            yield podcast

    mass: Any = provider.mass
    mass.music.podcasts.iter_library_items_by_prov_id = MagicMock(side_effect=_iterate)
    provider.report_skipped_sync_item = MagicMock()  # type: ignore[method-assign]


async def test_the_library_brings_the_saved_podcasts_up_to_date(
    provider: DRLydProvider, api: FakeApi
) -> None:
    """A saved podcast is read again from DR, which is how a changed title gets through."""
    api.documents = {
        SERIES_URL.format(GENSTART): series_item(GENSTART),
        SERIES_URL.format(MUSIKQUIZZEN): series_item(MUSIKQUIZZEN),
    }
    saved(provider, library_podcast(GENSTART), library_podcast(MUSIKQUIZZEN))
    podcasts = [podcast async for podcast in provider.get_library_podcasts()]
    assert [(podcast.item_id, podcast.name) for podcast in podcasts] == [
        (GENSTART, "Genstart"),
        (MUSIKQUIZZEN, "Musikquizzen"),
    ]
    mass: Any = provider.mass
    mass.music.podcasts.iter_library_items_by_prov_id.assert_called_once_with(INSTANCE_ID)


async def test_the_library_leaves_out_a_podcast_that_dr_no_longer_has(
    provider: DRLydProvider, api: FakeApi, caplog: pytest.LogCaptureFixture
) -> None:
    """A podcast DR says is gone leaves the library, which is logged."""
    api.documents = {SERIES_URL.format(MUSIKQUIZZEN): series_item(MUSIKQUIZZEN)}
    saved(provider, library_podcast(GENSTART), library_podcast(MUSIKQUIZZEN))
    with caplog.at_level("WARNING"):
        podcasts = [podcast async for podcast in provider.get_library_podcasts()]
    assert [podcast.item_id for podcast in podcasts] == [MUSIKQUIZZEN]
    assert GENSTART in caplog.text
    provider.report_skipped_sync_item.assert_not_called()  # type: ignore[attr-defined]


async def test_the_library_keeps_a_podcast_while_dr_cannot_be_reached(
    provider: DRLydProvider, api: FakeApi
) -> None:
    """A podcast that cannot be read now is reported as skipped, which keeps it in the library."""
    error = ProviderUnavailableError("Could not load")
    api.errors[SERIES_URL.format(GENSTART)] = error
    api.documents = {SERIES_URL.format(MUSIKQUIZZEN): series_item(MUSIKQUIZZEN)}
    saved(provider, library_podcast(GENSTART), library_podcast(MUSIKQUIZZEN))
    podcasts = [podcast async for podcast in provider.get_library_podcasts()]
    assert [podcast.item_id for podcast in podcasts] == [MUSIKQUIZZEN]
    provider.report_skipped_sync_item.assert_called_once_with(  # type: ignore[attr-defined]
        MediaType.PODCAST, GENSTART, error
    )


async def test_the_library_only_brings_up_to_date_what_was_saved_from_this_provider(
    provider: DRLydProvider, api: FakeApi
) -> None:
    """A mapping of another instance, or one that is not in the library, is left alone."""
    api.documents = {SERIES_URL.format(GENSTART): series_item(GENSTART)}
    saved(
        provider,
        library_podcast(MUSIKQUIZZEN, instance_id="dr_lyd--other"),
        library_podcast(DEN_SIDSTE_TIME, in_library=False),
        library_podcast(MUSIKQUIZZEN, in_library=None),
        library_podcast(GENSTART),
    )
    podcasts = [podcast async for podcast in provider.get_library_podcasts()]
    assert [podcast.item_id for podcast in podcasts] == [GENSTART]
    assert api.requests == [SERIES_URL.format(GENSTART)]


@pytest.fixture
def now_playing_api(api: FakeApi) -> FakeApi:
    """Return the stand-in for the API, with the channels and what they play."""
    api.documents = {
        CHANNELS_URL: CHANNELS_DOCUMENT,
        TRACKS_URL.format(CHANNELS["p3"].urn): TRACKS_P3,
        TRACKS_URL.format(CHANNELS["p2"].urn): TRACKS_P2,
        TRACKS_URL.format(CHANNELS["p8jazz"].urn): {"items": []},
        PROGRAMMES_URL: PROGRAMMES,
    }
    return api


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
        "music_assistant.providers.dr_lyd.provider.utc_timestamp",
        lambda: epoch("22:28:36") + STREAM_DELAY_SECONDS - 1,
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
    assert details.stream_metadata == StreamMetadata(title="DR P8 Jazz", artist=None)


async def test_the_last_metadata_stays_when_dr_cannot_be_read(
    provider: DRLydProvider, now_playing_api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When DR cannot say what plays, what was shown before stays."""
    listening_at(monkeypatch, "22:30:00")
    details = stream_details(provider)
    shown = StreamMetadata(title="Shown before")
    details.stream_metadata = shown
    now_playing_api.errors[TRACKS_URL.format(CHANNELS["p3"].urn)] = ProviderUnavailableError("down")
    await provider._update_stream_metadata(details, 0)
    assert details.stream_metadata is shown
    del now_playing_api.errors[TRACKS_URL.format(CHANNELS["p3"].urn)]
    now_playing_api.errors[PROGRAMMES_URL] = MediaNotFoundError("gone")
    await provider._update_stream_metadata(details, 0)
    assert details.stream_metadata is shown


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


async def test_each_document_is_read_once_per_half_minute(
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


async def test_the_catalogue_does_not_follow_a_page_that_is_not_on_the_api_of_dr(
    provider: DRLydProvider, api: FakeApi
) -> None:
    """The key of the API is not sent to another host."""
    page = {"items": SERIES_DOCUMENT["items"], "next": "https://example.com/series?offset=8"}
    api.documents = {CATALOGUE_URL: page}
    with pytest.raises(ProviderUnavailableError, match="not on its API"):
        await provider._get_catalogue()
    assert api.requests == [CATALOGUE_URL]


async def test_the_catalogue_leaves_out_series_that_are_not_available_and_duplicates(
    provider: DRLydProvider, api: FakeApi
) -> None:
    """A series that is not available, or listed twice, is listed once at most."""
    items = copy.deepcopy(SERIES_DOCUMENT["items"][:3])
    items[1]["isAvailableOnDemand"] = False
    items.append(dict(items[0], title="A copy"))
    api.documents = catalogue_documents([items])
    catalogue = await provider._get_catalogue()
    assert [series.title for series in catalogue] == [
        "Den sidste time",
        "Sara & Monopolet - podcast",
    ]


@pytest.mark.parametrize("document", [{"items": []}, {"message": "maintenance"}, [], None])
async def test_a_catalogue_without_series_is_an_error(
    provider: DRLydProvider, api: FakeApi, document: Any
) -> None:
    """DR listing no podcasts is no catalogue, it is kept out of the cache."""
    api.documents = {CATALOGUE_URL: document}
    with pytest.raises(ProviderUnavailableError, match="no podcasts"):
        await provider._get_catalogue()
    assert cache_entries(provider) == {}


async def test_channels_without_a_channel_are_an_error(
    provider: DRLydProvider, api: FakeApi
) -> None:
    """A channels document without a channel is kept out of the cache."""
    for document in ([{"slug": "p9"}], {"error": "maintenance"}, []):
        api.documents = {CHANNELS_URL: document}
        with pytest.raises(ProviderUnavailableError, match="no radio channels"):
            await provider._get_channels()
    assert cache_entries(provider) == {}


async def test_what_is_read_is_cached_for_how_long_it_stays_the_same(
    provider: DRLydProvider, episodes_api: FakeApi
) -> None:
    """Every lookup that repeats is cached, for the time its content stays the same."""
    episodes_api.documents.update(catalogue_documents([SERIES_DOCUMENT["items"]]))
    episodes_api.documents.update(
        {
            CHANNELS_URL: CHANNELS_DOCUMENT,
            PROGRAMMES_URL: PROGRAMMES,
            TRACKS_URL.format(CHANNELS["p3"].urn): TRACKS_P3,
        }
    )
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
        f"_get_episodes.{DEN_SIDSTE_TIME}": 900,
        "_get_channels": 12 * 3600,
        "_get_programmes": 30,
        f"_get_tracks.{CHANNELS['p3'].urn}": 30,
    }


async def test_a_second_lookup_is_served_from_the_cache(
    provider: DRLydProvider, episodes_api: FakeApi
) -> None:
    """What was read is returned again from the cache, the way it came out of its models."""
    episodes_api.documents.update(catalogue_documents([SERIES_DOCUMENT["items"]]))
    catalogue = await provider._get_catalogue()
    series = await provider._get_series(GENSTART)
    episodes = await provider._get_episodes(DEN_SIDSTE_TIME)
    await settle()
    requests = len(episodes_api.requests)
    assert await provider._get_catalogue() == catalogue
    assert await provider._get_series(GENSTART) == series
    assert await provider._get_episodes(DEN_SIDSTE_TIME) == episodes
    assert len(episodes_api.requests) == requests


async def test_a_series_that_is_not_in_the_catalogue_can_still_be_read_alone(
    provider: DRLydProvider, api: FakeApi
) -> None:
    """A series is parsed the same, whether it comes from the catalogue or from its own url."""
    api.documents = {SERIES_URL.format(GENSTART): series_item(GENSTART)}
    assert await provider._get_series(GENSTART) == parse_series(series_item(GENSTART))
