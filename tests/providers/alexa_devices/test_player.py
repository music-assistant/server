"""Tests for the Alexa Devices player."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aioamazondevices.exceptions import CannotConnect, CannotRetrieveData
from aioamazondevices.structures import AmazonMediaControls, AmazonMediaState, AmazonVolumeState
from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.alexa_devices.companion import CompanionSkillApi
from music_assistant.providers.alexa_devices.constants import CONF_ALEXA_LANGUAGE
from music_assistant.providers.alexa_devices.player import AlexaDevicesPlayer
from tests.common import MockProvider, create_mock_config

from .conftest import SERIAL, FakeSkillApi, amazon_device

STREAM_URL = "http://ma/flow/1.mp3"
OPEN_SCREEN = '{"status": "ok", "pageLive": true}'
CLOSED_SCREEN = '{"status": "ok", "pageLive": false}'


@pytest.fixture
def language() -> str:
    """Return the configured Alexa language."""
    return "en-US"


@pytest.fixture
def player(skill_api: FakeSkillApi, language: str) -> Iterator[AlexaDevicesPlayer]:
    """Return a player whose provider has a mocked Amazon API and the fake skill API."""
    provider = cast("Any", MockProvider("alexa_devices", instance_id="alexa_devices--1"))
    provider.mass.config.get_base_player_config.return_value = create_mock_config("Echo")
    provider.mass.streams.resolve_stream_url = AsyncMock(return_value=STREAM_URL)
    provider.config = MagicMock()
    provider.config.get_value.side_effect = {CONF_ALEXA_LANGUAGE: language}.get
    provider.api = MagicMock()
    provider.api.send_media_command = AsyncMock()
    provider.api.set_device_volume = AsyncMock()
    provider.api.call_alexa_text_command = AsyncMock()
    provider.companion = CompanionSkillApi(
        skill_api.session, skill_api.url, None, None, logging.getLogger("test")
    )
    alexa_player = AlexaDevicesPlayer(provider, amazon_device())
    with patch.object(alexa_player, "update_state"):
        yield alexa_player


def _amazon_api(player: AlexaDevicesPlayer) -> MagicMock:
    return cast("MagicMock", player._alexa.api)


@pytest.mark.parametrize(
    ("language", "command"),
    [
        pytest.param("en-US", "ask music assistant to play audio", id="english"),
        pytest.param("it-IT", "chiedi a music assistant di riprodurre audio", id="italian"),
        pytest.param("xx-XX", "ask music assistant to play audio", id="unknown_language"),
    ],
)
async def test_play_media_pushes_url_and_opens_skill(
    player: AlexaDevicesPlayer, skill_api: FakeSkillApi, command: str
) -> None:
    """The stream url goes to the skill, then a voice command opens the skill."""
    await player.play_media(PlayerMedia(uri="x", title="Song", artist="Artist"))

    assert skill_api.requests == [
        (
            "/ma/push-url",
            {
                "streamUrl": STREAM_URL,
                "title": "Song",
                "artist": "Artist",
                "album": None,
                "imageUrl": None,
                "playerId": SERIAL,
                "canSkipSpeech": True,
            },
        )
    ]
    _amazon_api(player).call_alexa_text_command.assert_awaited_once_with(player.device, command)
    assert player.playback_state == PlaybackState.PLAYING


async def test_play_media_on_open_screen_skips_voice_command(
    player: AlexaDevicesPlayer, skill_api: FakeSkillApi
) -> None:
    """A skill whose screen is open takes the new track without a voice command."""
    skill_api.push_answer = OPEN_SCREEN

    await player.play_media(PlayerMedia(uri="x", title="Song"))

    _amazon_api(player).call_alexa_text_command.assert_not_awaited()


@pytest.mark.parametrize(
    ("method", "intent", "utterance"),
    [
        pytest.param("pause", "AMAZON.PauseIntent", "pause", id="pause"),
        pytest.param("play", "AMAZON.ResumeIntent", "resume", id="resume"),
    ],
)
async def test_pause_resume_without_open_screen_use_skill_intent(
    player: AlexaDevicesPlayer,
    skill_api: FakeSkillApi,
    method: str,
    intent: str,
    utterance: str,
) -> None:
    """Without an open screen, pause/resume are voice commands from the skill's intents."""
    skill_api.intents = {
        "invocationName": "music assistant",
        "intents": [{"intent": intent, "utterances": [utterance, "other"]}],
    }
    await player.play_media(PlayerMedia(uri="x", title="Song"))
    _amazon_api(player).call_alexa_text_command.reset_mock()

    await getattr(player, method)()

    assert "/ma/control" not in skill_api.paths
    _amazon_api(player).call_alexa_text_command.assert_awaited_once_with(
        player.device, f"music assistant {utterance}"
    )


@pytest.mark.parametrize(
    ("method", "command", "state"),
    [
        pytest.param("pause", "pause", PlaybackState.PAUSED, id="pause"),
        pytest.param("play", "resume", PlaybackState.PLAYING, id="resume"),
    ],
)
async def test_pause_resume_go_to_open_screen(
    player: AlexaDevicesPlayer,
    skill_api: FakeSkillApi,
    method: str,
    command: str,
    state: PlaybackState,
) -> None:
    """Once the skill keeps its screen open, pause/resume go to the screen."""
    skill_api.push_answer = OPEN_SCREEN
    await player.play_media(PlayerMedia(uri="x", title="Song"))

    await getattr(player, method)()

    assert skill_api.requests[-1] == (
        "/ma/control",
        {"playerId": SERIAL, "command": command, "canSkipSpeech": True},
    )
    _amazon_api(player).call_alexa_text_command.assert_not_awaited()
    assert player.playback_state == state


@pytest.mark.parametrize(
    ("control_answer", "control_status"),
    [
        pytest.param(CLOSED_SCREEN, 200, id="screen_closed"),
        pytest.param("down", 500, id="skill_api_error"),
    ],
)
async def test_pause_falls_back_to_voice_command(
    player: AlexaDevicesPlayer,
    skill_api: FakeSkillApi,
    control_answer: str,
    control_status: int,
) -> None:
    """A closed screen or failing skill API falls back to the voice command."""
    skill_api.push_answer = CLOSED_SCREEN
    skill_api.control_answer = control_answer
    skill_api.control_status = control_status
    await player.play_media(PlayerMedia(uri="x", title="Song"))
    _amazon_api(player).call_alexa_text_command.reset_mock()

    await player.pause()

    _amazon_api(player).call_alexa_text_command.assert_awaited_once_with(player.device, "pause")


async def test_stop_uses_device_stop(player: AlexaDevicesPlayer) -> None:
    """Stop is a native Alexa stop, no voice command needed."""
    await player.stop()

    _amazon_api(player).send_media_command.assert_awaited_once_with(
        player.device, AmazonMediaControls.Stop
    )
    assert player.playback_state == PlaybackState.IDLE


async def test_volume_set(player: AlexaDevicesPlayer) -> None:
    """The volume level is sent to the device as-is (0-100)."""
    await player.volume_set(42)

    _amazon_api(player).set_device_volume.assert_awaited_once_with(player.device, 42)
    assert player.volume_level == 42


@pytest.mark.parametrize("error", [CannotConnect("down"), CannotRetrieveData("bad")])
async def test_amazon_error_fails_command(player: AlexaDevicesPlayer, error: Exception) -> None:
    """Library errors surface as a failed player command."""
    _amazon_api(player).set_device_volume.side_effect = error

    with pytest.raises(PlayerCommandFailed):
        await player.volume_set(42)


@pytest.mark.parametrize(
    ("player_state", "expected"),
    [
        pytest.param("PLAYING", PlaybackState.PLAYING, id="playing"),
        pytest.param("PAUSED", PlaybackState.PAUSED, id="paused"),
        pytest.param("IDLE", PlaybackState.IDLE, id="idle"),
        pytest.param(None, PlaybackState.IDLE, id="no_session"),
    ],
)
def test_pushed_media_state(
    player: AlexaDevicesPlayer, player_state: str | None, expected: PlaybackState
) -> None:
    """Pushed media states set the playback state and elapsed time."""
    updated_at = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
    player.update_media(
        AmazonMediaState(
            player_state=player_state,
            now_playing_url=None,
            now_playing_title=None,
            now_playing_line1=None,
            now_playing_line2=None,
            next_enabled=False,
            previous_enabled=False,
            pause_enabled=True,
            seek_forward_enabled=False,
            seek_back_enabled=False,
            shuffle_enabled=False,
            repeat_enabled=False,
            media_length=None,
            media_position=30,
            media_position_updated_at=updated_at,
            media_provider=None,
            media_provider_url=None,
        )
    )

    assert player.playback_state == expected
    assert player._attr_elapsed_time == 30
    assert player._attr_elapsed_time_last_updated == updated_at.timestamp()


def test_pushed_volume_state(player: AlexaDevicesPlayer) -> None:
    """Pushed volume states set volume and mute."""
    player.update_volume(AmazonVolumeState(volume=35, is_muted=True))

    assert player.volume_level == 35
    assert player.volume_muted is True


@pytest.mark.parametrize(
    ("device", "available"),
    [
        pytest.param(amazon_device(online=True), True, id="online"),
        pytest.param(amazon_device(online=False), False, id="offline"),
        pytest.param(None, False, id="removed"),
    ],
)
def test_device_refresh_availability(
    player: AlexaDevicesPlayer, device: Any, available: bool
) -> None:
    """Refreshed device data sets the availability."""
    player.update_device(device)

    assert player.available is available
