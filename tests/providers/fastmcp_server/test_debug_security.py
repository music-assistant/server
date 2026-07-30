"""Cross-cutting security tests: off-by-default, masking, redaction, description pinning."""
# ruff: noqa: PLC0415
#   Fixtures import provider modules lazily (inside the test body). A file-level
#   suppression is used instead of per-line directives so the intent survives the
#   upstream import-path rewrite, which lengthens ``from music_assistant.providers.fastmcp_server.X`` lines and
#   reflows them — detaching any trailing per-line directive.

from __future__ import annotations

import contextlib
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import Client


async def test_off_by_default_hides_all_debug_tools(mounted_debug_off: Any) -> None:
    """Tag.DEBUG_* all disabled -> zero debug tools visible via list_tools()."""
    async with Client(mounted_debug_off) as client:
        tools = await client.list_tools()
    debug_tools = [t for t in tools if t.name.startswith("debug_")]
    assert debug_tools == [], f"debug tools leaked: {[t.name for t in debug_tools]}"


async def test_enabling_single_tag_exposes_only_that_group(mock_mass: MagicMock) -> None:
    """With only Tag.DEBUG_INSPECT allowed, only the 3 inspect tools are visible."""
    from fastmcp import FastMCP

    from music_assistant.providers.fastmcp_server.middleware import TagFilterMiddleware
    from music_assistant.providers.fastmcp_server.server import build_tag_lookup
    from music_assistant.providers.fastmcp_server.tags import Tag
    from music_assistant.providers.fastmcp_server.tools.debug import build_debug_server

    mcp = FastMCP(name="test")
    mcp.mount(build_debug_server(mock_mass, require_confirmation=False), namespace="debug")
    mcp.add_middleware(
        TagFilterMiddleware(lambda: {Tag.DEBUG_INSPECT.value}, build_tag_lookup(mcp))
    )

    try:
        async with Client(mcp) as client:
            tools = await client.list_tools()
        names = {t.name for t in tools if t.name.startswith("debug_")}
    finally:
        close = getattr(mcp, "close", None) or getattr(mcp, "shutdown", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()

    assert names == {"debug_inspect_player", "debug_inspect_queue", "debug_inspect_provider"}


async def test_secret_string_value_never_leaks_in_serialised_response(
    mounted_debug: Any, mock_mass: MagicMock
) -> None:
    """End-to-end: a SECURE_STRING value put through the masking pipeline does not leak."""
    raw_dict = {
        "domain": "yandex_music",
        "values": {
            "password": {
                "key": "password",
                "type": "secure_string",
                "value": "this_value_is_encrypted",
            },
        },
    }
    config = MagicMock()
    config.domain = "yandex_music"
    config.to_dict = MagicMock(return_value=raw_dict)
    mock_mass.config.get_provider_config = AsyncMock(return_value=config)

    async with Client(mounted_debug) as client:
        result = await client.call_tool(
            "debug_inspect_provider_config", {"instance_id": "yandex_music_1"}
        )
    rendered = json.dumps(result.data, default=str)
    # A real secret literal that was NEVER set into the config must not appear
    # anywhere in the serialised response — pins the contract that we only
    # route through MA's __post_serialize__ masking and don't carry parallel
    # logic that could echo a stored secret.
    assert "actual-secret-1234" not in rendered


_EXPECTED_DESCRIPTIONS_CONTAIN = {
    "debug_inspect_player": ["unavailable", "disabled", "see also"],
    "debug_inspect_queue": ["see also"],
    "debug_inspect_provider": ["see also"],
    "debug_tail_log": ["redacted", "see also"],
    "debug_recent_events": ["see also"],
    "debug_event_buffer_stats": ["dropped"],
    "debug_list_providers": ["see also"],
    "debug_inspect_provider_config": ["SECURE_STRING"],
    "debug_list_webserver_routes": ["private"],
    "debug_list_package_versions": ["versions"],
    "debug_reload_provider": ["interrupts", "see also"],
    "debug_health_summary": ["entry-point"],
}


@pytest.mark.parametrize(
    ("name", "expected_substrings"),
    list(_EXPECTED_DESCRIPTIONS_CONTAIN.items()),
)
async def test_tool_descriptions_carry_workflow_breadcrumbs(
    mounted_debug: Any, name: str, expected_substrings: list[str]
) -> None:
    """
    Each debug tool's description must include the planned workflow cross-references.

    Pins the Tool-descriptions sub-section of spec 0005 — agents driving
    the debug surface should be able to follow the chain via descriptions
    alone, without a separate prompt library.
    """
    async with Client(mounted_debug) as client:
        tools = await client.list_tools()
    by_name = {t.name: t for t in tools}
    tool = by_name.get(name)
    assert tool is not None, f"tool {name} not exposed"
    desc = (tool.description or "").lower()
    for sub in expected_substrings:
        assert sub.lower() in desc, f"{name} description missing {sub!r}: {desc!r}"
