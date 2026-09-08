"""Tests for the Plex Connect interactive setup flow (run_setup)."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest
from music_assistant_models.enums import FlowStepType, PlayerType

from music_assistant.models.setup_flow import AbortFlow, SetupFlowContext, SetupSession
from music_assistant.providers.plex_connect import (
    CONF_MASS_PLAYER_ID,
    CONF_PLEX_PROVIDER_ID,
    CONF_PLEXTV_TOKEN,
)
from music_assistant.providers.plex_connect import setup_flow as pc_flow

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType


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
    instance_id: str | None = None,
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
    mass.config.get_provider_setup_value = mock.Mock(
        side_effect=lambda _instance_id, key: (setup_data or {}).get(key)
    )
    context = SetupFlowContext(
        kind="reconfigure" if instance_id else "setup",
        reason="user",
        domain="plex_connect",
        instance_id=instance_id,
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


@pytest.mark.parametrize("changed_key", [CONF_MASS_PLAYER_ID, CONF_PLEX_PROVIDER_ID])
@pytest.mark.parametrize("legacy_selection", [False, True])
@pytest.mark.parametrize("link_after_open", [False, True])
async def test_linked_reconfiguration_requires_unlink(
    changed_key: str, legacy_selection: bool, link_after_open: bool
) -> None:
    """Identity changes require unlinking, including while the form remains open."""
    selected = {CONF_PLEX_PROVIDER_ID: "plex--1", CONF_MASS_PLAYER_ID: "player1"}
    stored: dict[str, Any] = {
        CONF_PLEXTV_TOKEN: None if link_after_open else "devtoken",
    }
    if not legacy_selection:
        stored.update(selected)
    finish = mock.AsyncMock(return_value={"instance_id": "plex_connect--1"})
    session = _make_session(
        finish,
        plex_providers=[_provider("plex--1", "Plex"), _provider("plex--2", "Plex Audiobooks")],
        players=[_player("player1", "Kitchen"), _player("player2", "Living Room")],
        setup_data=stored,
        values=selected if legacy_selection else None,
        instance_id="plex_connect--1",
    )
    submitted: dict[str, ConfigValueType] = {
        **selected,
        changed_key: "player2" if changed_key == CONF_MASS_PLAYER_ID else "plex--2",
    }
    task = asyncio.create_task(pc_flow.run_setup(session))
    try:
        await _await_user_form(session)
        if link_after_open:
            stored[CONF_PLEXTV_TOKEN] = "devtoken"
        assert session.handle_submit(submitted) is None
        await session.wait_for_step_change(timeout=1)
        step = session.current_step
        assert step is not None
        assert step.errors == {"base": "plextv_unlink_required"}
        finish.assert_not_awaited()
        assert stored[CONF_PLEXTV_TOKEN] == "devtoken"

        stored[CONF_PLEXTV_TOKEN] = None
        assert session.handle_submit(submitted) is None
        await _wait_for_finish(session)
        await task

        finish.assert_awaited_once_with(session, submitted)
        assert stored[CONF_PLEXTV_TOKEN] is None
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.parametrize("token", ["devtoken", None])
async def test_reconfigure_unchanged_identity_preserves_link(token: str | None) -> None:
    """Keeping the same selections does not require unlinking or rewrite credentials."""
    selected: dict[str, ConfigValueType] = {
        CONF_PLEX_PROVIDER_ID: "plex--1",
        CONF_MASS_PLAYER_ID: "player1",
    }
    stored = {**selected, CONF_PLEXTV_TOKEN: token}
    finish = mock.AsyncMock(return_value={"instance_id": "plex_connect--1"})
    session = _make_session(finish, setup_data=stored, instance_id="plex_connect--1")
    task = asyncio.create_task(pc_flow.run_setup(session))
    await _await_user_form(session)
    assert session.handle_submit(selected) is None
    await _wait_for_finish(session)
    await task

    finish.assert_awaited_once_with(session, selected)
    assert stored[CONF_PLEXTV_TOKEN] == token


async def test_reconfigure_unlinked_identity_can_change() -> None:
    """An unlinked instance can select a different player and Plex provider."""
    selected = {CONF_PLEX_PROVIDER_ID: "plex--1", CONF_MASS_PLAYER_ID: "player1"}
    stored = {**selected, CONF_PLEXTV_TOKEN: None}
    finish = mock.AsyncMock(return_value={"instance_id": "plex_connect--1"})
    session = _make_session(
        finish,
        plex_providers=[_provider("plex--1", "Plex"), _provider("plex--2", "Plex Audiobooks")],
        players=[_player("player1", "Kitchen"), _player("player2", "Living Room")],
        setup_data=stored,
        instance_id="plex_connect--1",
    )
    submitted: dict[str, ConfigValueType] = {
        CONF_PLEX_PROVIDER_ID: "plex--2",
        CONF_MASS_PLAYER_ID: "player2",
    }
    task = asyncio.create_task(pc_flow.run_setup(session))
    await _await_user_form(session)
    assert session.handle_submit(submitted) is None
    await _wait_for_finish(session)
    await task

    finish.assert_awaited_once_with(session, submitted)


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
