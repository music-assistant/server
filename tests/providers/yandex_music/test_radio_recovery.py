"""Public station pagination recovers expired sessions with the real library."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.enums import MediaType
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


async def test_expired_prefetch_recovers_station_and_keeps_playback_feedback() -> None:
    """Recovery keeps the station's settings, track identity and subsequent prefetch alive."""
    provider, raw, wave = make_station_provider()
    provider.config = Mock(instance_id="yandex_music_instance")
    provider.manifest = Mock(domain="yandex_music")
    mass = Mock()
    provider.mass = mass
    tasks: list[asyncio.Task[None]] = []

    def schedule(coro: Any) -> asyncio.Task[None]:
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    mass.create_task.side_effect = schedule
    responses = iter(
        [
            {"unknownSession": True},
            {
                "radioSessionId": "fresh",
                "batchId": "fresh-batch",
                "sequence": [{"type": "track", "track": {"id": 43}, "liked": False}],
            },
            [{"id": 43}],
            {
                "batchId": "next-batch",
                "sequence": [{"type": "track", "track": {"id": 44}, "liked": False}],
            },
            [{"id": 44}],
        ]
    )

    async def respond(url: str, *_args: object, **_kwargs: object) -> object:
        if "feedback" in url:
            return {"result": "ok"}
        return next(responses)

    post = AsyncMock(side_effect=respond)
    with patch.object(raw.request, "post", post):
        await provider._prefetch_rotor_session("user:onyourwave#discover")
        assert wave.session_id == "fresh"
        assert wave.settings == {"language": "russian"}
        assert wave.radio_started_sent
        tracks = await provider.get_similar_tracks("42@user:onyourwave#discover", limit=1)
        assert [track.item_id for track in tracks] == ["43@user:onyourwave#discover"]
        assert {pm.item_id for pm in tracks[0].provider_mappings} == {tracks[0].item_id}
        assert wave.last_track_id == "43"
        await provider.on_played(MediaType.TRACK, tracks[0].item_id, False, 0, tracks[0], True)
        await asyncio.gather(*tasks)
        assert [track.id for track in wave.prefetched] == [44]
    creations = [c for c in post.await_args_list if "/rotor/session/new" in c.args[0]]
    assert len(creations) == 1
    assert creations[0].kwargs["json"]["seeds"] == [
        "user:onyourwave",
        "settingDiversity:discover",
        "settingLanguage:russian",
    ]
    feedback = [c for c in post.await_args_list if "feedback" in c.args[0]]
    assert [c.kwargs["json"]["event"]["type"] for c in feedback] == [
        "radioStarted",
        "trackStarted",
    ]
    assert any(
        "/session/fresh/feedback" in c.args[0]
        and c.kwargs["json"]["event"]["type"] == "trackStarted"
        for c in feedback
    )
    assert not any("track:42" in c.kwargs.get("json", {}).get("seeds", []) for c in creations)


@pytest.mark.parametrize("station", ["user:onyourwave", "user:onyourwave#discover", "genre:rock"])
async def test_prefetched_tracks_keep_station_mapping_and_cursor(station: str) -> None:
    """Drained tracks continue station playback instead of becoming stateless recommendations."""
    provider, _, wave = make_station_provider()
    provider.config = Mock(instance_id="yandex_music_instance")
    provider.manifest = Mock(domain="yandex_music")
    provider._wave_states = {station: wave}
    wave.prefetched = [Track(id=43), Track(id=44)]
    tracks = await provider.get_similar_tracks(f"42@{station}", limit=1)
    assert [track.item_id for track in tracks] == [f"43@{station}"]
    assert {pm.item_id for pm in tracks[0].provider_mappings} == {f"43@{station}"}
    assert wave.last_track_id == "43"
    assert [track.id for track in wave.prefetched] == [44]


@pytest.mark.parametrize("replacement", ["state", "session", "ended"])
async def test_expired_prefetch_cannot_reset_changed_station(replacement: str) -> None:
    """An expired response must not replace newer state or restart an ended station."""
    provider, raw, wave = make_station_provider()

    async def respond(*_args: object, **_kwargs: object) -> object:
        async with wave.lock:
            if replacement == "state":
                new_wave = _WaveState()
                new_wave.session_id = "replacement"
                provider._wave_states["user:onyourwave#discover"] = new_wave
            elif replacement == "session":
                wave.session_id = "replacement"
            else:
                wave.ended = True
        return {"unknownSession": True}

    post = AsyncMock(side_effect=respond)
    with patch.object(raw.request, "post", post):
        await provider._prefetch_rotor_session("user:onyourwave#discover")
    assert post.await_count == 1
    assert wave.batch_id == "old-batch"
    assert wave.last_track_id == "42"
    assert provider._wave_states["user:onyourwave#discover"].session_id == (
        "expired" if replacement == "ended" else "replacement"
    )


async def test_expired_prefetch_recovery_attempt_is_bounded() -> None:
    """Failed replacement creation clears expired state without looping."""
    provider, raw, wave = make_station_provider()
    post = AsyncMock(side_effect=[{"unknownSession": True}, None])
    with patch.object(raw.request, "post", post):
        await provider._prefetch_rotor_session("user:onyourwave#discover")
    assert post.await_count == 2
    assert wave.session_id is None
    assert wave.batch_id is None
    assert wave.settings == {"language": "russian"}


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
