"""Tests for the ListenBrainz client of the most listened recordings of an artist."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from aiohttp import ClientResponseError
from music_assistant_models.errors import RetriesExhausted

from music_assistant.constants import MASS_USER_AGENT
from music_assistant.providers.musicbrainz.listenbrainz import (
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
    from music_assistant.mass import MusicAssistant

WEEK = 7 * 24 * 3600
DAY = 24 * 3600
OTHER_ARTIST_MBID = "8ed2e0b3-aa4c-4e13-bec3-dc7393ed4d6b"
CACHE_CLOCK = "music_assistant.controllers.cache.controller.time"


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
    # the 44th entry is a second, less listened recording of "Creep"; the seven titles after
    # it fill the list, so the last two titles of the answer don't make it
    assert answer[43]["recording_name"] == "Creep"
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
    get.assert_called_once_with(TOP_RECORDINGS_URL, headers={"User-Agent": MASS_USER_AGENT})


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
