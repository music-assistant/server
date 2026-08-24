"""End-to-end tests for debug_reload_provider."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from music_assistant.providers.fastmcp_server.tools import debug as debug_module
from music_assistant.providers.fastmcp_server.tools.debug import build_debug_server


def _provider_config(instance_id: str = "yandex_music_1") -> SimpleNamespace:
    return SimpleNamespace(instance_id=instance_id, domain="yandex_music", enabled=True)


def _decliner() -> object:
    """Elicitation handler that always declines — mirrors tests/test_elicitation.py."""
    from fastmcp.client.elicitation import ElicitResult  # noqa: PLC0415

    async def handler(*args: Any, **kwargs: Any) -> ElicitResult:  # noqa: ARG001
        return ElicitResult(action="decline", content=None)

    return handler


async def test_reload_requires_confirmation(mock_mass: MagicMock) -> None:
    """When the client declines the elicit prompt, the tool errors and never loads."""
    mass = mock_mass
    mass._load_provider = AsyncMock()
    mass.config.get_provider_config = AsyncMock(return_value=_provider_config())

    mcp = FastMCP(name="test")
    mcp.mount(
        build_debug_server(mass, require_confirmation=True),
        namespace="debug",
    )

    async with Client(mcp, elicitation_handler=_decliner()) as client:
        with pytest.raises(ToolError, match="Operation cancelled"):
            await client.call_tool(
                "debug_reload_provider",
                {"instance_id": "yandex_music_1"},
            )

    assert mass._load_provider.called is False


async def test_reload_calls_load_provider_with_resolved_config(mock_mass: MagicMock) -> None:
    """Tool passes resolved config to mass._load_provider and reports new availability."""
    mass = mock_mass
    conf = _provider_config()
    mass.config.get_provider_config = AsyncMock(return_value=conf)
    mass._load_provider = AsyncMock()
    available_prov = SimpleNamespace(available=True, last_error=None)
    mass.get_provider = MagicMock(return_value=available_prov)

    mcp = FastMCP(name="test")
    mcp.mount(
        build_debug_server(mass, require_confirmation=False),
        namespace="debug",
    )

    async with Client(mcp) as client:
        result = await client.call_tool(
            "debug_reload_provider",
            {"instance_id": "yandex_music_1"},
        )

    mass._load_provider.assert_awaited_once_with(conf)
    assert result.data.new_available is True
    assert result.data.last_error is None


async def test_reload_timeout_populates_last_error(
    mock_mass: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tool reports last_error when provider fails to become available within timeout."""
    mass = mock_mass
    mass.config.get_provider_config = AsyncMock(return_value=_provider_config())
    mass._load_provider = AsyncMock()
    not_ready = SimpleNamespace(available=False, last_error="setup pending")
    mass.get_provider = MagicMock(return_value=not_ready)

    # Speed the 5s poll up so the test stays fast.
    # Use ``from music_assistant.providers.fastmcp_server.tools import debug`` (not ``import music_assistant.providers.fastmcp_server.tools.debug as``):
    # the upstream import-path rewrite only translates ``from music_assistant.providers.fastmcp_server.`` imports.
    from music_assistant.providers.fastmcp_server.tools import debug as debug_mod  # noqa: PLC0415

    monkeypatch.setattr(debug_mod, "_RELOAD_POLL_SECONDS", 0.05)
    monkeypatch.setattr(debug_mod, "_RELOAD_POLL_INTERVAL", 0.005)

    mcp = FastMCP(name="test")
    mcp.mount(
        build_debug_server(mass, require_confirmation=False),
        namespace="debug",
    )

    async with Client(mcp) as client:
        result = await client.call_tool(
            "debug_reload_provider",
            {"instance_id": "yandex_music_1"},
        )
    assert result.data.new_available is False
    assert result.data.last_error == "setup pending"


async def test_audit_log_written_before_load_provider(
    mock_mass: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """Audit log is written before mass._load_provider is called."""
    mass = mock_mass
    mass.config.get_provider_config = AsyncMock(return_value=_provider_config())
    call_order: list[str] = []

    async def _load(_conf: Any) -> None:
        call_order.append("load_provider")

    mass._load_provider = AsyncMock(side_effect=_load)
    mass.get_provider = MagicMock(return_value=SimpleNamespace(available=True, last_error=None))

    mcp = FastMCP(name="test")
    mcp.mount(
        build_debug_server(mass, require_confirmation=False),
        namespace="debug",
    )

    with caplog.at_level(logging.INFO, logger="music_assistant.providers.fastmcp_server.debug"):
        async with Client(mcp) as client:
            await client.call_tool("debug_reload_provider", {"instance_id": "yandex_music_1"})
    audit_records = [r for r in caplog.records if "reload" in r.message.lower()]
    assert audit_records, "expected an audit log line"
    assert "yandex_music_1" in audit_records[0].message
    assert call_order == ["load_provider"]


async def test_concurrent_reloads_serialise_through_lock(mock_mass: MagicMock) -> None:
    """Concurrent reload calls serialize through global lock."""
    mass = mock_mass
    mass.config.get_provider_config = AsyncMock(return_value=_provider_config())
    mass.get_provider = MagicMock(return_value=SimpleNamespace(available=True, last_error=None))
    in_flight = 0
    peak = 0

    async def _load(_conf: Any) -> None:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1

    mass._load_provider = AsyncMock(side_effect=_load)

    mcp = FastMCP(name="test")
    mcp.mount(
        build_debug_server(mass, require_confirmation=False),
        namespace="debug",
    )

    async with Client(mcp) as client:
        await asyncio.gather(
            client.call_tool("debug_reload_provider", {"instance_id": "yandex_music_1"}),
            client.call_tool("debug_reload_provider", {"instance_id": "yandex_music_1"}),
        )
    assert peak == 1, "lock failed to serialise concurrent reloads"


async def test_reload_tool_uses_interactive_timeout() -> None:
    """
    debug_reload_provider must use interactive timeout for the confirmation round-trip.

    The tool elicits confirmation and polls up to 5s for reload; a 10s timeout
    is too short for the confirmation round-trip to complete. Regression for
    a live timeout-mid-confirmation bug.
    """
    from music_assistant.providers.fastmcp_server.tools._common import (  # noqa: PLC0415
        TIMEOUT_FAST,
        TIMEOUT_INTERACTIVE,
    )

    mass = MagicMock()
    sub = build_debug_server(mass, require_confirmation=True)
    tools = await sub.list_tools()
    tools_by_name = {tool.name: tool for tool in tools}

    assert "reload_provider" in tools_by_name
    reload_tool = tools_by_name["reload_provider"]
    assert reload_tool.timeout == TIMEOUT_INTERACTIVE, (
        f"reload_provider should use TIMEOUT_INTERACTIVE (120s), got {reload_tool.timeout}s"
    )

    # Also verify other debug tools still use TIMEOUT_FAST
    other_inspect_tools = {
        "inspect_player",
        "inspect_queue",
        "inspect_provider",
        "recent_events",
        "event_buffer_stats",
        "list_providers",
        "inspect_provider_config",
        "list_webserver_routes",
        "list_package_versions",
        "health_summary",
        "tail_log",
    }
    for name in other_inspect_tools:
        if name in tools_by_name:
            tool = tools_by_name[name]
            assert tool.timeout == TIMEOUT_FAST, (
                f"{name} should keep TIMEOUT_FAST (10s), got {tool.timeout}s"
            )


async def test_reload_serialises_on_injected_lock(mock_mass: MagicMock) -> None:
    """
    The reload serialises on the per-runtime lock passed to build_debug_server.

    Holding the injected lock blocks the reload from reaching ``_load_provider``;
    releasing it lets the reload proceed. This pins that the lock is the
    injected per-runtime one, not module-level state shared across runtimes.
    """
    mass = mock_mass
    mass.config.get_provider_config = AsyncMock(return_value=_provider_config())
    mass._load_provider = AsyncMock()
    mass.get_provider = MagicMock(return_value=SimpleNamespace(available=True, last_error=None))

    lock = asyncio.Lock()
    mcp = FastMCP(name="test")
    mcp.mount(
        build_debug_server(mass, require_confirmation=False, reload_lock=lock),
        namespace="debug",
    )

    await lock.acquire()
    try:
        async with Client(mcp) as client:
            task = asyncio.create_task(
                client.call_tool("debug_reload_provider", {"instance_id": "yandex_music_1"})
            )
            await asyncio.sleep(0.1)
            assert not task.done()
            assert mass._load_provider.called is False
            lock.release()
            await task
    finally:
        if lock.locked():
            lock.release()
    assert mass._load_provider.called is True


def test_no_module_level_reload_lock() -> None:
    """Regression guard: the reload lock is per-runtime, not shared module state."""
    assert not hasattr(debug_module, "_RELOAD_LOCK")
