"""Unit tests for the SECURE_STRING write gate."""
# ruff: noqa: PLC0415
#   Fixtures import provider modules lazily (inside the test body). A file-level
#   suppression is used instead of per-line directives so the intent survives the
#   upstream import-path rewrite, which lengthens ``from music_assistant.providers.fastmcp_server.X`` lines and
#   reflows them — detaching any trailing per-line directive.

from __future__ import annotations

from typing import Any

import pytest
from fastmcp.exceptions import ToolError
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.providers.fastmcp_server.config_io.secret_handler import (
    gate_secret_writes,
    is_secret_key,
)


def _entries() -> dict[str, ConfigEntry]:
    return {
        "log_level": ConfigEntry(key="log_level", type=ConfigEntryType.STRING, label="L"),
        "token": ConfigEntry(key="token", type=ConfigEntryType.SECURE_STRING, label="T"),
    }


def test_nonsecret_key_passes_without_secret_tag() -> None:
    """Non-secret keys pass the gate without secret tag enabled."""
    gate_secret_writes(_entries(), {"log_level": "DEBUG"}, secret_tag_enabled=False)


def test_secret_key_blocked_without_secret_tag() -> None:
    """Secret keys are blocked without secret tag enabled."""
    with pytest.raises(ToolError, match="config:write:secret"):
        gate_secret_writes(_entries(), {"token": "abc"}, secret_tag_enabled=False)


def test_secret_key_allowed_with_secret_tag() -> None:
    """Secret keys are allowed when secret tag is enabled."""
    gate_secret_writes(_entries(), {"token": "abc"}, secret_tag_enabled=True)


def test_mixed_payload_blocked_atomically_names_secret_key() -> None:
    """Mixed payload is blocked atomically, naming the secret key."""
    with pytest.raises(ToolError, match="token"):
        gate_secret_writes(
            _entries(), {"log_level": "DEBUG", "token": "abc"}, secret_tag_enabled=False
        )


def test_is_secret_key() -> None:
    """is_secret_key correctly identifies SECURE_STRING entries."""
    assert is_secret_key(_entries(), "token") is True
    assert is_secret_key(_entries(), "log_level") is False
    assert is_secret_key(_entries(), "missing") is False


async def test_secret_write_blocked_without_secret_tag_e2e(
    mounted_config_no_secret: Any, mock_config_targets: Any
) -> None:
    """E2e: set_provider_value rejects SECURE_STRING write when secret tag is off."""
    from fastmcp import Client

    async with Client(mounted_config_no_secret) as client:
        with pytest.raises(ToolError, match="config:write:secret"):
            await client.call_tool(
                "config_set_provider_value",
                {"instance_id": "yandex_music", "key": "token", "value": "x"},
            )
    mock_config_targets.config.save_provider_config.assert_not_called()


async def test_secret_gate_reevaluated_per_request_via_callable(
    mock_config_targets: Any,
) -> None:
    """
    The secret gate must read a live callable so a hot-swapped toggle takes effect without a restart.

    Regression for PR #99 review finding A.
    """
    from fastmcp import Client, FastMCP

    from music_assistant.providers.fastmcp_server.tools.config import build_config_server

    flag: dict[str, bool] = {"on": False}
    sub = build_config_server(
        mock_config_targets,
        require_confirmation=False,
        secret_writes_enabled=lambda: flag["on"],
    )
    root = FastMCP(name="test")
    root.mount(sub, namespace="config")

    # gate closed — secret write must be rejected
    async with Client(root) as client:
        with pytest.raises(ToolError, match="config:write:secret"):
            await client.call_tool(
                "config_set_provider_value",
                {"instance_id": "yandex_music", "key": "token", "value": "x"},
            )

    # flip the live flag — no server rebuild
    flag["on"] = True

    # gate open — same server, secret write must now succeed
    async with Client(root) as client:
        result = await client.call_tool(
            "config_set_provider_value",
            {"instance_id": "yandex_music", "key": "token", "value": "x"},
        )
    assert result.data.applied is True
