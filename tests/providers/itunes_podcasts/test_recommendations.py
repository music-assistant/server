"""Test the iTunes Podcasts recommendations."""

import asyncio
import json
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from music_assistant_models.media_items import Podcast, ProviderMapping

from music_assistant.providers.itunes_podcasts import (
    SUPPORTED_FEATURES,
    ITunesPodcastsProvider,
)
from music_assistant.providers.itunes_podcasts.constants import (
    LIBRARY_RECOMMENDATIONS_CACHE_EXPIRATION,
    RECOMMENDATION_ROW_FOR_YOU,
    RECOMMENDATION_ROW_TOP_PODCASTS,
    TOP_PODCASTS_NUM_PAGES,
    TOP_PODCASTS_ROTATION,
)
from music_assistant.providers.itunes_podcasts.schema import (
    MappingDetails,
    PodcastSearchResult,
    TopPodcastsHelper,
)

TIME = "music_assistant.providers.itunes_podcasts.time.time"


@pytest.fixture
def mass_mock() -> Mock:
    """Return a mock MusicAssistant instance."""
    mass = Mock()
    mass.http_session = AsyncMock()
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.set = AsyncMock()
    _set_library(mass, [])
    return mass


def _set_library(mass: Mock, podcasts: list[Podcast]) -> None:
    async def _iter_library_items(_instance_id: str) -> AsyncGenerator[Podcast]:
        for podcast in podcasts:
            yield podcast

    mass.music.podcasts.iter_library_items_by_prov_id = _iter_library_items


def _library_podcast(name: str, feed_url: str, details: MappingDetails | None = None) -> Podcast:
    return Podcast(
        item_id=name,
        provider="library",
        name=name,
        provider_mappings={
            ProviderMapping(
                item_id=feed_url,
                provider_domain="itunes_podcasts",
                provider_instance="itunes_podcasts_test",
                details=json.dumps(details.to_dict()) if details else None,
            )
        },
    )


def _details(mapping: ProviderMapping) -> MappingDetails:
    assert mapping.details is not None
    return MappingDetails.from_dict(json.loads(mapping.details))


def _result(
    collection_id: int,
    genre_ids: list[str] | None = None,
    explicit: bool = False,
    name: str | None = None,
) -> PodcastSearchResult:
    return PodcastSearchResult(
        collection_id=collection_id,
        track_name=name or f"Podcast {collection_id}",
        artist_name="Publisher",
        feed_url=f"https://example.com/{collection_id}.xml",
        genre_ids=genre_ids or [],
        # like Apple: collectionExplicitness is "notExplicit" even for explicit podcasts
        collection_explicitness="notExplicit",
        track_explicitness="explicit" if explicit else "notExplicit",
    )


def _http_response(status: int, body: bytes) -> MagicMock:
    response = Mock(status=status, read=AsyncMock(return_value=body))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    return context


@pytest.fixture
def manifest_mock() -> Mock:
    """Return a mock provider manifest."""
    manifest = Mock()
    manifest.domain = "itunes_podcasts"
    return manifest


@pytest.fixture
def config_mock() -> Mock:
    """Return a mock provider config."""
    config = Mock()
    config.name = "iTunes Podcasts Test"
    config.instance_id = "itunes_podcasts_test"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {
        "locale": "us",
        "explicit": True,
        "num_episodes": 0,
        "log_level": "INFO",
    }.get(key, default)
    return config


@pytest.fixture
async def provider(
    mass_mock: Mock, manifest_mock: Mock, config_mock: Mock
) -> ITunesPodcastsProvider:
    """Return an ITunesPodcastsProvider instance."""
    provider = ITunesPodcastsProvider(mass_mock, manifest_mock, config_mock, SUPPORTED_FEATURES)
    await provider.handle_async_init()
    return provider


def _search_result() -> PodcastSearchResult:
    """Return a minimal top-podcast search result."""
    return PodcastSearchResult(
        track_name="Test Podcast",
        artist_name="Test Publisher",
        feed_url="https://example.com/feed.xml",
        artwork_url_600="https://example.com/artwork600.jpg",
    )


async def test_get_recommendations_static_row_without_backend_calls(
    provider: ITunesPodcastsProvider, mass_mock: Mock
) -> None:
    """get_recommendations() returns the static rows without any backend or cache I/O."""
    result = await provider.get_recommendations()

    assert [folder.item_id for folder in result] == [
        RECOMMENDATION_ROW_TOP_PODCASTS,
        RECOMMENDATION_ROW_FOR_YOU,
    ]
    folder = result[0]
    assert folder.name == "Trending Podcasts"
    assert folder.translation_key == "trending_podcasts"
    assert folder.icon == "mdi-trending-up"
    assert folder.provider == "itunes_podcasts_test"
    assert all(len(folder.items) == 0 for folder in result)
    mass_mock.http_session.get.assert_not_called()
    mass_mock.cache.get.assert_not_called()


async def test_get_recommendation_items_fetches_top_podcasts(
    provider: ITunesPodcastsProvider,
) -> None:
    """get_recommendation_items(<row>) triggers the top-podcasts fetch and returns its podcasts."""
    with (
        patch.object(
            provider, "_cache_get_top_podcasts", new_callable=AsyncMock
        ) as mock_top_podcasts,
        patch(TIME, return_value=0),
    ):
        mock_top_podcasts.return_value = [_search_result()]

        items = await provider.get_recommendation_items(RECOMMENDATION_ROW_TOP_PODCASTS)

    mock_top_podcasts.assert_awaited_once_with()
    assert [item.item_id for item in items] == ["https://example.com/feed.xml"]
    assert items[0].name == "Test Podcast"


async def test_get_recommendation_items_served_from_cache(
    provider: ITunesPodcastsProvider, mass_mock: Mock
) -> None:
    """A cached top-podcasts payload serves the row's items without any http calls."""
    helper = TopPodcastsHelper(top_podcasts=[_search_result()])
    mass_mock.cache.get = AsyncMock(return_value=helper.to_dict())

    with patch(TIME, return_value=0):
        items = await provider.get_recommendation_items(RECOMMENDATION_ROW_TOP_PODCASTS)

    assert [item.item_id for item in items] == ["https://example.com/feed.xml"]
    mass_mock.http_session.get.assert_not_called()


async def test_get_recommendation_items_unknown_id_returns_empty(
    provider: ITunesPodcastsProvider, mass_mock: Mock
) -> None:
    """An unknown row item_id returns an empty result without triggering any fetch."""
    with patch.object(
        provider, "_cache_get_top_podcasts", new_callable=AsyncMock
    ) as mock_top_podcasts:
        items = await provider.get_recommendation_items("unknown-row")

    assert len(items) == 0
    mock_top_podcasts.assert_not_called()
    mass_mock.http_session.get.assert_not_called()


async def test_top_podcasts_single_batched_lookup(
    provider: ITunesPodcastsProvider, mass_mock: Mock
) -> None:
    """The top podcasts are resolved with one batched lookup, keeping rank order, and cached."""
    top_podcasts = [3, 1, 2]
    with (
        patch.object(provider, "_get_top_podcast_ids", AsyncMock(return_value=top_podcasts)),
        patch.object(
            provider,
            "_perform_search",
            AsyncMock(return_value=[_result(1), _result(2), _result(3)]),
        ) as lookup,
    ):
        results = await provider._cache_get_top_podcasts()

    assert [r.collection_id for r in results] == top_podcasts
    lookup.assert_awaited_once()
    assert lookup.await_args is not None
    assert lookup.await_args.args[1]["id"] == "3,1,2"
    mass_mock.cache.set.assert_awaited_once()
    assert mass_mock.cache.set.await_args.kwargs["key"] == "top-podcasts-full-us"


@pytest.mark.parametrize(
    ("top_podcasts", "lookup"),
    [(None, [_result(1)]), ([1], None)],
    ids=["top-podcasts-failed", "lookup-failed"],
)
async def test_top_podcasts_failure_not_cached(
    provider: ITunesPodcastsProvider,
    mass_mock: Mock,
    top_podcasts: list[int] | None,
    lookup: list[PodcastSearchResult] | None,
) -> None:
    """A failed request returns nothing and is not cached, so the next call retries."""
    with (
        patch.object(provider, "_get_top_podcast_ids", AsyncMock(return_value=top_podcasts)),
        patch.object(provider, "_perform_search", AsyncMock(return_value=lookup)),
    ):
        assert await provider._cache_get_top_podcasts() == []
    mass_mock.cache.set.assert_not_called()


async def test_top_podcasts_page_rotates_and_wraps(provider: ITunesPodcastsProvider) -> None:
    """Every rotation interval shows the next offset into the top podcasts, wrapping around."""
    top_podcasts = [_result(i) for i in range(100)]
    with patch.object(provider, "_cache_get_top_podcasts", AsyncMock(return_value=top_podcasts)):
        pages = []
        for window in range(TOP_PODCASTS_NUM_PAGES + 1):
            with patch(TIME, return_value=window * TOP_PODCASTS_ROTATION):
                pages.append([r.collection_id for r in await provider._get_top_podcasts_page()])

    assert TOP_PODCASTS_NUM_PAGES == 7
    assert pages[0] == list(range(0, 100, 7))
    assert pages[1] == list(range(1, 100, 7))
    assert pages[6] == list(range(6, 100, 7))
    assert pages[7] == pages[0]
    assert sorted(i for page in pages[:7] for i in page if i is not None) == list(range(100))


async def test_top_podcasts_page_filters_library_and_explicit(
    provider: ITunesPodcastsProvider, mass_mock: Mock, config_mock: Mock
) -> None:
    """Library podcasts and, if disabled, explicit ones are dropped."""
    # same feed, different scheme/host spelling
    _set_library(mass_mock, [_library_podcast("Whatever", "http://www.example.com/1.xml/")])
    config_mock.get_value.side_effect = lambda key, default=None: {
        "locale": "us",
        "explicit": False,
    }.get(key, default)
    top_podcasts = [_result(1), _result(2), _result(3, explicit=True), _result(4)]
    with (
        patch.object(provider, "_cache_get_top_podcasts", AsyncMock(return_value=top_podcasts)),
        patch("music_assistant.providers.itunes_podcasts.TOP_PODCASTS_NUM_PAGES", 1),
        patch(TIME, return_value=0),
    ):
        page = await provider._get_top_podcasts_page()

    assert [r.collection_id for r in page] == [2, 4]


async def test_search_results_carry_itunes_details(provider: ITunesPodcastsProvider) -> None:
    """Podcasts from a search store the iTunes id and genres in their provider mapping."""
    (podcast,) = provider._get_podcast_list([_result(1, ["1488", "26"])])

    (mapping,) = podcast.provider_mappings
    assert _details(mapping) == MappingDetails(itunes_id=1, genre_ids=["1488", "26"])


async def test_get_podcast_keeps_stored_details(
    provider: ITunesPodcastsProvider, mass_mock: Mock
) -> None:
    """A podcast fetched from its feed carries the details stored on its library mapping."""
    stored = _library_podcast(
        "Podcast 1", "https://example.com/1.xml", MappingDetails(itunes_id=1, genre_ids=["1488"])
    )
    mass_mock.music.podcasts.get_library_item_by_prov_id = AsyncMock(return_value=stored)
    feed = {"title": "Podcast 1", "episodes": []}
    with patch.object(provider, "_cache_get_podcast", AsyncMock(return_value=feed)):
        podcast = await provider.get_podcast("https://example.com/1.xml")

    (mapping,) = podcast.provider_mappings
    assert _details(mapping) == MappingDetails(itunes_id=1, genre_ids=["1488"])


async def test_library_sync_keeps_stored_details(
    provider: ITunesPodcastsProvider, mass_mock: Mock
) -> None:
    """A synced library podcast carries the details stored on its mapping."""
    stored = _library_podcast(
        "Podcast 1", "https://example.com/1.xml", MappingDetails(itunes_id=1, genre_ids=["1488"])
    )
    mass_mock.music.podcasts.get_library_items_by_prov_id = AsyncMock(return_value=[stored])
    mass_mock.music.get_provider_sync_schedule = Mock(return_value=None)
    feed = {"title": "Podcast 1", "episodes": []}
    with patch(
        "music_assistant.providers.itunes_podcasts.refresh_cached_podcast",
        AsyncMock(return_value=feed),
    ):
        (podcast,) = [p async for p in provider.get_library_podcasts()]

    (mapping,) = podcast.provider_mappings
    assert _details(mapping) == MappingDetails(itunes_id=1, genre_ids=["1488"])


async def test_loaded_in_mass_starts_migration(
    provider: ITunesPodcastsProvider, mass_mock: Mock
) -> None:
    """The mapping migration runs as a background task once the provider is loaded."""
    await provider.loaded_in_mass()

    mass_mock.create_task.assert_called_once()
    mass_mock.create_task.call_args.args[0].close()  # the mocked task never runs it
    assert provider._migrate_task is mass_mock.create_task.return_value


async def test_unload_cancels_migrate_task(provider: ITunesPodcastsProvider) -> None:
    """Unloading the provider cancels a running mapping migration."""
    started = asyncio.Event()

    async def _migrate() -> None:
        started.set()
        await asyncio.sleep(3600)

    provider._migrate_task = asyncio.create_task(_migrate())
    await started.wait()

    await provider.unload()

    assert provider._migrate_task.cancelled()


async def test_migrate_provider_mappings(provider: ITunesPodcastsProvider, mass_mock: Mock) -> None:
    """Mappings without details get them from a title search, hits and misses alike."""
    _set_library(
        mass_mock,
        [
            _library_podcast("Done", "https://example.com/9.xml", MappingDetails(itunes_id=9)),
            # same feed, different scheme/host spelling
            _library_podcast("Podcast 2", "http://www.example.com/2.xml/"),
            _library_podcast("Unknown", "https://example.com/unknown.xml"),
        ],
    )
    mass_mock.music.podcasts.set_provider_mappings = AsyncMock()
    search = AsyncMock(return_value=[_result(1), _result(2, ["1488", "26"])])
    with patch.object(provider, "_perform_search", search):
        await provider._migrate_provider_mappings()

    assert [call.args[1]["term"] for call in search.await_args_list] == ["Podcast 2", "Unknown"]
    stored = {
        call.args[0]: _details(call.args[1][0])
        for call in mass_mock.music.podcasts.set_provider_mappings.await_args_list
    }
    assert stored == {
        "Podcast 2": MappingDetails(itunes_id=2, genre_ids=["1488", "26"]),
        "Unknown": MappingDetails(),
    }


async def test_migrate_provider_mappings_failure_not_stored(
    provider: ITunesPodcastsProvider, mass_mock: Mock
) -> None:
    """A failed search stores nothing, so the podcast is retried on the next load."""
    _set_library(mass_mock, [_library_podcast("Podcast 2", "https://example.com/2.xml")])
    mass_mock.music.podcasts.set_provider_mappings = AsyncMock()
    with patch.object(provider, "_perform_search", AsyncMock(return_value=None)):
        await provider._migrate_provider_mappings()

    mass_mock.music.podcasts.set_provider_mappings.assert_not_called()


async def test_library_recommendations(provider: ITunesPodcastsProvider, mass_mock: Mock) -> None:
    """Top podcasts of the library's primary genres are merged, library podcasts dropped."""
    _set_library(
        mass_mock,
        [
            _library_podcast(
                "Podcast 1",
                "https://example.com/1.xml",
                MappingDetails(itunes_id=1, genre_ids=["1488", "26"]),
            ),
            _library_podcast(
                "Podcast 2",
                "https://example.com/2.xml",
                MappingDetails(itunes_id=2, genre_ids=["1526", "26", "1489"]),
            ),
            # not migrated yet, contributes nothing
            _library_podcast("Podcast 3", "https://example.com/3.xml"),
        ],
    )
    top_podcasts = {"1488": [1, 10, 11], "1526": [20, 2, 10]}
    genre_top_podcasts = AsyncMock(side_effect=lambda _country, genre_id: top_podcasts[genre_id])
    with (
        patch.object(provider, "_get_genre_top_podcast_ids", genre_top_podcasts),
        patch.object(
            provider,
            "_perform_search",
            AsyncMock(
                side_effect=lambda _url, params: [
                    _result(int(i)) for i in str(params["id"]).split(",")
                ]
            ),
        ),
    ):
        results = await provider._get_library_recommendations()

    # 10 ranks in both genres, so it comes first; library podcasts 1 and 2 are gone
    assert [r.collection_id for r in results] == [10, 20, 11]
    # only primary genres are used, parent genre 1489 (News) is not fetched
    assert sorted(call.args[1] for call in genre_top_podcasts.await_args_list) == ["1488", "1526"]
    assert mass_mock.cache.set.await_args.kwargs["expiration"] == (
        LIBRARY_RECOMMENDATIONS_CACHE_EXPIRATION
    )


async def test_library_recommendations_without_genres(
    provider: ITunesPodcastsProvider, mass_mock: Mock
) -> None:
    """Without genres of library podcasts there is nothing to recommend and no request is sent."""
    _set_library(mass_mock, [_library_podcast("Podcast 3", "https://example.com/3.xml")])
    assert await provider._get_library_recommendations() == []
    mass_mock.http_session.get.assert_not_called()


async def test_library_recommendations_empty_library(
    provider: ITunesPodcastsProvider, mass_mock: Mock
) -> None:
    """Without library podcasts there is nothing to recommend and no request is sent."""
    assert await provider._get_library_recommendations() == []
    mass_mock.http_session.get.assert_not_called()


async def test_genre_top_podcasts_parsing(
    provider: ITunesPodcastsProvider, mass_mock: Mock
) -> None:
    """The legacy feed is parsed, a single entry is not wrapped in a list."""
    unwrapped = ITunesPodcastsProvider._get_genre_top_podcast_ids
    get_genre_top_podcast_ids = unwrapped.__wrapped__.__wrapped__  # type: ignore[attr-defined]
    mass_mock.http_session = Mock()
    mass_mock.http_session.get = Mock(
        return_value=_http_response(
            200, b'{"feed": {"entry": {"id": {"attributes": {"im:id": "42"}}}}}'
        )
    )
    assert await get_genre_top_podcast_ids(provider, "us", "1488") == [42]

    mass_mock.http_session.get = Mock(return_value=_http_response(503, b""))
    assert await get_genre_top_podcast_ids(provider, "us", "1488") is None
