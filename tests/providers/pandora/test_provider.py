"""Tests for the Pandora provider's dynamic-playlist and streaming surface."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import Mock

import pytest
from music_assistant_models.enums import MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.pandora.fragments import PandoraStationSession
from music_assistant.providers.pandora.provider import PandoraProvider

STATION_ID = "4360491625318318161"


def _tracks(count: int = 4, prefix: str = "S") -> list[dict[str, Any]]:
    """Build `count` raw Pandora track dicts with distinct music ids."""
    return [
        {
            "musicId": f"{prefix}{index}",
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


def _provider(fragments_to_serve: list[list[dict[str, Any]]] | None = None) -> PandoraProvider:
    """Build a bare provider with a stubbed fragment fetch and no network or mass."""
    provider = PandoraProvider.__new__(PandoraProvider)
    provider.manifest = Mock(domain="pandora")
    provider.config = Mock(instance_id="pandora--test")
    provider.logger = Mock()
    provider._sessions = {}
    provider._high_quality_available = False
    pending = list(fragments_to_serve or [_tracks()])

    async def _fake_fetch(session: PandoraStationSession) -> Any:
        """Serve the next canned fragment instead of calling Pandora."""
        return session.add_fragment(pending.pop(0) if pending else _tracks(), time.time())

    provider._fetch_fragment = _fake_fetch  # type: ignore[method-assign]
    return provider


async def test_pages_beyond_the_first_terminate_the_loop() -> None:
    """The core pages a playlist until it returns nothing; a station serves one batch."""
    provider = _provider()
    assert await provider.get_playlist_tracks(STATION_ID, page=1) == []


async def test_first_request_returns_a_fragment() -> None:
    """A station with no session yet fetches and returns its first fragment."""
    provider = _provider()
    tracks = await provider.get_playlist_tracks(STATION_ID)
    assert [track.item_id for track in tracks] == [f"{STATION_ID}_S{i}" for i in range(4)]


async def test_browse_then_play_returns_the_same_batch() -> None:
    """A browse leaves the fragment unresolved, so play must still get its tracks."""
    provider = _provider()
    browsed = await provider.get_playlist_tracks(STATION_ID)
    played = await provider.get_playlist_tracks(STATION_ID)
    assert [track.item_id for track in played] == [track.item_id for track in browsed]


async def test_refill_withholds_while_the_fragment_is_live() -> None:
    """Once a track is streaming, a refill must not pull a fragment that kills its URL."""
    provider = _provider()
    await provider.get_playlist_tracks(STATION_ID)
    await provider.get_stream_details(f"{STATION_ID}_S0", MediaType.TRACK)
    assert await provider.get_playlist_tracks(STATION_ID) == []


async def test_refill_advances_once_the_last_track_is_resolved() -> None:
    """Resolving the final track opens the gate for the next fragment."""
    provider = _provider([_tracks(prefix="A"), _tracks(prefix="B")])
    await provider.get_playlist_tracks(STATION_ID)
    await provider.get_stream_details(f"{STATION_ID}_A3", MediaType.TRACK)
    tracks = await provider.get_playlist_tracks(STATION_ID)
    assert [track.item_id for track in tracks] == [f"{STATION_ID}_B{i}" for i in range(4)]


async def test_stream_details_point_at_the_pandora_url() -> None:
    """The provider streams by URL and never buffers audio itself."""
    provider = _provider()
    await provider.get_playlist_tracks(STATION_ID)
    details = await provider.get_stream_details(f"{STATION_ID}_S1", MediaType.TRACK)
    assert details.stream_type is StreamType.HTTP
    assert details.path == "https://audio-sv5-t3-2.pandora.com/access/1.mp4"
    assert details.duration == 180
    assert details.can_seek is True
    assert details.allow_seek is True


async def test_stream_details_for_an_evicted_track_raises() -> None:
    """A track outside the live fragment has a dead URL; fail loudly instead of serving it."""
    provider = _provider([_tracks(prefix="A"), _tracks(prefix="B")])
    await provider.get_playlist_tracks(STATION_ID)
    await provider.get_stream_details(f"{STATION_ID}_A3", MediaType.TRACK)
    await provider.get_playlist_tracks(STATION_ID)
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details(f"{STATION_ID}_A0", MediaType.TRACK)


async def test_stream_details_rejects_other_media_types() -> None:
    """Stations expose tracks only; radio is gone."""
    provider = _provider()
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details(f"{STATION_ID}_S0", MediaType.RADIO)


async def test_stream_details_rejects_a_malformed_id() -> None:
    """An id without the station prefix (e.g. a legacy radio id) must not raise ValueError."""
    provider = _provider()
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details("12345", MediaType.TRACK)


async def test_get_track_resolves_from_retained_fragments() -> None:
    """Queue history keeps working for tracks whose fragment is no longer live."""
    provider = _provider([_tracks(prefix="A"), _tracks(prefix="B")])
    await provider.get_playlist_tracks(STATION_ID)
    await provider.get_stream_details(f"{STATION_ID}_A3", MediaType.TRACK)
    await provider.get_playlist_tracks(STATION_ID)
    track = await provider.get_track(f"{STATION_ID}_A0")
    assert track.name == "Song 0"


async def test_get_track_unknown_raises() -> None:
    """An id from no retained fragment is genuinely gone."""
    provider = _provider()
    with pytest.raises(MediaNotFoundError):
        await provider.get_track(f"{STATION_ID}_nope")
