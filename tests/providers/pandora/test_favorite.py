"""Tests for sending likes and dislikes on Pandora station tracks as thumbs up/down."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import Mock

import pytest
from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    ProviderUnavailableError,
)

from music_assistant.providers.pandora import SUPPORTED_FEATURES
from music_assistant.providers.pandora.constants import (
    ADD_FEEDBACK_ENDPOINT,
    CACHE_CATEGORY_FEEDBACK,
    DELETE_FEEDBACK_ENDPOINT,
)
from music_assistant.providers.pandora.provider import PandoraProvider

from .test_provider import STATION_ID, _provider, _tracks


class _FakeCache:
    """
    Stand-in for the cache controller, keyed like it: key, provider and category.

    Each call yields to the event loop first, as the real one does while it reaches its
    database, so overlapping ratings can interleave here too.
    """

    def __init__(self) -> None:
        self.data: dict[tuple[str, str, int], Any] = {}
        self.persistent: set[tuple[str, str, int]] = set()

    async def get(self, key: str, provider: str = "default", category: int = 0) -> Any:
        await asyncio.sleep(0)
        return self.data.get((key, provider, category))

    async def set(
        self,
        key: str,
        data: Any,
        expiration: int = 0,
        provider: str = "default",
        category: int = 0,
        persistent: bool = False,
    ) -> None:
        await asyncio.sleep(0)
        self.data[(key, provider, category)] = data
        if persistent:
            self.persistent.add((key, provider, category))

    async def delete(self, key: str, category: int = 0, provider: str = "default") -> None:
        await asyncio.sleep(0)
        self.data.pop((key, provider, category), None)


def _feedback_provider(
    tracks: list[dict[str, Any]] | None = None,
    *later: list[dict[str, Any]],
) -> tuple[PandoraProvider, list[dict[str, Any]]]:
    """
    Build a provider serving `tracks`, then each of `later`, as the fragments it fetches.

    Every addFeedback body is recorded. Tracks without a token get `token-<id>`.
    """
    tracks = tracks if tracks is not None else _tracks()
    for track in tracks:
        track.setdefault("trackToken", f"token-{track['pandoraId']}")
    provider = _provider(payloads=[tracks, *later])
    provider.mass = Mock(cache=_FakeCache())
    stations = {
        track["trackToken"]: track.get("stationId")
        for fragment in (tracks, *later)
        for track in fragment
    }
    sent: list[dict[str, Any]] = []
    inner = provider._api_request

    async def _api(
        method: str, url: str, data: dict[str, Any] | None = None, **kw: Any
    ) -> dict[str, Any]:
        if url == ADD_FEEDBACK_ENDPOINT:
            body = data or {}
            sent.append(body)
            # the parts of Pandora's answer the provider keeps
            return {
                "feedbackId": f"fb-{len(sent)}",
                "stationId": stations.get(body.get("trackToken")),
                "isPositive": body.get("isPositive"),
            }
        return await inner(method, url, data, **kw)

    provider._api_request = _api  # type: ignore[method-assign, assignment]
    return provider, sent


def test_track_favorites_are_editable() -> None:
    """The provider declares track favorite editing, so likes and dislikes reach it."""
    assert ProviderFeature.FAVORITE_TRACKS_EDIT in SUPPORTED_FEATURES


async def test_like_sends_thumbs_up_with_the_tracks_token() -> None:
    """A like posts the retained fragment's trackToken with isPositive=True."""
    provider, sent = _feedback_provider()
    await provider.get_dynamic_radio_tracks(STATION_ID)  # retains the fragment
    await provider.set_favorite("TR:S1", MediaType.TRACK, True)
    assert sent == [{"trackToken": "token-TR:S1", "isPositive": True}]


async def test_dislike_sends_thumbs_down() -> None:
    """A dislike posts isPositive=False."""
    provider, sent = _feedback_provider()
    await provider.get_dynamic_radio_tracks(STATION_ID)
    await provider.set_favorite("TR:S2", MediaType.TRACK, False)
    assert sent == [{"trackToken": "token-TR:S2", "isPositive": False}]


@pytest.mark.parametrize(
    ("media_type", "favorite"),
    [(MediaType.ALBUM, True), (MediaType.ARTIST, False), (MediaType.ALBUM, None)],
)
async def test_non_tracks_send_nothing(media_type: MediaType, favorite: bool | None) -> None:
    """Rating or clearing anything but a track makes no call to Pandora."""
    provider, sent = _feedback_provider()
    await provider.get_dynamic_radio_tracks(STATION_ID)
    await provider.set_favorite("TR:S1", media_type, favorite)
    assert sent == []


async def test_unknown_track_is_refused_without_a_call() -> None:
    """A track no retained fragment holds is refused before any call to Pandora."""
    provider, sent = _feedback_provider()
    await provider.get_dynamic_radio_tracks(STATION_ID)
    with pytest.raises(MediaNotFoundError):
        await provider.set_favorite("TR:nope", MediaType.TRACK, True)
    assert sent == []


async def test_track_without_token_is_refused() -> None:
    """A retained track that came without a trackToken cannot be rated."""
    tracks = _tracks()
    tracks[0]["trackToken"] = ""
    provider, sent = _feedback_provider(tracks)
    await provider.get_dynamic_radio_tracks(STATION_ID)
    with pytest.raises(InvalidDataError):
        await provider.set_favorite("TR:S0", MediaType.TRACK, True)
    assert sent == []


async def test_feedback_disallowed_by_pandora_is_refused() -> None:
    """A track Pandora marks allowFeedback=False is refused before any call."""
    tracks = _tracks()
    tracks[0]["allowFeedback"] = False
    provider, sent = _feedback_provider(tracks)
    await provider.get_dynamic_radio_tracks(STATION_ID)
    with pytest.raises(InvalidDataError):
        await provider.set_favorite("TR:S0", MediaType.TRACK, False)
    assert sent == []


def _station_tracks(station: str) -> list[dict[str, Any]]:
    """Build a fragment of the shared test songs as `station` serves them, with its own tokens."""
    return [
        {**track, "stationId": station, "trackToken": f"{station}-{track['pandoraId']}"}
        for track in _tracks()
    ]


async def test_rating_uses_the_station_that_played_the_track() -> None:
    """
    A song another station fetched after playback started is still rated on the playing one.

    Station B's copy is the freshest fetch, but A served the audio, so A's token is sent.
    """
    provider, sent = _feedback_provider(_station_tracks("a"), _station_tracks("b"))
    await provider.get_dynamic_radio_tracks("station-a")
    await provider.get_stream_details("TR:S0", MediaType.TRACK)
    station_a = provider._sessions["station-a"].current
    assert station_a is not None
    station_a.fetched_at -= 60  # B's fetch is strictly the fresher one
    await provider.get_dynamic_radio_tracks("station-b")
    await provider.set_favorite("TR:S0", MediaType.TRACK, False)
    assert sent == [{"trackToken": "a-TR:S0", "isPositive": False}]


async def test_rating_follows_the_latest_playback_of_the_track() -> None:
    """Played on A, then again on B: the rating goes to B, where it is playing now."""
    provider, sent = _feedback_provider(_station_tracks("a"), _station_tracks("b"))
    await provider.get_dynamic_radio_tracks("station-a")
    await provider.get_stream_details("TR:S0", MediaType.TRACK)
    station_a = provider._sessions["station-a"].current
    assert station_a is not None
    station_a.served["TR:S0"] -= 60  # A played it a minute ago
    await provider.get_dynamic_radio_tracks("station-b")
    await provider.get_stream_details("TR:S0", MediaType.TRACK)
    await provider.set_favorite("TR:S0", MediaType.TRACK, True)
    assert sent == [{"trackToken": "b-TR:S0", "isPositive": True}]


async def test_rating_a_track_not_yet_played_uses_the_freshest_copy() -> None:
    """With no playback to go by, the freshest fetch decides, as for track lookups."""
    provider, sent = _feedback_provider(_station_tracks("a"), _station_tracks("b"))
    await provider.get_dynamic_radio_tracks("station-a")
    await provider.get_dynamic_radio_tracks("station-b")
    station_a = provider._sessions["station-a"].current
    assert station_a is not None
    station_a.fetched_at -= 60
    await provider.set_favorite("TR:S1", MediaType.TRACK, True)
    assert sent == [{"trackToken": "b-TR:S1", "isPositive": True}]


def _clearing_provider(
    tracks: list[dict[str, Any]] | None = None,
    *later: list[dict[str, Any]],
    fail_deletes: int = 0,
) -> tuple[PandoraProvider, list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Build a `_feedback_provider` that also records every deleteFeedback body.

    :param fail_deletes: How many deletes fail, from the first, before they succeed.
    """
    provider, sent = _feedback_provider(tracks, *later)
    deleted: list[dict[str, Any]] = []
    failures = [fail_deletes]
    inner = provider._api_request

    async def _api(
        method: str, url: str, data: dict[str, Any] | None = None, **kw: Any
    ) -> dict[str, Any]:
        if url == DELETE_FEEDBACK_ENDPOINT:
            if failures[0]:
                failures[0] -= 1
                raise ProviderUnavailableError("Pandora server error")
            deleted.append(data or {})
            return {}
        return await inner(method, url, data, **kw)

    provider._api_request = _api  # type: ignore[method-assign, assignment]
    return provider, sent, deleted


def _cache(provider: PandoraProvider) -> _FakeCache:
    return cast("_FakeCache", provider.mass.cache)


def _kept(provider: PandoraProvider, track_id: str) -> Any:
    return _cache(provider).data.get((track_id, provider.instance_id, CACHE_CATEGORY_FEEDBACK))


@pytest.mark.parametrize("favorite", [True, False])
async def test_clearing_a_rating_deletes_the_feedback_on_pandora(favorite: bool) -> None:
    """Like then clear, or dislike then clear: the thumb comes off Pandora too."""
    provider, sent, deleted = _clearing_provider()
    await provider.get_dynamic_radio_tracks(STATION_ID)
    await provider.set_favorite("TR:S1", MediaType.TRACK, favorite)
    await provider.set_favorite("TR:S1", MediaType.TRACK, None)
    assert len(sent) == 1
    assert deleted == [{"feedbackId": "fb-1", "isPositive": favorite}]
    assert _kept(provider, "TR:S1") is None


async def test_feedback_is_kept_past_the_fragment_that_held_the_track() -> None:
    """The id survives restarts and cache clears: a clear can come long after the rating."""
    provider, _sent, deleted = _clearing_provider()
    await provider.get_dynamic_radio_tracks(STATION_ID)
    await provider.set_favorite("TR:S1", MediaType.TRACK, True)
    assert (
        "TR:S1",
        provider.instance_id,
        CACHE_CATEGORY_FEEDBACK,
    ) in _cache(provider).persistent
    provider._sessions.clear()  # the fragment is gone
    await provider.set_favorite("TR:S1", MediaType.TRACK, None)
    assert deleted == [{"feedbackId": "fb-1", "isPositive": True}]


async def test_a_new_thumb_on_the_same_station_replaces_the_kept_one() -> None:
    """Like then dislike on one station keeps only the dislike, so a clear deletes just it."""
    provider, _sent, deleted = _clearing_provider()
    await provider.get_dynamic_radio_tracks(STATION_ID)
    await provider.set_favorite("TR:S1", MediaType.TRACK, True)
    await provider.set_favorite("TR:S1", MediaType.TRACK, False)
    await provider.set_favorite("TR:S1", MediaType.TRACK, None)
    assert deleted == [{"feedbackId": "fb-2", "isPositive": False}]


async def test_clearing_deletes_the_feedback_on_every_station() -> None:
    """A song rated on two stations loses both thumbs when the rating is cleared."""
    provider, _sent, deleted = _clearing_provider(_station_tracks("a"), _station_tracks("b"))
    await provider.get_dynamic_radio_tracks("station-a")
    await provider.get_stream_details("TR:S0", MediaType.TRACK)
    await provider.set_favorite("TR:S0", MediaType.TRACK, True)
    await provider.get_dynamic_radio_tracks("station-b")
    await provider.get_stream_details("TR:S0", MediaType.TRACK)
    await provider.set_favorite("TR:S0", MediaType.TRACK, True)
    await provider.set_favorite("TR:S0", MediaType.TRACK, None)
    assert [body["feedbackId"] for body in deleted] == ["fb-1", "fb-2"]
    assert _kept(provider, "TR:S0") is None


async def test_clearing_an_unknown_rating_warns_and_sends_nothing() -> None:
    """A thumb this provider never recorded cannot be deleted, and the log says so."""
    provider, _sent, deleted = _clearing_provider()
    await provider.set_favorite("TR:S1", MediaType.TRACK, None)
    assert deleted == []
    assert isinstance(provider.logger, Mock)
    provider.logger.warning.assert_called_once()


async def test_a_failed_delete_keeps_the_feedback_for_another_try() -> None:
    """A delete Pandora did not take leaves the id kept, so clearing again can finish it."""
    provider, _sent, deleted = _clearing_provider(fail_deletes=1)
    await provider.get_dynamic_radio_tracks(STATION_ID)
    await provider.set_favorite("TR:S1", MediaType.TRACK, True)
    with pytest.raises(ProviderUnavailableError):
        await provider.set_favorite("TR:S1", MediaType.TRACK, None)
    assert _kept(provider, "TR:S1") == [
        {"feedbackId": "fb-1", "stationId": STATION_ID, "isPositive": True}
    ]
    await provider.set_favorite("TR:S1", MediaType.TRACK, None)
    assert deleted == [{"feedbackId": "fb-1", "isPositive": True}]


async def test_feedback_without_an_id_is_not_kept() -> None:
    """If Pandora answers without a feedback id, nothing is kept and the log says why."""
    provider, _sent, _deleted = _clearing_provider()
    await provider.get_dynamic_radio_tracks(STATION_ID)
    await provider._remember_feedback("TR:S1", {"stationId": STATION_ID})
    assert _kept(provider, "TR:S1") is None
    assert isinstance(provider.logger, Mock)
    provider.logger.warning.assert_called_once()


def _hold_answers(provider: PandoraProvider, url: str, count: int) -> list[asyncio.Event]:
    """
    Hold back Pandora's answer to the next `count` requests to `url` until each event is set.

    The request itself goes through at once, so Pandora applies it in the order it was sent
    while the provider waits for the answer, as with a slow response.
    """
    gates = [asyncio.Event() for _ in range(count)]
    waiting = list(gates)
    inner = provider._api_request

    async def _api(
        method: str, url_: str, data: dict[str, Any] | None = None, **kw: Any
    ) -> dict[str, Any]:
        result = await inner(method, url_, data, **kw)
        if url_ == url and waiting:
            await waiting.pop(0).wait()
        return result

    provider._api_request = _api  # type: ignore[method-assign, assignment]
    return gates


async def _settle() -> None:
    """Let every started task run until it blocks."""
    for _ in range(20):
        await asyncio.sleep(0)


async def test_a_like_during_a_clear_keeps_its_feedback() -> None:
    """
    A like that lands while a clear waits on Pandora is kept once the clear finishes.

    Clears run as background tasks, so a like can come in while one is still deleting.
    """
    provider, _sent, deleted = _clearing_provider()
    await provider.get_dynamic_radio_tracks(STATION_ID)
    await provider.set_favorite("TR:S1", MediaType.TRACK, True)
    (delete_answer,) = _hold_answers(provider, DELETE_FEEDBACK_ENDPOINT, 1)
    clear = asyncio.create_task(provider.set_favorite("TR:S1", MediaType.TRACK, None))
    await _settle()
    like = asyncio.create_task(provider.set_favorite("TR:S1", MediaType.TRACK, True))
    await _settle()
    delete_answer.set()
    await asyncio.gather(clear, like)
    assert deleted == [{"feedbackId": "fb-1", "isPositive": True}]
    assert _kept(provider, "TR:S1") == [
        {"feedbackId": "fb-2", "stationId": STATION_ID, "isPositive": True}
    ]


async def test_overlapping_thumbs_keep_the_last_one() -> None:
    """
    Dislike then like, with the dislike answered last: the like is kept, as on Pandora.

    Pandora holds whichever thumb it received last, so that is the one a clear must delete.
    """
    provider, _sent, _deleted = _clearing_provider()
    await provider.get_dynamic_radio_tracks(STATION_ID)
    (dislike_answer,) = _hold_answers(provider, ADD_FEEDBACK_ENDPOINT, 1)
    dislike = asyncio.create_task(provider.set_favorite("TR:S1", MediaType.TRACK, False))
    await _settle()
    like = asyncio.create_task(provider.set_favorite("TR:S1", MediaType.TRACK, True))
    await _settle()
    dislike_answer.set()
    await asyncio.gather(dislike, like)
    assert _kept(provider, "TR:S1") == [
        {"feedbackId": "fb-2", "stationId": STATION_ID, "isPositive": True}
    ]


async def test_overlapping_thumbs_on_two_stations_are_both_kept() -> None:
    """Thumbs on two stations answered together each keep their id, so a clear finds both."""
    provider, _sent, _deleted = _clearing_provider(_station_tracks("a"), _station_tracks("b"))
    answers = _hold_answers(provider, ADD_FEEDBACK_ENDPOINT, 2)
    await provider.get_dynamic_radio_tracks("station-a")
    await provider.get_stream_details("TR:S0", MediaType.TRACK)
    on_a = asyncio.create_task(provider.set_favorite("TR:S0", MediaType.TRACK, True))
    await _settle()
    await provider.get_dynamic_radio_tracks("station-b")
    await provider.get_stream_details("TR:S0", MediaType.TRACK)
    on_b = asyncio.create_task(provider.set_favorite("TR:S0", MediaType.TRACK, True))
    await _settle()
    for answer in answers:
        answer.set()
    await asyncio.gather(on_a, on_b)
    kept = _kept(provider, "TR:S0")
    assert sorted(entry["stationId"] for entry in kept) == ["a", "b"]
