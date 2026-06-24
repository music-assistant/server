"""Cross-cutting config security tests."""

from __future__ import annotations

import contextlib
import logging
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from music_assistant.providers.fastmcp_server.middleware import TagFilterMiddleware
from music_assistant.providers.fastmcp_server.server import build_tag_lookup
from music_assistant.providers.fastmcp_server.tags import Tag
from music_assistant.providers.fastmcp_server.tools.config import build_config_server


async def test_off_by_default_hides_all_config_tools(mounted_config_off: Any) -> None:
    """Tag.CONFIG_* all disabled -> zero config tools visible via list_tools()."""
    async with Client(mounted_config_off) as client:
        tools = await client.list_tools()
    leaked = [t.name for t in tools if t.name.startswith("config_")]
    assert leaked == [], f"config tools leaked: {leaked}"


async def test_enabling_read_tag_exposes_only_read_tools(mock_mass: Any) -> None:
    """With only Tag.CONFIG_READ allowed, only the 6 read tools are visible."""
    mcp = FastMCP(name="t")
    mcp.mount(build_config_server(mock_mass, require_confirmation=False), namespace="config")
    mcp.add_middleware(TagFilterMiddleware(lambda: {Tag.CONFIG_READ.value}, build_tag_lookup(mcp)))
    try:
        async with Client(mcp) as client:
            names = {t.name for t in await client.list_tools() if t.name.startswith("config_")}
    finally:
        close = getattr(mcp, "close", None) or getattr(mcp, "shutdown", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()
    assert names == {
        "config_list_targets",
        "config_get_provider",
        "config_get_core",
        "config_get_player",
        "config_get_entries",
        "config_get_dsp",
    }


async def test_audit_log_written_before_save(mock_config_targets: Any, caplog: Any) -> None:
    """Audit log is written before _do_save, with key but never value."""
    # mounted_config fixture uses the bare mock_mass, but we need config mocks
    # so we build our own here with mock_config_targets (which has config mocks set up)
    mcp = FastMCP(name="test")
    mcp.mount(
        build_config_server(mock_config_targets, require_confirmation=False), namespace="config"
    )
    with caplog.at_level(logging.INFO, logger="music_assistant.providers.fastmcp_server.config"):
        try:
            async with Client(mcp) as client:
                await client.call_tool(
                    "config_set_provider_value",
                    {"instance_id": "yandex_music", "key": "log_level", "value": "DEBUG"},
                )
        finally:
            close = getattr(mcp, "close", None) or getattr(mcp, "shutdown", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    close()
    audit = [r for r in caplog.records if "config_write" in r.message]
    assert audit, "expected audit line"
    assert "log_level" in audit[0].message
    # The value "DEBUG" must NOT appear in the audit message (key only).
    assert "DEBUG" not in audit[0].message


async def test_dry_run_not_audited(mock_config_targets: Any, caplog: Any) -> None:
    """dry_run=True skips audit logging."""
    mcp = FastMCP(name="test")
    mcp.mount(
        build_config_server(mock_config_targets, require_confirmation=False), namespace="config"
    )
    with caplog.at_level(logging.INFO, logger="music_assistant.providers.fastmcp_server.config"):
        try:
            async with Client(mcp) as client:
                await client.call_tool(
                    "config_set_provider_value",
                    {
                        "instance_id": "yandex_music",
                        "key": "log_level",
                        "value": "DEBUG",
                        "dry_run": True,
                    },
                )
        finally:
            close = getattr(mcp, "close", None) or getattr(mcp, "shutdown", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    close()
    assert not [r for r in caplog.records if "config_write" in r.message]


_EXPECTED_DESC = {
    "config_get_provider": ["see also"],
    "config_set_provider_value": ["dry_run", "config:write:secret"],
    "config_set_core_value": ["restart"],
    "config_get_dsp": ["dsp"],
    "config_save_dsp": ["dspconfig"],
    "config_trigger_provider_action": ["confirmation"],
}


@pytest.mark.parametrize(("name", "subs"), list(_EXPECTED_DESC.items()))
async def test_tool_descriptions_carry_breadcrumbs(
    mounted_config: Any, name: str, subs: list[str]
) -> None:
    """Each config tool's description must include the planned workflow cross-references."""
    async with Client(mounted_config) as client:
        tools = {t.name: t for t in await client.list_tools()}
    assert name in tools, f"{name} not exposed"
    desc = (tools[name].description or "").lower()
    for s in subs:
        assert s.lower() in desc, f"{name} desc missing {s!r}: {desc!r}"
