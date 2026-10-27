"""Tests for the API commands that make a Station speak or handle a text request."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.auth import Scope
from music_assistant_models.errors import (
    InsufficientPermissions,
    InvalidDataError,
    PlayerCommandFailed,
    PlayerUnavailableError,
)

from music_assistant.providers.yandex_station.constants import CONF_ALICE_TEXT_COMMANDS
from music_assistant.providers.yandex_station.glagol import YandexGlagol
from music_assistant.providers.yandex_station.player import YandexStationPlayer
from music_assistant.providers.yandex_station.provider import YandexStationProvider


def _make_player(send_result: dict[str, Any] | None = None) -> YandexStationPlayer:
    """Build a bare player whose Glagol client records the sent payloads."""
    player = YandexStationPlayer.__new__(YandexStationPlayer)
    player._player_id = "station_1"
    glagol = YandexGlagol.__new__(YandexGlagol)
    object.__setattr__(glagol, "send", AsyncMock(return_value=send_result or {"status": "SUCCESS"}))
    player.glagol = glagol
    return player


def _make_provider(
    players: dict[str, Any] | None = None, *, text_commands: bool = False
) -> YandexStationProvider:
    """Build a bare provider with a mocked mass that records API registrations."""
    provider = YandexStationProvider.__new__(YandexStationProvider)
    provider.config = MagicMock()
    provider.config.get_value = MagicMock(
        side_effect=lambda key, *_: text_commands if key == CONF_ALICE_TEXT_COMMANDS else None
    )
    mass = MagicMock()
    mass.register_api_command = MagicMock(side_effect=lambda *_a, **_kw: MagicMock())
    mass.players.get_player = MagicMock(side_effect=lambda pid: (players or {}).get(pid))
    provider.mass = mass
    provider._unregister_api_commands = []
    return provider


def _send_mock(player: YandexStationPlayer) -> AsyncMock:
    return cast("AsyncMock", player.glagol.send)


def _sent(player: YandexStationPlayer) -> list[dict[str, Any]]:
    return [call.args[0] for call in _send_mock(player).await_args_list]


async def test_loaded_in_mass_registers_only_say_by_default() -> None:
    """Text requests to Alice stay unregistered until the provider opts in."""
    provider = _make_provider()

    await provider.loaded_in_mass()

    register = cast("MagicMock", provider.mass.register_api_command)
    assert [call.args[0] for call in register.call_args_list] == ["yandex_station/say"]


async def test_loaded_in_mass_registers_alice_commands() -> None:
    """With the opt-in on, both commands are registered with the players.control scope."""
    provider = _make_provider(text_commands=True)

    await provider.loaded_in_mass()

    register = cast("MagicMock", provider.mass.register_api_command)
    registered = {call.args[0]: call.kwargs for call in register.call_args_list}
    assert set(registered) == {"yandex_station/say", "yandex_station/command"}
    assert all(kw["required_scope"] == Scope.PLAYERS_CONTROL for kw in registered.values())


async def test_unload_unregisters_alice_commands() -> None:
    """Unloading calls every unregister handle so a reload can register again."""
    provider = _make_provider(text_commands=True)
    object.__setattr__(provider, "_cleanup_session", AsyncMock())
    await provider.loaded_in_mass()
    handles = [cast("MagicMock", handle) for handle in provider._unregister_api_commands]

    await provider.unload()

    assert len(handles) == 2
    for handle in handles:
        handle.assert_called_once_with()
    assert provider._unregister_api_commands == []


async def test_say_sends_repeat_phrase() -> None:
    """say() asks Alice to repeat the text through the repeat_phrase scenario."""
    player = _make_player()

    await player.say("Ужин готов")

    (payload,) = _sent(player)
    assert payload["command"] == "serverAction"
    form = payload["serverActionEventPayload"]["payload"]["form_update"]
    assert form["name"] == "personal_assistant.scenarios.quasar.iot.repeat_phrase"
    assert form["slots"] == [{"type": "string", "name": "phrase_to_repeat", "value": "Ужин готов"}]


async def test_send_command_sends_text() -> None:
    """send_command() hands the text to Alice as a spoken request."""
    player = _make_player()

    await player.send_command("включи свет на кухне")

    assert _sent(player) == [{"command": "sendText", "text": "включи свет на кухне"}]


async def test_say_raises_on_glagol_error() -> None:
    """A transport error surfaces as PlayerCommandFailed."""
    player = _make_player({"error": "not_connected"})

    with pytest.raises(PlayerCommandFailed, match="not_connected"):
        await player.say("Привет")


async def test_send_command_raises_on_non_success_status() -> None:
    """A rejected command surfaces as PlayerCommandFailed."""
    player = _make_player({"status": "ERROR", "message": "busy"})

    with pytest.raises(PlayerCommandFailed, match="busy"):
        await player.send_command("сколько времени")


async def test_api_say_routes_to_player() -> None:
    """The say API command reaches the addressed Station."""
    player = _make_player()
    provider = _make_provider({"station_1": player})

    await provider.say("station_1", "Привет")

    assert _sent(player)[0]["command"] == "serverAction"


async def test_api_command_routes_to_player() -> None:
    """The command API command reaches the addressed Station."""
    player = _make_player()
    provider = _make_provider({"station_1": player})

    await provider.command("station_1", "сколько времени")

    assert _sent(player) == [{"command": "sendText", "text": "сколько времени"}]


async def test_api_say_rejects_unknown_player() -> None:
    """A player id that is not a Yandex Station is rejected."""
    other = MagicMock()
    provider = _make_provider({"chromecast_1": other})

    with pytest.raises(PlayerUnavailableError):
        await provider.say("chromecast_1", "Привет")
    with pytest.raises(PlayerUnavailableError):
        await provider.say("missing", "Привет")


async def test_api_say_rejects_blank_text() -> None:
    """Blank text is rejected before anything is sent."""
    player = _make_player()
    provider = _make_provider({"station_1": player})

    with pytest.raises(InvalidDataError):
        await provider.say("station_1", "   ")
    with pytest.raises(InvalidDataError):
        await provider.command("station_1", "")

    _send_mock(player).assert_not_awaited()


async def test_api_say_rejects_player_outside_user_filter() -> None:
    """A user limited to other players cannot address this Station."""
    player = _make_player()
    provider = _make_provider({"station_1": player})
    user = MagicMock(username="guest", player_filter=["other_player"])

    with (
        patch(
            "music_assistant.providers.yandex_station.provider.get_current_user", return_value=user
        ),
        pytest.raises(InsufficientPermissions),
    ):
        await provider.say("station_1", "Привет")

    _send_mock(player).assert_not_awaited()


async def test_api_say_allows_player_in_user_filter() -> None:
    """A user whose filter includes the Station can use it."""
    player = _make_player()
    provider = _make_provider({"station_1": player})
    user = MagicMock(username="member", player_filter=["station_1"])

    with patch(
        "music_assistant.providers.yandex_station.provider.get_current_user", return_value=user
    ):
        await provider.say("station_1", "Привет")

    _send_mock(player).assert_awaited_once()
