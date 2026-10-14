"""Tests for sending playback commands to the skill's open screen instead of as voice commands."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from music_assistant_models.errors import ActionUnavailable
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.alexa import AlexaPlayer
from tests.common import MockProvider, create_mock_config

PLAYER_ID = "Kitchen Echo"
OLD_SKILL = '{"status": "ok", "version": 3}'


def _answer(*, screen_open: bool) -> str:
    return f'{{"status": "ok", "pageLive": {"true" if screen_open else "false"}}}'


@pytest.fixture
def alexa() -> Iterator[tuple[AlexaPlayer, MagicMock, AsyncMock]]:
    """Return a real AlexaPlayer, its Alexa API mock and a mock for the skill's API."""
    provider = cast("Any", MockProvider("alexa", instance_id="alexa--1"))
    provider.mass.config.get_base_player_config.return_value = create_mock_config(PLAYER_ID)
    provider.mass.streams.resolve_stream_url = AsyncMock(return_value="http://ma/flow/1.mp3")
    provider.config = MagicMock()
    provider.config.get_value.return_value = "en-US"
    provider.get_intent_utterance = AsyncMock(side_effect=lambda _intent, utter: utter)
    player = AlexaPlayer(provider, PLAYER_ID, MagicMock())
    alexa_api = MagicMock()
    alexa_api.run_custom = AsyncMock()
    skill_api = AsyncMock()
    with (
        patch.object(player, "update_state"),
        patch.object(AlexaPlayer, "api", new_callable=PropertyMock, return_value=alexa_api),
        patch("music_assistant.providers.alexa.api_request", skill_api),
    ):
        yield player, alexa_api, skill_api


def _endpoints(skill_api: AsyncMock) -> list[str]:
    return [call.args[1] for call in skill_api.await_args_list]


def _last_payload(skill_api: AsyncMock) -> dict[str, Any]:
    assert skill_api.await_args is not None
    return cast("dict[str, Any]", skill_api.await_args.kwargs["json_data"])


async def test_old_skill_gets_voice_commands_as_before(
    alexa: tuple[AlexaPlayer, MagicMock, AsyncMock],
) -> None:
    """A skill that never answers pageLive gets voice commands, and no /ma/control requests."""
    player, alexa_api, skill_api = alexa
    skill_api.return_value = OLD_SKILL
    await player.play_media(PlayerMedia(uri="x", title="t"))
    await player.pause()
    await player.play()
    assert _endpoints(skill_api) == ["/ma/push-url"]
    assert alexa_api.run_custom.await_count == 3


async def test_new_track_goes_to_the_open_screen(
    alexa: tuple[AlexaPlayer, MagicMock, AsyncMock],
) -> None:
    """The push tells the skill it may skip the voice command; an open screen means none is sent."""
    player, alexa_api, skill_api = alexa
    skill_api.return_value = _answer(screen_open=True)
    await player.play_media(PlayerMedia(uri="x", title="t"))
    payload = _last_payload(skill_api)
    assert payload["canSkipSpeech"] is True
    assert payload["playerId"] == PLAYER_ID
    assert payload["streamUrl"] == "http://ma/flow/1.mp3"
    alexa_api.run_custom.assert_not_awaited()


async def test_new_track_without_open_screen_is_spoken(
    alexa: tuple[AlexaPlayer, MagicMock, AsyncMock],
) -> None:
    """Without an open screen, the new track still gets the voice command."""
    player, alexa_api, skill_api = alexa
    skill_api.return_value = _answer(screen_open=False)
    await player.play_media(PlayerMedia(uri="x", title="t"))
    alexa_api.run_custom.assert_awaited_once()


@pytest.mark.parametrize(("command", "method"), [("pause", "pause"), ("resume", "play")])
async def test_pause_resume_go_to_the_open_screen(
    alexa: tuple[AlexaPlayer, MagicMock, AsyncMock], command: str, method: str
) -> None:
    """Once the skill has shown it keeps its screen open, pause/resume go to the screen."""
    player, alexa_api, skill_api = alexa
    skill_api.return_value = _answer(screen_open=True)
    await player.play_media(PlayerMedia(uri="x", title="t"))
    await getattr(player, method)()
    assert _endpoints(skill_api) == ["/ma/push-url", "/ma/control"]
    assert _last_payload(skill_api) == {
        "playerId": PLAYER_ID,
        "command": command,
        "canSkipSpeech": True,
    }
    alexa_api.run_custom.assert_not_awaited()


async def test_pause_after_screen_closed_is_spoken(
    alexa: tuple[AlexaPlayer, MagicMock, AsyncMock],
) -> None:
    """If the screen has closed since the last track, the pause gets the voice command."""
    player, alexa_api, skill_api = alexa
    skill_api.side_effect = [_answer(screen_open=True), _answer(screen_open=False)]
    await player.play_media(PlayerMedia(uri="x", title="t"))
    await player.pause()
    alexa_api.run_custom.assert_awaited_once_with("pause")


async def test_pause_during_new_track_reaches_the_skill_after_it(
    alexa: tuple[AlexaPlayer, MagicMock, AsyncMock],
) -> None:
    """A pause pressed while a new track is still being sent goes to the skill after the track."""
    player, alexa_api, skill_api = alexa
    skill_api.return_value = _answer(screen_open=True)
    await player.play_media(PlayerMedia(uri="x", title="t"))
    track_sent = asyncio.Event()

    async def skill(_provider: Any, endpoint: str, **_kwargs: Any) -> str:
        if endpoint == "/ma/push-url":
            await track_sent.wait()
        return _answer(screen_open=True)

    skill_api.side_effect = skill
    next_track = asyncio.create_task(player.play_media(PlayerMedia(uri="y", title="u")))
    pause = asyncio.create_task(player.pause())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert _endpoints(skill_api) == ["/ma/push-url", "/ma/push-url"]
    track_sent.set()
    await asyncio.gather(next_track, pause)
    assert _endpoints(skill_api) == ["/ma/push-url", "/ma/push-url", "/ma/control"]
    alexa_api.run_custom.assert_not_awaited()


async def test_failed_control_request_is_spoken(
    alexa: tuple[AlexaPlayer, MagicMock, AsyncMock],
) -> None:
    """A failing /ma/control request falls back to the voice command."""
    player, alexa_api, skill_api = alexa
    skill_api.side_effect = [_answer(screen_open=True), ActionUnavailable("down")]
    await player.play_media(PlayerMedia(uri="x", title="t"))
    await player.play()
    alexa_api.run_custom.assert_awaited_once_with("resume")


async def test_failed_control_request_stops_further_tries(
    alexa: tuple[AlexaPlayer, MagicMock, AsyncMock],
) -> None:
    """After a failed /ma/control, pause/resume skip it until the next track confirms the screen."""
    player, alexa_api, skill_api = alexa
    skill_api.side_effect = [
        _answer(screen_open=True),
        ActionUnavailable("down"),
        _answer(screen_open=True),
        _answer(screen_open=True),
    ]
    await player.play_media(PlayerMedia(uri="x", title="t"))
    await player.pause()
    await player.play()
    assert _endpoints(skill_api) == ["/ma/push-url", "/ma/control"]
    await player.play_media(PlayerMedia(uri="x", title="t"))
    await player.pause()
    assert _endpoints(skill_api) == ["/ma/push-url", "/ma/control", "/ma/push-url", "/ma/control"]
    assert alexa_api.run_custom.await_count == 2


async def test_non_json_answer_counts_as_old_skill(
    alexa: tuple[AlexaPlayer, MagicMock, AsyncMock],
) -> None:
    """An answer that isn't a JSON object counts as an old skill: voice commands only."""
    player, alexa_api, skill_api = alexa
    skill_api.return_value = "OK"
    await player.play_media(PlayerMedia(uri="x", title="t"))
    await player.pause()
    assert _endpoints(skill_api) == ["/ma/push-url"]
    assert alexa_api.run_custom.await_count == 2
