"""Tests for the Plex Connect interactive setup flow (run_setup)."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest import mock

import pytest
from music_assistant_models.enums import FlowStepType, PlayerType

from music_assistant.models.setup_flow import AbortFlow, SetupFlowContext, SetupSession
from music_assistant.providers.plex_connect import CONF_MASS_PLAYER_ID, CONF_PLEX_PROVIDER_ID
from music_assistant.providers.plex_connect import setup_flow as pc_flow


def _provider(instance_id: str, name: str) -> mock.Mock:
    """Build a stand-in for a loaded plex provider instance."""
    provider = mock.Mock()
    provider.instance_id = instance_id
    provider.name = name
    return provider


def _player(
    player_id: str, display_name: str, player_type: PlayerType = PlayerType.PLAYER
) -> mock.Mock:
    """Build a stand-in for a Music Assistant player."""
    player = mock.Mock()
    player.player_id = player_id
    player.display_name = display_name
    player.type = player_type
    return player


def _make_session(
    finish_handler: Any,
    plex_providers: list[mock.Mock] | None = None,
    players: list[mock.Mock] | None = None,
    setup_data: dict[str, Any] | None = None,
    values: dict[str, Any] | None = None,
) -> SetupSession:
    """Build a real SetupSession backed by a Mock mass with the given live state."""
    mass = mock.Mock()
    mass.get_provider_instances = mock.Mock(
        return_value=plex_providers
        if plex_providers is not None
        else [_provider("plex--1", "Plex")]
    )
    mass.players.all_players = mock.Mock(
        return_value=players if players is not None else [_player("player1", "Kitchen")]
    )
    context = SetupFlowContext(
        kind="setup",
        reason="user",
        domain="plex_connect",
        setup_data=setup_data or {},
        values=values or {},
    )
    return SetupSession(mass, "flow-test", context, finish_handler)


async def _await_user_form(session: SetupSession) -> Any:
    """Wait until the user form is presented and return the step."""
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        step = session.current_step
        if step is not None and step.type == FlowStepType.FORM:
            return step
        await asyncio.sleep(0.01)
    raise AssertionError("form step not published within timeout")


async def _wait_for_finish(session: SetupSession) -> None:
    """Wait until the flow finished."""
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if session.finished:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("flow did not finish within timeout")


async def test_collects_plex_provider_and_player() -> None:
    """The single form step collects both selections into setup_data."""
    collected: dict[str, Any] = {}

    async def finish(_s: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"instance_id": "plex_connect--1"}

    session = _make_session(
        finish,
        plex_providers=[_provider("plex--1", "Plex"), _provider("plex--2", "Plex Audiobooks")],
        players=[
            _player("player2", "living room"),
            _player("player1", "Kitchen"),
            _player("turntable", "Turntable", PlayerType.SOURCE),
        ],
    )
    task = asyncio.create_task(pc_flow.run_setup(session))
    step = await _await_user_form(session)
    # players are offered sorted case-insensitively by display name;
    # capture-only source players are not offered as a playback target
    assert [option.value for option in step.entries[1].options] == ["player1", "player2"]
    session.handle_submit({CONF_PLEX_PROVIDER_ID: "plex--2", CONF_MASS_PLAYER_ID: "player2"})
    await _wait_for_finish(session)
    await task

    assert collected == {CONF_PLEX_PROVIDER_ID: "plex--2", CONF_MASS_PLAYER_ID: "player2"}


async def test_prefills_stored_selection() -> None:
    """A reconfigure re-shows the stored selection, ignoring values that no longer exist."""

    async def finish(_s: SetupSession, _values: dict[str, Any]) -> dict[str, str]:
        return {"instance_id": "plex_connect--1"}

    session = _make_session(
        finish,
        plex_providers=[_provider("plex--1", "Plex"), _provider("plex--2", "Plex Audiobooks")],
        players=[_player("player1", "Kitchen"), _player("player2", "Living Room")],
        setup_data={CONF_PLEX_PROVIDER_ID: "plex--2", CONF_MASS_PLAYER_ID: "gone"},
    )
    task = asyncio.create_task(pc_flow.run_setup(session))
    step = await _await_user_form(session)

    assert step.entries[0].value == "plex--2"
    # the stored player disappeared: fall back to the first available option
    assert step.entries[1].value == "player1"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_prefills_from_legacy_option_values() -> None:
    """Installs made before this flow existed keep their selection from the config values."""

    async def finish(_s: SetupSession, _values: dict[str, Any]) -> dict[str, str]:
        return {"instance_id": "plex_connect--1"}

    session = _make_session(
        finish,
        plex_providers=[_provider("plex--1", "Plex"), _provider("plex--2", "Plex Audiobooks")],
        players=[_player("player1", "Kitchen"), _player("player2", "Living Room")],
        values={CONF_PLEX_PROVIDER_ID: "plex--2", CONF_MASS_PLAYER_ID: "player2"},
    )
    task = asyncio.create_task(pc_flow.run_setup(session))
    step = await _await_user_form(session)

    assert step.entries[0].value == "plex--2"
    assert step.entries[1].value == "player2"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_aborts_without_loaded_plex_provider() -> None:
    """Nothing to pick from: the flow aborts instead of showing an empty dropdown."""

    async def finish(_s: SetupSession, _values: dict[str, Any]) -> dict[str, str]:
        raise AssertionError("finish must not be called")

    session = _make_session(finish, plex_providers=[])
    with pytest.raises(AbortFlow) as err:
        await pc_flow.run_setup(session)

    assert err.value.reason == "no_plex_provider"


async def test_aborts_without_players() -> None:
    """No player to expose: the flow aborts instead of showing an empty dropdown."""

    async def finish(_s: SetupSession, _values: dict[str, Any]) -> dict[str, str]:
        raise AssertionError("finish must not be called")

    session = _make_session(finish, players=[])
    with pytest.raises(AbortFlow) as err:
        await pc_flow.run_setup(session)

    assert err.value.reason == "no_players"
