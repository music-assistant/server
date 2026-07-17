"""Test KION Music recommendations() row filtering via the `wanted` parameter."""

from __future__ import annotations

import json
import pathlib
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

ALL_ITEM_IDS = {
    MY_WAVE_PLAYLIST_ID,
    "feed",
    "chart",
    "new_releases",
    "new_playlists",
    "top_picks",
    "mood_mix",
    "activity_mix",
    "seasonal_mix",
}


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
async def test_recommendations_wanted_none_fetches_all_rows(provider: KionMusicProvider) -> None:
    """wanted=None (default) fetches and builds all nine rows — unchanged behavior."""
    _install_cache_mocks(provider)
    client = cast("Mock", provider.client)

    result = await provider.recommendations()

    assert _awaited_methods(client) == set(CLIENT_METHODS)
    assert {f.item_id for f in result} == ALL_ITEM_IDS


@pytest.mark.asyncio
async def test_recommendations_wanted_feed_only_fetches_feed(provider: KionMusicProvider) -> None:
    """wanted={'feed'} issues only the feed backend fetch and returns only that row."""
    _install_cache_mocks(provider)
    client = cast("Mock", provider.client)

    result = await provider.recommendations(wanted={"feed"})

    client.get_feed.assert_awaited_once()
    assert _awaited_methods(client) == {"get_feed"}
    assert [f.item_id for f in result] == ["feed"]


@pytest.mark.asyncio
async def test_recommendations_wanted_my_wave_uses_constant(provider: KionMusicProvider) -> None:
    """wanted={MY_WAVE_PLAYLIST_ID} issues only the My Mix fetch and returns only that row."""
    _install_cache_mocks(provider)
    client = cast("Mock", provider.client)

    result = await provider.recommendations(wanted={MY_WAVE_PLAYLIST_ID})

    assert _awaited_methods(client) == {"get_my_wave_tracks"}
    assert [f.item_id for f in result] == [MY_WAVE_PLAYLIST_ID]
