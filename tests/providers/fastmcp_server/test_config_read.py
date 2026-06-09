"""End-to-end tests for the CONFIG_READ tool group."""
# ruff: noqa: D103, PLC0415
#   D103: test functions don't need docstrings.
#   PLC0415: mock reconfiguration inside test bodies requires deferred imports.

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError


async def _call(mcp: Any, name: str, **kwargs: Any) -> Any:
    async with Client(mcp) as client:
        return await client.call_tool(f"config_{name}", kwargs)


async def test_get_provider_masks_secret(mounted_config: Any, mock_config_targets: Any) -> None:  # noqa: ARG001
    result = await _call(mounted_config, "get_provider", instance_id="yandex_music")
    by_key = {v.key: v for v in result.data.values}
    assert by_key["token"].value == "this_value_is_encrypted"
    assert "real-secret" not in str(result.data)


async def test_get_core_returns_values(mounted_config: Any, mock_config_targets: Any) -> None:  # noqa: ARG001
    result = await _call(mounted_config, "get_core", domain="webserver")
    assert any(v.key == "log_level" for v in result.data.values)


async def test_get_player_returns_values(mounted_config: Any, mock_config_targets: Any) -> None:  # noqa: ARG001
    result = await _call(mounted_config, "get_player", player_id="kitchen")
    assert result.data.player_id == "kitchen"
    assert any(v.key == "http_port" for v in result.data.values)


async def test_get_entries_lists_editable(mounted_config: Any, mock_config_targets: Any) -> None:  # noqa: ARG001
    result = await _call(
        mounted_config, "get_entries", target_type="provider", target_id="yandex_music"
    )
    keys = {e.key for e in result.data.entries}
    assert {"log_level", "http_port", "token"} <= keys


async def test_get_provider_unknown_raises(mounted_config: Any, mock_mass: Any) -> None:
    from unittest.mock import AsyncMock

    mock_mass.config.get_provider_config = AsyncMock(side_effect=KeyError("nope"))
    with pytest.raises(ToolError, match="not found"):
        await _call(mounted_config, "get_provider", instance_id="nope")


async def test_get_dsp_returns_shape(mounted_config: Any, mock_config_targets: Any) -> None:  # noqa: ARG001
    result = await _call(mounted_config, "get_dsp", player_id="kitchen")
    assert result.data.player_id == "kitchen"
    assert result.data.enabled is True
    assert result.data.filters == []


async def test_list_targets_rolls_up(mounted_config: Any, mock_config_targets: Any) -> None:  # noqa: ARG001
    result = await _call(mounted_config, "list_targets")
    assert len(result.data.providers) >= 1
    assert len(result.data.core) >= 1
    assert len(result.data.players) >= 1


async def test_get_dsp_unknown_player_raises(mounted_config: Any, mock_mass: Any) -> None:
    from unittest.mock import AsyncMock

    mock_mass.config.get_player_config = AsyncMock(side_effect=KeyError("nope"))
    with pytest.raises(ToolError, match="not found"):
        await _call(mounted_config, "get_dsp", player_id="nope")


async def test_get_entries_masks_secret_current_value(
    mounted_config: Any,
    mock_config_targets: Any,  # noqa: ARG001
) -> None:
    """config_get_entries must mask SECURE_STRING current_value.

    Regression for PR #99 review finding B.
    """
    from music_assistant_models.constants import SECURE_STRING_SUBSTITUTE

    result = await _call(
        mounted_config, "get_entries", target_type="provider", target_id="yandex_music"
    )
    by_key = {e.key: e for e in result.data.entries}
    assert by_key["token"].type == "secure_string"
    assert by_key["token"].current_value == SECURE_STRING_SUBSTITUTE
    # the raw fixture secret value must not leak
    assert "raw-secret-xyz" not in str(result.data)
