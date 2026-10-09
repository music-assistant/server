"""Test NetEase Cloud Music two-method recommendations contract."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.errors import ResourceTemporarilyUnavailable
from music_assistant_models.media_items import Playlist

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
SONG_DETAIL_PAYLOAD = {"code": 200, "songs": [_SONG]}
_HEART_SONG = {
    **_SONG,
    "id": 1003,
    "al": {"id": 8, "name": "Album", "picUrl": "https://p1.music.126.net/heart.jpg"},
}
HEART_MODE_TRACKS_PAYLOAD = {"code": 200, "data": [{"songInfo": _HEART_SONG}]}
RADAR_DETAIL_PAYLOADS = {
    "3136952023": {
        "code": 200,
        "playlist": {
            "id": 3136952023,
            "name": "私人雷达",
            "coverImgUrl": "https://p1.music.126.net/radar1.jpg",
        },
    },
    "5320167908": {
        "code": 200,
        "playlist": {
            "id": 5320167908,
            "name": "时光雷达",
            "coverImgUrl": "https://p1.music.126.net/radar2.jpg",
        },
    },
}
_SONG_NO_COVER = {**_SONG, "id": 1004, "al": {"id": 9, "name": "Album"}}
_SONG_LATER_COVER = {
    **_SONG,
    "id": 1005,
    "al": {"id": 10, "name": "Album", "picUrl": "https://p1.music.126.net/later.jpg"},
}
PERSONAL_FM_NO_COVER_PAYLOAD = {"code": 200, "data": [{"song": _SONG_NO_COVER}]}
DAILY_MIXED_COVER_PAYLOAD = {
    "code": 200,
    "data": {"dailySongs": [_SONG_NO_COVER, _SONG_LATER_COVER]},
}


def _stub_client_get(
    provider: NeteaseCloudMusicProvider,
    *,
    fail_paths: set[str] | None = None,
    overrides: dict[str, dict[str, Any]] | None = None,
) -> AsyncMock:
    """Attach a client.get stub that returns canned payloads keyed by path."""

    async def _fake(path: str, **kwargs: Any) -> dict[str, Any]:
        if fail_paths and path in fail_paths:
            raise ResourceTemporarilyUnavailable(f"stubbed failure for {path}")
        if overrides and path in overrides:
            return overrides[path]
        if path == "/playlist/detail" and kwargs.get("params", {}).get("id"):
            radar_id = str(kwargs["params"]["id"])
            if radar_id in RADAR_DETAIL_PAYLOADS:
                return RADAR_DETAIL_PAYLOADS[radar_id]
        return {
            "/personal_fm": PERSONAL_FM_PAYLOAD,
            "/recommend/songs": DAILY_PAYLOAD,
            "/user/playlist": USER_PLAYLIST_PAYLOAD,
            "/personalized/newsong": NEWSONG_PAYLOAD,
            "/personalized": PLAYLISTS_PAYLOAD,
            "/playlist/detail": PLAYLIST_DETAIL_PAYLOAD,
            "/playlist/track/all": PLAYLIST_TRACKS_PAYLOAD,
            "/song/detail": SONG_DETAIL_PAYLOAD,
            "/playmode/intelligence/list": HEART_MODE_TRACKS_PAYLOAD,
        }[path]

    mock = AsyncMock(side_effect=_fake)
    provider._client = Mock(get=mock)
    return mock


def _install_cache_mocks(provider: NeteaseCloudMusicProvider) -> None:
    """Make the recommendation payload cache treat every call as a miss."""
    use_real_create_task(provider.mass)
    provider.mass.cache.get = AsyncMock(return_value=None)  # type: ignore[method-assign]
    provider.mass.cache.set = AsyncMock()  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_get_recommendations_static_rows_without_backend_calls(
    provider: NeteaseCloudMusicProvider,
) -> None:
    """get_recommendations returns all three row descriptors without any backend call."""
    client_mock = _stub_client_get(provider)

    result = await provider.get_recommendations()

    assert client_mock.call_args_list == []
    assert [folder.item_id for folder in result] == [
        "personal_recommend",
        "recommended_new_songs",
        "recommended_playlists",
    ]
    assert all(not folder.items for folder in result)


@pytest.mark.asyncio
async def test_get_recommendation_items_personal_recommend(
    provider: NeteaseCloudMusicProvider,
) -> None:
    """personal_recommend builds the five personalized playlists in the fixed order."""
    _install_cache_mocks(provider)
    client_mock = _stub_client_get(provider)

    result = await provider.get_recommendation_items("personal_recommend")

    assert [item.item_id for item in result] == [
        "personal_fm_dynamic",
        "daily_recommend_dynamic",
        "personal_radar_dynamic",
        "time_radar_dynamic",
        "heart_mode_dynamic:1001:2002",
    ]
    assert result[1].name == "Daily Recommendations"
    # the radars use the official (account-localized) NCM names
    assert result[2].name == "私人雷达"
    assert result[3].name == "时光雷达"
    assert isinstance(result[2], Playlist)
    assert isinstance(result[3], Playlist)
    assert result[2].translation_key is None
    assert result[3].translation_key is None
    # every dynamic playlist carries a real cover image, never a placeholder
    assert all(isinstance(item, Playlist) and item.metadata.images for item in result)
    # heart mode uses the first recommended track's art, not the daily/likes covers
    assert isinstance(result[4], Playlist)
    heart_images = result[4].metadata.images
    assert heart_images
    assert next(iter(heart_images)).path == "https://p1.music.126.net/heart.jpg"
    called_paths = [call.args[0] for call in client_mock.call_args_list]
    # the concurrent builders share one coalesced fetch of the daily payload
    assert called_paths.count("/recommend/songs") == 1
    assert set(called_paths) == {
        "/personal_fm",
        "/recommend/songs",
        "/playlist/detail",
        "/user/playlist",
        "/playmode/intelligence/list",
    }
    radar_calls = [
        call for call in client_mock.call_args_list if call.args[0] == "/playlist/detail"
    ]
    assert {call.kwargs["params"]["id"] for call in radar_calls} == {"3136952023", "5320167908"}
    # each radar detail is fetched exactly once and reused for both name and cover
    assert len(radar_calls) == 2


@pytest.mark.asyncio
async def test_get_recommendation_items_personal_recommend_survives_daily_failure(
    provider: NeteaseCloudMusicProvider,
) -> None:
    """A failing daily request must not take down the FM and radar items of the row."""
    _install_cache_mocks(provider)
    client_mock = _stub_client_get(provider, fail_paths={"/recommend/songs"})

    result = await provider.get_recommendation_items("personal_recommend")

    ids = [item.item_id for item in result]
    assert "personal_fm_dynamic" in ids
    assert "personal_radar_dynamic" in ids
    assert "time_radar_dynamic" in ids
    # heart mode needs a daily seed song, so it drops out on its own
    assert not any(item_id.startswith("heart_mode_dynamic") for item_id in ids)
    # the failed daily request is attempted once and never retried by the builders
    called_paths = [call.args[0] for call in client_mock.call_args_list]
    assert called_paths.count("/recommend/songs") == 1


@pytest.mark.asyncio
async def test_personal_fm_cover_scans_daily_songs_for_artwork(
    provider: NeteaseCloudMusicProvider,
) -> None:
    """FM with no cover on its own track falls back to the first daily song that has art."""
    _install_cache_mocks(provider)
    _stub_client_get(
        provider,
        overrides={
            "/personal_fm": PERSONAL_FM_NO_COVER_PAYLOAD,
            "/recommend/songs": DAILY_MIXED_COVER_PAYLOAD,
        },
    )

    result = await provider.get_recommendation_items("personal_recommend")

    fm = next(item for item in result if item.item_id == "personal_fm_dynamic")
    assert isinstance(fm, Playlist)
    images = fm.metadata.images
    assert images
    # the first daily song has no art, so the scan must reach the later one
    assert next(iter(images)).path == "https://p1.music.126.net/later.jpg"


@pytest.mark.asyncio
async def test_get_playlist_dynamic_recommend_playlists(
    provider: NeteaseCloudMusicProvider,
) -> None:
    """The daily/radar dynamic playlist ids resolve with name and real cover image."""
    use_real_create_task(provider.mass)
    provider.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
        return_value=(None, False, False)
    )
    _install_cache_mocks(provider)
    client_mock = _stub_client_get(provider)

    daily = await provider.get_playlist("daily_recommend_dynamic")
    radar = await provider.get_playlist("personal_radar_dynamic")
    time_radar = await provider.get_playlist("time_radar_dynamic")

    called_paths = {call.args[0] for call in client_mock.call_args_list}
    assert called_paths == {"/recommend/songs", "/playlist/detail"}
    # dynamic playlists are served outside the long-lived static playlist cache
    assert provider.mass.cache.get_with_freshness.await_count == 0
    assert daily.item_id == "daily_recommend_dynamic"
    assert daily.name == "Daily Recommendations"
    assert daily.is_dynamic
    assert daily.metadata.images
    assert radar.item_id == "personal_radar_dynamic"
    assert radar.name == "私人雷达"
    assert radar.translation_key is None
    assert radar.metadata.images
    assert time_radar.item_id == "time_radar_dynamic"
    assert time_radar.name == "时光雷达"
    assert time_radar.translation_key is None
    assert time_radar.metadata.images


@pytest.mark.asyncio
async def test_get_playlist_heart_mode_carries_feed_cover(
    provider: NeteaseCloudMusicProvider,
) -> None:
    """The heart mode playlist detail resolves the first recommended track's cover."""
    use_real_create_task(provider.mass)
    provider.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
        return_value=(None, False, False)
    )
    _install_cache_mocks(provider)
    _stub_client_get(provider)

    playlist = await provider.get_playlist("heart_mode_dynamic:1001:2002")

    assert playlist.item_id == "heart_mode_dynamic:1001:2002"
    assert playlist.name == "Heart Mode"
    images = playlist.metadata.images
    assert images
    assert next(iter(images)).path == "https://p1.music.126.net/heart.jpg"


@pytest.mark.asyncio
async def test_get_playlist_tracks_daily_recommend(
    provider: NeteaseCloudMusicProvider,
) -> None:
    """The daily recommendations dynamic playlist returns the daily songs as tracks."""
    _install_cache_mocks(provider)
    client_mock = _stub_client_get(provider)

    result = await provider.get_playlist_tracks("daily_recommend_dynamic")

    called_paths = [call.args[0] for call in client_mock.call_args_list]
    assert called_paths == ["/recommend/songs"]
    assert [track.item_id for track in result] == ["1001"]
    assert result[0].position == 1
    assert result[0].duration == 200


@pytest.mark.asyncio
async def test_get_playlist_tracks_personal_radar(
    provider: NeteaseCloudMusicProvider,
) -> None:
    """The personal radar dynamic playlist streams tracks of the official radar playlist."""
    use_real_create_task(provider.mass)
    provider.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
        return_value=(None, False, False)
    )
    provider.mass.cache.set = AsyncMock()  # type: ignore[method-assign]
    client_mock = _stub_client_get(provider)

    result = await provider.get_playlist_tracks("personal_radar_dynamic")

    call = client_mock.call_args_list[0]
    assert call.args[0] == "/playlist/track/all"
    assert call.kwargs["params"]["id"] == "3136952023"
    assert [track.item_id for track in result] == ["1001"]


@pytest.mark.asyncio
async def test_radar_tracks_cache_key_includes_account(
    provider: NeteaseCloudMusicProvider,
) -> None:
    """Radar track listings are cached per account, never shared across accounts."""
    use_real_create_task(provider.mass)
    provider.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
        return_value=(None, False, False)
    )
    cache_set = AsyncMock()
    provider.mass.cache.set = cache_set  # type: ignore[method-assign]
    _stub_client_get(provider)

    await provider.get_playlist_tracks("personal_radar_dynamic")

    assert cache_set.await_count == 1
    cache_key = cache_set.call_args.kwargs["key"]
    assert "3136952023" in cache_key
    # the account uid is part of the key (fixture uid is 42), so another account cannot reuse it
    assert "account_scope42" in cache_key


@pytest.mark.asyncio
async def test_personal_recommend_radar_detail_failure_is_not_retried(
    provider: NeteaseCloudMusicProvider,
) -> None:
    """A failed radar detail fetch is not retried for the cover, nor triggers the track fallback."""
    _install_cache_mocks(provider)
    client_mock = _stub_client_get(provider, fail_paths={"/playlist/detail"})

    result = await provider.get_recommendation_items("personal_recommend")

    ids = [item.item_id for item in result]
    assert "personal_radar_dynamic" in ids
    assert "time_radar_dynamic" in ids
    called_paths = [call.args[0] for call in client_mock.call_args_list]
    # each radar detail is attempted exactly once, never retried for the cover
    assert called_paths.count("/playlist/detail") == 2
    # a failed detail fetch must not fall back to the radar track listing
    assert "/playlist/track/all" not in called_paths


@pytest.mark.asyncio
async def test_radar_metadata_cache_key_includes_account(
    provider: NeteaseCloudMusicProvider,
) -> None:
    """The radar detail cache is scoped per account, so it is not reused across accounts."""
    provider.mass.cache.get = AsyncMock(return_value=None)  # type: ignore[method-assign]
    cache_set = AsyncMock()
    provider.mass.cache.set = cache_set  # type: ignore[method-assign]
    _stub_client_get(provider)

    await provider._get_radar_playlist_detail("3136952023")

    assert cache_set.await_count == 1
    cache_key = cache_set.call_args.kwargs["key"]
    # the uid (fixture uid is 42) is part of the key
    assert "radar_playlist_42_3136952023" in cache_key


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
    use_real_create_task(provider.mass)
    provider.mass.cache.get = AsyncMock(return_value=None)  # type: ignore[method-assign]
    cache_set = AsyncMock()
    provider.mass.cache.set = cache_set  # type: ignore[method-assign]
    _stub_client_get(provider)

    await provider.get_playlist_tracks("daily_recommend_dynamic")

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
