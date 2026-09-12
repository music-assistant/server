"""Tests for the iHeartRadio session, library sync and artist radio."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import MediaType, ProviderFeature, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Podcast, Radio

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.providers.iheartradio.auth import IHeartRadioAuthManager, IHeartRadioSession
from music_assistant.providers.iheartradio.constants import (
    BATCH_URL_TTL,
    CONF_PROFILE_ID,
    CONF_SESSION_ID,
    CONF_SESSION_USERNAME,
    PATH_ARTIST_STATION,
    PATH_FOLLOWS_ARTIST,
    PATH_FOLLOWS_ARTIST_ITEM,
    PATH_FOLLOWS_LIVE,
    PATH_FOLLOWS_LIVE_ITEM,
    PATH_GUEST_LOGIN,
    PATH_LIVE_STATION,
    PATH_LOGIN,
    PATH_PLAYBACK_REPORTING,
    PATH_PLAYBACK_STREAMS,
    PATH_PODCAST_FOLLOW_ITEM,
    PATH_PODCAST_FOLLOWS,
    PATH_SEARCH,
    PATH_SESSION,
)
from music_assistant.providers.iheartradio.provider import IHeartRadioProvider

from .conftest import (
    ARTIST_STATION,
    INSTANCE_ID,
    PODCAST,
    PROFILE_ID,
    RADIO_ITEM,
    SESSION_ID,
    STATION,
    FakeApi,
)

STATION_ID = str(STATION["id"])
TRACK_ID = str(RADIO_ITEM["content"]["id"])
ARTIST_RADIO_ID = "artist:1805"


def _auth(provider: IHeartRadioProvider) -> IHeartRadioAuthManager:
    """Return the provider's auth manager."""
    assert provider.auth is not None
    return provider.auth


def _configure(provider: IHeartRadioProvider, **setup: str) -> None:
    """Give the provider setup values and no session yet."""
    provider.get_setup_value = Mock(side_effect=setup.get)  # type: ignore[method-assign]
    _auth(provider).session = None


async def _reports(api: FakeApi) -> list[dict[str, Any]]:
    """Return the play reports sent so far, once the background tasks have run."""
    await asyncio.sleep(0)
    return [
        body
        for _, path, body in api.requests
        if path == PATH_PLAYBACK_REPORTING and isinstance(body, dict)
    ]


async def test_guest_session_without_credentials(
    provider: IHeartRadioProvider, api: FakeApi
) -> None:
    """Without credentials a guest session is opened, persisted, and adds no library features."""
    _configure(provider)
    api.responses[("POST", PATH_GUEST_LOGIN)] = {"profileId": 1, "sessionId": "guest-session"}
    persist = Mock()
    provider.mass.config.set_raw_provider_config_value = persist  # type: ignore[method-assign]
    await _auth(provider).login()
    assert _auth(provider).session == IHeartRadioSession("1", "guest-session", "")
    _, _, body = api.requests[-1]
    assert body is not None
    assert body["accessTokenType"] == "anon"
    persisted = {call.args[1]: call.args[2] for call in persist.call_args_list}
    assert persisted == {
        CONF_PROFILE_ID: "1",
        CONF_SESSION_ID: "guest-session",
        CONF_SESSION_USERNAME: "",
    }
    assert ProviderFeature.LIBRARY_RADIOS not in provider.supported_features


async def test_stored_session_is_reused(provider: IHeartRadioProvider, api: FakeApi) -> None:
    """A persisted session that the API still accepts skips the login."""
    _configure(provider, **{CONF_USERNAME: "gav@example.com", CONF_PASSWORD: "secret"})
    stored = {
        CONF_PROFILE_ID: PROFILE_ID,
        CONF_SESSION_ID: SESSION_ID,
        CONF_SESSION_USERNAME: "gav@example.com",
    }
    provider.mass.config.get_raw_provider_config_value = Mock(  # type: ignore[method-assign]
        side_effect=lambda _instance, key: stored.get(key)
    )
    await _auth(provider).login()
    assert [(method, path) for method, path, _ in api.requests] == [("HEAD", PATH_SESSION)]
    assert _auth(provider).is_account
    assert ProviderFeature.LIBRARY_PODCASTS_EDIT in provider.supported_features


async def test_account_login_when_stored_session_is_stale(
    provider: IHeartRadioProvider, api: FakeApi
) -> None:
    """A stored session of another account is dropped and the credentials are used."""
    _configure(provider, **{CONF_USERNAME: "gav@example.com", CONF_PASSWORD: "secret"})
    stored = {CONF_PROFILE_ID: "9", CONF_SESSION_ID: "old", CONF_SESSION_USERNAME: ""}
    provider.mass.config.get_raw_provider_config_value = Mock(  # type: ignore[method-assign]
        side_effect=lambda _instance, key: stored.get(key)
    )
    api.responses[("POST", PATH_LOGIN)] = {"profileId": PROFILE_ID, "sessionId": SESSION_ID}
    await _auth(provider).login()
    method, path, body = api.requests[-1]
    assert (method, path) == ("POST", PATH_LOGIN)
    assert body is not None
    assert body["userName"] == "gav@example.com"
    assert body["host"] == "webapp.AU"
    assert _auth(provider).session == IHeartRadioSession(PROFILE_ID, SESSION_ID, "gav@example.com")


async def test_library_radios(provider: IHeartRadioProvider, api: FakeApi) -> None:
    """Followed live stations come first, then followed artists as artist radios."""
    api.responses[PATH_FOLLOWS_LIVE] = {"data": [{"liveStationId": STATION["id"]}]}
    api.responses[PATH_LIVE_STATION.format(station_id=STATION_ID)] = {"hits": [STATION]}
    api.responses[PATH_FOLLOWS_ARTIST] = {
        "data": [{"stationId": ARTIST_STATION["id"], "artistSeed": 1805, "artistName": "Tom Petty"}]
    }
    radios = [radio async for radio in provider.get_library_radios()]
    assert [(radio.item_id, radio.name, radio.is_dynamic) for radio in radios] == [
        (STATION_ID, "KIIS 1065", False),
        (ARTIST_RADIO_ID, "Tom Petty Radio", True),
    ]
    # an empty follow list is a 404
    api.responses[PATH_FOLLOWS_LIVE] = MediaNotFoundError("not found")
    api.responses[PATH_FOLLOWS_ARTIST] = MediaNotFoundError("not found")
    assert [radio async for radio in provider.get_library_radios()] == []


async def test_library_podcasts_follow_the_cursor(
    provider: IHeartRadioProvider, api: FakeApi
) -> None:
    """Followed podcasts are read page by page and parsed from the follow entries themselves."""
    api.pages[PATH_PODCAST_FOLLOWS] = [
        {"data": [PODCAST], "links": {"next": "cursor-2"}},
        {"data": [{**PODCAST, "id": 2, "title": "Second"}], "links": {}},
    ]
    podcasts = [podcast async for podcast in provider.get_library_podcasts()]
    assert [podcast.name for podcast in podcasts] == [PODCAST["title"], "Second"]
    assert api.calls[-1][1]["pageKey"] == "cursor-2"


async def test_library_add_and_remove_routes(provider: IHeartRadioProvider, api: FakeApi) -> None:
    """Following and unfollowing hit the endpoint of the item's kind with numeric ids."""
    live = Radio(item_id=STATION_ID, provider=INSTANCE_ID, name="KIIS", provider_mappings=set())
    artist = Radio(
        item_id=ARTIST_RADIO_ID,
        provider=INSTANCE_ID,
        name="Tom Petty Radio",
        provider_mappings=set(),
    )
    podcast = Podcast(
        item_id="21124503", provider=INSTANCE_ID, name="History", provider_mappings=set()
    )
    for item in (live, artist, podcast):
        assert await provider.library_add(item)
        assert await provider.library_remove(item.item_id, item.media_type)
    assert api.requests == [
        ("PUT", PATH_FOLLOWS_LIVE, {"liveStationId": 6185}),
        ("DELETE", PATH_FOLLOWS_LIVE_ITEM.format(station_id=STATION_ID), None),
        ("PUT", PATH_FOLLOWS_ARTIST, {"artistId": 1805}),
        ("DELETE", PATH_FOLLOWS_ARTIST_ITEM.format(artist_id="1805"), None),
        ("PUT", PATH_PODCAST_FOLLOW_ITEM.format(podcast_id="21124503"), None),
        ("DELETE", PATH_PODCAST_FOLLOW_ITEM.format(podcast_id="21124503"), None),
    ]


async def test_artist_radio_batches_and_track_stream(
    provider: IHeartRadioProvider, api: FakeApi
) -> None:
    """A batch registers the station once, retains its tracks and streams them with a report."""
    station_path = PATH_ARTIST_STATION.format(profile_id=PROFILE_ID, artist_id="1805")
    api.responses[("POST", station_path)] = ARTIST_STATION
    api.responses[("POST", PATH_PLAYBACK_STREAMS)] = {"items": [RADIO_ITEM]}
    api.responses[("POST", PATH_PLAYBACK_REPORTING)] = {
        "hourSkipsRemaining": 6,
        "daySkipsRemaining": 14,
    }
    tracks = await provider.get_dynamic_radio_tracks(ARTIST_RADIO_ID)
    assert [(track.item_id, track.name, track.artist_str) for track in tracks] == [
        (TRACK_ID, "I Won't Back Down", "Tom Petty")
    ]
    assert tracks[0].album is not None
    assert tracks[0].album.name == "Full Moon Fever"
    # the audio url is not on the track
    assert "custom-hls" not in str(tracks[0].to_dict())
    await provider.get_dynamic_radio_tracks(ARTIST_RADIO_ID)
    assert [path for _, path, _ in api.requests].count(station_path) == 1
    batch_request = next(body for _, path, body in api.requests if path == PATH_PLAYBACK_STREAMS)
    assert batch_request is not None
    assert batch_request["stationId"] == ARTIST_STATION["id"]

    details = await provider.get_stream_details(TRACK_ID, MediaType.TRACK)
    assert details.stream_type == StreamType.HLS
    assert details.path == RADIO_ITEM["streamUrl"]
    assert details.duration == 176
    assert 0 < details.expiration <= BATCH_URL_TTL
    reports = await _reports(api)
    assert [(report["status"], report["secondsPlayed"]) for report in reports] == [("START", 0)]
    assert reports[0]["reportPayload"] == "opaque-report-token"
    assert reports[0]["stationId"] == ARTIST_STATION["id"]
    # a retained track resolves without the catalog
    assert (await provider.get_track(TRACK_ID)).name == "I Won't Back Down"


async def test_expired_batch_is_refused(provider: IHeartRadioProvider, api: FakeApi) -> None:
    """A track whose batch has outlived its audio urls is refused instead of handed to ffmpeg."""
    station = provider.stations.register("1805", ARTIST_STATION["id"], now=0)
    station.add_batch({TRACK_ID: RADIO_ITEM}, now=0)
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details(TRACK_ID, MediaType.TRACK)
    assert await _reports(api) == []


async def test_on_played_reports_done_or_skip(provider: IHeartRadioProvider, api: FakeApi) -> None:
    """A finished track is reported as DONE, a stopped one as SKIP with its position."""
    station = provider.stations.register("1805", ARTIST_STATION["id"], now=1e12)
    station.add_batch({TRACK_ID: RADIO_ITEM}, now=1e12)
    api.responses[("POST", PATH_PLAYBACK_REPORTING)] = {}
    track = Mock()
    await provider.on_played(MediaType.TRACK, TRACK_ID, True, 176, track)
    await provider.on_played(MediaType.TRACK, TRACK_ID, False, 40, track)
    # still playing and "mark as unplayed" are not plays
    await provider.on_played(MediaType.TRACK, TRACK_ID, False, 40, track, is_playing=True)
    await provider.on_played(MediaType.TRACK, TRACK_ID, False, 0, track)
    reports = await _reports(api)
    assert [(report["status"], report["secondsPlayed"]) for report in reports] == [
        ("DONE", 176),
        ("SKIP", 40),
    ]


async def test_search_offers_artists_as_artist_radio(
    provider: IHeartRadioProvider, api: FakeApi
) -> None:
    """An artist hit becomes its artist radio, listed after the live stations."""
    api.responses[PATH_SEARCH] = {
        "results": {
            "stations": [{"id": 1, "name": "One"}],
            "artists": [{"id": 1805, "name": "Tom Petty", "image": "https://i.iheart.com/x.jpg"}],
        }
    }
    results = await provider.search("tom", [MediaType.RADIO])
    assert [(radio.item_id, radio.name) for radio in results.radio] == [
        ("1", "One"),
        (ARTIST_RADIO_ID, "Tom Petty Radio"),
    ]
    assert api.calls[-1][1]["artist"] == "true"


async def test_rejected_session_is_renewed_once(provider: IHeartRadioProvider) -> None:
    """A request the API answers with 401 is retried once with a fresh session."""
    statuses = [401, 200]
    seen_headers: list[dict[str, str]] = []

    @asynccontextmanager
    async def fake_request(_method: str, _url: str, **kwargs: Any) -> AsyncIterator[Mock]:
        seen_headers.append(kwargs["headers"])
        status = statuses.pop(0)
        yield Mock(status=status, headers={}, read=AsyncMock(return_value=b'{"ok": true}'))

    provider.mass = Mock(http_session=Mock(request=fake_request))
    provider.request = IHeartRadioProvider.request.__get__(provider)  # type: ignore[method-assign]

    async def relogin() -> None:
        _auth(provider).session = IHeartRadioSession("2", "fresh", "")

    _auth(provider).relogin = relogin  # type: ignore[method-assign]
    assert await provider.request("GET", "/api/v3/anything") == {"ok": True}
    assert [headers["X-IHR-Session-ID"] for headers in seen_headers] == [SESSION_ID, "fresh"]
