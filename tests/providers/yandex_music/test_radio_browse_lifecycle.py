"""Radio browse can restart an ended station without continuing its old session."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.enums import ProviderFeature
from music_assistant_models.media_items import BrowseFolder
from ya_passport_auth import SecretStr
from yandex_music import ClientAsync, Track

from music_assistant.providers.yandex_music.api_client import YandexMusicClient
from music_assistant.providers.yandex_music.browse import _BrowseRouter
from music_assistant.providers.yandex_music.constants import CONF_WAVE_PRESETS_DATA
from music_assistant.providers.yandex_music.provider import YandexMusicProvider, _WaveState

from .conftest import use_real_create_task


@pytest.fixture
def radio_provider() -> tuple[YandexMusicProvider, ClientAsync]:
    """Use real routing, session state and library parsing with an offline transport."""
    raw = ClientAsync()
    client = YandexMusicClient(SecretStr("fake_token"))
    client._client = raw
    provider = YandexMusicProvider.__new__(YandexMusicProvider)
    provider._client = client
    provider.logger = logging.getLogger(__name__)
    provider.mass = Mock()
    provider.mass.metadata.locale = "en_US"
    provider.manifest = Mock(domain="yandex_music")
    provider.config = Mock(instance_id="yandex_music_instance")
    provider.config.get_value.side_effect = lambda key: (
        '[{"name":"Russian","language":"russian"}]' if key == CONF_WAVE_PRESETS_DATA else None
    )
    provider._supported_features = {ProviderFeature.BROWSE}
    provider._wave_states = {}
    provider._browse_router = _BrowseRouter(provider)
    return provider, raw


@pytest.mark.parametrize(
    ("suffix", "station"),
    [
        ("my_wave", "user:onyourwave"),
        ("my_wave_modes/discover", "user:onyourwave#discover"),
        ("my_wave_presets/0", "user:onyourwave#preset_0"),
        ("waves/genre/rock", "genre:rock"),
    ],
)
async def test_fresh_browse_restarts_ended_station(
    radio_provider: tuple[YandexMusicProvider, ClientAsync], suffix: str, station: str
) -> None:
    """Load more stops after termination, but opening the station again starts it fresh."""
    provider, raw = radio_provider
    wave = _WaveState()
    wave.session_id = "old"
    wave.last_track_id = "42"
    wave.batch_id = "old-batch"
    wave.playlist_next_cursor = "42"
    wave.radio_started_sent = True
    wave.seen_track_ids.add("42")
    wave.settings = {"language": "russian"}
    provider._wave_states[station] = wave
    path = f"{provider.instance_id}://{suffix}"
    responses = iter(
        [
            {
                "terminated": True,
                "batchId": "final",
                "sequence": [{"type": "track", "track": {"id": 43}, "liked": False}],
            },
            [{"id": 43}],
            {
                "radioSessionId": "fresh",
                "batchId": "fresh-batch",
                "sequence": [{"type": "track", "track": {"id": 43}, "liked": False}],
            },
            [{"id": 43}],
            {"terminated": True},
        ]
    )

    async def respond(url: str, *_args: object, **_kwargs: object) -> object:
        if "feedback" in url:
            return {}
        return next(responses)

    post = AsyncMock(side_effect=respond)
    with (
        patch.object(raw.request, "post", post),
        patch.object(raw.request, "get", AsyncMock(return_value=[])),
    ):
        assert any(item.item_id.startswith("43@") for item in await provider.browse(f"{path}/next"))
        wave.prefetched = [Track(id=44)]
        await provider.browse(path)
    creations = [call for call in post.await_args_list if "/rotor/session/new" in call.args[0]]
    assert len(creations) == 1
    assert "settingLanguage:russian" in creations[0].kwargs["json"]["seeds"]
    wave = provider._wave_states[station]
    assert wave.session_id == "fresh"
    assert wave.batch_id == "fresh-batch"
    assert not wave.prefetched
    assert wave.playlist_next_cursor is None
    assert wave.seen_track_ids == {"43"}
    assert wave.radio_started_sent
    feedback = [call for call in post.await_args_list if "feedback" in call.args[0]]
    assert len(feedback) == 1
    assert "fresh" in feedback[0].args[0]


@pytest.mark.parametrize(
    ("suffix", "station"),
    [
        ("my_wave/next", "user:onyourwave"),
        ("my_wave_modes/discover/next", "user:onyourwave#discover"),
        ("my_wave_presets/0/next", "user:onyourwave#preset_0"),
        ("waves/genre/rock/next", "genre:rock"),
    ],
)
@pytest.mark.parametrize("ended", [False, True])
async def test_browse_pagination_follows_session_completion(
    radio_provider: tuple[YandexMusicProvider, ClientAsync], suffix: str, station: str, ended: bool
) -> None:
    """Tracks stay playable, and only active sessions offer a pagination folder."""
    provider, raw = radio_provider
    wave = _WaveState()
    wave.session_id = "old"
    wave.last_track_id = "42"
    wave.radio_started_sent = True
    provider._wave_states[station] = wave
    post = AsyncMock(
        side_effect=[
            {
                "terminated": ended,
                "batchId": "final",
                "sequence": [{"type": "track", "track": {"id": 43}, "liked": False}],
            },
            [{"id": 43}],
        ]
    )
    with (
        patch.object(raw.request, "post", post),
        patch.object(raw.request, "get", AsyncMock(return_value=[])),
    ):
        items = await provider.browse(f"{provider.instance_id}://{suffix}")
    assert [item.item_id for item in items if not isinstance(item, BrowseFolder)] == [
        f"43@{station}"
    ]
    assert any(isinstance(item, BrowseFolder) for item in items) is not ended
    assert wave.batch_id == "final"


@pytest.mark.parametrize("cached_empty_page", [False, True])
async def test_my_wave_playlist_restarts_on_page_zero_only(
    radio_provider: tuple[YandexMusicProvider, ClientAsync], cached_empty_page: bool
) -> None:
    """Fresh playback restarts completed sessions while later pages remain terminal."""
    provider, raw = radio_provider
    provider.mass.cache = Mock(
        get_with_freshness=AsyncMock(
            return_value=([], True, True) if cached_empty_page else (None, False, False)
        ),
        set=AsyncMock(),
    )
    use_real_create_task(provider.mass)
    wave = _WaveState()
    wave.session_id = "old"
    wave.last_track_id = "42"
    wave.radio_started_sent = True
    wave.settings = {"language": "russian"}
    provider._wave_states["user:onyourwave"] = wave
    responses = iter(
        [
            {"terminated": True},
            {
                "radioSessionId": "fresh",
                "batchId": "fresh-batch",
                "sequence": [{"type": "track", "track": {"id": 43}, "liked": False}],
            },
            [{"id": 43}],
            {"terminated": True},
            {
                "radioSessionId": "fresh-again",
                "batchId": "next-batch",
                "sequence": [{"type": "track", "track": {"id": 44}, "liked": False}],
            },
            [{"id": 44}],
            {"terminated": True},
        ]
    )

    async def respond(url: str, *_args: object, **_kwargs: object) -> object:
        if "feedback" in url:
            return {}
        return next(responses)

    post = AsyncMock(side_effect=respond)
    with patch.object(raw.request, "post", post):
        assert await provider.get_rotor_station_tracks("user:onyourwave") == ([], None)
        tracks = await provider.get_playlist_tracks("my_wave", page=0)
        assert [track.item_id for track in tracks] == ["43@user:onyourwave"]
        assert wave.session_id == "fresh"
        after_completion = post.await_count
        assert await provider.get_playlist_tracks("my_wave", page=1) == []
        assert await provider.get_playlist_tracks("my_wave", page=2) == []
        assert post.await_count == after_completion
        tracks = await provider.get_playlist_tracks("my_wave", page=0)
        assert [track.item_id for track in tracks] == ["44@user:onyourwave"]
    creations = [call for call in post.await_args_list if "/rotor/session/new" in call.args[0]]
    assert len(creations) == 2
    assert all("settingLanguage:russian" in call.kwargs["json"]["seeds"] for call in creations)
    feedback = [call for call in post.await_args_list if "feedback" in call.args[0]]
    assert len(feedback) == 2
    assert "fresh/feedback" in feedback[0].args[0]
    assert "fresh-again/feedback" in feedback[1].args[0]


async def test_my_wave_playlist_later_page_continues_active_session(
    radio_provider: tuple[YandexMusicProvider, ClientAsync],
) -> None:
    """Playlist pagination keeps its session and delivers the final batch once."""
    provider, raw = radio_provider
    use_real_create_task(provider.mass)
    wave = _WaveState()
    wave.session_id = "active"
    wave.last_track_id = "42"
    wave.playlist_next_cursor = "42"
    wave.seen_track_ids.add("42")
    wave.radio_started_sent = True
    provider._wave_states["user:onyourwave"] = wave
    post = AsyncMock(
        side_effect=[
            {
                "terminated": True,
                "batchId": "final",
                "sequence": [{"type": "track", "track": {"id": 43}, "liked": False}],
            },
            [{"id": 43}],
        ]
    )
    with patch.object(raw.request, "post", post):
        tracks = await provider.get_playlist_tracks("my_wave", page=1)
        assert [track.item_id for track in tracks] == ["43@user:onyourwave"]
        assert await provider.get_playlist_tracks("my_wave", page=2) == []
    assert post.await_count == 2
    assert "/rotor/session/active/tracks" in post.await_args_list[0].args[0]
    assert wave.session_id == "active"
    assert wave.batch_id == "final"
