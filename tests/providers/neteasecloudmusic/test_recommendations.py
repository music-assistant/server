"""Test NetEase Cloud Music recommendations() row filtering via the `wanted` parameter."""

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
async def test_recommendations_wanted_none_fetches_all_rows(
    provider: NeteaseCloudMusicProvider,
) -> None:
    """wanted=None (default) fetches and builds all four rows — unchanged behavior."""
    _install_cache_mocks(provider)
    client_mock = _stub_client_get(provider)

    result = await provider.recommendations()

    called_paths = {call.args[0] for call in client_mock.call_args_list}
    assert called_paths == {
        "/personal_fm",
        "/recommend/songs",
        "/user/playlist",
        "/personalized/newsong",
        "/personalized",
    }
    assert [folder.item_id for folder in result] == [
        "recommended_radios",
        "daily_songs",
        "recommended_new_songs",
        "recommended_playlists",
    ]


@pytest.mark.asyncio
async def test_recommendations_wanted_daily_songs_only_fetches_daily(
    provider: NeteaseCloudMusicProvider,
) -> None:
    """wanted={"daily_songs"} fetches only /recommend/songs and returns only that row."""
    _install_cache_mocks(provider)
    client_mock = _stub_client_get(provider)

    result = await provider.recommendations(wanted={"daily_songs"})

    called_paths = [call.args[0] for call in client_mock.call_args_list]
    assert called_paths == ["/recommend/songs"]
    assert [folder.item_id for folder in result] == ["daily_songs"]
