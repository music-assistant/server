"""Test that the Deezer provider hides explicit content as the account or source asks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock

import pytest
from deezer_python_gql import GraphQLClientError
from deezer_python_gql.generated.get_podcast import GetPodcast
from deezer_python_gql.generated.get_podcast_episode import GetPodcastEpisode
from deezer_python_gql.generated.get_podcast_episodes_by_ids import GetPodcastEpisodesByIds
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    MediaItemMetadata,
    Podcast,
    ProviderMapping,
    RecommendationFolder,
    SearchResults,
    Track,
)
from music_assistant_models.unique_list import UniqueList

from music_assistant.providers.deezer.constants import (
    FLOW_PLAYLIST_ID,
    USER_TOP_TRACKS_PLAYLIST_ID,
)
from music_assistant.providers.deezer.explicit_filter import (
    CONF_EXPLICIT_CONTENT,
    CONF_HIDE_EXPLICIT_ALBUMS,
    ExplicitMode,
    filtered_media_types,
    gw_song_is_explicit,
)
from music_assistant.providers.deezer.media import DeezerMediaManager
from music_assistant.providers.deezer.provider import DeezerProvider
from music_assistant.providers.deezer.streaming import DeezerStreamingManager
from tests.common import use_real_create_task

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

INSTANCE_ID = "deezer--test123"


def _mapping(item_id: str) -> ProviderMapping:
    """Return the provider mapping of an item on the test instance."""
    return ProviderMapping(item_id=item_id, provider_domain="deezer", provider_instance=INSTANCE_ID)


def _track(track_id: str, explicit: bool | None) -> Track:
    """Return a minimal Deezer track with the given explicit flag."""
    return Track(
        item_id=track_id,
        provider=INSTANCE_ID,
        name=track_id,
        metadata=MediaItemMetadata(explicit=explicit),
        provider_mappings={_mapping(track_id)},
    )


def _album(album_id: str, explicit: bool) -> Album:
    """Return a minimal Deezer album with the given explicit flag."""
    return Album(
        item_id=album_id,
        provider=INSTANCE_ID,
        name=album_id,
        metadata=MediaItemMetadata(explicit=explicit),
        provider_mappings={_mapping(album_id)},
    )


CLEAN = _track("clean", False)
UNLABELLED = _track("unlabelled", None)
EXPLICIT = _track("explicit", True)
ALL_TRACKS = [CLEAN, EXPLICIT, UNLABELLED]
VISIBLE_TRACKS = [CLEAN, UNLABELLED]


def _podcast(podcast_id: str, explicit: bool) -> Podcast:
    """Return a minimal Deezer podcast with the given explicit flag."""
    return Podcast(
        item_id=podcast_id,
        provider=INSTANCE_ID,
        name=podcast_id,
        metadata=MediaItemMetadata(explicit=explicit),
        provider_mappings={_mapping(podcast_id)},
    )


CLEAN_ALBUM = _album("clean_album", False)
EXPLICIT_ALBUM = _album("explicit_album", True)
ALL_ALBUMS = [CLEAN_ALBUM, EXPLICIT_ALBUM]

CLEAN_PODCAST = _podcast("clean_podcast", False)
EXPLICIT_PODCAST = _podcast("explicit_podcast", True)

EXPLICIT_SONG = {
    "SNG_ID": "1",
    "DURATION": "180",
    "EXPLICIT_LYRICS": "1",
    "EXPLICIT_TRACK_CONTENT": {"EXPLICIT_LYRICS_STATUS": 1, "EXPLICIT_COVER_STATUS": 0},
}


def _configure(provider: DeezerProvider, setting: str) -> None:
    """Set the explicit content option of the provider instance."""
    provider.config.get_value.side_effect = lambda key, default=None: {  # type: ignore[attr-defined]
        "log_level": "GLOBAL",
        CONF_EXPLICIT_CONTENT: setting,
    }.get(key, default)


def _account_level(provider: DeezerProvider, level: str | None) -> Mock:
    """Make Deezer report this explicit level for the signed-in account."""
    client = Mock(execute=AsyncMock(return_value=Mock()))
    client.get_data.return_value = {"me": {"explicitLevel": level}}
    provider.gql_client = client
    return client


def _managers(provider: DeezerProvider) -> None:
    """Stub the media and browse managers to return every test track and album."""
    provider.media_manager = Mock()
    provider.media_manager.get_album_tracks = AsyncMock(return_value=list(ALL_TRACKS))
    provider.media_manager.get_similar_tracks = AsyncMock(return_value=list(ALL_TRACKS))
    provider.media_manager.get_track = AsyncMock(return_value=EXPLICIT)
    provider.media_manager.get_artist_albums = AsyncMock(return_value=list(ALL_ALBUMS))
    provider.media_manager.get_album = AsyncMock(return_value=EXPLICIT_ALBUM)
    provider.media_manager.search = AsyncMock(
        return_value=SearchResults(albums=list(ALL_ALBUMS), tracks=list(ALL_TRACKS))
    )
    provider.media_manager.get_library_tracks = lambda: _iterate(ALL_TRACKS)
    provider.media_manager.get_library_albums = lambda: _iterate(ALL_ALBUMS)
    provider.media_manager.get_library_podcasts = lambda: _iterate(
        [CLEAN_PODCAST, EXPLICIT_PODCAST]
    )
    provider.browse_manager = Mock()
    provider.browse_manager.get_playlist_tracks = AsyncMock(return_value=list(ALL_TRACKS))
    provider.browse_manager.browse = AsyncMock(side_effect=lambda *_args: list(ALL_TRACKS))
    provider.browse_manager.get_recommendations = AsyncMock(
        side_effect=lambda: [_recommendation_row()]
    )
    provider.browse_manager.get_recommendation_items = AsyncMock(
        side_effect=lambda _item_id: UniqueList(ALL_TRACKS)
    )


async def _iterate[T](items: list[T]) -> AsyncGenerator[T]:
    """Yield the items, standing in for a paged library listing."""
    for item in items:
        yield item


async def _collect[T](generator: AsyncGenerator[T]) -> list[T]:
    """Return everything a library listing yields."""
    return [item async for item in generator]


def _recommendation_row() -> RecommendationFolder:
    """Return a recommendation row holding every test track."""
    return RecommendationFolder(
        item_id="row", provider=INSTANCE_ID, name="Row", items=UniqueList(ALL_TRACKS)
    )


def _cache(provider: DeezerProvider, previous_state: str | None) -> AsyncMock:
    """Give the provider a cache that remembers the given previous filter state."""
    cache = AsyncMock()
    cache.get.return_value = previous_state
    provider.mass.cache = cache
    provider.mass.music.domain = "music"
    return cache


def _streaming(mode: ExplicitMode) -> DeezerStreamingManager:
    """Return a streaming manager for a provider in the given explicit mode."""
    provider = Mock(explicit_mode=mode, instance_id=INSTANCE_ID)
    url_details = {"sources": [{"url": "https://media"}], "format": "MP3_128"}
    provider.gw_client.get_deezer_track_urls = AsyncMock(return_value=(url_details, EXPLICIT_SONG))
    return DeezerStreamingManager(provider)


def _episode(podcast_explicit: bool) -> Mock:
    """Return a podcast episode as the GraphQL client parses it."""
    episode = Mock(duration=600)
    episode.media.url = "https://media"
    episode.media.codec = None
    episode.podcast.is_explicit = podcast_explicit
    return episode


# -- Settings --


async def test_config_entries_serialize(provider: DeezerProvider) -> None:
    """Both options reach the frontend as plain values, the albums one only when hiding."""
    entries = {entry.key: entry.to_dict() for entry in await provider.get_config_entries()}

    explicit = entries[CONF_EXPLICIT_CONTENT]
    assert explicit["default_value"] == "account"
    assert [option["value"] for option in explicit["options"]] == [
        "account",
        "hide",
        "no_recommendations",
        "show",
    ]
    albums = entries[CONF_HIDE_EXPLICIT_ALBUMS]
    assert albums["default_value"] is False
    assert albums["depends_on"] == CONF_EXPLICIT_CONTENT
    assert albums["depends_on_value_not"] == "show"


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        ("EXPLICIT_HIDE", ExplicitMode.HIDE),
        ("EXPLICIT_NO_RECOMMENDATION", ExplicitMode.NO_RECOMMENDATIONS),
        ("EXPLICIT_DISPLAY", ExplicitMode.SHOW),
        ("UNKNOWN", ExplicitMode.SHOW),
        (None, ExplicitMode.SHOW),
    ],
)
async def test_follows_account_level(
    provider: DeezerProvider, level: str | None, expected: ExplicitMode
) -> None:
    """By default the source follows the explicit level set on deezer.com."""
    _account_level(provider, level)

    assert await provider._resolve_explicit_mode() == expected


@pytest.mark.parametrize(
    ("setting", "expected"),
    [
        ("hide", ExplicitMode.HIDE),
        ("no_recommendations", ExplicitMode.NO_RECOMMENDATIONS),
        ("show", ExplicitMode.SHOW),
    ],
)
async def test_source_setting_overrides_account(
    provider: DeezerProvider, setting: str, expected: ExplicitMode
) -> None:
    """An explicit choice on the source wins over the account and asks Deezer nothing."""
    _configure(provider, setting)
    client = _account_level(provider, "EXPLICIT_DISPLAY" if setting != "show" else "EXPLICIT_HIDE")

    assert await provider._resolve_explicit_mode() == expected
    client.execute.assert_not_called()


async def test_unreadable_account_level_shows_explicit(provider: DeezerProvider) -> None:
    """When Deezer refuses to return the level, the source keeps working, unfiltered."""
    provider.gql_client = Mock(execute=AsyncMock(side_effect=GraphQLClientError("denied")))

    assert await provider._resolve_explicit_mode() == ExplicitMode.SHOW


# -- Listings --


async def test_hide_filters_every_listing(provider: DeezerProvider) -> None:
    """Hiding drops explicit tracks from albums, playlists, search and similar tracks."""
    _managers(provider)
    provider.explicit_mode = ExplicitMode.HIDE

    assert await provider.get_album_tracks("1") == VISIBLE_TRACKS
    assert await provider.get_playlist_tracks("123") == VISIBLE_TRACKS
    assert await provider.get_similar_tracks("1") == VISIBLE_TRACKS
    results = await provider.search("query", [MediaType.TRACK])
    assert list(results.tracks) == VISIBLE_TRACKS


async def test_hide_refuses_an_explicit_track(provider: DeezerProvider) -> None:
    """A hidden track can not be fetched by its id either."""
    _managers(provider)
    provider.explicit_mode = ExplicitMode.HIDE

    with pytest.raises(MediaNotFoundError):
        await provider.get_track("explicit")


async def test_no_recommendations_filters_only_recommendations(
    provider: DeezerProvider,
) -> None:
    """Without recommendations, explicit tracks stay in albums and the user's own lists."""
    _managers(provider)
    provider.explicit_mode = ExplicitMode.NO_RECOMMENDATIONS

    assert await provider.get_album_tracks("1") == ALL_TRACKS
    assert await provider.get_playlist_tracks("123") == ALL_TRACKS
    assert await provider.get_playlist_tracks(USER_TOP_TRACKS_PLAYLIST_ID) == ALL_TRACKS
    assert await provider.get_track("explicit") == EXPLICIT
    assert await provider.get_playlist_tracks(FLOW_PLAYLIST_ID) == VISIBLE_TRACKS
    assert await provider.get_similar_tracks("1") == VISIBLE_TRACKS


async def test_show_filters_nothing(provider: DeezerProvider) -> None:
    """Showing explicit content leaves every listing as Deezer returns it."""
    _managers(provider)

    assert await provider.get_album_tracks("1") == ALL_TRACKS
    assert await provider.get_playlist_tracks(FLOW_PLAYLIST_ID) == ALL_TRACKS
    assert await provider.get_track("explicit") == EXPLICIT


# -- Albums --


async def test_explicit_albums_shown_by_default(provider: DeezerProvider) -> None:
    """Without the albums option an explicit album stays, only its explicit tracks go."""
    _managers(provider)
    provider.explicit_mode = ExplicitMode.HIDE

    assert await provider.get_artist_albums("1") == ALL_ALBUMS
    assert await provider.get_album("explicit_album") == EXPLICIT_ALBUM


async def test_hide_explicit_albums(provider: DeezerProvider) -> None:
    """With the albums option explicit albums are hidden from listings, search and by id."""
    _managers(provider)
    provider.explicit_mode = ExplicitMode.HIDE
    provider.explicit_media_types = filtered_media_types(hide_albums=True)

    assert await provider.get_artist_albums("1") == [CLEAN_ALBUM]
    results = await provider.search("query", [MediaType.ALBUM, MediaType.TRACK])
    assert list(results.albums) == [CLEAN_ALBUM]
    assert list(results.tracks) == VISIBLE_TRACKS
    with pytest.raises(MediaNotFoundError):
        await provider.get_album("explicit_album")


async def test_hide_explicit_albums_needs_hiding(provider: DeezerProvider) -> None:
    """The albums option does nothing while explicit content is shown."""
    _managers(provider)
    provider.explicit_media_types = filtered_media_types(hide_albums=True)

    assert await provider.get_artist_albums("1") == ALL_ALBUMS


# -- Playback --


async def test_hide_refuses_explicit_playback() -> None:
    """An explicit track that reaches playback some other way is still refused."""
    with pytest.raises(MediaNotFoundError):
        await _streaming(ExplicitMode.HIDE).get_stream_details("1", MediaType.TRACK)


@pytest.mark.parametrize("mode", [ExplicitMode.SHOW, ExplicitMode.NO_RECOMMENDATIONS])
async def test_explicit_playback_allowed_unless_hidden(mode: ExplicitMode) -> None:
    """Only hiding refuses playback, a track the user picked plays otherwise."""
    details = await _streaming(mode).get_stream_details("1", MediaType.TRACK)

    assert details.item_id == "1"


@pytest.mark.parametrize(
    ("song", "expected"),
    [
        (EXPLICIT_SONG, True),
        ({"EXPLICIT_TRACK_CONTENT": {"EXPLICIT_LYRICS_STATUS": 4}}, True),
        ({"EXPLICIT_LYRICS": "1"}, True),
        ({"EXPLICIT_LYRICS": "0", "EXPLICIT_TRACK_CONTENT": {"EXPLICIT_LYRICS_STATUS": 0}}, False),
        ({"EXPLICIT_TRACK_CONTENT": {"EXPLICIT_LYRICS_STATUS": 3}}, False),
        ({}, False),
        ({"EXPLICIT_LYRICS": "unexpected"}, False),
    ],
)
def test_gw_song_explicit_flag(song: dict[str, Any], expected: bool) -> None:
    """Explicit and partially explicit songs are flagged, edited and unknown ones are not."""
    assert gw_song_is_explicit(song) is expected


@pytest.mark.parametrize(
    ("mode", "podcast_explicit", "refused"),
    [
        (ExplicitMode.HIDE, True, True),
        (ExplicitMode.HIDE, False, False),
        (ExplicitMode.SHOW, True, False),
    ],
)
async def test_explicit_podcast_episode_playback(
    mode: ExplicitMode, podcast_explicit: bool, refused: bool
) -> None:
    """An episode of an explicit podcast is refused at playback only when hiding."""
    provider = Mock(explicit_mode=mode, instance_id=INSTANCE_ID)
    provider.gql_client.get_podcast_episode = AsyncMock(return_value=_episode(podcast_explicit))
    streaming = DeezerStreamingManager(provider)

    if refused:
        with pytest.raises(MediaNotFoundError):
            await streaming.get_stream_details("1", MediaType.PODCAST_EPISODE)
    else:
        details = await streaming.get_stream_details("1", MediaType.PODCAST_EPISODE)
        assert details.item_id == "1"


async def test_explicit_audiobook_playback_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """An audiobook with an explicit chapter is refused at playback when hiding."""
    chapters = [
        Mock(node=Mock(id="1", duration=60, is_explicit=False)),
        Mock(node=Mock(id="2", duration=60, is_explicit=True)),
    ]
    monkeypatch.setattr(
        "music_assistant.providers.deezer.streaming.fetch_all_audiobook_chapter_edges",
        AsyncMock(return_value=chapters),
    )
    streaming = DeezerStreamingManager(Mock(explicit_mode=ExplicitMode.HIDE))

    with pytest.raises(MediaNotFoundError):
        await streaming.get_stream_details("book", MediaType.AUDIOBOOK)


# -- Cache --


async def test_filter_change_drops_cached_results(provider: DeezerProvider) -> None:
    """Searches and listings cached under another filter are dropped, here and globally."""
    cache = _cache(provider, "show")
    provider.explicit_mode = ExplicitMode.HIDE

    await provider._drop_results_cached_under_other_filter()

    deleted = {call.kwargs["provider"] for call in cache.delete.await_args_list}
    assert deleted == {INSTANCE_ID, "music"}
    cleared = {call.kwargs["provider_filter"] for call in cache.clear.await_args_list}
    assert cleared == {f"{INSTANCE_ID}://", "library://"}
    assert cache.set.await_args.args[1] == "hide"


async def test_albums_option_change_drops_cached_results(provider: DeezerProvider) -> None:
    """Turning the albums option on clears cached results filtered without it."""
    cache = _cache(provider, "hide")
    provider.explicit_mode = ExplicitMode.HIDE
    provider.explicit_media_types = filtered_media_types(hide_albums=True)

    await provider._drop_results_cached_under_other_filter()

    cache.delete.assert_awaited()
    assert cache.set.await_args.args[1] == "hide+albums"


async def test_unchanged_filter_keeps_cached_results(provider: DeezerProvider) -> None:
    """A restart with the same filter leaves the cache alone."""
    cache = _cache(provider, "hide")
    provider.explicit_mode = ExplicitMode.HIDE

    await provider._drop_results_cached_under_other_filter()

    cache.delete.assert_not_called()
    cache.clear.assert_not_called()


# -- Browse, recommendations and library --


@pytest.mark.parametrize(
    ("mode", "recommendation_folder", "own_items"),
    [
        (ExplicitMode.HIDE, VISIBLE_TRACKS, VISIBLE_TRACKS),
        (ExplicitMode.NO_RECOMMENDATIONS, VISIBLE_TRACKS, ALL_TRACKS),
        (ExplicitMode.SHOW, ALL_TRACKS, ALL_TRACKS),
    ],
)
async def test_browse(
    provider: DeezerProvider,
    mode: ExplicitMode,
    recommendation_folder: list[Track],
    own_items: list[Track],
) -> None:
    """Only Deezer's recommendation folders count as recommendations when browsing."""
    _managers(provider)
    provider.explicit_mode = mode

    for folder in ("Made For You/Flow", "Explore/Charts", "Shaker", "Discover Audiobooks"):
        assert await provider.browse(f"{INSTANCE_ID}://{folder}") == recommendation_folder
    for path in (f"{INSTANCE_ID}://tracks", f"{INSTANCE_ID}://Recently Played", INSTANCE_ID):
        assert await provider.browse(path) == own_items


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (ExplicitMode.HIDE, VISIBLE_TRACKS),
        (ExplicitMode.NO_RECOMMENDATIONS, VISIBLE_TRACKS),
        (ExplicitMode.SHOW, ALL_TRACKS),
    ],
)
async def test_recommendations(
    provider: DeezerProvider, mode: ExplicitMode, expected: list[Track]
) -> None:
    """Both hiding modes drop explicit items from the recommendation rows and their items."""
    _managers(provider)
    provider.explicit_mode = mode

    rows = await provider.get_recommendations()
    assert list(rows[0].items) == expected
    assert list(await provider.get_recommendation_items("row")) == expected


@pytest.mark.parametrize(
    ("mode", "hide_albums", "tracks", "albums", "podcasts"),
    [
        (ExplicitMode.HIDE, False, VISIBLE_TRACKS, ALL_ALBUMS, [CLEAN_PODCAST]),
        (ExplicitMode.HIDE, True, VISIBLE_TRACKS, [CLEAN_ALBUM], [CLEAN_PODCAST]),
        (
            ExplicitMode.NO_RECOMMENDATIONS,
            True,
            ALL_TRACKS,
            ALL_ALBUMS,
            [CLEAN_PODCAST, EXPLICIT_PODCAST],
        ),
    ],
)
async def test_library_sync(
    provider: DeezerProvider,
    mode: ExplicitMode,
    hide_albums: bool,
    tracks: list[Track],
    albums: list[Album],
    podcasts: list[Podcast],
) -> None:
    """Library sync leaves explicit items out only when hiding."""
    _managers(provider)
    provider.explicit_mode = mode
    provider.explicit_media_types = filtered_media_types(hide_albums=hide_albums)

    assert await _collect(provider.get_library_tracks()) == tracks
    assert await _collect(provider.get_library_albums()) == albums
    assert await _collect(provider.get_library_podcasts()) == podcasts


# -- Podcast episodes --


def _episode_data(episode_id: str) -> dict[str, Any]:
    """Return a podcast episode as the GraphQL API returns it."""
    return {
        "__typename": "PodcastEpisode",
        "id": episode_id,
        "title": episode_id,
        "description": None,
        "duration": 600,
        "cover": None,
        "publicationDate": "2025-07-10",
        "media": {"url": "https://media", "codec": None},
    }


def _podcast_data(explicit: bool) -> dict[str, Any]:
    """Return a podcast as the GraphQL API returns it."""
    return {
        "__typename": "Podcast",
        "id": "1234",
        "displayTitle": "Show",
        "cover": None,
        "description": None,
        "isExplicit": explicit,
        "isFavorite": False,
        "type": "EPISODIC",
    }


def _real_media_manager(provider: DeezerProvider, cached: dict[str, Any] | None = None) -> Mock:
    """
    Wire a real media manager to an empty cache and return the GraphQL client stub.

    :param provider: The provider under test.
    :param cached: Parsed episodes already in the per-episode cache, by cache key.
    """
    cache = Mock()
    cache.get = AsyncMock(side_effect=lambda key, **_kwargs: (cached or {}).get(key))
    cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    cache.set = AsyncMock()
    provider.mass.cache = cache
    use_real_create_task(provider.mass)
    client = Mock()
    # the episode listing also asks for the play positions, there are none
    client.get_podcast_episode_bookmarks = AsyncMock(return_value=None)
    provider.gql_client = client
    provider.media_manager = DeezerMediaManager(provider)
    return client


def _serve_podcast(client: Mock, explicit: bool, episode_ids: list[str]) -> None:
    """Make the GraphQL client serve a podcast and its episodes."""
    podcast = {
        **_podcast_data(explicit),
        "rawEpisodes": episode_ids,
        "isAdvertisingAllowed": True,
        "isDownloadAllowed": True,
        "rights": {"ads": {"available": True}, "sub": {"available": True}},
        "episodes": {"edges": [], "pageInfo": {"hasNextPage": False, "endCursor": None}},
    }
    client.get_podcast = AsyncMock(
        return_value=GetPodcast.model_validate({"podcast": podcast}).podcast
    )
    episodes = {"podcastEpisodesByIds": [_episode_data(eid) for eid in episode_ids]}
    client.get_podcast_episodes_by_ids = AsyncMock(
        return_value=GetPodcastEpisodesByIds.model_validate(episodes).podcast_episodes_by_ids
    )


@pytest.mark.parametrize(
    ("mode", "podcast_explicit", "visible"),
    [
        (ExplicitMode.HIDE, True, False),
        (ExplicitMode.HIDE, False, True),
        (ExplicitMode.SHOW, True, True),
    ],
)
async def test_episodes_of_explicit_podcast(
    provider: DeezerProvider, mode: ExplicitMode, podcast_explicit: bool, visible: bool
) -> None:
    """Episodes take the explicit flag of their podcast and are hidden with it."""
    client = _real_media_manager(provider)
    _serve_podcast(client, podcast_explicit, ["ep_1", "ep_2"])
    provider.explicit_mode = mode

    episodes = await _collect(provider.get_podcast_episodes("1234"))

    assert [episode.item_id for episode in episodes] == (["ep_1", "ep_2"] if visible else [])


async def test_cached_episodes_take_the_podcast_flag(provider: DeezerProvider) -> None:
    """An episode cached before it carried the explicit flag gets it from its podcast."""
    unflagged = {
        "podcast_episode.ep_1": {
            "item_id": "ep_1",
            "provider": INSTANCE_ID,
            "name": "ep_1",
            "position": 0,
            "duration": 600,
            "podcast": {"item_id": "1234", "provider": INSTANCE_ID, "name": "Show"},
            "provider_mappings": [
                {
                    "item_id": "ep_1",
                    "provider_domain": "deezer",
                    "provider_instance": INSTANCE_ID,
                }
            ],
            "media_type": "podcast_episode",
        }
    }
    client = _real_media_manager(provider, cached=unflagged)
    _serve_podcast(client, True, ["ep_1"])

    episodes = await _collect(provider.get_podcast_episodes("1234"))

    assert [episode.metadata.explicit for episode in episodes] == [True]
    client.get_podcast_episodes_by_ids.assert_not_called()


@pytest.mark.parametrize("podcast_explicit", [True, False])
async def test_single_episode_takes_the_podcast_flag(
    provider: DeezerProvider, podcast_explicit: bool
) -> None:
    """An episode fetched by its id is flagged from its podcast, and hidden when explicit."""
    client = _real_media_manager(provider)
    episode = {
        **_episode_data("ep_1"),
        "url": {"__typename": "Url", "webUrl": "https://www.deezer.com/episode/ep_1"},
        "podcast": _podcast_data(podcast_explicit),
    }
    client.get_podcast_episode = AsyncMock(
        return_value=GetPodcastEpisode.model_validate({"podcastEpisode": episode}).podcast_episode
    )
    provider.explicit_mode = ExplicitMode.HIDE

    if podcast_explicit:
        with pytest.raises(MediaNotFoundError):
            await provider.get_podcast_episode("ep_1")
    else:
        parsed = await provider.get_podcast_episode("ep_1")
        assert parsed.metadata.explicit is False
