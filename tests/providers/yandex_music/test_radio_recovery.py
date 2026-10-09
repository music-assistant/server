"""Public station pagination recovers expired sessions with the real library."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

from ya_passport_auth import SecretStr
from yandex_music import ClientAsync

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
