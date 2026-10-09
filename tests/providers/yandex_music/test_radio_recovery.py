"""Public station pagination recovers expired sessions with the real library."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from ya_passport_auth import SecretStr
from yandex_music import ClientAsync, Track

from music_assistant.providers.yandex_music.api_client import YandexMusicClient
from music_assistant.providers.yandex_music.provider import YandexMusicProvider, _WaveState


def make_station_provider() -> tuple[YandexMusicProvider, ClientAsync, _WaveState]:
    """Build a station with real provider state and a disconnected library client."""
    raw = ClientAsync()
    client = YandexMusicClient(SecretStr("fake_token"))
    client._client = raw
    provider = YandexMusicProvider.__new__(YandexMusicProvider)
    provider._client = client
    provider.logger = logging.getLogger(__name__)
    wave = _WaveState()
    wave.session_id = "expired"
    wave.last_track_id = "42"
    wave.batch_id = "old-batch"
    wave.radio_started_sent = True
    wave.seen_track_ids.add("42")
    wave.settings = {"language": "russian"}
    provider._wave_states = {"user:onyourwave#discover": wave}
    return provider, raw, wave


async def test_station_recovers_unknown_session_once_and_preserves_settings() -> None:
    """An expired station can supply a new batch without reusing old session state."""
    provider, raw, wave = make_station_provider()
    post = AsyncMock(
        side_effect=[
            {"unknownSession": True},
            {"radioSessionId": "fresh", "batchId": "fresh-batch", "sequence": []},
        ]
    )
    with patch.object(raw.request, "post", post):
        assert await provider.get_rotor_station_tracks("user:onyourwave#discover", queue="42") == (
            [],
            "fresh-batch",
        )
    assert wave.session_id == "fresh"
    assert wave.batch_id == "fresh-batch"
    assert not wave.radio_started_sent
    assert not wave.seen_track_ids
    assert post.await_count == 2
    assert post.await_args is not None
    body = post.await_args.kwargs["json"]
    assert body["seeds"] == [
        "user:onyourwave",
        "settingDiversity:discover",
        "settingLanguage:russian",
    ]


async def test_station_delivers_final_tracks_from_terminated_session() -> None:
    """A termination marker must not discard playable tracks in the final batch."""
    provider, raw, _ = make_station_provider()
    post = AsyncMock(
        side_effect=[
            {
                "terminated": True,
                "batchId": "final-batch",
                "sequence": [
                    {"type": "track", "track": {"id": 43}, "liked": False},
                ],
            },
            [{"id": 43}],
        ]
    )
    with patch.object(raw.request, "post", post):
        tracks, batch = await provider.get_rotor_station_tracks("user:onyourwave#discover")
    assert [track.id for track in tracks] == [43]
    assert batch == "final-batch"


async def test_terminated_station_does_not_restart_automatically() -> None:
    """An explicitly ended station stays ended instead of generating a new session."""
    provider, raw, wave = make_station_provider()
    post = AsyncMock(return_value={"terminated": True})
    with patch.object(raw.request, "post", post):
        assert await provider.get_rotor_station_tracks("user:onyourwave#discover") == ([], None)
    assert wave.session_id == "expired"
    assert post.await_count == 1


async def test_unknown_station_recovery_stops_after_failed_creation() -> None:
    """Recovery performs one bounded creation attempt when the server cannot recover."""
    provider, raw, wave = make_station_provider()
    post = AsyncMock(side_effect=[{"unknownSession": True}, None])
    with patch.object(raw.request, "post", post):
        assert await provider.get_rotor_station_tracks("user:onyourwave#discover") == ([], None)
    assert wave.session_id is None
    assert wave.batch_id is None
    assert post.await_count == 2


async def test_final_station_batch_stops_pagination_before_unknown_session() -> None:
    """Deliver the final tracks once and never ask an ended session for another page."""
    provider, raw, _ = make_station_provider()
    post = AsyncMock(
        side_effect=[
            {
                "terminated": True,
                "batchId": "final-batch",
                "sequence": [{"type": "track", "track": {"id": 43}, "liked": False}],
            },
            [{"id": 43}],
            {"unknownSession": True},
            None,
        ]
    )
    with patch.object(raw.request, "post", post):
        tracks, batch = await provider.get_rotor_station_tracks("user:onyourwave#discover")
        assert [track.id for track in tracks] == [43]
        assert batch == "final-batch"
        assert await provider.get_rotor_station_tracks("user:onyourwave#discover", queue="43") == (
            [],
            None,
        )
    assert post.await_count == 2


async def test_empty_terminal_batch_stops_later_station_requests() -> None:
    """An ended session without tracks stays ended on later calls too."""
    provider, raw, _ = make_station_provider()
    post = AsyncMock(return_value={"terminated": True})
    with patch.object(raw.request, "post", post):
        assert await provider.get_rotor_station_tracks("user:onyourwave#discover") == ([], None)
        assert await provider.get_rotor_station_tracks("user:onyourwave#discover") == ([], None)
    assert post.await_count == 1


@pytest.mark.parametrize("with_tracks", [False, True])
async def test_terminal_prefetch_keeps_final_tracks_and_stops_requests(with_tracks: bool) -> None:
    """A background terminal batch stops pagination without dropping its buffered tracks."""
    provider, raw, wave = make_station_provider()
    sequence = [{"type": "track", "track": {"id": 43}, "liked": False}] if with_tracks else []
    responses: list[Any] = [{"terminated": True, "batchId": "final-batch", "sequence": sequence}]
    if with_tracks:
        responses.append([{"id": 43}])
    post = AsyncMock(side_effect=responses)
    with patch.object(raw.request, "post", post):
        await provider._prefetch_rotor_session("user:onyourwave#discover")
        assert [track.id for track in wave.prefetched] == ([43] if with_tracks else [])
        await provider._prefetch_rotor_session("user:onyourwave#discover")
        assert [track.id for track in wave.prefetched] == ([43] if with_tracks else [])
    assert post.await_count == (2 if with_tracks else 1)


async def test_terminal_prefetch_preserves_tracks_buffered_while_waiting() -> None:
    """Concurrent buffering must not discard the final batch or its termination marker."""
    provider, raw, wave = make_station_provider()

    async def post_response(url: str, *_args: object, **_kwargs: object) -> object:
        if "/rotor/session/" in url:
            return {
                "terminated": True,
                "batchId": "final-batch",
                "sequence": [{"type": "track", "track": {"id": 43}, "liked": False}],
            }
        wave.prefetched = [Track(id=44)]
        return [{"id": 43}]

    post = AsyncMock(side_effect=post_response)
    with patch.object(raw.request, "post", post):
        await provider._prefetch_rotor_session("user:onyourwave#discover")
        assert [track.id for track in wave.prefetched] == [44, 43]
        tracks, batch = await provider.get_rotor_station_tracks("user:onyourwave#discover")
        assert [track.id for track in tracks] == [44, 43]
        assert batch == "final-batch"
        assert await provider.get_rotor_station_tracks("user:onyourwave#discover") == ([], None)
    assert post.await_count == 2


async def test_terminal_prefetch_does_not_end_replacement_wave() -> None:
    """A late terminal response cannot affect a replacement station state."""
    provider, raw, old_wave = make_station_provider()
    new_wave = _WaveState()
    new_wave.session_id = "replacement"

    async def post_response(*_args: object, **_kwargs: object) -> object:
        provider._wave_states["user:onyourwave#discover"] = new_wave
        return {"terminated": True}

    with patch.object(raw.request, "post", AsyncMock(side_effect=post_response)):
        await provider._prefetch_rotor_session("user:onyourwave#discover")
    assert not new_wave.ended
    assert not old_wave.ended


async def test_public_station_pagination_delivers_final_prefetched_batch_once() -> None:
    """The public radio API drains final buffered tracks before reporting completion."""
    provider, raw, _ = make_station_provider()
    post = AsyncMock(
        side_effect=[
            {
                "terminated": True,
                "batchId": "final-batch",
                "sequence": [{"type": "track", "track": {"id": 43}, "liked": False}],
            },
            [{"id": 43}],
        ]
    )
    with patch.object(raw.request, "post", post):
        await provider._prefetch_rotor_session("user:onyourwave#discover")
        tracks, batch = await provider.get_rotor_station_tracks("user:onyourwave#discover")
        assert [track.id for track in tracks] == [43]
        assert batch == "final-batch"
        assert await provider.get_rotor_station_tracks("user:onyourwave#discover") == ([], None)
    assert post.await_count == 2
