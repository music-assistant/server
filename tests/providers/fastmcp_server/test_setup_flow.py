"""Tests for the MCP Connect Wizard setup flow."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest import mock

import pytest
from aiohttp.test_utils import make_mocked_request

pytest.importorskip("music_assistant.models.setup_flow")

from music_assistant.models.setup_flow import SetupFlowContext, SetupSession
from music_assistant.providers.fastmcp_server import setup_flow


async def _wait_for(predicate: Any) -> Any:
    """Wait for a setup-flow state transition."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("setup-flow state did not change")


async def test_setup_mounts_wizard_and_finishes_after_callback() -> None:
    """A new provider stays in setup until the Connect Wizard signals completion."""
    routes: dict[str, Any] = {}
    mass = mock.Mock()
    mass.webserver.base_url = "http://localhost:8095/ma"
    mass.players.all_players.return_value = []
    mass.get_provider.return_value = None

    def register_dynamic_route(path: str, handler: Any, method: str = "*") -> Any:
        del method
        routes[path] = handler
        return lambda: routes.pop(path, None)

    mass.webserver.register_dynamic_route.side_effect = register_dynamic_route
    collected: dict[str, Any] = {}

    async def finish(_session: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"instance_id": "fastmcp_server"}

    session = SetupSession(
        mass,
        "a1b2",
        SetupFlowContext(kind="setup", reason="user", domain="fastmcp_server"),
        finish,
    )
    unmount = mock.Mock()
    with (
        mock.patch.object(
            setup_flow,
            "mount_connect_wizard",
            mock.AsyncMock(return_value=unmount),
        ) as mount,
        mock.patch.object(
            setup_flow,
            "_dispatch_open_connect",
            mock.AsyncMock(return_value="http://localhost:8095/ma/mcp/v1/connect"),
        ) as dispatch,
    ):
        task = asyncio.create_task(setup_flow.run_setup(session))
        step = await _wait_for(
            lambda: (
                session.current_step
                if session.current_step
                and getattr(session.current_step.type, "value", session.current_step.type)
                == "external"
                else None
            )
        )
        assert not session.finished
        callback_path = f"/setup_flow/callback/{session.flow_id}"
        await routes[callback_path](make_mocked_request("GET", callback_path))
        await _wait_for(lambda: session.finished)
        await task

    mount.assert_awaited_once()
    dispatch.assert_awaited_once()
    assert dispatch.await_args is not None
    assert dispatch.await_args.kwargs["setup_callback_path"] == callback_path
    unmount.assert_called_once_with()
    assert collected == {}
    assert step.step_id == "connect"
