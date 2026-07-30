"""
Tests for ``provider.prompts.register_prompts``.

The prompts module shipped without tests; a refactor that dropped a prompt
or broke the ``CONF_RES_PROMPTS`` gate would land unobserved. This file
pins both the gate and the three registered prompts' names + tool references.
"""
# mypy: disable-error-code="arg-type, no-untyped-def, type-arg, assignment, operator, misc, union-attr"

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastmcp import Client, FastMCP

from music_assistant.providers.fastmcp_server.prompts import register_prompts

_EXPECTED_NAMES = {"find_and_play", "curate_party_playlist", "now_playing_summary"}


def _config(*, prompts_enabled: bool) -> MagicMock:
    """Build a minimal ``ProviderConfig`` stub gating on ``CONF_RES_PROMPTS``."""
    cfg = MagicMock()
    cfg._values = {"res_prompts": prompts_enabled}
    cfg.get_value = MagicMock(side_effect=lambda key, default=None: cfg._values.get(key, default))
    return cfg


@pytest.fixture
def mcp_with_prompts() -> FastMCP:
    """Build a FastMCP root with all three prompts registered (gate ON)."""
    mcp: FastMCP = FastMCP(name="t")
    register_prompts(mcp, _config(prompts_enabled=True))
    return mcp


async def test_gate_off_registers_no_prompts() -> None:
    """``CONF_RES_PROMPTS=False`` skips registration entirely."""
    mcp: FastMCP = FastMCP(name="t")
    register_prompts(mcp, _config(prompts_enabled=False))

    async with Client(mcp) as client:
        prompts = await client.list_prompts()
    assert prompts == [], f"expected no prompts when gate is off, got {prompts!r}"


async def test_gate_on_registers_exactly_three_named_prompts(mcp_with_prompts: FastMCP) -> None:
    """The three prompt names are exposed verbatim — clients address them by name."""
    async with Client(mcp_with_prompts) as client:
        prompts = await client.list_prompts()
    names = {p.name for p in prompts}
    assert names == _EXPECTED_NAMES, (
        f"prompt set drifted from {_EXPECTED_NAMES}; got {names}. "
        f"Adding or removing a prompt is a public-contract change."
    )


async def test_find_and_play_references_expected_tools(mcp_with_prompts: FastMCP) -> None:
    """``find_and_play`` orients the LLM toward the right tool chain."""
    async with Client(mcp_with_prompts) as client:
        result = await client.get_prompt("find_and_play", {"query": "test", "target_player": "p1"})
    text = " ".join(m.content.text for m in result.messages if hasattr(m.content, "text"))
    for tool_name in (
        "library_search_tracks",
        "playback_play_media",
        "queue_get_active_queue",
        "players_list_players",
    ):
        assert tool_name in text, f"missing tool ref {tool_name!r} in find_and_play"


async def test_curate_party_playlist_references_playlist_tools(
    mcp_with_prompts: FastMCP,
) -> None:
    """``curate_party_playlist`` references the create + add-tracks chain."""
    async with Client(mcp_with_prompts) as client:
        result = await client.get_prompt(
            "curate_party_playlist", {"theme": "indie", "length_minutes": "30"}
        )
    text = " ".join(m.content.text for m in result.messages if hasattr(m.content, "text"))
    for tool_name in (
        "library_search_tracks",
        "playlists_create_playlist",
        "playlists_add_tracks",
    ):
        assert tool_name in text, f"missing tool ref {tool_name!r} in curate_party_playlist"


async def test_now_playing_summary_branches_on_player_id(mcp_with_prompts: FastMCP) -> None:
    """``now_playing_summary`` switches its tool plan based on whether a player_id is given."""
    async with Client(mcp_with_prompts) as client:
        with_id = await client.get_prompt("now_playing_summary", {"player_id": "p1"})
        without_id = await client.get_prompt("now_playing_summary", {})

    def _text(result: Any) -> str:
        return " ".join(m.content.text for m in result.messages if hasattr(m.content, "text"))

    assert "queue_get_active_queue" in _text(with_id)
    assert "p1" in _text(with_id)
    # Without an id we expect the broader list-then-fan-out plan.
    assert "players_list_players" in _text(without_id)
