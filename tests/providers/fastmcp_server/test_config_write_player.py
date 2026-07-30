"""End-to-end tests for the CONFIG_WRITE_PLAYER tool group."""
# ruff: noqa: D103
#   D103: test functions don't need docstrings.
#   PLC0415: mock reconfiguration inside test bodies requires deferred imports.

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError


async def test_set_player_value_persists(mounted_config: Any, mock_config_targets: Any) -> None:
    async with Client(mounted_config) as client:
        result = await client.call_tool(
            "config_set_player_value",
            {"player_id": "kitchen", "key": "log_level", "value": "DEBUG"},
        )
    mock_config_targets.config.save_player_config.assert_awaited_once()
    assert result.data.applied is True


async def test_save_player_bulk_persists(mounted_config: Any, mock_config_targets: Any) -> None:
    async with Client(mounted_config) as client:
        result = await client.call_tool(
            "config_save_player",
            {"player_id": "kitchen", "values": {"log_level": "DEBUG", "http_port": 8095}},
        )
    mock_config_targets.config.save_player_config.assert_awaited_once()
    assert result.data.applied is True


async def test_save_dsp_persists(mounted_config: Any, mock_config_targets: Any) -> None:
    async with Client(mounted_config) as client:
        result = await client.call_tool(
            "config_save_dsp",
            {
                "player_id": "kitchen",
                "dsp": {"enabled": True, "input_gain": 0.0, "output_gain": 0.0, "filters": []},
            },
        )
    mock_config_targets.config.save_dsp_config.assert_awaited_once()
    assert result.data.applied is True


async def test_save_dsp_dry_run_no_persist(mounted_config: Any, mock_config_targets: Any) -> None:
    async with Client(mounted_config) as client:
        result = await client.call_tool(
            "config_save_dsp",
            {
                "player_id": "kitchen",
                "dsp": {"enabled": True, "input_gain": 0.0, "output_gain": 0.0, "filters": []},
                "dry_run": True,
            },
        )
    assert result.data.applied is False
    mock_config_targets.config.save_dsp_config.assert_not_called()


async def test_save_player_payload_over_64kb_rejected(
    mounted_config: Any,
    mock_config_targets: Any,  # noqa: ARG001
) -> None:
    big = {"log_level": "x" * (65 * 1024)}
    async with Client(mounted_config) as client:
        with pytest.raises(ToolError, match="64 KB"):
            await client.call_tool("config_save_player", {"player_id": "kitchen", "values": big})


async def test_save_dsp_invalid_payload_rejected(
    mounted_config: Any,
    mock_config_targets: Any,  # noqa: ARG001
) -> None:
    # input_gain way out of DSPConfig's -60..60 range → DSPConfig.from_dict or
    # .validate() should surface as ToolError (not a raw exception).
    async with Client(mounted_config) as client:
        with pytest.raises(ToolError):
            await client.call_tool(
                "config_save_dsp",
                {"player_id": "kitchen", "dsp": {"enabled": True, "input_gain": 999.0}},
            )
