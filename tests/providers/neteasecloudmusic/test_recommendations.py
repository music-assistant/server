"""Test NetEase Cloud Music two-method recommendations contract."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from music_assistant.providers.neteasecloudmusic import NeteaseCloudMusicProvider

_SONG = {
    "id": 1001,
    "name": "Song One",
    "dt": 200000,
    "ar": [{"id": 5, "name": "Artist"}],
    "al": {"id": 7, "name": "Album", "picUrl": "https://p1.music.126.net/a.jpg"},
}
PERSONAL_FM_PAYLOAD = {"code": 200, "data": [{"song": _SONG}]}
DAILY_PAYLOAD = {"code": 200, "data": {"dailySongs": [_SONG]}}
USER_PLAYLIST_PAYLOAD = {
    "code": 200,
    "playlist": [
        {"id": 2002, "name": "My Playlist", "coverImgUrl": "https://p1.music.126.net/p.jpg"}
    ],
}
NEWSONG_PAYLOAD = {"code": 200, "result": [{"song": {**_SONG, "id": 1002}}]}
PLAYLISTS_PAYLOAD = {
    "code": 200,
    "result": [{"id": 3003, "name": "Rec Playlist", "picUrl": "https://p1.music.126.net/r.jpg"}],
}


def _stub_client_get(provider: NeteaseCloudMusicProvider) -> AsyncMock:
    """Attach a client.get stub that returns canned payloads keyed by path."""

    async def _fake(path: str, **_kwargs: Any) -> dict[str, Any]:
        return {
            "/personal_fm": PERSONAL_FM_PAYLOAD,
            "/recommend/songs": DAILY_PAYLOAD,
            "/user/playlist": USER_PLAYLIST_PAYLOAD,
            "/personalized/newsong": NEWSONG_PAYLOAD,
            "/personalized": PLAYLISTS_PAYLOAD,
        }[path]

    mock = AsyncMock(side_effect=_fake)
    provider._client = Mock(get=mock)
    return mock


def _install_cache_mocks(provider: NeteaseCloudMusicProvider) -> None:
    """Make the recommendation payload cache treat every call as a miss."""
    provider.mass.cache.get = AsyncMock(return_value=None)  # type: ignore[method-assign]
    provider.mass.cache.set = AsyncMock()  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_get_recommendations_static_rows_without_backend_calls(
    provider: NeteaseCloudMusicProvider,
) -> None:
    """get_recommendations returns all four row descriptors without any backend call."""
    client_mock = _stub_client_get(provider)

    result = await provider.get_recommendations()

    assert client_mock.call_args_list == []
    assert [folder.item_id for folder in result] == [
        "recommended_radios",
        "daily_songs",
        "recommended_new_songs",
        "recommended_playlists",
    ]
    assert all(not folder.items for folder in result)


@pytest.mark.asyncio
async def test_get_recommendation_items_radios(
    provider: NeteaseCloudMusicProvider,
) -> None:
    """recommended_radios builds both dynamic playlists from its dedicated fetches only."""
    _install_cache_mocks(provider)
    client_mock = _stub_client_get(provider)

    result = await provider.get_recommendation_items("recommended_radios")

    called_paths = {call.args[0] for call in client_mock.call_args_list}
    assert called_paths == {"/personal_fm", "/recommend/songs", "/user/playlist"}
    assert [item.item_id for item in result] == [
        "personal_fm_dynamic",
        "heart_mode_dynamic:1001:2002",
    ]


@pytest.mark.asyncio
async def test_get_recommendation_items_daily_songs(
    provider: NeteaseCloudMusicProvider,
) -> None:
    """daily_songs fetches only /recommend/songs and returns the parsed tracks."""
    _install_cache_mocks(provider)
    client_mock = _stub_client_get(provider)

    result = await provider.get_recommendation_items("daily_songs")

    called_paths = [call.args[0] for call in client_mock.call_args_list]
    assert called_paths == ["/recommend/songs"]
    assert [item.item_id for item in result] == ["1001"]


@pytest.mark.asyncio
async def test_get_recommendation_items_new_songs(
    provider: NeteaseCloudMusicProvider,
) -> None:
    """recommended_new_songs fetches only /personalized/newsong and returns the parsed tracks."""
    _install_cache_mocks(provider)
    client_mock = _stub_client_get(provider)

    result = await provider.get_recommendation_items("recommended_new_songs")

    called_paths = [call.args[0] for call in client_mock.call_args_list]
    assert called_paths == ["/personalized/newsong"]
    assert [item.item_id for item in result] == ["1002"]


@pytest.mark.asyncio
async def test_get_recommendation_items_playlists(
    provider: NeteaseCloudMusicProvider,
) -> None:
    """recommended_playlists fetches only /personalized and returns the parsed playlists."""
    _install_cache_mocks(provider)
    client_mock = _stub_client_get(provider)

    result = await provider.get_recommendation_items("recommended_playlists")

    called_paths = [call.args[0] for call in client_mock.call_args_list]
    assert called_paths == ["/personalized"]
    assert [item.item_id for item in result] == ["3003"]


@pytest.mark.asyncio
async def test_get_recommendation_items_unknown_id_returns_empty(
    provider: NeteaseCloudMusicProvider,
) -> None:
    """An unknown row item_id returns an empty result without any backend call."""
    client_mock = _stub_client_get(provider)

    result = await provider.get_recommendation_items("no_such_row")

    assert client_mock.call_args_list == []
    assert not result
