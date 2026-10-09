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
