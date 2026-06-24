"""End-to-end tests for the CONFIG_WRITE_CORE tool group."""
# ruff: noqa: D103, PLC0415
#   D103: test functions don't need docstrings.
#   PLC0415: mock reconfiguration inside test bodies requires deferred imports.

from __future__ import annotations

import contextlib
from typing import Any

from fastmcp import Client


def _decliner() -> Any:
    from fastmcp.client.elicitation import ElicitResult

    async def handler(*_a: Any, **_kw: Any) -> ElicitResult:
        return ElicitResult(action="decline")

    return handler


async def test_core_value_set_persists(mounted_config: Any, mock_config_targets: Any) -> None:
    async with Client(mounted_config) as client:
        result = await client.call_tool(
            "config_set_core_value",
            {"domain": "webserver", "key": "log_level", "value": "DEBUG"},
        )
    mock_config_targets.config.save_core_config.assert_awaited_once()
    assert result.data.applied is True


async def test_core_save_bulk_persists(mounted_config: Any, mock_config_targets: Any) -> None:
    async with Client(mounted_config) as client:
        result = await client.call_tool(
            "config_save_core",
            {"domain": "webserver", "values": {"log_level": "DEBUG", "http_port": 8095}},
        )
    mock_config_targets.config.save_core_config.assert_awaited_once()
    assert result.data.applied is True


async def test_core_save_confirm_prompt_mentions_restart(mock_config_targets: Any) -> None:
    from fastmcp import FastMCP

    from music_assistant.providers.fastmcp_server.tools.config import build_config_server

    seen: list[str] = []

    async def handler(message: str, response_type: Any, params: Any, ctx: Any) -> Any:  # noqa: ARG001
        seen.append(message)
        return _decliner()()

    mcp = FastMCP(name="t")
    mcp.mount(
        build_config_server(mock_config_targets, require_confirmation=True), namespace="config"
    )
    async with Client(mcp, elicitation_handler=handler) as client:
        with contextlib.suppress(Exception):
            await client.call_tool(
                "config_set_core_value",
                {"domain": "webserver", "key": "log_level", "value": "DEBUG"},
            )
    assert any("restart" in p.lower() or "all playback" in p.lower() for p in seen)
