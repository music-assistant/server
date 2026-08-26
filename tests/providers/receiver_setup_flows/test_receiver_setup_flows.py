"""Tests for receiver-style plugin setup flows."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest
from music_assistant_models.enums import FlowStepType

from music_assistant.constants import CONF_BIND_IP, CONF_BIND_PORT
from music_assistant.models.setup_flow import AbortFlow, SetupFlowContext, SetupSession
from music_assistant.providers.ariacast_receiver import (
    CONF_MASS_PLAYER_ID as ARIACAST_PLAYER_ID,
)
from music_assistant.providers.ariacast_receiver import setup_flow as ariacast_flow
from music_assistant.providers.vban_receiver import setup_flow as vban_flow
from music_assistant.providers.vban_receiver.constants import (
    CONF_AUDIO_CHANNELS,
    CONF_PCM_AUDIO_FORMAT,
    CONF_PCM_SAMPLE_RATE,
    CONF_SENDER_HOST,
    CONF_VBAN_STREAM_NAME,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType


def _player(player_id: str, display_name: str) -> mock.Mock:
    """Return a minimal player for setup-flow option generation."""
    player = mock.Mock()
    player.player_id = player_id
    player.display_name = display_name
    return player


def _make_session(
    domain: str,
    *,
    setup_data: dict[str, Any] | None = None,
    values: dict[str, Any] | None = None,
) -> tuple[SetupSession, dict[str, Any]]:
    """Return a real setup session and the values collected by its finish handler."""
    mass = mock.Mock()
    mass.players.all_players.return_value = [
        _player("living-room", "Living Room"),
        _player("kitchen", "Kitchen"),
    ]
    collected: dict[str, Any] = {}

    async def finish(_session: SetupSession, submitted: dict[str, Any]) -> dict[str, str]:
        collected.update(submitted)
        return {"instance_id": f"{domain}--test"}

    context = SetupFlowContext(
        kind="setup",
        reason="user",
        domain=domain,
        setup_data=setup_data or {},
        values=values or {},
    )
    return SetupSession(mass, "flow-test", context, finish), collected


async def _start_form(session: SetupSession, flow_module: Any) -> tuple[asyncio.Task[None], Any]:
    """Start a setup flow and wait for its form step."""
    task = asyncio.create_task(flow_module.run_setup(session))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if session.current_step and session.current_step.type == FlowStepType.FORM:
            return task, session.current_step
        await asyncio.sleep(0.01)
    raise AssertionError("form step not published")


async def _wait_finished(session: SetupSession) -> None:
    """Wait for a setup session to finish."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if session.finished:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("setup flow did not finish")


async def test_ariacast_flow_collects_mandatory_player() -> None:
    """The AriaCast flow persists the mandatory target player (no automatic option)."""
    session, collected = _make_session("ariacast_receiver")
    task, step = await _start_form(session, ariacast_flow)
    player_entry = next(entry for entry in step.entries if entry.key == ARIACAST_PLAYER_ID)
    assert [option.value for option in player_entry.options] == ["kitchen", "living-room"]

    session.handle_submit({ARIACAST_PLAYER_ID: "kitchen"})
    await _wait_finished(session)
    await task

    assert collected == {ARIACAST_PLAYER_ID: "kitchen"}


async def test_ariacast_flow_requires_a_player_selection() -> None:
    """Submitting without a selection re-renders the form with a required error."""
    session, collected = _make_session("ariacast_receiver")
    task, _step = await _start_form(session, ariacast_flow)

    step = session.handle_submit({})
    assert step is not None
    assert step.errors == {ARIACAST_PLAYER_ID: "required"}
    assert collected == {}

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_ariacast_flow_vanished_stored_player_renders_unselected() -> None:
    """A stored player that no longer exists is not preselected (nor silently replaced)."""
    session, _collected = _make_session(
        "ariacast_receiver", setup_data={ARIACAST_PLAYER_ID: "gone-player"}
    )
    task, step = await _start_form(session, ariacast_flow)
    player_entry = next(entry for entry in step.entries if entry.key == ARIACAST_PLAYER_ID)
    assert player_entry.value is None

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_ariacast_flow_aborts_without_players() -> None:
    """With no players registered the flow aborts with the no_players reason."""
    session, _collected = _make_session("ariacast_receiver")
    session.mass.players.all_players.return_value = []  # type: ignore[attr-defined]

    with pytest.raises(AbortFlow) as excinfo:
        await ariacast_flow.run_setup(session)
    assert excinfo.value.reason == "no_players"


async def test_vban_flow_collects_receiver_endpoint_and_stream_format() -> None:
    """The VBAN flow collects all values needed before starting its UDP receiver."""
    session, collected = _make_session("vban_receiver")
    submitted: dict[str, ConfigValueType] = {
        CONF_BIND_PORT: 6981,
        CONF_VBAN_STREAM_NAME: "Studio",
        CONF_SENDER_HOST: "192.0.2.10",
        CONF_PCM_AUDIO_FORMAT: "S16LE",
        CONF_PCM_SAMPLE_RATE: 48000,
        CONF_AUDIO_CHANNELS: 2,
        CONF_BIND_IP: "0.0.0.0",
    }
    with mock.patch.object(
        vban_flow, "get_ip_addresses", mock.AsyncMock(return_value=["192.0.2.2"])
    ):
        task, step = await _start_form(session, vban_flow)
        assert {entry.key for entry in step.entries} >= set(submitted)
        session.handle_submit(submitted)
        await _wait_finished(session)
        await task

    assert collected == submitted
