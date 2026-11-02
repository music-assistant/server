"""YouTube visibility survives the real helper, parser, cache and sharing API paths."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import nullcontext
from time import time
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import urlsplit

import pytest
import requests
from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import ProviderAccess, ProviderConfig
from music_assistant_models.enums import ProviderSharing, ProviderType
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError
from music_assistant_models.provider import ProviderManifest
from ytmusicapi import YTMusic

from music_assistant.controllers.music.constants import CACHE_CATEGORY_SHARE_VISIBILITY
from music_assistant.controllers.music.sharing import (
    check_share_source_access,
    share_source_access,
)
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_user,
    set_current_user,
)
from music_assistant.helpers.throttle_retry import Throttler, ThrottlerManager
from music_assistant.helpers.util import scoped_task_id
from music_assistant.mass import MusicAssistant
from music_assistant.providers.ytmusic import YoutubeMusicProvider
from tests.common import set_music_source_access
from tests.controllers.music.test_sharing import SPOTIFY_ID, USER_PATH, mapping
from tests.controllers.music.test_sharing_authorization_race import _revoke_sources

if TYPE_CHECKING:
    from music_assistant_models.media_items import Track

INSTANCE = "ytmusic--account"
VIDEO_ID = "AjXQiKP5kMs"


@pytest.fixture(autouse=True)
def block_external_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any transport or socket connection escaping this module's HTTP fixtures."""

    def blocked(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("Unexpected external HTTP or socket connection")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)


@pytest.fixture
def fake_credentials() -> dict[str, str]:
    """Provide only fabricated browser credentials, without a cached visitor ID."""
    return {
        "cookie": "__Secure-3PAPISID=test-cookie",
        "authorization": "SAPISIDHASH test-authorization",
        "origin": "https://music.youtube.com",
    }


@pytest.fixture
def provider(
    music_mass_with_cache: MusicAssistant, fake_credentials: dict[str, str]
) -> YoutubeMusicProvider:
    """Use real provider parsing, caching and catalogue resolution with temporary storage."""
    mass = music_mass_with_cache
    mass.streams = Mock()
    mass.streams.audio_analysis.get_track_audio_metadata = AsyncMock(return_value=None)
    set_music_source_access(mass, {INSTANCE: None})
    result = YoutubeMusicProvider(
        mass,
        ProviderManifest(
            type=ProviderType.MUSIC,
            domain="ytmusic",
            name="YouTube Music",
            description="Test provider",
            codeowners=[],
        ),
        ProviderConfig(type=ProviderType.MUSIC, domain="ytmusic", instance_id=INSTANCE, values={}),
    )
    result._headers = fake_credentials
    result.language = "en"
    result.available = True
    result.throttler = ThrottlerManager(rate_limit=1, period=0.001)
    mass._providers[INSTANCE] = result
    return result


@pytest.fixture
def song_response() -> dict[str, Any]:
    """Reproduce the private-upload fields in pinned ytmusicapi 1.12.3's get_song example."""
    return {
        "videoDetails": {
            "videoId": VIDEO_ID,
            "title": "Sparks",
            "lengthSeconds": "245",
            "channelId": "UCvCk2zFqkCYzpnSgWfx0qOg",
            "author": "Thomas Bergersen",
            "isPrivate": True,
            "isCrawlable": False,
            "musicVideoType": "MUSIC_VIDEO_TYPE_PRIVATELY_OWNED_TRACK",
        },
        "microformat": {
            "microformatDataRenderer": {
                "thumbnail": {"thumbnails": []},
                "unlisted": True,
            }
        },
        "playabilityStatus": {"status": "OK"},
    }


@pytest.fixture
def legacy_track(provider: YoutubeMusicProvider) -> Track:
    """Parse a listing with the legacy public-looking mapping but no visibility flags."""
    return provider._parse_track(
        {
            "videoId": VIDEO_ID,
            "title": "Sparks",
            "artists": [{"channelId": "UCvCk2zFqkCYzpnSgWfx0qOg", "name": "Thomas Bergersen"}],
        }
    )


@pytest.fixture
def song_transport(song_response: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Serve prepared visitor GETs and player POSTs beneath real ytmusicapi requests."""
    http = Mock()

    def send(
        _session: requests.Session, request: requests.PreparedRequest, **_kwargs: Any
    ) -> requests.Response:
        url = urlsplit(request.url or "")
        response = requests.Response()
        response.status_code = 200
        response.request = request
        if request.method == "GET" and request.url == "https://music.youtube.com/":
            http.visitor(request)
            response._content = b'ytcfg.set({"VISITOR_DATA":"test-visitor"});'
        elif (
            request.method == "POST"
            and url.scheme == "https"
            and url.netloc == "music.youtube.com"
            and url.path == "/youtubei/v1/player"
        ):
            assert isinstance(request.body, str | bytes)
            body = json.loads(request.body)
            assert body["video_id"] == song_response.get("videoDetails", {}).get(
                "videoId", VIDEO_ID
            )
            assert body["context"]["client"]["hl"] == "en"
            assert request.headers["X-Goog-Visitor-Id"] == "test-visitor"
            authorization = request.headers["authorization"]
            assert isinstance(authorization, str)
            assert authorization.startswith("SAPISIDHASH ")
            assert "__Secure-3PAPISID=test-cookie" in request.headers["cookie"]
            user = get_current_user()
            http(request, user.user_id if user else None)
            response._content = json.dumps(song_response).encode()
        else:
            pytest.fail(f"Unexpected HTTP request: {request.method} {request.url}")
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    return http


@pytest.fixture
async def sharing_reader() -> AsyncIterator[None]:
    """Carry a real request identity through the cache task and executor thread."""
    previous = get_current_user()
    set_current_user(User(user_id="reader", username="reader", role=UserRole.USER))
    try:
        yield
    finally:
        set_current_user(previous)


@pytest.fixture
def song_http(song_transport: Mock, song_response: dict[str, Any]) -> Mock:
    """Serve a public song through the real authenticated client and parser."""
    song_response["videoDetails"].update(
        isPrivate=False, isCrawlable=True, musicVideoType="MUSIC_VIDEO_TYPE_ATV"
    )
    song_response["microformat"]["microformatDataRenderer"]["unlisted"] = False

    return song_transport


async def test_song_transport_runs_constructor_and_song_offline(
    provider: YoutubeMusicProvider, song_http: Mock, song_response: dict[str, Any]
) -> None:
    """Every fresh client fetches its visitor ID before its real player POST, offline."""
    for item_id in (VIDEO_ID, "kXYiU_JCYtU"):
        song_response["videoDetails"]["videoId"] = item_id
        assert (await provider.get_track(item_id)).item_id == item_id
    assert song_http.call_count == song_http.visitor.call_count == 2
    assert [call.args[0].method for call in song_http.mock_calls] == ["GET", "POST"] * 2


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_song_transport_rejects_unexpected_http(song_http: Mock, method: str) -> None:
    """The fixture rejects unrecognized YouTube endpoints instead of passing them through."""
    with pytest.raises(pytest.fail.Exception, match="Unexpected HTTP request"):
        requests.request(method, "https://music.youtube.com/unexpected", timeout=5)
    song_http.assert_not_called()
    song_http.visitor.assert_not_called()


@pytest.mark.usefixtures("sharing_reader")
@pytest.mark.parametrize("revocation", ["private", "config_removed", "removed", None])
@pytest.mark.parametrize("cache_hit", [False, True])
async def test_share_revocation_during_track_cache_read_prevents_lookup(
    provider: YoutubeMusicProvider, song_http: Mock, revocation: str | None, cache_hit: bool
) -> None:
    """Sharing must reauthorize after the real use_cache read on misses and hits."""
    mass = provider.mass
    if cache_hit:
        await provider.get_track(VIDEO_ID)
        await _wait_for_track_cache(provider)
        song_http.reset_mock()
    assert mass.cache.database is not None
    get_row = mass.cache.database.get_row
    reading, resume = asyncio.Event(), asyncio.Event()

    async def cache_read(*args: Any, **kwargs: Any) -> Any:
        row = await get_row(*args, **kwargs)
        if args[1].get("key") == f"get_track.{VIDEO_ID}":
            assert (row is not None) == cache_hit
            reading.set()
            await resume.wait()
        return row

    with (
        patch.object(mass.cache.database, "get_row", side_effect=cache_read),
        patch.object(YTMusic, "get_song", autospec=True, side_effect=YTMusic.get_song) as get_song,
    ):
        request = asyncio.create_task(
            mass.music.get_item_share_info(f"{INSTANCE}://track/{VIDEO_ID}")
        )
        try:
            await asyncio.wait_for(reading.wait(), timeout=5)
            if revocation:
                _revoke_sources(mass, [INSTANCE], revocation)
            resume.set()
            if revocation:
                with pytest.raises(MediaNotFoundError):
                    await request
            else:
                result = await request
                assert result["links"][0]["url"] == f"https://music.youtube.com/watch?v={VIDEO_ID}"
        finally:
            resume.set()
            await asyncio.gather(request, return_exceptions=True)
    assert get_song.call_count == song_http.call_count == (0 if revocation or cache_hit else 1)


@pytest.mark.usefixtures("sharing_reader")
@pytest.mark.parametrize("revocation", ["private", "config_removed", "removed", None])
async def test_share_revocation_during_track_throttle_prevents_lookup(
    provider: YoutubeMusicProvider, song_http: Mock, revocation: str | None
) -> None:
    """A real throttler queue must not release an unauthorized authenticated lookup."""
    mass = provider.mass
    provider.throttler = ThrottlerManager(rate_limit=1, period=60)
    throttler = provider.throttler.throttler
    await throttler.acquire()
    blocker = asyncio.create_task(throttler.acquire())
    with (
        patch.object(YTMusic, "get_song", autospec=True, side_effect=YTMusic.get_song) as get_song,
        patch.object(provider.throttler, "acquire", wraps=provider.throttler.acquire) as acquire,
    ):
        request = asyncio.create_task(
            mass.music.get_item_share_info(f"{INSTANCE}://track/{VIDEO_ID}")
        )
        try:
            async with asyncio.timeout(5):
                while len(throttler._waiters) != 2:
                    if request.done():
                        await request
                        pytest.fail("Sharing finished before entering the throttler queue")
                    await asyncio.sleep(0)
            if revocation:
                _revoke_sources(mass, [INSTANCE], revocation)
            throttler._task_logs.clear()
            blocker.cancel()
            await asyncio.gather(blocker, return_exceptions=True)
            if revocation:
                with pytest.raises(MediaNotFoundError):
                    await request
            else:
                result = await request
                assert result["links"][0]["url"] == f"https://music.youtube.com/watch?v={VIDEO_ID}"
                await _wait_for_track_cache(provider)
                # The next share must use the cache even while the throttle is still full.
                async with asyncio.timeout(5):
                    assert (
                        await mass.music.get_item_share_info(f"{INSTANCE}://track/{VIDEO_ID}")
                        == result
                    )
        finally:
            throttler._task_logs.clear()
            blocker.cancel()
            await asyncio.gather(blocker, request, return_exceptions=True)
    acquire.assert_called_once()
    assert get_song.call_count == song_http.call_count == (0 if revocation else 1)


@pytest.mark.usefixtures("sharing_reader")
@pytest.mark.parametrize("revocation", ["private", "config_removed", "removed", None])
@pytest.mark.parametrize("boundary", ["executor", "client"])
async def test_share_revocation_in_track_worker_prevents_lookup(
    provider: YoutubeMusicProvider,
    song_http: Mock,
    revocation: str | None,
    boundary: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recheck inside the real executor, including after real client initialization."""
    mass = provider.mass
    loop = asyncio.get_running_loop()
    waiting, resume = asyncio.Event(), threading.Event()
    to_thread = asyncio.to_thread
    initialize = YTMusic.__init__

    def pause() -> None:
        loop.call_soon_threadsafe(waiting.set)
        assert resume.wait(timeout=5)

    async def queued_thread(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        def run() -> Any:
            if boundary == "executor" and func.__name__ == "_get_song":
                pause()
            return func(*args, **kwargs)

        return await to_thread(run)

    def initialize_client(self: YTMusic, *args: Any, **kwargs: Any) -> None:
        initialize(self, *args, **kwargs)
        if boundary == "client":
            pause()

    monkeypatch.setattr(asyncio, "to_thread", queued_thread)
    monkeypatch.setattr(YTMusic, "__init__", initialize_client)
    with patch.object(YTMusic, "get_song", autospec=True, side_effect=YTMusic.get_song) as get_song:
        request = asyncio.create_task(
            mass.music.get_item_share_info(f"{INSTANCE}://track/{VIDEO_ID}")
        )
        try:
            await asyncio.wait_for(waiting.wait(), timeout=5)
            if revocation:
                _revoke_sources(mass, [INSTANCE], revocation)
            resume.set()
            if revocation:
                with pytest.raises(MediaNotFoundError):
                    await request
            else:
                result = await request
                assert result["links"][0]["url"] == f"https://music.youtube.com/watch?v={VIDEO_ID}"
        finally:
            resume.set()
            await asyncio.gather(request, return_exceptions=True)
    assert get_song.call_count == song_http.call_count == (0 if revocation else 1)


@pytest.mark.parametrize("previous_share", ["none", "success", "failure"])
async def test_normal_metadata_without_user_keeps_access_after_sharing(
    provider: YoutubeMusicProvider,
    song_http: Mock,
    song_response: dict[str, Any],
    previous_share: str,
) -> None:
    """Sharing guards must reset on success and errors without restricting internal lookups."""
    assert get_current_user() is None
    if previous_share != "none":
        set_current_user(User(user_id="reader", username="reader", role=UserRole.USER))
        try:
            if previous_share == "failure":
                details = song_response.pop("videoDetails")
                with pytest.raises(MediaNotFoundError):
                    await provider.mass.music.get_item_share_info(f"{INSTANCE}://track/{VIDEO_ID}")
                song_response["videoDetails"] = details
            else:
                await provider.mass.music.get_item_share_info(f"{INSTANCE}://track/{VIDEO_ID}")
        finally:
            set_current_user(None)
    _revoke_sources(provider.mass, [INSTANCE], "private")
    next_id = "kXYiU_JCYtU"
    song_response["videoDetails"]["videoId"] = next_id
    song_http.reset_mock()
    track = await provider.get_track(next_id)
    assert track.item_id == next_id
    assert track.name == "Sparks"
    song_http.assert_called_once()


@pytest.mark.parametrize("reader_first", [True, False])
@pytest.mark.parametrize(
    ("caller", "entry"),
    [
        ("owner", "controller"),
        ("background", "controller"),
        ("owner", "provider"),
        ("background", "provider"),
        ("owner", "sharing"),
    ],
)
async def test_overlapping_share_keeps_request_authorization(
    provider: YoutubeMusicProvider, song_http: Mock, reader_first: bool, caller: str, entry: str
) -> None:
    """Revoking a queued reader neither poisons another caller nor borrows its guard."""
    mass = provider.mass
    provider.throttler = ThrottlerManager(rate_limit=1, period=60)
    throttler = provider.throttler.throttler
    await throttler.acquire()
    blocker = asyncio.create_task(throttler.acquire())
    flights: dict[str | None, set[asyncio.Task[Any]]] = {}
    create_task = mass.create_task
    authorized_user = "other" if caller == "owner" else None

    def track_task(*args: Any, **kwargs: Any) -> asyncio.Task[Any]:
        task = create_task(*args, **kwargs)
        if kwargs.get("task_id"):
            user = get_current_user()
            flights.setdefault(user.user_id if user else None, set()).add(task)
        return task

    async def request(reader: bool) -> Any:
        return await _lookup_as_user(
            provider, "reader" if reader else authorized_user, "sharing" if reader else entry
        )

    tasks = []
    with (
        patch.object(mass, "create_task", side_effect=track_task),
        patch.object(provider.throttler, "acquire", wraps=provider.throttler.acquire) as acquire,
    ):
        try:
            tasks.append(asyncio.create_task(request(reader_first)))
            async with asyncio.timeout(5):
                while len(throttler._waiters) != 2:
                    if tasks[0].done():
                        await tasks[0]
                        pytest.fail("First request did not enter the throttle")
                    await asyncio.sleep(0)
            tasks.append(asyncio.create_task(request(not reader_first)))
            async with asyncio.timeout(5):
                while len(throttler._waiters) != 3 and not (
                    flights.get("reader", set()) & flights.get(authorized_user, set())
                ):
                    await asyncio.sleep(0)
            _revoke_sources(mass, [INSTANCE], "private")
            _release_throttle(throttler, blocker)
            results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
        finally:
            _release_throttle(throttler, blocker)
            await asyncio.gather(blocker, *tasks, return_exceptions=True)
    reader_result, authorized_result = results if reader_first else results[::-1]
    assert isinstance(reader_result, MediaNotFoundError)
    assert not isinstance(authorized_result, BaseException)
    if entry == "sharing":
        assert (
            authorized_result["links"][0]["url"] == f"https://music.youtube.com/watch?v={VIDEO_ID}"
        )
    else:
        assert authorized_result.item_id == VIDEO_ID
    assert acquire.call_count == 2
    song_http.assert_called_once()
    song_http.visitor.assert_called_once()
    assert song_http.call_args.args[1] == authorized_user
    await _wait_for_track_cache(provider)
    assert await request(False) == authorized_result
    song_http.assert_called_once()


@pytest.mark.usefixtures("sharing_reader")
@pytest.mark.parametrize("sharing", [True, False])
@pytest.mark.parametrize("entry", ["controller", "provider"])
@pytest.mark.parametrize("cancel", [True, False])
async def test_same_request_deduplicates_and_shares_stored_data(
    provider: YoutubeMusicProvider, song_http: Mock, sharing: bool, entry: str, cancel: bool
) -> None:
    """One scope still deduplicates, survives a cancelled waiter and shares stored metadata."""
    provider.throttler = ThrottlerManager(rate_limit=1, period=60)
    throttler = provider.throttler.throttler
    await throttler.acquire()
    blocker = asyncio.create_task(throttler.acquire())
    started: list[bool] = []

    async def fetch() -> Track:
        started.append(True)
        if entry == "controller":
            return await provider.mass.music.tracks.get_provider_item(
                VIDEO_ID, INSTANCE, allow_fallback=False, strict_provider_instance=True
            )
        return await provider.get_track(VIDEO_ID)

    policy = Mock(return_value=True)
    with share_source_access(policy) if sharing else nullcontext():
        tasks = [asyncio.create_task(fetch()) for _ in range(3)]
    with patch.object(provider.throttler, "acquire", wraps=provider.throttler.acquire) as acquire:
        try:
            async with asyncio.timeout(5):
                while len(started) != 3 or len(throttler._waiters) != 2:
                    await asyncio.sleep(0)
            if cancel:
                tasks[0].cancel()
                with pytest.raises(asyncio.CancelledError):
                    await tasks[0]
            _release_throttle(throttler, blocker)
            results = await asyncio.wait_for(asyncio.gather(*tasks[int(cancel) :]), 5)
        finally:
            _release_throttle(throttler, blocker)
            await asyncio.gather(blocker, *tasks, return_exceptions=True)
    assert all(track.item_id == VIDEO_ID for track in results)
    acquire.assert_called_once()
    assert bool(policy.call_count) is sharing
    song_http.assert_called_once()
    song_http.visitor.assert_called_once()
    await _wait_for_track_cache(provider)
    for context in (nullcontext(), share_source_access(policy)):
        with context:
            assert (await fetch()).item_id == VIDEO_ID
    song_http.assert_called_once()


@pytest.mark.parametrize("exit_kind", ["success", "error", "cancel"])
async def test_nested_sharing_scope_restores_guard_and_task_context(exit_kind: str) -> None:
    """Nested sharing policies restore both contexts after success, failure and cancellation."""
    outer_policy = Mock(return_value=True)
    inner_policy = Mock(return_value=False)
    entered = asyncio.Event()
    assert scoped_task_id("probe") == "probe"
    with share_source_access(outer_policy):
        outer_key = scoped_task_id("probe")

        async def nested() -> None:
            try:
                with share_source_access(inner_policy):
                    assert scoped_task_id("probe") not in {"probe", outer_key}
                    with pytest.raises(MediaNotFoundError):
                        check_share_source_access(INSTANCE, "ytmusic")
                    entered.set()
                    if exit_kind == "cancel":
                        await asyncio.Event().wait()
                    if exit_kind == "error":
                        raise ValueError("nested failure")
            finally:
                assert scoped_task_id("probe") == outer_key
                check_share_source_access(INSTANCE, "ytmusic")

        task = asyncio.create_task(nested())
        await asyncio.wait_for(entered.wait(), 5)
        if exit_kind == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif exit_kind == "error":
            with pytest.raises(ValueError, match="nested failure"):
                await task
        else:
            await task
        assert scoped_task_id("probe") == outer_key
    assert scoped_task_id("probe") == "probe"
    check_share_source_access(INSTANCE, "ytmusic")
    outer_policy.assert_called_once_with(INSTANCE, "ytmusic")
    inner_policy.assert_called_once_with(INSTANCE, "ytmusic")


@pytest.mark.parametrize(
    "visibility",
    [
        "private",
        "unlisted",
        "public",
        "unknown",
        "private_flag_only",
        "upload_type_only",
        "unlisted_flag_only",
        "not_crawlable",
        "missing_private_flag",
        "missing_unlisted_flag",
        "missing_microformat",
    ],
)
async def test_song_visibility_through_helper_parser_and_api(
    provider: YoutubeMusicProvider,
    song_response: dict[str, Any],
    visibility: str,
    song_transport: Mock,
) -> None:
    """A normal watch URL is returned only for positively identified public songs."""
    details = song_response["videoDetails"]
    microformat = song_response["microformat"]["microformatDataRenderer"]
    if visibility != "private":
        details.update(isPrivate=False, musicVideoType="MUSIC_VIDEO_TYPE_ATV")
        details["isCrawlable"] = visibility != "unlisted"
        microformat["unlisted"] = visibility == "unlisted"
    if visibility == "unknown":
        details.pop("isPrivate")
        details.pop("isCrawlable")
        microformat.pop("unlisted")
    if visibility == "private_flag_only":
        details["isPrivate"] = True
    if visibility == "upload_type_only":
        details["musicVideoType"] = "MUSIC_VIDEO_TYPE_PRIVATELY_OWNED_TRACK"
    if visibility == "unlisted_flag_only":
        microformat["unlisted"] = True
    if visibility == "not_crawlable":
        details["isCrawlable"] = False
    if visibility == "missing_private_flag":
        details.pop("isPrivate")
    if visibility == "missing_unlisted_flag":
        microformat.pop("unlisted")
    if visibility == "missing_microformat":
        song_response.pop("microformat")
    with patch.object(provider.throttler, "acquire", wraps=provider.throttler.acquire) as acquire:
        uri = f"{INSTANCE}://track/{VIDEO_ID}"
        result = await provider.mass.music.get_item_share_info(uri)
        assert await provider.mass.music.get_item_share_info(uri) == result
    assert result["title"] == "Sparks"
    if visibility == "public":
        assert result["links"] == [
            {
                "provider_domain": "ytmusic",
                "provider_instance": INSTANCE,
                "url": f"https://music.youtube.com/watch?v={VIDEO_ID}",
            }
        ]
        assert result["text"] is None
    else:
        assert result["links"] == []
        assert result["text"] == "Thomas Bergersen - Sparks"
    song_transport.assert_called_once()
    song_transport.visitor.assert_called_once()
    acquire.assert_called_once()


@pytest.mark.parametrize("source", ["library", INSTANCE, "ytmusic"])
@pytest.mark.parametrize("other_link", [False, True])
async def test_legacy_library_mapping_is_unknown_offline(
    provider: YoutubeMusicProvider, legacy_track: Track, source: str, other_link: bool
) -> None:
    """Unknown YTM mappings stay text without fetching; other public links survive."""
    mass = provider.mass
    if other_link:
        set_music_source_access(mass, {INSTANCE: None, "spotify--account": None})
        legacy_track.provider_mappings.add(
            mapping("spotify", SPOTIFY_ID, instance="spotify--account")
        )
    library_track = await mass.music.tracks.add_item_to_library(legacy_track)
    await mass.cache.set(f"get_track.{VIDEO_ID}", legacy_track.to_dict(), provider=INSTANCE)
    provider.available = False
    item_id = library_track.item_id if source == "library" else VIDEO_ID
    with patch.object(provider, "get_track", AsyncMock()) as fetch:
        result = await mass.music.get_item_share_info(f"{source}://track/{item_id}")
    fetch.assert_not_awaited()
    if other_link:
        assert [link["provider_domain"] for link in result["links"]] == ["spotify"]
        assert result["text"] is None
    else:
        assert result["links"] == []
        assert result["text"] == "Thomas Bergersen - Sparks"


async def test_legacy_track_cache_refreshes_visibility(
    provider: YoutubeMusicProvider,
    legacy_track: Track,
    song_response: dict[str, Any],
    song_transport: Mock,
) -> None:
    """Normal metadata access refreshes pre-visibility cache entries once."""
    await provider.mass.cache.set(
        f"get_track.{VIDEO_ID}", legacy_track.to_dict(), provider=INSTANCE
    )
    song_response["videoDetails"].update(
        isPrivate=False, isCrawlable=True, musicVideoType="MUSIC_VIDEO_TYPE_ATV"
    )
    song_response["microformat"]["microformatDataRenderer"]["unlisted"] = False
    assert legacy_track.uri is not None
    result = await provider.mass.music.get_item_share_info(legacy_track.uri)
    assert await provider.mass.music.get_item_share_info(legacy_track.uri) == result
    assert result["links"][0]["url"] == f"https://music.youtube.com/watch?v={VIDEO_ID}"
    song_transport.assert_called_once()
    song_transport.visitor.assert_called_once()


@pytest.mark.parametrize("evidence", ["fresh", "expired", "other_account"])
async def test_library_sharing_requires_fresh_evidence_for_same_account(
    provider: YoutubeMusicProvider, legacy_track: Track, evidence: str
) -> None:
    """Visibility does not cross accounts or expiration boundaries, even while offline."""
    mass = provider.mass
    library_track = await mass.music.tracks.add_item_to_library(legacy_track)
    assert library_track.uri is not None
    await mass.cache.set(
        f"track.{VIDEO_ID}",
        True,
        provider=INSTANCE if evidence != "other_account" else "ytmusic--other",
        category=CACHE_CATEGORY_SHARE_VISIBILITY,
        expiration=3600,
    )
    provider.available = False
    with (
        patch(
            "music_assistant.controllers.cache.controller.time.time",
            return_value=time() + (3601 if evidence == "expired" else 0),
        ),
        patch.object(provider, "get_track", AsyncMock()) as fetch,
    ):
        result = await mass.music.get_item_share_info(library_track.uri)
    assert bool(result["links"]) is (evidence == "fresh")
    fetch.assert_not_awaited()


@pytest.mark.parametrize("evidence", [None, False, True])
async def test_episode_visibility_uses_same_video_evidence(
    provider: YoutubeMusicProvider, evidence: bool | None
) -> None:
    """Podcast watch IDs need public video evidence; episode parsing alone cannot prove it."""
    client = Mock()
    client.get_podcast.return_value = {"podcastId": "PLpodcast", "title": "Podcast"}
    client.get_episode.return_value = {"videoId": VIDEO_ID, "title": "Episode"}
    if evidence is not None:
        await provider.mass.cache.set(
            f"track.{VIDEO_ID}",
            evidence,
            provider=INSTANCE,
            category=CACHE_CATEGORY_SHARE_VISIBILITY,
        )
    with patch("music_assistant.providers.ytmusic.helpers.ytmusicapi.YTMusic", return_value=client):
        result = await provider.mass.music.get_item_share_info(
            f"{INSTANCE}://podcast_episode/PLpodcast|{VIDEO_ID}"
        )
        podcast = await provider.mass.music.get_item_share_info(f"{INSTANCE}://podcast/PLpodcast")
    assert bool(result["links"]) is (evidence is True)
    assert result["text"] == (None if evidence else "Episode")
    assert podcast["title"] == podcast["text"] == "Podcast"
    assert podcast["links"] == []
    client.get_song.assert_not_called()


async def test_hidden_ytmusic_mapping_does_not_read_visibility(
    provider: YoutubeMusicProvider, legacy_track: Track
) -> None:
    """The cache is not consulted for mappings the caller cannot access."""
    mass = provider.mass
    set_music_source_access(mass, {INSTANCE: None, "spotify--account": None})
    legacy_track.provider_mappings.add(mapping("spotify", SPOTIFY_ID, instance="spotify--account"))
    library_track = await mass.music.tracks.add_item_to_library(legacy_track)
    assert library_track.uri is not None
    mass.config.set(
        f"providers/{INSTANCE}/access",
        ProviderAccess(owner="other", sharing=ProviderSharing.PRIVATE).to_dict(),
    )
    user = User(user_id="reader", username="reader", role=UserRole.USER)
    with (
        patch(USER_PATH, return_value=user),
        patch.object(mass.cache, "get", wraps=mass.cache.get) as lookup,
        patch.object(provider, "get_track", AsyncMock()) as fetch,
    ):
        result = await mass.music.get_item_share_info(library_track.uri)
    assert [link["provider_domain"] for link in result["links"]] == ["spotify"]
    lookup.assert_not_awaited()
    fetch.assert_not_awaited()


@pytest.mark.parametrize("response", ["private", "missing", "invalid"])
async def test_metadata_refresh_revokes_previous_public_evidence(
    provider: YoutubeMusicProvider,
    song_response: dict[str, Any],
    song_transport: Mock,
    response: str,
) -> None:
    """A fresh negative result revokes cached visibility even if the item cannot be parsed."""
    await provider.mass.cache.set(
        f"track.{VIDEO_ID}",
        True,
        provider=INSTANCE,
        category=CACHE_CATEGORY_SHARE_VISIBILITY,
    )
    if response == "missing":
        song_response.pop("videoDetails")
    if response == "invalid":
        song_response["videoDetails"]["channelId"] = None
    if response == "private":
        await provider.get_track(VIDEO_ID)
    else:
        with pytest.raises(MediaNotFoundError if response == "missing" else InvalidDataError):
            await provider.get_track(VIDEO_ID)
    song_transport.assert_called_once()
    song_transport.visitor.assert_called_once()
    assert (
        await provider.mass.cache.get(
            f"track.{VIDEO_ID}", provider=INSTANCE, category=CACHE_CATEGORY_SHARE_VISIBILITY
        )
        is False
    )


async def _wait_for_track_cache(provider: YoutubeMusicProvider) -> None:
    """Wait for the decorator's asynchronous metadata cache write to finish."""
    async with asyncio.timeout(5):
        while (
            await provider.mass.cache.get(
                f"get_track.{VIDEO_ID}", provider=INSTANCE, checksum="track_visibility_v1"
            )
            is None
        ):
            await asyncio.sleep(0)


async def _lookup_as_user(provider: YoutubeMusicProvider, user_id: str | None, entry: str) -> Any:
    """Look up the same item through the requested entry point with a real user context."""
    previous = get_current_user()
    set_current_user(
        User(user_id=user_id, username=user_id, role=UserRole.USER) if user_id else None
    )
    try:
        if entry == "sharing":
            return await provider.mass.music.get_item_share_info(f"{INSTANCE}://track/{VIDEO_ID}")
        if entry == "controller":
            return await provider.mass.music.tracks.get_provider_item(
                VIDEO_ID, INSTANCE, allow_fallback=False, strict_provider_instance=True
            )
        return await provider.get_track(VIDEO_ID)
    finally:
        set_current_user(previous)


def _release_throttle(throttler: Throttler, blocker: asyncio.Task[float]) -> None:
    """Wake queued lookups without waiting for the deliberately long throttle period."""
    throttler.period = 0.001
    throttler._task_logs.clear()
    blocker.cancel()
