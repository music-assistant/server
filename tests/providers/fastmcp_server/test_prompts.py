"""Tests for discovery-first canned MCP prompts."""
# mypy: disable-error-code="arg-type, no-untyped-def, type-arg, assignment, operator, misc, union-attr"

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastmcp import Client, FastMCP

from music_assistant.providers.fastmcp_server.prompts import register_prompts

_EXPECTED_NAMES = {"find_and_play", "curate_party_playlist", "now_playing_summary"}
_META_CHAIN = ("search_tools", "get_tool_schema", "call_tool")


def _config(*, prompts_enabled: bool) -> MagicMock:
    config = MagicMock()
    config.get_value.side_effect = lambda key, default=None: (
        prompts_enabled if key == "res_prompts" else default
    )
    return config


@pytest.fixture
def mcp_with_prompts() -> FastMCP:
    """Build a root with the enabled prompt set."""
    mcp = FastMCP(name="t")
    register_prompts(mcp, _config(prompts_enabled=True))
    return mcp


def _text(result: Any) -> str:
    return " ".join(
        message.content.text for message in result.messages if hasattr(message.content, "text")
    )


async def test_gate_off_registers_no_prompts() -> None:
    """The provider setting can disable all canned prompts."""
    mcp = FastMCP(name="t")
    register_prompts(mcp, _config(prompts_enabled=False))
    async with Client(mcp) as client:
        assert await client.list_prompts() == []


async def test_gate_on_registers_exact_prompt_set(mcp_with_prompts: FastMCP) -> None:
    """The retained prompt names stay stable for clients."""
    async with Client(mcp_with_prompts) as client:
        assert {prompt.name for prompt in await client.list_prompts()} == _EXPECTED_NAMES


@pytest.mark.parametrize(
    ("name", "arguments", "native_fragment"),
    [
        ("find_and_play", {"query": "test", "target_player": "p1"}, "player_queues/play_media"),
        (
            "curate_party_playlist",
            {"theme": "indie", "length_minutes": "30"},
            "music/playlists/create_playlist",
        ),
        ("now_playing_summary", {"player_id": "p1"}, "player_queues/get_active_queue"),
    ],
)
async def test_prompts_use_meta_discovery_chain(
    mcp_with_prompts: FastMCP,
    name: str,
    arguments: dict[str, str],
    native_fragment: str,
) -> None:
    """Every workflow teaches the only executable MCP surface and a native target hint."""
    async with Client(mcp_with_prompts) as client:
        text = _text(await client.get_prompt(name, arguments))
    assert all(meta_tool in text for meta_tool in _META_CHAIN)
    assert "ma_api:*" in text
    assert native_fragment in text


async def test_now_playing_summary_branches_on_player_id(mcp_with_prompts: FastMCP) -> None:
    """The prompt scopes its plan differently with and without a player id."""
    async with Client(mcp_with_prompts) as client:
        with_id = _text(await client.get_prompt("now_playing_summary", {"player_id": "p1"}))
        without_id = _text(await client.get_prompt("now_playing_summary", {}))
    assert "player_id 'p1'" in with_id
    assert "players/all" in without_id
