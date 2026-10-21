"""Tests for sending likes and dislikes on Pandora station tracks as thumbs up/down."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Self, cast
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest
from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    ProviderUnavailableError,
)

from music_assistant.providers.pandora import SUPPORTED_FEATURES
from music_assistant.providers.pandora import provider as provider_module
from music_assistant.providers.pandora.constants import (
    ADD_FEEDBACK_ENDPOINT,
    CACHE_CATEGORY_FEEDBACK,
    DELETE_FEEDBACK_ENDPOINT,
    FEEDBACK_NOT_FOUND_ERROR,
)
from music_assistant.providers.pandora.provider import PandoraProvider

from .test_provider import STATION_ID, _provider, _tracks
from .test_throttle import _CountingThrottler, _real_provider, _Response, _Session


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

    async def delete(
        self, key: str | None, category: int | None = None, provider: str | None = None
    ) -> None:
        """Delete the matching entries; a filter left out (None) matches every value."""
        await asyncio.sleep(0)
        for entry in list(self.data):
            if all(
                wanted is None or wanted == actual
                for wanted, actual in zip((key, provider, category), entry, strict=True)
            ):
                del self.data[entry]


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


@pytest.mark.parametrize("favorite", [True, False])
async def test_rating_a_track_no_fragment_holds_warns_and_sends_nothing(favorite: bool) -> None:
    """
    A track rated long after it played (from the library, say) is not sent, and no error is raised.

    Music Assistant has already saved the rating, so the log says why Pandora was not told.
    """
    provider, sent = _feedback_provider()
    await provider.get_dynamic_radio_tracks(STATION_ID)
    await provider.set_favorite("TR:nope", MediaType.TRACK, favorite)
    assert sent == []
    assert _kept(provider, "TR:nope") is None
    assert isinstance(provider.logger, Mock)
    provider.logger.warning.assert_called_once()


@pytest.mark.parametrize(
    ("field", "value"),
    [("trackToken", ""), ("allowFeedback", False)],
)
@pytest.mark.parametrize("favorite", [True, False])
async def test_a_track_pandora_cannot_rate_warns_and_sends_nothing(
    field: str, value: Any, favorite: bool
) -> None:
    """
    A retained track served without a trackToken, or with allowFeedback=False, is not sent.

    As for a track no fragment holds, Music Assistant has already saved the rating, so the log
    says why Pandora was not told instead of an error the user cannot act on.
    """
    tracks = _tracks()
    tracks[0][field] = value
    provider, sent = _feedback_provider(tracks)
    await provider.get_dynamic_radio_tracks(STATION_ID)
    await provider.set_favorite("TR:S0", MediaType.TRACK, favorite)
    assert sent == []
    assert _kept(provider, "TR:S0") is None
    assert isinstance(provider.logger, Mock)
    provider.logger.warning.assert_called_once()


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
    gone: frozenset[str] = frozenset(),
) -> tuple[PandoraProvider, list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Build a `_feedback_provider` that also records every deleteFeedback body.

    :param fail_deletes: How many deletes fail, from the first, before they succeed.
    :param gone: Feedback ids Pandora no longer holds, as after a removal in the Pandora app.
    """
    provider, sent = _feedback_provider(tracks, *later)
    deleted: list[dict[str, Any]] = []
    failures = [fail_deletes]
    inner = provider._api_request

    async def _api(
        method: str, url: str, data: dict[str, Any] | None = None, **kw: Any
    ) -> dict[str, Any]:
        if url == DELETE_FEEDBACK_ENDPOINT:
            if (data or {}).get("feedbackId") in gone:
                raise MediaNotFoundError("Pandora feedback not found")
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


async def _rate_on_both_stations(provider: PandoraProvider) -> None:
    """Play TR:S0 on station a and like it, then the same on station b (fb-1, then fb-2)."""
    for station in ("station-a", "station-b"):
        await provider.get_dynamic_radio_tracks(station)
        await provider.get_stream_details("TR:S0", MediaType.TRACK)
        await provider.set_favorite("TR:S0", MediaType.TRACK, True)


async def test_a_thumb_removed_on_pandora_is_forgotten() -> None:
    """
    A thumb removed in the Pandora app since is forgotten, so the clear does not fail.

    Kept for a retry, it would fail every later clear and the rating could never be cleared.
    """
    provider, _sent, deleted = _clearing_provider(gone=frozenset({"fb-1"}))
    await provider.get_dynamic_radio_tracks(STATION_ID)
    await provider.set_favorite("TR:S1", MediaType.TRACK, True)
    await provider.set_favorite("TR:S1", MediaType.TRACK, None)
    assert deleted == []
    assert _kept(provider, "TR:S1") is None


async def test_a_thumb_removed_on_pandora_does_not_block_the_other_stations() -> None:
    """With one station's thumb gone from Pandora, the other station's thumb is still deleted."""
    provider, _sent, deleted = _clearing_provider(
        _station_tracks("a"), _station_tracks("b"), gone=frozenset({"fb-1"})
    )
    await _rate_on_both_stations(provider)
    await provider.set_favorite("TR:S0", MediaType.TRACK, None)
    assert [body["feedbackId"] for body in deleted] == ["fb-2"]
    assert _kept(provider, "TR:S0") is None


async def test_a_failed_delete_on_one_station_still_deletes_the_other() -> None:
    """One delete failing still deletes the rest, and keeps only the failed one for a retry."""
    provider, _sent, deleted = _clearing_provider(
        _station_tracks("a"), _station_tracks("b"), fail_deletes=1
    )
    await _rate_on_both_stations(provider)
    with pytest.raises(ProviderUnavailableError):
        await provider.set_favorite("TR:S0", MediaType.TRACK, None)
    assert [body["feedbackId"] for body in deleted] == ["fb-2"]
    assert _kept(provider, "TR:S0") == [
        {"feedbackId": "fb-1", "stationId": "a", "isPositive": True}
    ]


async def test_a_timed_out_delete_is_kept_for_a_retry() -> None:
    """A delete that times out is kept like any other failed one, and the rest still go."""
    provider, _sent, deleted = _clearing_provider(_station_tracks("a"), _station_tracks("b"))
    await _rate_on_both_stations(provider)
    inner = provider._api_request
    timeouts = [1]

    async def _api(
        method: str, url: str, data: dict[str, Any] | None = None, **kw: Any
    ) -> dict[str, Any]:
        if url == DELETE_FEEDBACK_ENDPOINT and timeouts[0]:
            timeouts[0] -= 1
            raise TimeoutError
        return await inner(method, url, data, **kw)

    provider._api_request = _api  # type: ignore[method-assign, assignment]
    with pytest.raises(TimeoutError):
        await provider.set_favorite("TR:S0", MediaType.TRACK, None)
    assert [body["feedbackId"] for body in deleted] == ["fb-2"]
    assert _kept(provider, "TR:S0") == [
        {"feedbackId": "fb-1", "stationId": "a", "isPositive": True}
    ]


async def test_a_malformed_kept_entry_stops_the_clear() -> None:
    """An error that is not a failed request is not retried later: it stops the clear at once."""
    provider, _sent, deleted = _clearing_provider()
    kept = [
        {"stationId": "a", "isPositive": True},
        {"feedbackId": "fb-2", "stationId": "b", "isPositive": True},
    ]
    await provider._set_kept_feedback("TR:S1", kept)
    with pytest.raises(KeyError):
        await provider._clear_feedback("TR:S1")
    assert deleted == []
    assert _kept(provider, "TR:S1") == kept


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (
            {
                "message": "Could not find feedback for id 1",
                "errorCode": 0,
                "errorString": FEEDBACK_NOT_FOUND_ERROR,
            },
            MediaNotFoundError,
        ),
        ({"message": "Bad request", "errorCode": 0, "errorString": "OTHER"}, InvalidDataError),
    ],
)
async def test_feedback_pandora_no_longer_holds_is_reported_as_not_found(
    payload: dict[str, Any], error: type[Exception]
) -> None:
    """Pandora's HTTP 400 for a missing feedback id becomes not found; other 400s stay invalid."""
    provider = _real_provider([_Response(400, payload)], _CountingThrottler())
    with pytest.raises(error):
        await provider._api_request(
            "POST", DELETE_FEEDBACK_ENDPOINT, data={"feedbackId": "1", "isPositive": True}
        )


@pytest.mark.parametrize("is_removed", [True, False])
async def test_removing_the_provider_drops_its_kept_feedback(is_removed: bool) -> None:
    """
    Removing the provider deletes its feedback ids; a reload keeps them for later clears.

    They are persistent, which the cache cleanup for a removed provider skips.
    """
    provider, _sent, _deleted = _clearing_provider()
    await provider.get_dynamic_radio_tracks(STATION_ID)
    await provider.set_favorite("TR:S1", MediaType.TRACK, True)
    other = ("TR:S1", "pandora--other", CACHE_CATEGORY_FEEDBACK)
    _cache(provider).data[other] = ["another instance"]
    await provider.unload(is_removed=is_removed)
    assert (_kept(provider, "TR:S1") is None) is is_removed
    assert _cache(provider).data[other] == ["another instance"]


class _ProxySession(_Session):
    """A SOCKS proxy session, which the provider owns; once closed it is never reopened."""

    closed = False

    async def close(self) -> None:
        self.closed = True


class _DroppedResponse(_Response):
    """A request whose connection drops before Pandora answers."""

    def __init__(self) -> None:
        super().__init__(0, {})

    async def __aenter__(self) -> Self:
        raise aiohttp.ClientConnectionError("Connection dropped")


class _TimedOutResponse(_Response):
    """A request that runs past its timeout before Pandora answers."""

    def __init__(self) -> None:
        super().__init__(0, {})

    async def __aenter__(self) -> Self:
        raise TimeoutError


class _GarbledResponse(_Response):
    """A 200 answer whose body cannot be read as JSON."""

    def __init__(self) -> None:
        super().__init__(200, {})

    async def json(self) -> dict[str, Any]:
        raise json.JSONDecodeError("Expecting value", "", 0)


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        (_Response(400, {"errorString": FEEDBACK_NOT_FOUND_ERROR}), MediaNotFoundError),
        (_Response(400, {"errorString": "OTHER"}), InvalidDataError),
        (_Response(404, {}), MediaNotFoundError),
        (_Response(500, {}), ProviderUnavailableError),
        (_DroppedResponse(), ProviderUnavailableError),
        (_GarbledResponse(), InvalidDataError),
    ],
)
async def test_a_failed_request_leaves_the_socks_proxy_session_open(
    failure: _Response, error: type[Exception]
) -> None:
    """A failed request behind a SOCKS proxy keeps the session, so the next one works."""
    provider = _real_provider([], _CountingThrottler())
    session = _ProxySession([failure, _Response(200, {"ok": True})])
    provider.http_session = session  # type: ignore[assignment]
    provider._socks_proxy = True
    with pytest.raises(error):
        await provider._api_request(
            "POST", DELETE_FEEDBACK_ENDPOINT, data={"feedbackId": "1", "isPositive": True}
        )
    assert not session.closed
    assert await provider._api_request("POST", DELETE_FEEDBACK_ENDPOINT, data={}) == {"ok": True}


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        (_Response(404, {}), MediaNotFoundError),
        (_Response(400, {"errorString": "OTHER"}), InvalidDataError),
    ],
)
async def test_a_rejected_fragment_fetch_leaves_the_socks_proxy_session_open(
    failure: _Response, error: type[Exception]
) -> None:
    """A station fetch Pandora rejects behind a SOCKS proxy keeps the session for the next one."""
    provider = _real_provider([], _CountingThrottler())
    session = _ProxySession([failure, _Response(200, {"ok": True})])
    provider.http_session = session  # type: ignore[assignment]
    provider._socks_proxy = True
    with pytest.raises(error):
        await provider._fetch_fragment(provider._get_or_create_session(STATION_ID))
    assert not session.closed
    assert await provider._api_request("POST", DELETE_FEEDBACK_ENDPOINT, data={}) == {"ok": True}


async def test_a_request_before_login_leaves_the_socks_proxy_session_open() -> None:
    """A request made before login fails without closing the proxy session login will need."""
    provider = _real_provider([], _CountingThrottler())
    session = _ProxySession([])
    provider.http_session = session  # type: ignore[assignment]
    provider._socks_proxy = True
    provider._auth_token = None
    with pytest.raises(LoginFailed):
        await provider._api_request("POST", DELETE_FEEDBACK_ENDPOINT, data={})
    assert not session.closed


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        (_Response(403, {}), LoginFailed),
        (_Response(200, {}), LoginFailed),
        (_DroppedResponse(), ProviderUnavailableError),
    ],
)
async def test_a_failed_login_leaves_the_socks_proxy_session_open(
    monkeypatch: pytest.MonkeyPatch, failure: _Response, error: type[Exception]
) -> None:
    """A login that fails behind a SOCKS proxy keeps the session, so the next login works."""
    provider = _real_provider([], _CountingThrottler())
    session = _ProxySession([failure, _Response(200, {"authToken": "token"})])
    provider.http_session = session  # type: ignore[assignment]
    provider._socks_proxy = True
    monkeypatch.setattr(provider_module, "get_csrf_token", AsyncMock(return_value="csrf"))
    with pytest.raises(error):
        await provider._authenticate("user", "secret")
    assert not session.closed
    await provider._authenticate("user", "secret")
    assert provider._auth_token == "token"


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        (_Response(403, {}), LoginFailed),
        (_DroppedResponse(), ProviderUnavailableError),
        (_TimedOutResponse(), TimeoutError),
    ],
)
async def test_a_failed_first_login_closes_the_socks_proxy_session(
    monkeypatch: pytest.MonkeyPatch, failure: _Response, error: type[Exception]
) -> None:
    """A provider whose first login fails is never registered, so it closes its own session."""
    provider = _real_provider([], _CountingThrottler())
    session = _ProxySession([failure])
    provider.mass = Mock()
    monkeypatch.setattr(provider, "get_setup_value", Mock(return_value="credential"))
    monkeypatch.setattr(provider_module, "get_socks5_url", Mock(return_value="socks5://proxy"))
    monkeypatch.setattr(provider_module, "create_clientsession", Mock(return_value=session))
    monkeypatch.setattr(provider_module, "get_csrf_token", AsyncMock(return_value="csrf"))
    with pytest.raises(error):
        await provider.handle_async_init()
    assert session.closed


async def test_a_bug_during_the_first_login_is_not_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An error that is not a failed login reaches the loader untouched, without the cleanup."""
    provider = _real_provider([], _CountingThrottler())
    session = _ProxySession([])
    provider.mass = Mock()
    monkeypatch.setattr(provider, "get_setup_value", Mock(return_value="credential"))
    monkeypatch.setattr(provider_module, "get_socks5_url", Mock(return_value="socks5://proxy"))
    monkeypatch.setattr(provider_module, "create_clientsession", Mock(return_value=session))
    monkeypatch.setattr(provider_module, "get_csrf_token", AsyncMock(side_effect=RuntimeError))
    with pytest.raises(RuntimeError):
        await provider.handle_async_init()
    assert not session.closed


async def test_unloading_closes_the_socks_proxy_session() -> None:
    """The provider's own proxy session is closed when the provider unloads."""
    provider = _real_provider([], _CountingThrottler())
    session = _ProxySession([])
    provider.http_session = session  # type: ignore[assignment]
    provider._socks_proxy = True
    await provider.unload()
    assert session.closed


async def test_a_clear_during_a_like_deletes_the_new_feedback() -> None:
    """
    A clear that arrives while a like waits on Pandora deletes the thumb that like set.

    Without waiting for the like, the clear would find nothing kept and leave the thumb on
    Pandora while Music Assistant shows the rating cleared.
    """
    provider, _sent, deleted = _clearing_provider()
    await provider.get_dynamic_radio_tracks(STATION_ID)
    (like_answer,) = _hold_answers(provider, ADD_FEEDBACK_ENDPOINT, 1)
    like = asyncio.create_task(provider.set_favorite("TR:S1", MediaType.TRACK, True))
    await _settle()
    clear = asyncio.create_task(provider.set_favorite("TR:S1", MediaType.TRACK, None))
    await _settle()
    like_answer.set()
    await asyncio.gather(like, clear)
    assert deleted == [{"feedbackId": "fb-1", "isPositive": True}]
    assert _kept(provider, "TR:S1") is None


async def test_two_answers_without_a_station_are_both_kept() -> None:
    """Two answers without a stationId cannot be matched to a station, so both ids are kept."""
    provider, _sent, deleted = _clearing_provider()
    await provider.get_dynamic_radio_tracks(STATION_ID)
    await provider._remember_feedback("TR:S1", {"feedbackId": "fb-x", "isPositive": True})
    await provider._remember_feedback("TR:S1", {"feedbackId": "fb-y", "isPositive": False})
    assert [entry["feedbackId"] for entry in _kept(provider, "TR:S1")] == ["fb-x", "fb-y"]
    await provider.set_favorite("TR:S1", MediaType.TRACK, None)
    assert [body["feedbackId"] for body in deleted] == ["fb-x", "fb-y"]
    assert _kept(provider, "TR:S1") is None


async def test_feedback_recorded_without_a_station_is_still_cleared() -> None:
    """
    An answer without a stationId is kept beside a later thumb on a named station.

    It cannot be matched to the later thumb's station, so a clear deletes both; had both named
    the same thumb, the second delete would be answered not found and forgotten.
    """
    provider, _sent, deleted = _clearing_provider()
    await provider.get_dynamic_radio_tracks(STATION_ID)
    await provider._remember_feedback("TR:S1", {"feedbackId": "fb-x", "isPositive": True})
    await provider.set_favorite("TR:S1", MediaType.TRACK, False)
    assert [entry["stationId"] for entry in _kept(provider, "TR:S1")] == [None, STATION_ID]
    await provider.set_favorite("TR:S1", MediaType.TRACK, None)
    assert [body["feedbackId"] for body in deleted] == ["fb-x", "fb-1"]
    assert _kept(provider, "TR:S1") is None
