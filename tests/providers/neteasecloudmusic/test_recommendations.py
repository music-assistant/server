"""Test NetEase Cloud Music two-method recommendations contract."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from music_assistant.providers.neteasecloudmusic import NeteaseCloudMusicProvider
from tests.common import use_real_create_task

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
PLAYLIST_DETAIL_PAYLOAD = {
    "code": 200,
    "playlist": {
        "id": 2002,
        "name": "My Playlist",
        "coverImgUrl": "https://p1.music.126.net/p.jpg",
    },
}
PLAYLIST_TRACKS_PAYLOAD = {"code": 200, "songs": [_SONG]}


def _stub_client_get(provider: NeteaseCloudMusicProvider) -> AsyncMock:
    """Attach a client.get stub that returns canned payloads keyed by path."""

    async def _fake(path: str, **_kwargs: Any) -> dict[str, Any]:
        return {
            "/personal_fm": PERSONAL_FM_PAYLOAD,
            "/recommend/songs": DAILY_PAYLOAD,
            "/user/playlist": USER_PLAYLIST_PAYLOAD,
            "/personalized/newsong": NEWSONG_PAYLOAD,
            "/personalized": PLAYLISTS_PAYLOAD,
            "/playlist/detail": PLAYLIST_DETAIL_PAYLOAD,
            "/playlist/track/all": PLAYLIST_TRACKS_PAYLOAD,
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
    fm_call = next(call for call in client_mock.call_args_list if call.args[0] == "/personal_fm")
    assert fm_call.kwargs["params"]["cookie"] == "MUSIC_U=test"
    assert fm_call.kwargs["cookie"] == "MUSIC_U=test"
    user_playlist_call = next(
        call for call in client_mock.call_args_list if call.args[0] == "/user/playlist"
    )
    assert user_playlist_call.kwargs["params"]["cookie"] == "MUSIC_U=test"
    assert user_playlist_call.kwargs["cookie"] == "MUSIC_U=test"
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
    call = client_mock.call_args_list[0]
    assert call.kwargs["params"]["cookie"] == "MUSIC_U=test"
    assert call.kwargs["cookie"] == "MUSIC_U=test"


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
    call = client_mock.call_args_list[0]
    assert call.kwargs["params"]["cookie"] == "MUSIC_U=test"
    assert call.kwargs["cookie"] == "MUSIC_U=test"
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


@pytest.mark.asyncio
async def test_recommendation_cache_key_excludes_login_cookie(
    provider: NeteaseCloudMusicProvider,
) -> None:
    """The login cookie sent as a query param never reaches the persisted cache key."""
    provider.mass.cache.get = AsyncMock(return_value=None)  # type: ignore[method-assign]
    cache_set = AsyncMock()
    provider.mass.cache.set = cache_set  # type: ignore[method-assign]
    _stub_client_get(provider)

    await provider.get_recommendation_items("daily_songs")

    assert cache_set.await_count == 1
    cache_key = cache_set.call_args.kwargs["key"]
    assert cache_key.endswith("daily_songs:{}")
    assert "MUSIC_U=test" not in cache_key


@pytest.mark.asyncio
async def test_pick_personal_fm_fresh_sends_cookie_in_params(
    provider: NeteaseCloudMusicProvider,
) -> None:
    """The fresh-FM path passes the login cookie as a query param alongside the header."""
    client_mock = _stub_client_get(provider)

    await provider._pick_personal_fm_tracks(fresh=True, target_count=1)

    assert client_mock.call_args_list
    for call in client_mock.call_args_list:
        assert call.args[0] == "/personal_fm"
        assert call.kwargs["params"]["cookie"] == "MUSIC_U=test"
        assert call.kwargs["cookie"] == "MUSIC_U=test"


@pytest.mark.asyncio
async def test_get_playlist_sends_cookie_in_params(
    provider: NeteaseCloudMusicProvider,
) -> None:
    """Playlist detail passes the login cookie as a query param alongside the header."""
    use_real_create_task(provider.mass)
    provider.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
        return_value=(None, False, False)
    )
    provider.mass.cache.set = AsyncMock()  # type: ignore[method-assign]
    client_mock = _stub_client_get(provider)

    await provider.get_playlist("2002")

    call = client_mock.call_args_list[0]
    assert call.args[0] == "/playlist/detail"
    assert call.kwargs["params"]["cookie"] == "MUSIC_U=test"
    assert call.kwargs["cookie"] == "MUSIC_U=test"


@pytest.mark.asyncio
async def test_get_playlist_tracks_sends_cookie_in_params(
    provider: NeteaseCloudMusicProvider,
) -> None:
    """Playlist track listing passes the login cookie as a query param alongside the header."""
    use_real_create_task(provider.mass)
    provider.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
        return_value=(None, False, False)
    )
    provider.mass.cache.set = AsyncMock()  # type: ignore[method-assign]
    client_mock = _stub_client_get(provider)

    await provider._get_playlist_tracks_cached("2002")

    call = client_mock.call_args_list[0]
    assert call.args[0] == "/playlist/track/all"
    assert call.kwargs["params"]["cookie"] == "MUSIC_U=test"
    assert call.kwargs["cookie"] == "MUSIC_U=test"
