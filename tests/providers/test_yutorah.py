"""Tests for the YuTorah provider."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import LoginFailed, MediaNotFoundError, ProviderUnavailableError
from music_assistant_models.media_items import BrowseFolder, Podcast

from music_assistant.providers.yutorah import YuTorahProvider

# ---------------------------------------------------------------------------
# Shared sample data
# ---------------------------------------------------------------------------

SAMPLE_SHIUR: dict[str, Any] = {
    "shiurID": "12345",
    "shiurTitle": "Test Shiur",
    "shiurFileURL": "https://cdn.example.com/test.mp3",
    "shiurLength": "45:00",
    "teacherid": 7,
    "teacherfullname": "Rabbi C",
    "photo": "https://cdn.example.com/c.jpg",
    "shiurSeries": "100",
    "shiurSeriesName": "Daf Yomi",
}

# Flat format returned by search/get
SAMPLE_SEARCH_DOC: dict[str, Any] = {
    "shiurID": "12345",
    "shiurtitle": "Test Shiur",
    "shiurdownloadurl": "https://cdn.example.com/test.mp3",
    "duration": 45,
    "teacherid": "7",
    "teacherfullname": "Rabbi C",
    "photo": "https://cdn.example.com/c.jpg",
}

SAMPLE_SERIES: dict[str, Any] = {
    "ID": "100",
    "name": "Daf Yomi",
    "imageURL": "https://cdn.example.com/daf.jpg",
    "shiurCount": 200,
}

SAMPLE_TEACHER: dict[str, Any] = {
    "ID": 7,
    "fullName": "Rabbi C",
    "imageURL": "https://cdn.example.com/c.jpg",
    "shiurCount": 100,
    "isHidden": False,
}

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mass_mock() -> AsyncMock:
    """Return a mock MusicAssistant instance."""
    mass = AsyncMock()
    mass.http_session = AsyncMock()
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.set = AsyncMock()
    return mass


@pytest.fixture
def provider(mass_mock: AsyncMock) -> YuTorahProvider:
    """Return a YuTorahProvider instance with mocked dependencies."""
    manifest = MagicMock()
    manifest.domain = "yutorah"
    config = MagicMock()
    config.get_value.return_value = None  # no credentials

    with patch("music_assistant.models.provider.logging.Logger.setLevel"):
        prov = YuTorahProvider(mass_mock, manifest, config)

    prov._user_token = None
    return prov


@pytest.fixture
def auth_provider(provider: YuTorahProvider) -> YuTorahProvider:
    """Return a provider with a mock authentication token set."""
    provider._user_token = "test_token"
    return provider


# ---------------------------------------------------------------------------
# handle_async_init
# ---------------------------------------------------------------------------


async def test_handle_async_init_no_credentials(provider: YuTorahProvider) -> None:
    """Without credentials, user token stays None."""
    provider.config.get_value.return_value = None  # type: ignore[attr-defined]
    await provider.handle_async_init()
    assert provider._user_token is None


async def test_handle_async_init_with_credentials_calls_login(
    provider: YuTorahProvider,
) -> None:
    """With username and password configured, _login is called."""

    def get_value(key: str) -> str | None:
        return {"username": "user@example.com", "password": "secret"}.get(key)

    provider.config.get_value.side_effect = get_value  # type: ignore[attr-defined]
    with patch.object(provider, "_login", new=AsyncMock()) as mock_login:
        await provider.handle_async_init()
    mock_login.assert_awaited_once_with("user@example.com", "secret")


# ---------------------------------------------------------------------------
# _login
# ---------------------------------------------------------------------------


async def test_login_raises_on_bad_credentials(provider: YuTorahProvider) -> None:
    """Failed login raises LoginFailed."""
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(
        return_value=MagicMock(
            raise_for_status=MagicMock(),
            json=AsyncMock(return_value={"loginSuccess": False}),
        )
    )
    mock_cm.__aexit__ = AsyncMock(return_value=False)
    provider.mass.http_session.post = MagicMock(return_value=mock_cm)  # type: ignore[method-assign]

    with pytest.raises(LoginFailed):
        await provider._login("bad@example.com", "wrong")


# ---------------------------------------------------------------------------
# get_podcast
# ---------------------------------------------------------------------------


async def test_get_podcast_series_found(provider: YuTorahProvider) -> None:
    """A known series ID returns the matching Podcast."""
    with patch.object(provider, "_fetch_series_list", new=AsyncMock(return_value=[SAMPLE_SERIES])):
        podcast = await provider.get_podcast("100")
    assert podcast.name == "Daf Yomi"
    assert podcast.item_id == "100"


async def test_get_podcast_series_not_found_raises(
    provider: YuTorahProvider,
) -> None:
    """Unknown series ID raises MediaNotFoundError."""
    with (
        patch.object(provider, "_fetch_series_list", new=AsyncMock(return_value=[])),
        pytest.raises(MediaNotFoundError),
    ):
        await provider.get_podcast("999")


async def test_get_podcast_teacher_prefix_found(provider: YuTorahProvider) -> None:
    """A 't_' prefixed ID looks up the teacher via the cached map and returns a named Podcast."""
    teachers_map = {"7": SAMPLE_TEACHER}
    with patch.object(provider, "_fetch_teachers_map", new=AsyncMock(return_value=teachers_map)):
        podcast = await provider.get_podcast("t_7")
    assert podcast.item_id == "t_7"
    assert podcast.name == "Rabbi C"


async def test_get_podcast_teacher_prefix_uses_cache(provider: YuTorahProvider) -> None:
    """get_podcast('t_X') uses _fetch_teachers_map (cached) not a direct API call."""
    teachers_map = {"7": SAMPLE_TEACHER}
    mock_map = AsyncMock(return_value=teachers_map)
    mock_api = AsyncMock()
    with (
        patch.object(provider, "_fetch_teachers_map", new=mock_map),
        patch.object(provider, "_api_get", new=mock_api),
    ):
        await provider.get_podcast("t_7")
    mock_map.assert_awaited_once()
    mock_api.assert_not_awaited()


async def test_get_podcast_teacher_prefix_not_found_returns_fallback(
    provider: YuTorahProvider,
) -> None:
    """Unknown teacher ID with 't_' prefix returns a fallback Podcast."""
    with patch.object(provider, "_fetch_teachers_map", new=AsyncMock(return_value={})):
        podcast = await provider.get_podcast("t_999")
    assert podcast.item_id == "t_999"
    assert "999" in podcast.name


# ---------------------------------------------------------------------------
# get_podcast_episodes
# ---------------------------------------------------------------------------


async def test_get_podcast_episodes_unauthenticated(provider: YuTorahProvider) -> None:
    """Without a token, episodes come from landingpage/landing."""
    landing_data = {
        "recentlyAddedShiurim": [SAMPLE_SHIUR],
        "topShiurim": [],
        "featuredShiurim": [],
    }
    with patch.object(provider, "_api_get", new=AsyncMock(return_value=landing_data)):
        episodes = [ep async for ep in provider.get_podcast_episodes("100")]
    assert len(episodes) == 1
    assert episodes[0].item_id == "12345"


async def test_get_podcast_episodes_unauthenticated_deduplicates(
    provider: YuTorahProvider,
) -> None:
    """Shiur IDs appearing in multiple landing sections are deduplicated."""
    landing_data = {
        "recentlyAddedShiurim": [SAMPLE_SHIUR],
        "topShiurim": [SAMPLE_SHIUR],  # same shiur repeated
        "featuredShiurim": [],
    }
    with patch.object(provider, "_api_get", new=AsyncMock(return_value=landing_data)):
        episodes = [ep async for ep in provider.get_podcast_episodes("100")]
    assert len(episodes) == 1


async def test_get_podcast_episodes_unauthenticated_api_none(
    provider: YuTorahProvider,
) -> None:
    """None response from API yields no episodes without raising."""
    with patch.object(provider, "_api_get", new=AsyncMock(return_value=None)):
        episodes = [ep async for ep in provider.get_podcast_episodes("100")]
    assert episodes == []


async def test_get_podcast_episodes_unauthenticated_teacher_prefix(
    provider: YuTorahProvider,
) -> None:
    """Without a token, 't_' IDs call landingpage/landing with type=speaker and bare teacher ID."""
    landing_data = {
        "recentlyAddedShiurim": [SAMPLE_SHIUR],
        "topShiurim": [],
        "featuredShiurim": [],
    }
    with patch.object(provider, "_api_get", new=AsyncMock(return_value=landing_data)) as mock_api:
        episodes = [ep async for ep in provider.get_podcast_episodes("t_7")]
    assert len(episodes) == 1
    call_kwargs = mock_api.call_args.kwargs
    assert call_kwargs.get("type") == "speaker"
    assert call_kwargs.get("value") == "7"  # prefix stripped


async def test_get_podcast_episodes_authenticated_series(
    auth_provider: YuTorahProvider,
) -> None:
    """With a token, series episodes come from paginated search/get."""
    with patch.object(auth_provider, "_api_get", new=AsyncMock(return_value=[SAMPLE_SEARCH_DOC])):
        episodes = [ep async for ep in auth_provider.get_podcast_episodes("100")]
    assert len(episodes) == 1
    assert episodes[0].item_id == "12345"


async def test_get_podcast_episodes_authenticated_teacher_prefix(
    auth_provider: YuTorahProvider,
) -> None:
    """Teacher-prefixed ID fetches shiurim by teacherID, not seriesID."""
    with patch.object(
        auth_provider, "_api_get", new=AsyncMock(return_value=[SAMPLE_SEARCH_DOC])
    ) as mock_api:
        episodes = [ep async for ep in auth_provider.get_podcast_episodes("t_7")]
    assert len(episodes) == 1
    # The first call should pass teacherID=7, not seriesID
    call_kwargs = mock_api.call_args_list[0].kwargs
    assert call_kwargs.get("teacherID") == "7"
    assert "seriesID" not in call_kwargs


# ---------------------------------------------------------------------------
# get_podcast_episode
# ---------------------------------------------------------------------------


async def test_get_podcast_episode_success(provider: YuTorahProvider) -> None:
    """Valid shiur/details response returns a PodcastEpisode."""
    with patch.object(provider, "_api_get", new=AsyncMock(return_value=SAMPLE_SHIUR)):
        ep = await provider.get_podcast_episode("12345")
    assert ep.item_id == "12345"
    assert ep.name == "Test Shiur"
    assert ep.duration == 2700  # 45 min


async def test_get_podcast_episode_mp3_url_stored_in_mapping(provider: YuTorahProvider) -> None:
    """Direct MP3 URL is cached in ProviderMapping.details to avoid re-fetches."""
    with patch.object(provider, "_api_get", new=AsyncMock(return_value=SAMPLE_SHIUR)):
        ep = await provider.get_podcast_episode("12345")
    mapping = next(iter(ep.provider_mappings))
    assert mapping.details == "https://cdn.example.com/test.mp3"


async def test_get_podcast_episode_not_found_raises(provider: YuTorahProvider) -> None:
    """None from API raises MediaNotFoundError."""
    with (
        patch.object(provider, "_api_get", new=AsyncMock(return_value=None)),
        pytest.raises(MediaNotFoundError),
    ):
        await provider.get_podcast_episode("99999")


async def test_get_podcast_episode_no_mp3_raises(provider: YuTorahProvider) -> None:
    """A shiur dict with no playable URL raises MediaNotFoundError."""
    shiur = {**SAMPLE_SHIUR, "shiurFileURL": ""}
    with (
        patch.object(provider, "_api_get", new=AsyncMock(return_value=shiur)),
        pytest.raises(MediaNotFoundError),
    ):
        await provider.get_podcast_episode("12345")


# ---------------------------------------------------------------------------
# get_artist
# ---------------------------------------------------------------------------


async def test_get_artist_found(provider: YuTorahProvider) -> None:
    """Teacher found in the teachers map returns a correctly named Artist."""
    teachers_map = {"7": SAMPLE_TEACHER}
    with patch.object(provider, "_fetch_teachers_map", new=AsyncMock(return_value=teachers_map)):
        artist = await provider.get_artist("7")
    assert artist.item_id == "7"
    assert artist.name == "Rabbi C"


async def test_get_artist_not_in_map_returns_fallback(provider: YuTorahProvider) -> None:
    """Unknown teacher ID returns a fallback Artist with the ID in the name."""
    with patch.object(provider, "_fetch_teachers_map", new=AsyncMock(return_value={})):
        artist = await provider.get_artist("999")
    assert artist.item_id == "999"
    assert "999" in artist.name


# ---------------------------------------------------------------------------
# get_artist_toptracks
# ---------------------------------------------------------------------------


async def test_get_artist_toptracks_no_token_returns_empty(
    provider: YuTorahProvider,
) -> None:
    """Without a token, top tracks cannot be fetched and returns empty list."""
    tracks = await provider.get_artist_toptracks("7")
    assert tracks == []


async def test_get_artist_toptracks_with_token(auth_provider: YuTorahProvider) -> None:
    """With a token, shiurim are returned as Track objects with correct artist linkage."""
    with patch.object(auth_provider, "_api_get", new=AsyncMock(return_value=[SAMPLE_SEARCH_DOC])):
        tracks = await auth_provider.get_artist_toptracks("7")
    assert len(tracks) == 1
    assert tracks[0].item_id == "12345"
    assert tracks[0].artists[0].name == "Rabbi C"


async def test_get_artist_toptracks_empty_response(auth_provider: YuTorahProvider) -> None:
    """Empty API response returns an empty list."""
    with patch.object(auth_provider, "_api_get", new=AsyncMock(return_value=[])):
        tracks = await auth_provider.get_artist_toptracks("7")
    assert tracks == []


# ---------------------------------------------------------------------------
# get_track
# ---------------------------------------------------------------------------


async def test_get_track_success(provider: YuTorahProvider) -> None:
    """Valid shiur/details response returns a Track."""
    with patch.object(provider, "_api_get", new=AsyncMock(return_value=SAMPLE_SHIUR)):
        track = await provider.get_track("12345")
    assert track.item_id == "12345"
    assert track.name == "Test Shiur"


async def test_get_track_not_found_raises(provider: YuTorahProvider) -> None:
    """None from API raises MediaNotFoundError."""
    with (
        patch.object(provider, "_api_get", new=AsyncMock(return_value=None)),
        pytest.raises(MediaNotFoundError),
    ):
        await provider.get_track("99999")


async def test_get_track_no_mp3_raises(provider: YuTorahProvider) -> None:
    """A shiur dict with no playable URL raises MediaNotFoundError."""
    shiur = {**SAMPLE_SHIUR, "shiurFileURL": ""}
    with (
        patch.object(provider, "_api_get", new=AsyncMock(return_value=shiur)),
        pytest.raises(MediaNotFoundError),
    ):
        await provider.get_track("12345")


# ---------------------------------------------------------------------------
# get_stream_details
# ---------------------------------------------------------------------------


async def test_get_stream_details_success(provider: YuTorahProvider) -> None:
    """Valid shiur/details response yields a StreamDetails with the correct URL."""
    api_response = {
        "shiurID": 12345,
        "shiurFileURL": "https://cdn.example.com/shiur.mp3",
    }
    with patch.object(provider, "_api_get", new=AsyncMock(return_value=api_response)):
        details = await provider.get_stream_details("12345", MediaType.PODCAST_EPISODE)
    assert details.path == "https://cdn.example.com/shiur.mp3"


async def test_get_stream_details_not_found_raises(provider: YuTorahProvider) -> None:
    """None response from _api_get should raise MediaNotFoundError."""
    with (
        patch.object(provider, "_api_get", new=AsyncMock(return_value=None)),
        pytest.raises(MediaNotFoundError),
    ):
        await provider.get_stream_details("99999", MediaType.PODCAST_EPISODE)


async def test_get_stream_details_no_url_raises(provider: YuTorahProvider) -> None:
    """Empty shiurFileURL should raise MediaNotFoundError."""
    api_response = {"shiurID": 12345, "shiurFileURL": ""}
    with (
        patch.object(provider, "_api_get", new=AsyncMock(return_value=api_response)),
        pytest.raises(MediaNotFoundError),
    ):
        await provider.get_stream_details("12345", MediaType.PODCAST_EPISODE)


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


async def test_search_returns_podcasts_unauthenticated(provider: YuTorahProvider) -> None:
    """Unauthenticated search filters the cached series list by name."""
    series_list = [
        {"ID": "100", "name": "Daf Yomi Shiurim", "imageURL": ""},
        {"ID": "101", "name": "Unrelated Content", "imageURL": ""},
    ]
    with patch.object(provider, "_fetch_series_list", new=AsyncMock(return_value=series_list)):
        results = await provider.search("daf", [MediaType.PODCAST])
    assert len(results.podcasts) == 1
    assert results.podcasts[0].name == "Daf Yomi Shiurim"


async def test_search_unauthenticated_no_match(provider: YuTorahProvider) -> None:
    """No matching series name returns empty results."""
    series_list = [{"ID": "100", "name": "Daf Yomi", "imageURL": ""}]
    with patch.object(provider, "_fetch_series_list", new=AsyncMock(return_value=series_list)):
        results = await provider.search("halacha", [MediaType.PODCAST])
    assert results.podcasts == []


async def test_search_authenticated_returns_podcasts_and_artists(
    auth_provider: YuTorahProvider,
) -> None:
    """Authenticated search builds Podcasts from series facets and Artists from teacher facets."""
    api_response = {
        "facet_counts": {
            "facet_fields": {
                "series": [{"SeriesId": "100", "SeriesName": "Daf Yomi"}],
                "teachers": [{"TeacherId": "7", "TeacherName": "Rabbi C"}],
            }
        }
    }
    teachers_map = {"7": SAMPLE_TEACHER}
    with (
        patch.object(auth_provider, "_api_get", new=AsyncMock(return_value=api_response)),
        patch.object(
            auth_provider, "_fetch_teachers_map", new=AsyncMock(return_value=teachers_map)
        ),
    ):
        results = await auth_provider.search("test", [MediaType.PODCAST, MediaType.ARTIST])
    assert len(results.podcasts) == 1
    assert results.podcasts[0].name == "Daf Yomi"
    assert len(results.artists) == 1
    assert results.artists[0].name == "Rabbi C"


async def test_search_authenticated_api_error_returns_empty(
    auth_provider: YuTorahProvider,
) -> None:
    """None from API when authenticated returns empty SearchResults."""
    with patch.object(auth_provider, "_api_get", new=AsyncMock(return_value=None)):
        results = await auth_provider.search("test", [MediaType.PODCAST])
    assert results.podcasts == []


# ---------------------------------------------------------------------------
# browse
# ---------------------------------------------------------------------------


async def test_browse_root(provider: YuTorahProvider) -> None:
    """Root path returns the four top-level browse folders."""
    results = await provider.browse("yutorah://")
    names = [r.name for r in results]
    assert "Browse by Series" in names
    assert "Browse by Teacher" in names
    assert "Browse by Topic" in names
    assert "Recent Shiurim" in names


async def test_browse_series_list(provider: YuTorahProvider) -> None:
    """yutorah://series returns a sorted list of series folders."""
    series_list = [
        {"ID": "100", "name": "Daf Yomi"},
        {"ID": "200", "name": "Tefilla"},
    ]
    with patch.object(provider, "_fetch_series_list", new=AsyncMock(return_value=series_list)):
        results = await provider.browse("yutorah://series")
    assert len(results) == 2
    assert results[0].name == "Daf Yomi"  # alphabetically first


async def test_browse_series_list_uses_numeric_id_in_path(provider: YuTorahProvider) -> None:
    """Series folders use the numeric ID in their browse path."""
    series_list = [{"ID": "100", "name": "Daf Yomi"}]
    with patch.object(provider, "_fetch_series_list", new=AsyncMock(return_value=series_list)):
        results = await provider.browse("yutorah://series")
    folder = results[0]
    assert isinstance(folder, BrowseFolder)
    assert folder.path == "yutorah://series/100"


async def test_browse_series_unauthenticated_returns_series_podcast(
    provider: YuTorahProvider,
) -> None:
    """Without auth, browsing a series by ID returns the Podcast item but no teacher folders."""
    series_list = [{"ID": "100", "name": "Daf Yomi"}]
    with patch.object(provider, "_fetch_series_list", new=AsyncMock(return_value=series_list)):
        results = await provider.browse("yutorah://series/100")
    items = list(results)
    assert len(items) == 1
    assert isinstance(items[0], Podcast)
    assert items[0].item_id == "100"


async def test_browse_series_authenticated(auth_provider: YuTorahProvider) -> None:
    """With a token, drilling into a series by ID returns the Podcast then teacher sub-folders."""
    series_list = [{"ID": "100", "name": "Daf Yomi"}]
    facets_response = {
        "facet_counts": {
            "facet_fields": {"teachers": [{"TeacherId": "7", "TeacherName": "Rabbi C", "Match": 5}]}
        }
    }

    async def api_side(endpoint: str, **_kw: Any) -> Any:
        return facets_response if endpoint == "search/get" else None

    with (
        patch.object(auth_provider, "_fetch_series_list", new=AsyncMock(return_value=series_list)),
        patch.object(auth_provider, "_api_get", new=AsyncMock(side_effect=api_side)),
    ):
        results = await auth_provider.browse("yutorah://series/100")
    assert len(results) == 2
    assert isinstance(results[0], Podcast)
    assert results[0].item_id == "100"
    assert "Rabbi C" in results[1].name
    assert isinstance(results[1], BrowseFolder)
    assert results[1].path == "yutorah://series/100/teacher/7"


async def test_browse_series_teacher_episodes(auth_provider: YuTorahProvider) -> None:
    """yutorah://series/<id>/teacher/<id> returns a subscribable Podcast then episodes."""
    series_list = [{"ID": "100", "name": "Daf Yomi"}]
    teachers_map = {"7": SAMPLE_TEACHER}

    async def api_side(endpoint: str, **_kw: Any) -> Any:
        return [SAMPLE_SEARCH_DOC] if endpoint == "search/get" else None

    with (
        patch.object(auth_provider, "_fetch_series_list", new=AsyncMock(return_value=series_list)),
        patch.object(
            auth_provider, "_fetch_teachers_map", new=AsyncMock(return_value=teachers_map)
        ),
        patch.object(auth_provider, "_api_get", new=AsyncMock(side_effect=api_side)),
    ):
        results = await auth_provider.browse("yutorah://series/100/teacher/7")
    assert len(results) == 2
    assert isinstance(results[0], Podcast)
    assert results[0].item_id == "st_100_7"
    assert "Daf Yomi" in results[0].name
    assert "Rabbi C" in results[0].name
    assert results[1].item_id == "12345"


async def test_browse_teachers(provider: YuTorahProvider) -> None:
    """yutorah://teachers returns a subscribable Podcast for each active teacher."""
    teachers_map = {"7": SAMPLE_TEACHER}
    with patch.object(provider, "_fetch_teachers_map", new=AsyncMock(return_value=teachers_map)):
        results = await provider.browse("yutorah://teachers")
    assert len(results) == 1
    podcast = results[0]
    assert isinstance(podcast, Podcast)
    assert "Rabbi C" in podcast.name
    assert podcast.item_id == "t_7"


async def test_browse_teachers_uses_cache(provider: YuTorahProvider) -> None:
    """yutorah://teachers uses _fetch_teachers_map (cached), not a direct API call."""
    teachers_map = {"7": SAMPLE_TEACHER}
    mock_map = AsyncMock(return_value=teachers_map)
    mock_api = AsyncMock()
    with (
        patch.object(provider, "_fetch_teachers_map", new=mock_map),
        patch.object(provider, "_api_get", new=mock_api),
    ):
        await provider.browse("yutorah://teachers")
    mock_map.assert_awaited_once()
    mock_api.assert_not_awaited()


async def test_browse_teachers_filters_hidden(provider: YuTorahProvider) -> None:
    """Hidden teachers are excluded from the teacher list."""
    hidden_teacher = {**SAMPLE_TEACHER, "isHidden": True}
    teachers_map = {"7": hidden_teacher}
    with patch.object(provider, "_fetch_teachers_map", new=AsyncMock(return_value=teachers_map)):
        results = await provider.browse("yutorah://teachers")
    assert list(results) == []


async def test_browse_teacher_episodes_by_id(provider: YuTorahProvider) -> None:
    """yutorah://teachers/<id> returns landing episodes for the teacher."""
    landing_data = {
        "recentlyAddedShiurim": [SAMPLE_SHIUR],
        "topShiurim": [],
        "featuredShiurim": [],
    }
    with patch.object(provider, "_api_get", new=AsyncMock(return_value=landing_data)):
        results = await provider.browse("yutorah://teachers/7")
    assert len(results) == 1
    assert results[0].item_id == "12345"


async def test_browse_teacher_episodes_authenticated(auth_provider: YuTorahProvider) -> None:
    """yutorah://teachers/<id> with auth uses paginated search."""
    with patch.object(auth_provider, "_api_get", new=AsyncMock(return_value=[SAMPLE_SEARCH_DOC])):
        results = await auth_provider.browse("yutorah://teachers/7")
    assert len(results) == 1


async def test_browse_categories(provider: YuTorahProvider) -> None:
    """yutorah://categories returns sub-category folders with numeric IDs in paths."""
    cat_data = [
        {
            "name": "Jewish Law",
            "subCategories": [{"ID": "50", "name": "Kashrut"}],
        }
    ]
    with patch.object(provider, "_api_get", new=AsyncMock(return_value=cat_data)):
        results = await provider.browse("yutorah://categories")
    assert len(results) == 1
    folder = results[0]
    assert isinstance(folder, BrowseFolder)
    assert "Kashrut" in folder.name
    assert "Jewish Law" in folder.name
    assert folder.path == "yutorah://categories/50"


async def test_browse_category_by_id(provider: YuTorahProvider) -> None:
    """yutorah://categories/<id> passes the ID directly to the API and returns series."""
    landing_data = {
        "recentlyAddedShiurim": [{**SAMPLE_SEARCH_DOC, "shiurSeries": "100"}],
        "topShiurim": [],
        "featuredShiurim": [],
    }

    async def api_side(endpoint: str, **_kw: Any) -> Any:
        if endpoint == "landingpage/landing":
            return landing_data
        if endpoint == "browse/series":
            return [SAMPLE_SERIES]
        return None

    with patch.object(provider, "_api_get", new=AsyncMock(side_effect=api_side)):
        results = await provider.browse("yutorah://categories/50")
    assert len(results) == 1
    assert results[0].name == "Daf Yomi"


async def test_browse_recent(provider: YuTorahProvider) -> None:
    """yutorah://recent returns recently uploaded shiurim as PodcastEpisodes."""
    homepage_data = {"recentlyUploaded": [SAMPLE_SHIUR]}
    with patch.object(provider, "_api_get", new=AsyncMock(return_value=homepage_data)):
        results = await provider.browse("yutorah://recent")
    assert len(results) == 1
    assert results[0].item_id == "12345"


async def test_browse_unknown_path_returns_empty(provider: YuTorahProvider) -> None:
    """An unrecognised browse path returns an empty list."""
    results = await provider.browse("yutorah://not-a-real-section")
    assert list(results) == []


# ---------------------------------------------------------------------------
# Security: auth token must not appear in error logs / exceptions
# ---------------------------------------------------------------------------


async def test_api_error_raises_provider_unavailable(
    auth_provider: YuTorahProvider,
) -> None:
    """Non-404 HTTP errors raise ProviderUnavailableError."""
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=MagicMock(), history=(), status=500, message="Server Error"
        )
    )
    mock_cm.__aexit__ = AsyncMock(return_value=False)
    auth_provider.mass.http_session.get = MagicMock(return_value=mock_cm)  # type: ignore[method-assign]

    with pytest.raises(ProviderUnavailableError) as exc_info:
        await auth_provider._api_get("shiur/details", shiurID="123")

    assert "test_token" not in str(exc_info.value)


async def test_api_404_returns_none(provider: YuTorahProvider) -> None:
    """404 responses return None rather than raising."""
    mock_resp = MagicMock()
    mock_resp.status = 404
    mock_resp.raise_for_status = MagicMock()
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)
    provider.mass.http_session.get = MagicMock(return_value=mock_cm)  # type: ignore[method-assign]

    result = await provider._api_get("shiur/details", shiurID="123")
    assert result is None
