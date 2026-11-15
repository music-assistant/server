"""Tests for the ListenBrainz client of the most listened recordings of an artist."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientResponseError, ClientSession, ClientTimeout, web
from aiohttp.test_utils import TestServer
from music_assistant_models.auth import User, UserRole
from music_assistant_models.errors import RetriesExhausted

from music_assistant.constants import MASS_USER_AGENT
from music_assistant.controllers.webserver.helpers.auth_middleware import user_auth_context
from music_assistant.providers.musicbrainz.listenbrainz import (
    REQUEST_TIMEOUT,
    TOP_TRACKS_LIMIT,
    ListenBrainzClient,
    TopRecording,
)

from .helpers import (
    RADIOHEAD_MBID,
    TOP_RECORDINGS_URL,
    answer_listenbrainz,
    own_throttler,
    recorded_answer,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant.mass import MusicAssistant

WEEK = 7 * 24 * 3600
DAY = 24 * 3600
OTHER_ARTIST_MBID = "8ed2e0b3-aa4c-4e13-bec3-dc7393ed4d6b"
USER_A = User(user_id="user-a", username="user-a", role=UserRole.USER)
USER_B = User(user_id="user-b", username="user-b", role=UserRole.USER)
CACHE_CLOCK = "music_assistant.controllers.cache.controller.time"
CLIENT = "music_assistant.providers.musicbrainz.listenbrainz"


@pytest.fixture
async def client(mass_minimal: MusicAssistant) -> ListenBrainzClient:
    """Return a client on a real cache."""
    await mass_minimal.cache._setup_database()
    client = ListenBrainzClient(mass_minimal)
    client.throttler = own_throttler()
    return client


async def test_most_listened_recording_of_each_title_is_kept_up_to_the_cap(
    client: ListenBrainzClient,
) -> None:
    """The ranking is kept in its order, one recording per title, the most listened first."""
    get = answer_listenbrainz(client.mass)

    top = await client.get_top_recordings(RADIOHEAD_MBID)

    answer = recorded_answer()
    assert top.fetched
    assert len(top.recordings) == TOP_TRACKS_LIMIT == 50
    # the 44th entry is a second, less listened recording of "Creep", the 50th one of a
    # recording Radiohead is not credited on; the seven titles besides those fill the list,
    # so the last titles of the answer don't make it
    assert answer[43]["recording_name"] == "Creep"
    assert RADIOHEAD_MBID not in answer[49]["artist_mbids"]
    assert [recording.name for recording in top.recordings] == [
        *(entry["recording_name"] for entry in answer[:43]),
        "Bangers and Mash",
        "Street Spirit",
        "The Daily Mail",
        "Supercollider",
        "Just (You Do It to Yourself)",
        "You (acoustic)",
        "This Mess We\u2019re In",
    ]
    creep = next(recording for recording in top.recordings if recording.name == "Creep")
    assert creep.recording_mbid == answer[4]["recording_mbid"]
    # the whole list in one request, without a query parameter or a token
    get.assert_called_once_with(
        TOP_RECORDINGS_URL, headers={"User-Agent": MASS_USER_AGENT}, timeout=REQUEST_TIMEOUT
    )


async def test_recording_not_credited_to_the_artist_is_left_out(
    client: ListenBrainzClient,
) -> None:
    """A recording of others in the ranking hides no recording of the artist of the same title."""
    wish_you_were_here = [
        entry for entry in recorded_answer() if entry["recording_name"].startswith("Wish You")
    ]
    # Sparklehorse feat. Thom Yorke, then Radiohead's own, much less listened recording
    assert [RADIOHEAD_MBID in entry["artist_mbids"] for entry in wish_you_were_here] == [
        False,
        True,
    ]
    answer_listenbrainz(client.mass, body=wish_you_were_here)

    top = await client.get_top_recordings(RADIOHEAD_MBID)

    assert [recording.recording_mbid for recording in top.recordings] == [
        wish_you_were_here[1]["recording_mbid"]
    ]


async def test_recording_without_release_cover_or_length(client: ListenBrainzClient) -> None:
    """An entry lacks its release, cover or length where ListenBrainz has none."""
    answer_listenbrainz(client.mass)

    top = await client.get_top_recordings(RADIOHEAD_MBID)

    recordings = {recording.name: recording for recording in top.recordings}
    assert recordings["Karma Police"] == TopRecording(
        recording_mbid="9e2ad5bc-c6f9-40d2-a36f-3122ee2072a3",
        name="Karma Police",
        duration=262,
        artist_mbids=(RADIOHEAD_MBID,),
        release_mbid="1834eae1-741b-3c03-9ca5-0df3decb43ea",
        release_name="OK Computer",
        # a direct address on the Cover Art Archive, nothing is looked up for it
        cover_url=(
            "https://coverartarchive.org/release/30702389-5c67-4438-9ea0-2351c8de0f1d/"
            "43287476628-500.jpg"
        ),
    )
    assert recordings["You (acoustic)"] == TopRecording(
        recording_mbid="80a4dace-ea41-4c47-ba2d-0a1c1a3fa164",
        name="You (acoustic)",
        duration=None,
        artist_mbids=(RADIOHEAD_MBID,),
        release_mbid=None,
        release_name=None,
        cover_url=None,
    )
    assert recordings["Bangers and Mash"].duration is None
    assert recordings["Bangers and Mash"].cover_url
    assert recordings["Street Spirit"].cover_url is None
    assert recordings["Street Spirit"].release_name
    assert recordings["This Mess We\u2019re In"].artist_mbids == (
        RADIOHEAD_MBID,
        "e795e03d-b5d5-4a5f-834d-162cfb308a2c",
    )


async def test_list_is_kept_for_a_week(client: ListenBrainzClient) -> None:
    """A second call inside the week makes no request, after it the list is asked for again."""
    get = answer_listenbrainz(client.mass)
    now = time.time()

    first = await client.get_top_recordings(RADIOHEAD_MBID)
    with patch(CACHE_CLOCK) as clock:
        clock.time.return_value = now + WEEK - 60
        kept = await client.get_top_recordings(RADIOHEAD_MBID)
        assert get.call_count == 1
        clock.time.return_value = now + WEEK + 60
        await client.get_top_recordings(RADIOHEAD_MBID)

    assert get.call_count == 2
    assert not kept.fetched
    assert kept.recordings == first.recordings


async def test_401_is_no_list_kept_for_a_day(client: ListenBrainzClient) -> None:
    """A 401 means no list, for that artist only, and ListenBrainz is asked again a day later."""
    get = answer_listenbrainz(
        client.mass,
        status=401,
        body={"code": 401, "error": "you need to provide an Auth token for this endpoint."},
    )
    now = time.time()

    top = await client.get_top_recordings(RADIOHEAD_MBID)

    assert top.recordings == []
    with patch(CACHE_CLOCK) as clock:
        clock.time.return_value = now + DAY - 60
        assert (await client.get_top_recordings(RADIOHEAD_MBID)).recordings == []
        assert get.call_count == 1
        # an artist ListenBrainz does not know answers the same 401, the others are asked
        await client.get_top_recordings(OTHER_ARTIST_MBID)
        assert get.call_count == 2
        clock.time.return_value = now + DAY + 60
        await client.get_top_recordings(RADIOHEAD_MBID)
    assert get.call_count == 3


async def test_rate_limit_is_not_retried_and_holds_back_the_next_call(
    client: ListenBrainzClient,
) -> None:
    """A 429 is not retried; the wait ListenBrainz asks for holds back the next call."""
    get = answer_listenbrainz(client.mass, status=429, headers={"X-RateLimit-Reset-In": "9"})

    with pytest.raises(RetriesExhausted):
        await client.get_top_recordings(RADIOHEAD_MBID)

    assert get.call_count == 1
    assert 8 < client.throttler.cooldown_remaining <= 9


async def test_long_rate_limit_sends_nothing_until_it_is_over(client: ListenBrainzClient) -> None:
    """While ListenBrainz asks for a long wait, a call fails at once and sends nothing."""
    get = answer_listenbrainz(client.mass, status=429, headers={"X-RateLimit-Reset-In": "120"})

    for _ in range(2):
        with pytest.raises(RetriesExhausted):
            await client.get_top_recordings(RADIOHEAD_MBID)

    assert get.call_count == 1
    assert 118 < client.throttler.cooldown_remaining <= 120


async def test_calls_at_the_same_moment_share_one_request(client: ListenBrainzClient) -> None:
    """Two users opening the artist at once cost one request; one of them gets it as fetched."""
    get, released = _answer_when_released(client)

    with user_auth_context(USER_A):
        first = asyncio.create_task(client.get_top_recordings(RADIOHEAD_MBID))
    with user_auth_context(USER_B):
        second = asyncio.create_task(client.get_top_recordings(RADIOHEAD_MBID))
    await _until(lambda: get.call_count == 1)
    released.set()
    tops = [await first, await second]
    third = await client.get_top_recordings(RADIOHEAD_MBID)

    assert get.call_count == 1
    assert [top.fetched for top in tops] == [True, False]
    assert tops[0].recordings == tops[1].recordings == third.recordings
    assert not third.fetched
    # nothing is left behind for the artist
    await asyncio.sleep(0)
    assert not [task_id for task_id in client.mass._tracked_tasks if "listenbrainz" in task_id]


async def test_cancelled_caller_leaves_the_shared_request_to_the_other(
    client: ListenBrainzClient,
) -> None:
    """A caller that gives up neither cancels the request nor fails the call that joined it."""
    get, released = _answer_when_released(client)

    first = asyncio.create_task(client.get_top_recordings(RADIOHEAD_MBID))
    second = asyncio.create_task(client.get_top_recordings(RADIOHEAD_MBID))
    await _until(lambda: get.call_count == 1)
    first.cancel()
    released.set()
    top = await second

    with pytest.raises(asyncio.CancelledError):
        await first
    assert get.call_count == 1
    assert top.fetched
    assert len(top.recordings) == TOP_TRACKS_LIMIT


async def test_call_that_missed_the_list_as_it_was_kept_makes_no_request(
    client: ListenBrainzClient,
) -> None:
    """A call that found no list just before another call's fetch kept one does not fetch."""
    get, released = _answer_when_released(client)
    cache_get = client.mass.cache.get
    first = asyncio.create_task(client.get_top_recordings(RADIOHEAD_MBID))
    await _until(lambda: get.call_count == 1)
    looks = 0

    async def look_late(key: str, **kwargs: Any) -> Any:
        nonlocal looks
        looks += 1
        kept = await cache_get(key, **kwargs)
        if looks == 1:
            # the first call's fetch keeps the list and lets go of the artist meanwhile
            released.set()
            await first
            await asyncio.sleep(0)
        return kept

    with patch.object(client.mass.cache, "get", AsyncMock(side_effect=look_late)):
        second = await client.get_top_recordings(RADIOHEAD_MBID)

    assert get.call_count == 1
    assert not second.fetched
    assert second.recordings == (await first).recordings


async def test_stalled_answer_is_given_up_on(client: ListenBrainzClient) -> None:
    """An answer that does not come within the timeout fails the call and keeps nothing."""
    requests: list[web.Request] = []

    async def stall(request: web.Request) -> web.Response:
        requests.append(request)
        await asyncio.sleep(10)
        return web.json_response([])

    app = web.Application()
    app.router.add_get("/1/popularity/top-recordings-for-artist/{mbid}", stall)
    async with TestServer(app) as server, ClientSession() as session:
        client.mass._http_session = session
        with (
            patch(f"{CLIENT}.LB_BASE_URL", str(server.make_url("/1"))),
            patch(f"{CLIENT}.REQUEST_TIMEOUT", ClientTimeout(total=0.2)),
        ):
            for _ in range(2):
                with pytest.raises(TimeoutError):
                    await client.get_top_recordings(RADIOHEAD_MBID)

    # nothing is kept, so the second call asked again
    assert len(requests) == 2


async def test_failure_is_not_kept(client: ListenBrainzClient) -> None:
    """A failed request keeps nothing, so the next call asks ListenBrainz again."""
    answer_listenbrainz(client.mass, status=500)

    with pytest.raises(ClientResponseError):
        await client.get_top_recordings(RADIOHEAD_MBID)

    get = answer_listenbrainz(client.mass)
    top = await client.get_top_recordings(RADIOHEAD_MBID)
    get.assert_called_once()
    assert top.fetched
    assert len(top.recordings) == TOP_TRACKS_LIMIT


def _answer_when_released(client: ListenBrainzClient) -> tuple[MagicMock, asyncio.Event]:
    """
    Have ListenBrainz answer with the recorded answer once the returned event is set.

    :return: The mocked get of the HTTP session, and the event that releases the answer.
    """
    get = answer_listenbrainz(client.mass)
    released = asyncio.Event()
    body = recorded_answer()

    async def answer(**_kwargs: object) -> list[dict[str, object]]:
        await released.wait()
        return body

    get.return_value.__aenter__.return_value.json = AsyncMock(side_effect=answer)
    return get, released


async def _until(condition: Callable[[], bool]) -> None:
    """Let the event loop run until the condition holds."""
    for _ in range(100):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never held")
