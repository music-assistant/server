"""End-to-end tests for the CONFIG_WRITE_PROVIDER tool group."""
# ruff: noqa: D103, PLC0415
#   D103: test functions don't need docstrings.
#   PLC0415: mock reconfiguration inside test bodies requires deferred imports.

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError


def _decliner() -> Any:
    from fastmcp.client.elicitation import ElicitResult

    async def handler(*_a: Any, **_kw: Any) -> ElicitResult:
        return ElicitResult(action="decline")

    return handler


async def test_set_provider_value_persists(mounted_config: Any, mock_config_targets: Any) -> None:
    async with Client(mounted_config) as client:
        result = await client.call_tool(
            "config_set_provider_value",
            {"instance_id": "yandex_music", "key": "log_level", "value": "DEBUG"},
        )
    mock_config_targets.config.save_provider_config.assert_awaited_once()
    assert result.data.applied is True


async def test_dry_run_returns_diff_no_persist(
    mounted_config: Any, mock_config_targets: Any
) -> None:
    async with Client(mounted_config) as client:
        result = await client.call_tool(
            "config_set_provider_value",
            {"instance_id": "yandex_music", "key": "log_level", "value": "DEBUG", "dry_run": True},
        )
    assert result.data.applied is False
    assert result.data.diff is not None
    mock_config_targets.config.save_provider_config.assert_not_called()


async def test_set_provider_value_validation_rejects(
    mounted_config: Any,
    mock_config_targets: Any,  # noqa: ARG001
) -> None:
    async with Client(mounted_config) as client:
        with pytest.raises(ToolError, match="failed validation"):
            await client.call_tool(
                "config_set_provider_value",
                {"instance_id": "yandex_music", "key": "http_port", "value": 999999},
            )


async def test_requires_reload_flag_surfaced(mounted_config: Any, mock_config_targets: Any) -> None:  # noqa: ARG001
    # token entry has requires_reload=True in the fixture and is SECURE_STRING;
    # mounted_config has all tags incl. secret, so this is allowed.
    async with Client(mounted_config) as client:
        result = await client.call_tool(
            "config_set_provider_value",
            {"instance_id": "yandex_music", "key": "token", "value": "newtok"},
        )
    assert result.data.requires_reload is True


async def test_confirm_declined_blocks_write(mock_config_targets: Any) -> None:
    from fastmcp import FastMCP

    from music_assistant.providers.fastmcp_server.tools.config import build_config_server

    mcp = FastMCP(name="t")
    mcp.mount(
        build_config_server(mock_config_targets, require_confirmation=True), namespace="config"
    )
    async with Client(mcp, elicitation_handler=_decliner()) as client:
        with pytest.raises(ToolError):
            await client.call_tool(
                "config_set_provider_value",
                {"instance_id": "yandex_music", "key": "log_level", "value": "DEBUG"},
            )
    mock_config_targets.config.save_provider_config.assert_not_called()


async def test_trigger_action_relays_entries(mounted_config: Any, mock_config_targets: Any) -> None:
    async with Client(mounted_config) as client:
        result = await client.call_tool(
            "config_trigger_provider_action",
            {"instance_id": "yandex_music", "action_key": "auth_qr"},
        )
    assert result.data.action_key == "auth_qr"
    mock_config_targets.config.invoke_provider_config_action.assert_awaited()


async def test_save_provider_bulk_persists(mounted_config: Any, mock_config_targets: Any) -> None:
    async with Client(mounted_config) as client:
        result = await client.call_tool(
            "config_save_provider",
            {"instance_id": "yandex_music", "values": {"log_level": "DEBUG", "http_port": 8095}},
        )
    mock_config_targets.config.save_provider_config.assert_awaited_once()
    assert result.data.applied is True


async def test_secret_write_delegates_plaintext_and_never_logs_it(
    mounted_config: Any, mock_config_targets: Any, caplog: Any
) -> None:
    with caplog.at_level(logging.INFO, logger="music_assistant.providers.fastmcp_server.config"):
        async with Client(mounted_config) as client:
            await client.call_tool(
                "config_set_provider_value",
                {"instance_id": "yandex_music", "key": "token", "value": "sup3rsecret"},
            )
    args = mock_config_targets.config.save_provider_config.await_args
    assert "sup3rsecret" in str(args)  # plaintext passed to MA (which encrypts)
    assert "sup3rsecret" not in caplog.text  # never logged


async def test_write_tools_use_interactive_timeout() -> None:
    """
    Confirmation-gated writes must not use the 10s fast timeout.

    The elicitation round-trip + save+reload needs a human-scale window.
    Regression for a live timeout-mid-confirmation bug.
    """
    from unittest.mock import MagicMock

    from music_assistant.providers.fastmcp_server.tools._common import (
        TIMEOUT_FAST,
        TIMEOUT_INTERACTIVE,
    )
    from music_assistant.providers.fastmcp_server.tools.config import build_config_server

    assert TIMEOUT_INTERACTIVE > TIMEOUT_FAST
    sub = build_config_server(MagicMock(), require_confirmation=True)
    tools = await sub.list_tools()
    write_names = {
        "set_provider_value",
        "save_provider",
        "trigger_provider_action",
        "set_core_value",
        "save_core",
        "set_player_value",
        "save_player",
        "save_dsp",
    }
    read_names = {
        "list_targets",
        "get_provider",
        "get_core",
        "get_player",
        "get_entries",
        "get_dsp",
    }
    tools_by_name = {tool.name: tool for tool in tools}
    for name in write_names:
        assert name in tools_by_name, f"{name} not found in tools"
        tool = tools_by_name[name]
        assert tool.timeout == TIMEOUT_INTERACTIVE, (
            f"{name} should use TIMEOUT_INTERACTIVE (120s), got {tool.timeout}s"
        )
    for name in read_names:
        assert name in tools_by_name, f"{name} not found in tools"
        tool = tools_by_name[name]
        assert tool.timeout == TIMEOUT_FAST, (
            f"{name} should keep TIMEOUT_FAST (10s), got {tool.timeout}s"
        )
