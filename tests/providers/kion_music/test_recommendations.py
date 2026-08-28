"""Test KION Music two-method recommendations: static rows + per-row item fetches."""

from __future__ import annotations

import asyncio
import json
import pathlib
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from yandex_music import Album as YandexAlbum
from yandex_music import Playlist as YandexPlaylist
from yandex_music import Track as YandexTrack

from music_assistant.providers.kion_music import SUPPORTED_FEATURES
from music_assistant.providers.kion_music.constants import MY_WAVE_PLAYLIST_ID
from music_assistant.providers.kion_music.provider import KionMusicProvider
from tests.common import use_real_create_task

from .conftest import DE_JSON_CLIENT

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"

# One client method (or method set) backs each recommendation row.
CLIENT_METHODS = (
    "get_my_wave_tracks",
    "get_feed",
    "get_chart",
    "get_new_releases",
    "get_albums",
    "get_new_playlists",
    "get_playlists",
    "get_tag_playlists",
    "get_landing_tags",
)

# Expected row ids in the order get_recommendations() returns them.
ROW_IDS = [
    MY_WAVE_PLAYLIST_ID,
    "feed",
    "chart",
    "new_releases",
    "new_playlists",
    "top_picks",
    "mood_mix",
    "activity_mix",
    "seasonal_mix",
]


def _load_fixture(relpath: str) -> dict[str, Any]:
    """Load a JSON fixture relative to the fixtures dir."""
    with open(FIXTURES_DIR / relpath) as f:
        return cast("dict[str, Any]", json.load(f))


def _make_client_mock() -> Mock:
    """Build a client mock whose backend methods return parseable canned data."""
    track = YandexTrack.de_json(_load_fixture("tracks/with_artist_and_album.json"), DE_JSON_CLIENT)
    album = YandexAlbum.de_json(_load_fixture("albums/minimal.json"), DE_JSON_CLIENT)
    playlist = YandexPlaylist.de_json(_load_fixture("playlists/minimal.json"), DE_JSON_CLIENT)
    client = Mock()
    client.user_id = 12345
    client.get_my_wave_tracks = AsyncMock(return_value=([track], None))
    client.get_feed = AsyncMock(
        return_value=SimpleNamespace(
            generated_playlists=[SimpleNamespace(data=playlist, ready=True)]
        )
    )
    client.get_chart = AsyncMock(
        return_value=SimpleNamespace(chart=SimpleNamespace(tracks=[SimpleNamespace(track=track)]))
    )
    client.get_new_releases = AsyncMock(return_value=SimpleNamespace(new_releases=[300]))
    client.get_albums = AsyncMock(return_value=[album])
    client.get_new_playlists = AsyncMock(
        return_value=SimpleNamespace(new_playlists=[SimpleNamespace(uid=12345, kind=3)])
    )
    client.get_playlists = AsyncMock(return_value=[playlist])
    client.get_tag_playlists = AsyncMock(return_value=[playlist])
    client.get_landing_tags = AsyncMock(return_value=[])
    return client


def _install_cache_mocks(provider: KionMusicProvider) -> None:
    """Make the @use_cache decorator treat every call as a cache miss."""
    provider.mass.cache.get = AsyncMock(return_value=None)  # type: ignore[method-assign]
    provider.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
        return_value=(None, False, False)
    )
    provider.mass.cache.set = AsyncMock()  # type: ignore[method-assign]


def _awaited_methods(client: Mock) -> set[str]:
    """Return the names of backend client methods that were actually awaited."""
    return {name for name in CLIENT_METHODS if getattr(client, name).await_count}


@pytest.fixture
def provider() -> KionMusicProvider:
    """Create a real KionMusicProvider with mocked dependencies."""
    mass = Mock()
    mass.metadata.locale = "en_US"
    mass.translations.get_translation = Mock(return_value=None)
    # default: every cache lookup is a miss (tests override to simulate warm entries)
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    mass.cache.set = AsyncMock()
    use_real_create_task(mass)
    manifest = Mock()
    manifest.domain = "kion_music"
    config = Mock()
    config.instance_id = "kion_music--test123"
    config.name = "KION Music Test"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {
        "log_level": "GLOBAL",
    }.get(key, default)
    provider = KionMusicProvider(mass, manifest, config, SUPPORTED_FEATURES)
    provider._client = _make_client_mock()
    return provider


@pytest.mark.asyncio
async def test_get_recommendations_returns_static_rows_without_backend_calls(
    provider: KionMusicProvider,
) -> None:
    """get_recommendations() returns all nine row descriptors with zero backend I/O."""
    client = cast("Mock", provider.client)

    result = await provider.get_recommendations()

    assert [f.item_id for f in result] == ROW_IDS
    assert _awaited_methods(client) == set()
    assert all(not f.items for f in result)
    # Mood/Activity row titles are static; the rotating tag only shows as subtitle
    # once the tag-list cache is warm (cold here, so no subtitle).
    by_id = {f.item_id: f for f in result}
    assert by_id["mood_mix"].name == "Mood Mix"
    assert by_id["mood_mix"].translation_key == "mood_mix"
    assert by_id["activity_mix"].name == "Activity Mix"
    assert by_id["activity_mix"].translation_key == "activity_mix"
    assert by_id["seasonal_mix"].name.startswith("Seasonal: ")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("item_id", "expected_backend_calls"),
    [
        (MY_WAVE_PLAYLIST_ID, {"get_my_wave_tracks"}),
        ("feed", {"get_feed"}),
        ("chart", {"get_chart"}),
        ("new_releases", {"get_new_releases", "get_albums"}),
        ("new_playlists", {"get_new_playlists", "get_playlists"}),
        ("top_picks", {"get_tag_playlists"}),
        ("mood_mix", {"get_landing_tags", "get_tag_playlists"}),
        ("activity_mix", {"get_landing_tags", "get_tag_playlists"}),
        ("seasonal_mix", {"get_tag_playlists"}),
    ],
)
async def test_get_recommendation_items_triggers_only_that_rows_fetches(
    provider: KionMusicProvider, item_id: str, expected_backend_calls: set[str]
) -> None:
    """get_recommendation_items(row) issues only that row's backend fetches and returns items."""
    _install_cache_mocks(provider)
    client = cast("Mock", provider.client)

    result = await provider.get_recommendation_items(item_id)

    assert _awaited_methods(client) == expected_backend_calls
    assert len(result) > 0


@pytest.mark.asyncio
async def test_get_recommendation_items_unknown_id_returns_empty(
    provider: KionMusicProvider,
) -> None:
    """An unknown row item_id returns an empty list without any backend calls."""
    _install_cache_mocks(provider)
    client = cast("Mock", provider.client)

    result = await provider.get_recommendation_items("no_such_row")

    assert list(result) == []
    assert _awaited_methods(client) == set()


@pytest.mark.asyncio
async def test_recommendation_fetch_is_shared_between_callers(
    provider: KionMusicProvider,
) -> None:
    """Concurrent callers for one recommendation row share its backend fetch."""
    _install_cache_mocks(provider)
    client = cast("Mock", provider.client)
    chart = client.get_chart.return_value
    fetch_started = asyncio.Event()
    release_fetch = asyncio.Event()

    async def _get_chart() -> Any:
        fetch_started.set()
        await release_fetch.wait()
        return chart

    client.get_chart.side_effect = _get_chart
    tasks = [asyncio.create_task(provider.get_recommendation_items("chart")) for _ in range(3)]
    await asyncio.wait_for(fetch_started.wait(), timeout=1)
    await asyncio.sleep(0.05)
    release_fetch.set()

    results = await asyncio.gather(*tasks)

    assert all(result for result in results)
    assert client.get_chart.await_count == 1


def _install_tag_cache(provider: KionMusicProvider, tags_by_category: dict[str, list[str]]) -> None:
    """Serve the validated-tag-list cache entries as warm hits, everything else as a miss."""

    async def _cache_get_legacy(key: str, **_kwargs: Any) -> Any:
        for category, tags in tags_by_category.items():
            if key == f"_get_valid_tags_for_category.{category}":
                return tags
        return None

    async def _cache_get(key: str, **_kwargs: Any) -> tuple[Any, bool, bool]:
        for category, tags in tags_by_category.items():
            if key == f"_get_valid_tags_for_category.{category}":
                return tags, True, True
        return None, False, False

    provider.mass.cache.get = AsyncMock(  # type: ignore[method-assign]
        side_effect=_cache_get_legacy
    )
    provider.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
        side_effect=_cache_get
    )
    provider.mass.cache.set = AsyncMock()  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_expired_tag_list_is_served_while_refreshing(
    provider: KionMusicProvider,
) -> None:
    """An expired tag list remains available while its refresh runs in the background."""
    stale_tags = ["chill", "focus"]
    provider.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
        return_value=(stale_tags, False, True)
    )
    refresh_gate = asyncio.Event()
    refresh_started = asyncio.Event()

    async def _blocked_landing_tags() -> list[Any]:
        refresh_started.set()
        await refresh_gate.wait()
        return []

    client = cast("Mock", provider.client)
    client.get_landing_tags.side_effect = _blocked_landing_tags
    background_tasks: set[asyncio.Task[Any]] = set()

    def _create_task(target: Any, **_kwargs: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(target)
        background_tasks.add(task)
        return task

    provider.mass.create_task = Mock(side_effect=_create_task)  # type: ignore[method-assign]
    request = asyncio.create_task(provider._get_valid_tags_for_category("mood"))
    await asyncio.sleep(0)

    try:
        assert request.done()
        assert await request == stale_tags
        await asyncio.wait_for(refresh_started.wait(), timeout=1)
        assert any(not task.done() for task in background_tasks)
        provider.mass.cache.get_with_freshness.assert_awaited_once_with(
            "_get_valid_tags_for_category.mood",
            provider=provider.instance_id,
            checksum=None,
            category=0,
            allow_bypass=True,
            base_class=None,
            include_expired=True,
        )
    finally:
        request.cancel()
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(request, *background_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_rows_subtitle_matches_served_items_tag(
    provider: KionMusicProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a warm tag cache the rows subtitle and the items call resolve the same tag."""
    # freeze the clock so the hourly tag bucket cannot flip mid-test
    monkeypatch.setattr(
        "music_assistant.providers.kion_music.provider.utc",
        lambda: datetime(2026, 7, 24, 12, 30, tzinfo=UTC),
    )
    mood_tags = ["chill", "focus"]
    _install_tag_cache(provider, {"mood": mood_tags, "activity": ["workout"]})
    client = cast("Mock", provider.client)

    result = await provider.get_recommendations()

    by_id = {f.item_id: f for f in result}
    mood_tag = provider._rotating_row_tag("mood", mood_tags)
    assert by_id["mood_mix"].subtitle == mood_tag.title()
    assert by_id["activity_mix"].subtitle == "Workout"
    # deterministic: a second rows call yields the same subtitle
    again = {f.item_id: f for f in await provider.get_recommendations()}
    assert again["mood_mix"].subtitle == by_id["mood_mix"].subtitle
    # the tag came from the cache: rows still issue zero backend calls
    assert _awaited_methods(client) == set()

    await provider.get_recommendation_items("mood_mix")

    # the items call independently derived the same tag from the cached list
    client.get_tag_playlists.assert_any_await(mood_tag)
    assert "get_landing_tags" not in _awaited_methods(client)


@pytest.mark.asyncio
async def test_rows_without_cached_tags_have_no_subtitle(provider: KionMusicProvider) -> None:
    """With a cold tag cache the mood/activity rows have no subtitle."""
    _install_cache_mocks(provider)

    result = await provider.get_recommendations()

    by_id = {f.item_id: f for f in result}
    assert by_id["mood_mix"].subtitle is None
    assert by_id["activity_mix"].subtitle is None
