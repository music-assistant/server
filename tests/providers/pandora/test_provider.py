"""Tests for the Pandora provider's dynamic-radio and streaming surface."""

from __future__ import annotations

import time
from typing import Any, Self
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Radio, SearchResults, Track

from music_assistant.providers.pandora import provider as provider_module
from music_assistant.providers.pandora.constants import STATIONS_ENDPOINT
from music_assistant.providers.pandora.fragments import (
    FRAGMENT_STALE_SECONDS,
    FRAGMENT_URL_TTL_SECONDS,
    MAX_RETAINED_FRAGMENTS,
)
from music_assistant.providers.pandora.provider import PandoraProvider

STATION_ID = "4360491625318318161"


def _tracks(count: int = 4, prefix: str = "S") -> list[dict[str, Any]]:
    """Build `count` raw Pandora track dicts with distinct Pandora ids."""
    return [
        {
            "musicId": f"{prefix}{index}",
            "pandoraId": f"TR:{prefix}{index}",
            "stationId": STATION_ID,
            "songTitle": f"Song {index}",
            "artistName": "Some Artist",
            "albumTitle": "Some Album",
            "albumDetailURL": "https://www.pandora.com/artist/album",
            "songDetailURL": "https://www.pandora.com/artist/album/song",
            "trackLength": 180,
            "audioURL": f"https://audio-sv5-t3-2.pandora.com/access/{index}.mp4",
        }
        for index in range(count)
    ]


def _stations(names: list[str]) -> list[dict[str, Any]]:
    """Build raw Pandora station dicts with the given names."""
    return [{"stationId": f"station-{index}", "name": name} for index, name in enumerate(names)]


def _provider(
    payloads: list[list[dict[str, Any]]] | None = None,
    stations: list[dict[str, Any]] | None = None,
) -> PandoraProvider:
    """
    Build a bare provider whose Pandora API calls return canned payloads.

    The stub sits at `_api_request`, not `_fetch_fragment`/`_get_stations`, so the real
    filtering, empty-fragment guard and session retention all execute under test.
    """
    provider = PandoraProvider.__new__(PandoraProvider)
    provider.manifest = Mock(domain="pandora")
    provider.config = Mock(instance_id="pandora--test")
    provider.logger = Mock()
    provider.http_session = Mock(closed=False)
    provider._sessions = {}
    provider._high_quality_available = False
    pending = list(payloads or [_tracks()])
    station_list = stations or []

    async def _fake_api_request(
        method: str,  # noqa: ARG001
        url: str,
        data: dict[str, Any] | None = None,  # noqa: ARG001
        **kwargs: Any,  # noqa: ARG001
    ) -> dict[str, Any]:
        """Return the next canned payload instead of calling Pandora."""
        if url == STATIONS_ENDPOINT:
            return {"stations": station_list}
        return {"tracks": pending.pop(0) if pending else _tracks()}

    provider._api_request = _fake_api_request  # type: ignore[method-assign, assignment]
    return provider


async def test_stations_are_served_as_dynamic_radio() -> None:
    """A station is a Radio with is_dynamic set, not a Playlist."""
    provider = _provider(stations=_stations(["Station One", "Station Two"]))
    stations = [station async for station in provider.get_library_radios()]
    assert [station.item_id for station in stations] == ["station-0", "station-1"]
    assert all(isinstance(station, Radio) for station in stations)
    assert all(station.is_dynamic for station in stations)


async def test_get_radio_resolves_a_station_by_id() -> None:
    """A station id resolves to its Radio."""
    provider = _provider(stations=_stations(["Station One", "Station Two"]))
    station = await provider.get_radio("station-1")
    assert station.item_id == "station-1"
    assert station.is_dynamic


async def test_get_radio_raises_for_an_unknown_station() -> None:
    """An id no station carries is not found."""
    provider = _provider(stations=_stations(["Station One"]))
    with pytest.raises(MediaNotFoundError, match="station-404"):
        await provider.get_radio("station-404")


async def test_dynamic_radio_tracks_serve_the_live_fragment() -> None:
    """The station's pending tracks come back, with no paging parameter to satisfy."""
    provider = _provider()
    tracks = await provider.get_dynamic_radio_tracks(STATION_ID)
    assert [track.item_id for track in tracks] == ["TR:S0", "TR:S1", "TR:S2", "TR:S3"]


async def test_search_returns_stations_as_radio() -> None:
    """Station search answers MediaType.RADIO into SearchResults.radio."""
    provider = _provider(stations=_stations(["Rock Radio", "Jazz Radio"]))
    results = await provider.search("jazz", [MediaType.RADIO])
    assert [station.item_id for station in results.radio] == ["station-1"]
    assert results.playlists == []


async def test_search_ignores_a_playlist_only_request() -> None:
    """Stations are no longer playlists, so a playlist search finds nothing."""
    provider = _provider(stations=_stations(["Rock Radio"]))
    results = await provider.search("rock", [MediaType.PLAYLIST])
    assert results.radio == []
    assert results.playlists == []


async def test_album_is_addressed_by_its_tracks_id() -> None:
    """A fragment names no album, so the track's id stands in - and it must round-trip."""
    provider = _provider()
    await provider.get_dynamic_radio_tracks(STATION_ID)
    album = await provider.get_album("TR:S0")
    assert album.item_id == "TR:S0"
    assert album.name == "Some Album"


async def test_album_is_gone_once_its_track_ages_out() -> None:
    """A track-keyed album only exists while the fragment naming it is still retained."""
    prefixes = [chr(ord("A") + index) for index in range(MAX_RETAINED_FRAGMENTS + 1)]
    provider = _provider([_tracks(prefix=prefix) for prefix in prefixes])
    for prefix in prefixes:
        await provider.get_dynamic_radio_tracks(STATION_ID)
        await provider.get_stream_details(f"TR:{prefix}3", MediaType.TRACK)
    with pytest.raises(MediaNotFoundError):
        await provider.get_album("TR:A0")


async def test_artist_is_identified_by_name() -> None:
    """Pandora names a fragment's artist but never identifies it, so the name is the id."""
    provider = _provider()
    artist = await provider.get_artist("Some Artist")
    assert artist.item_id == "Some Artist"
    assert artist.name == "Some Artist"


async def test_get_track_matches_radio_tracks_identity() -> None:
    """A track resolves to the same album and artist by either entry point."""
    provider = _provider()
    listed = (await provider.get_dynamic_radio_tracks(STATION_ID))[0]
    assert isinstance(listed, Track)
    looked_up = await provider.get_track("TR:S0")
    assert looked_up.album is not None
    assert listed.album is not None
    assert looked_up.album.item_id == listed.album.item_id
    assert looked_up.artists[0].item_id == listed.artists[0].item_id


async def test_search_returns_a_matching_station_as_a_radio() -> None:
    """A station whose name matches the query comes back in the radio results."""
    provider = _provider(stations=_stations(["Coldplay Radio", "Jazz Radio"]))
    results = await provider.search("Coldplay Radio", [MediaType.RADIO])
    assert [radio.name for radio in results.radio] == ["Coldplay Radio"]


async def test_search_matches_part_of_a_station_name() -> None:
    """A partial query finds the station; whole-name-only matching makes search useless."""
    provider = _provider(stations=_stations(["Classic Rock Radio", "Jazz Radio"]))
    results = await provider.search("rock", [MediaType.RADIO])
    assert [radio.name for radio in results.radio] == ["Classic Rock Radio"]


async def test_search_ignores_case() -> None:
    """Queries match case-insensitively."""
    provider = _provider(stations=_stations(["Classic Rock Radio"]))
    results = await provider.search("CLASSIC rock", [MediaType.RADIO])
    assert len(results.radio) == 1


async def test_search_honours_the_limit() -> None:
    """A query matching many stations stops at the requested limit."""
    provider = _provider(stations=_stations([f"Rock Radio {index}" for index in range(5)]))
    results = await provider.search("rock", [MediaType.RADIO], limit=2)
    assert len(results.radio) == 2


async def test_search_finds_nothing_for_a_non_matching_query() -> None:
    """A query that matches no station name returns no stations."""
    provider = _provider(stations=_stations(["Coldplay Radio"]))
    results = await provider.search("Nonexistent Station", [MediaType.RADIO])
    assert results.radio == []


async def test_search_without_radio_media_type_skips_the_station_lookup() -> None:
    """Stations only ever surface as radio, so excluding that type returns nothing at all."""
    provider = _provider(stations=_stations(["Coldplay Radio"]))
    results = await provider.search("Coldplay Radio", [MediaType.TRACK])
    assert results == SearchResults()


@pytest.mark.parametrize("search_query", ["", "   "])
async def test_search_for_an_empty_query_returns_nothing(search_query: str) -> None:
    """An empty query matches every station as a substring, so it must not reach the lookup."""
    provider = _provider(stations=_stations(["Coldplay Radio", "Jazz Radio"]))
    calls: list[str] = []
    inner = provider._api_request

    async def _recording_api_request(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        """Record the endpoint before delegating, so a needless lookup is visible."""
        calls.append(url)
        return await inner(method, url, **kwargs)

    provider._api_request = _recording_api_request  # type: ignore[method-assign, assignment]
    results = await provider.search(search_query, [MediaType.RADIO])
    assert results.radio == []
    assert calls == []


async def test_first_request_returns_a_fragment() -> None:
    """A station with no session yet fetches and returns its first fragment."""
    provider = _provider()
    tracks = await provider.get_dynamic_radio_tracks(STATION_ID)
    assert [track.item_id for track in tracks] == [f"TR:S{i}" for i in range(4)]


async def test_track_id_is_the_bare_pandora_id() -> None:
    """A track is identified by Pandora's own catalogue id, not by station and musicId."""
    provider = _provider()
    tracks = await provider.get_dynamic_radio_tracks(STATION_ID)
    assert [track.item_id for track in tracks] == [f"TR:S{index}" for index in range(4)]


async def test_the_same_song_from_two_stations_is_one_item() -> None:
    """Station context must not fork a song's identity - that is two library rows."""
    provider = _provider()
    first = await provider.get_dynamic_radio_tracks("station-a")
    second = await provider.get_dynamic_radio_tracks("station-b")
    assert first[0].item_id == second[0].item_id


async def test_a_track_without_a_pandora_id_is_not_served() -> None:
    """A track lacking a pandoraId cannot be handed to the queue: the id is identity now."""
    usable = _tracks(count=2)
    unusable = _tracks(count=2, prefix="X")
    for track in unusable:
        del track["pandoraId"]
    provider = _provider(payloads=[usable + unusable])
    tracks = await provider.get_dynamic_radio_tracks(STATION_ID)
    assert [track.item_id for track in tracks] == ["TR:S0", "TR:S1"]


async def test_a_fragment_with_no_identifiable_track_is_refused() -> None:
    """Retaining a fragment nothing can be served from would stall the station."""
    tracks = _tracks()
    for track in tracks:
        del track["pandoraId"]
    provider = _provider(payloads=[tracks])
    with pytest.raises(MediaNotFoundError):
        await provider.get_dynamic_radio_tracks(STATION_ID)


async def test_stream_details_resolve_without_station_context() -> None:
    """A bare pandoraId resolves against whichever session holds it."""
    provider = _provider()
    await provider.get_dynamic_radio_tracks(STATION_ID)
    details = await provider.get_stream_details("TR:S1", MediaType.TRACK)
    assert details.item_id == "TR:S1"
    assert details.stream_type == StreamType.HTTP


async def test_browse_then_play_returns_the_same_batch() -> None:
    """A browse leaves the fragment live, so play must still get its tracks."""
    provider = _provider()
    browsed = await provider.get_dynamic_radio_tracks(STATION_ID)
    played = await provider.get_dynamic_radio_tracks(STATION_ID)
    assert [track.item_id for track in played] == [track.item_id for track in browsed]


async def test_refill_serves_the_live_fragment_without_refetching() -> None:
    """
    A refill mid-fragment must not pull a new one, and must not re-serve a played track.

    Returning [] here would read as end-of-playlist; the core de-duplicates the remaining
    repeats via its unplayed-tail check, but a track already handed to the audio pipeline
    must never come back - that check drops it once playback has moved past it.
    """
    provider = _provider([_tracks(prefix="A"), _tracks(prefix="B")])
    await provider.get_dynamic_radio_tracks(STATION_ID)
    await provider.get_stream_details("TR:A0", MediaType.TRACK)
    tracks = await provider.get_dynamic_radio_tracks(STATION_ID)
    assert [track.item_id for track in tracks] == [f"TR:A{i}" for i in range(1, 4)]
    assert "TR:A0" not in [track.item_id for track in tracks]


async def test_replay_after_stopping_mid_fragment_still_builds_a_queue() -> None:
    """Stopping after one track and playing again must not yield an empty queue."""
    provider = _provider()
    await provider.get_dynamic_radio_tracks(STATION_ID)
    await provider.get_stream_details("TR:S0", MediaType.TRACK)
    # user stops, then presses play again on the same station
    replayed = await provider.get_dynamic_radio_tracks(STATION_ID)
    assert [track.item_id for track in replayed] == [f"TR:S{i}" for i in range(1, 4)]


async def test_empty_fragment_is_not_retained() -> None:
    """
    A fragment with no playable tracks must raise, not become the live fragment.

    Retaining it would make it current with nothing able to spend it, so the station
    would serve nothing until the staleness window elapsed.
    """
    curator_only = [
        {"musicId": "S0", "stationId": STATION_ID, "songTitle": "Curator Message", "audioURL": ""}
    ]
    provider = _provider([curator_only])
    with pytest.raises(MediaNotFoundError):
        await provider.get_dynamic_radio_tracks(STATION_ID)
    assert provider._sessions[STATION_ID].current is None


async def test_track_with_null_song_title_gets_a_fallback_name() -> None:
    """A JSON-null songTitle must not crash title parsing; the track still comes through."""
    tracks = _tracks()
    tracks[0]["songTitle"] = None
    provider = _provider([tracks])
    result = await provider.get_dynamic_radio_tracks(STATION_ID)
    assert [track.item_id for track in result] == [f"TR:S{i}" for i in range(4)]
    assert result[0].name == "Unknown Song"


async def test_track_with_null_track_length_gets_zero_duration() -> None:
    """A JSON-null trackLength must not crash int(); it degrades to a zero duration."""
    tracks = _tracks()
    tracks[0]["trackLength"] = None
    provider = _provider([tracks])
    result = await provider.get_dynamic_radio_tracks(STATION_ID)
    assert result[0].duration == 0


async def test_track_with_null_album_title_gets_a_fallback_name() -> None:
    """A JSON-null albumTitle must not crash album parsing; it falls back to a default name."""
    tracks = _tracks()
    tracks[0]["albumTitle"] = None
    provider = _provider([tracks])
    first = (await provider.get_dynamic_radio_tracks(STATION_ID))[0]
    assert isinstance(first, Track)
    assert first.album is not None
    assert first.album.name == "Unknown Album"


async def test_track_with_a_sized_art_entry_missing_url_does_not_crash() -> None:
    """A size-500 art entry without a url key must not raise KeyError while parsing album art."""
    tracks = _tracks()
    tracks[0]["albumArt"] = [{"size": 500}]
    provider = _provider([tracks])
    result = await provider.get_dynamic_radio_tracks(STATION_ID)
    assert [track.item_id for track in result] == [f"TR:S{i}" for i in range(4)]


async def test_station_art_lands_on_the_parsed_station() -> None:
    """A station's art entry becomes an image on the parsed Radio's metadata."""
    stations = _stations(["Station One"])
    stations[0]["art"] = [{"size": 500, "url": "https://example.com/station.jpg"}]
    provider = _provider(stations=stations)
    station = await provider.get_radio("station-0")
    assert [image.path for image in station.metadata.images or []] == [
        "https://example.com/station.jpg"
    ]


async def test_track_art_lands_on_the_parsed_track() -> None:
    """A track's albumArt entry becomes an image on the parsed Track's metadata."""
    tracks = _tracks()
    tracks[0]["albumArt"] = [{"size": 500, "url": "https://example.com/track.jpg"}]
    provider = _provider([tracks])
    result = await provider.get_dynamic_radio_tracks(STATION_ID)
    assert [image.path for image in result[0].metadata.images or []] == [
        "https://example.com/track.jpg"
    ]


async def test_refill_advances_once_the_last_track_is_resolved() -> None:
    """Resolving the final track opens the gate for the next fragment."""
    provider = _provider([_tracks(prefix="A"), _tracks(prefix="B")])
    await provider.get_dynamic_radio_tracks(STATION_ID)
    await provider.get_stream_details("TR:A3", MediaType.TRACK)
    tracks = await provider.get_dynamic_radio_tracks(STATION_ID)
    assert [track.item_id for track in tracks] == [f"TR:B{i}" for i in range(4)]


async def test_stream_details_point_at_the_pandora_url() -> None:
    """The provider streams by URL and never buffers audio itself."""
    provider = _provider()
    await provider.get_dynamic_radio_tracks(STATION_ID)
    details = await provider.get_stream_details("TR:S1", MediaType.TRACK)
    assert details.stream_type is StreamType.HTTP
    assert details.path == "https://audio-sv5-t3-2.pandora.com/access/1.mp4"
    assert details.duration == 180
    assert details.can_seek is True
    assert details.allow_seek is True


async def test_stream_details_for_an_evicted_track_raises() -> None:
    """A track outside the live fragment has a dead URL; fail loudly instead of serving it."""
    provider = _provider([_tracks(prefix="A"), _tracks(prefix="B")])
    await provider.get_dynamic_radio_tracks(STATION_ID)
    await provider.get_stream_details("TR:A3", MediaType.TRACK)
    await provider.get_dynamic_radio_tracks(STATION_ID)
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details("TR:A0", MediaType.TRACK)


async def test_stream_details_after_the_urls_expire_raises() -> None:
    """
    A pause long enough to outlive the signed URLs must fail by name, not by a CDN 403.

    Nothing refills a paused queue, so the gate in get_dynamic_radio_tracks never runs - this path
    is the only thing standing between a resumed track and an expired URL.
    """
    provider = _provider()
    await provider.get_dynamic_radio_tracks(STATION_ID)
    fragment = provider._sessions[STATION_ID].current
    assert fragment is not None
    fragment.fetched_at -= FRAGMENT_URL_TTL_SECONDS + 1
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details("TR:S0", MediaType.TRACK)


async def test_stream_details_after_an_ordinary_pause_still_serves() -> None:
    """
    A pause past the staleness window must still resume: those URLs have not expired.

    Staleness decides whether a fragment is worth replacing on the next refill; it is not a
    reason to refuse playback of tracks that would otherwise play perfectly well.
    """
    provider = _provider()
    await provider.get_dynamic_radio_tracks(STATION_ID)
    fragment = provider._sessions[STATION_ID].current
    assert fragment is not None
    fragment.last_activity_at -= FRAGMENT_STALE_SECONDS + 1
    assert fragment.is_stale(time.time()) is True
    details = await provider.get_stream_details("TR:S0", MediaType.TRACK)
    assert details.path == "https://audio-sv5-t3-2.pandora.com/access/0.mp4"


async def test_a_fresher_session_serves_a_track_another_holds_expired() -> None:
    """
    Stations overlap: one station's expired copy must not fail a track another can still play.

    Refusing on the first match made playback failure depend on which station was browsed
    first, which is not something the user can see or influence.
    """
    provider = _provider()
    await provider.get_dynamic_radio_tracks("station-a")
    await provider.get_dynamic_radio_tracks("station-b")
    stale = provider._sessions["station-a"].current
    assert stale is not None
    stale.fetched_at -= FRAGMENT_URL_TTL_SECONDS + 1
    details = await provider.get_stream_details("TR:S0", MediaType.TRACK)
    assert details.item_id == "TR:S0"
    assert details.path == "https://audio-sv5-t3-2.pandora.com/access/0.mp4"


async def test_every_copy_expired_still_raises_the_named_error() -> None:
    """With no session able to serve it, the failure is still the paused-too-long one."""
    provider = _provider()
    await provider.get_dynamic_radio_tracks("station-a")
    await provider.get_dynamic_radio_tracks("station-b")
    for station in ("station-a", "station-b"):
        fragment = provider._sessions[station].current
        assert fragment is not None
        fragment.fetched_at -= FRAGMENT_URL_TTL_SECONDS + 1
    with pytest.raises(MediaNotFoundError, match="expired while playback was stopped"):
        await provider.get_stream_details("TR:S0", MediaType.TRACK)


async def test_the_serving_session_is_the_one_marked_as_having_served() -> None:
    """
    Recording the hand-out on another station's fragment corrupts both stations' refills.

    The served track stays pending where it played and is re-offered, while the fragment
    that never served it is driven towards spent.
    """
    provider = _provider()
    await provider.get_dynamic_radio_tracks("station-a")
    await provider.get_dynamic_radio_tracks("station-b")
    older = provider._sessions["station-a"].current
    newer = provider._sessions["station-b"].current
    assert older is not None
    assert newer is not None
    older.fetched_at -= 60
    await provider.get_stream_details("TR:S0", MediaType.TRACK)
    assert newer.served == {"TR:S0"}
    assert older.served == set()


async def test_get_track_uses_the_freshest_fragment() -> None:
    """
    A song must not resolve differently depending on session insertion order.

    The freshest fetch is Pandora's latest answer for the track, so it is what decides which
    copy resolves - not the order sessions happen to sit in the dict.
    """
    stale_tracks = _tracks()
    stale_tracks[0] = {**stale_tracks[0], "songTitle": "Stale Song 0"}
    provider = _provider(payloads=[stale_tracks, _tracks()])
    await provider.get_dynamic_radio_tracks("station-a")
    await provider.get_dynamic_radio_tracks("station-b")
    stale = provider._sessions["station-a"].current
    assert stale is not None
    stale.fetched_at -= 60
    track = await provider.get_track("TR:S0")
    assert track.name == "Song 0"


async def test_freshest_fragment_wins_regardless_of_session_order() -> None:
    """
    Freshest-fragment selection must not coincide only with insertion order.

    `test_a_fresher_session_serves_a_track_another_holds_expired`,
    `test_the_serving_session_is_the_one_marked_as_having_served`, and
    `test_get_track_uses_the_freshest_fragment` always make the first-inserted session,
    station-a, the degraded one, so a regression to any insertion-order rule would still pass
    them. Here station-a is inserted first but holds the freshest fragment - a station
    refetching after another was opened - while station-b, inserted second, is the one that
    has gone stale.
    """
    stale_tracks = _tracks()
    stale_tracks[0] = {**stale_tracks[0], "songTitle": "Stale Song 0"}
    provider = _provider(payloads=[_tracks(), stale_tracks])
    await provider.get_dynamic_radio_tracks("station-a")
    await provider.get_dynamic_radio_tracks("station-b")
    stale = provider._sessions["station-b"].current
    assert stale is not None
    stale.fetched_at -= 60
    track = await provider.get_track("TR:S0")
    assert track.name == "Song 0"


async def test_stream_details_rejects_other_media_types() -> None:
    """A station's tracks stream as tracks; the station id itself is not a stream."""
    provider = _provider()
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details("TR:S0", MediaType.RADIO)


async def test_unknown_track_id_is_refused() -> None:
    """An id no retained fragment holds cannot be streamed."""
    provider = _provider()
    await provider.get_dynamic_radio_tracks(STATION_ID)
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details("TR:not-a-real-track", MediaType.TRACK)


async def test_get_track_resolves_from_retained_fragments() -> None:
    """Queue history keeps working for tracks whose fragment is no longer live."""
    provider = _provider([_tracks(prefix="A"), _tracks(prefix="B")])
    await provider.get_dynamic_radio_tracks(STATION_ID)
    await provider.get_stream_details("TR:A3", MediaType.TRACK)
    await provider.get_dynamic_radio_tracks(STATION_ID)
    track = await provider.get_track("TR:A0")
    assert track.name == "Song 0"


async def test_get_track_unknown_raises() -> None:
    """An id from no retained fragment is genuinely gone."""
    provider = _provider()
    with pytest.raises(MediaNotFoundError):
        await provider.get_track("TR:nope")


class _LoginResponse:
    """Stand-in for the aiohttp POST `_authenticate` reads the login payload from."""

    status = 200

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def post(self, *args: Any, **kwargs: Any) -> Self:
        return self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def json(self) -> dict[str, Any]:
        return self._payload


async def _login(monkeypatch: pytest.MonkeyPatch, flags: list[str]) -> PandoraProvider:
    """Run a real _authenticate against a canned login payload carrying the given flags."""
    provider = _provider()
    provider.http_session = _LoginResponse(  # type: ignore[assignment]
        {"authToken": "token", "listenerId": "listener", "config": {"flags": flags}}
    )
    monkeypatch.setattr(provider_module, "get_csrf_token", AsyncMock(return_value="csrf"))
    await provider._authenticate("user", "secret")
    return provider


async def test_authentication_records_the_high_quality_entitlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The requested audio format hangs on this one assignment out of the login payload."""
    provider = await _login(monkeypatch, ["highQualityStreamingAvailable"])
    assert provider._high_quality_available is True


async def test_authentication_leaves_a_free_account_unentitled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A login without the flag must not leave it set from a previous account."""
    provider = await _login(monkeypatch, ["adSupportedSkip"])
    assert provider._high_quality_available is False
