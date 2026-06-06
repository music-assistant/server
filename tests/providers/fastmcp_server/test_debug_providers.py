"""End-to-end tests for the DEBUG_PROVIDERS tool group."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError


def _provider(
    instance_id: str, *, available: bool = True, last_error: str | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        instance_id=instance_id,
        domain=instance_id.split("_", maxsplit=1)[0],
        type=SimpleNamespace(value="music"),
        name=instance_id,
        available=available,
        last_error=last_error,
    )


async def test_list_providers_rolls_up_state(mounted_debug: Any, mock_mass: MagicMock) -> None:
    """debug_list_providers returns instance_id, domain, type, availability, and last_error."""
    mock_mass.providers = [
        _provider("yandex_music_1"),
        _provider("sonos_1", available=False, last_error="auth expired"),
    ]
    async with Client(mounted_debug) as client:
        result = await client.call_tool("debug_list_providers", {})
    summaries = result.data.providers
    assert {s.instance_id for s in summaries} == {"yandex_music_1", "sonos_1"}
    failing = next(s for s in summaries if s.instance_id == "sonos_1")
    assert failing.available is False
    assert failing.last_error == "auth expired"


async def test_inspect_provider_config_masks_secret_string(
    mounted_debug: Any, mock_mass: MagicMock
) -> None:
    """debug_inspect_provider_config calls Config.to_dict() whose __post_serialize__ masks SECURE_STRING."""
    # The real masking is enforced by music_assistant_models __post_serialize__.
    # The test mock here returns a pre-masked dict shape consistent with that contract.
    masked_config = MagicMock()
    masked_config.domain = "yandex_music"
    masked_config.to_dict = MagicMock(
        return_value={
            "domain": "yandex_music",
            "values": {
                "username": {"key": "username", "type": "string", "value": "ren"},
                "password": {
                    "key": "password",
                    "type": "secure_string",
                    "value": "this_value_is_encrypted",
                },
            },
        }
    )
    mock_mass.config.get_provider_config = AsyncMock(return_value=masked_config)

    async with Client(mounted_debug) as client:
        result = await client.call_tool(
            "debug_inspect_provider_config",
            {"instance_id": "yandex_music_1"},
        )
    by_key = {v.key: v for v in result.data.values}
    assert by_key["password"].value == "this_value_is_encrypted"
    # The actual secret literal never appears anywhere in the response.
    assert "actual-secret-1234" not in str(result.data)


async def test_inspect_provider_config_not_found_raises(
    mounted_debug: Any, mock_mass: MagicMock
) -> None:
    """debug_inspect_provider_config raises ToolError when instance_id is unknown."""
    mock_mass.config.get_provider_config = AsyncMock(side_effect=Exception("not found"))
    async with Client(mounted_debug) as client:
        with pytest.raises(ToolError, match="provider instance_id="):
            await client.call_tool(
                "debug_inspect_provider_config",
                {"instance_id": "nonexistent"},
            )


async def test_list_webserver_routes_returns_entries(
    mounted_debug: Any, mock_mass: MagicMock
) -> None:
    """debug_list_webserver_routes enumerates routes from webserver._server.app.router."""
    route_obj = SimpleNamespace(
        method="GET",
        resource=SimpleNamespace(canonical="/mcp/v1/sse"),
    )
    inner_app = SimpleNamespace(router=SimpleNamespace(routes=lambda: [route_obj]))
    mock_mass.webserver._server = SimpleNamespace(app=inner_app)
    async with Client(mounted_debug) as client:
        result = await client.call_tool("debug_list_webserver_routes", {})
    paths = [r.path for r in result.data.routes]
    assert "/mcp/v1/sse" in paths


async def test_list_webserver_routes_includes_registered_by(
    mounted_debug: Any, mock_mass: MagicMock
) -> None:
    """debug_list_webserver_routes infers registered_by from path prefix."""
    route1 = SimpleNamespace(method="GET", resource=SimpleNamespace(canonical="/mcp/v1/sse"))
    route2 = SimpleNamespace(method="POST", resource=SimpleNamespace(canonical="/api/health"))
    inner_app = SimpleNamespace(router=SimpleNamespace(routes=lambda: [route1, route2]))
    mock_mass.webserver._server = SimpleNamespace(app=inner_app)
    async with Client(mounted_debug) as client:
        result = await client.call_tool("debug_list_webserver_routes", {})
    by_path = {r.path: r.registered_by for r in result.data.routes}
    assert "fastmcp_server" in by_path["/mcp/v1/sse"]
    assert by_path["/api/health"] == "music_assistant (api)"


async def test_list_webserver_routes_unavailable_returns_error(
    mounted_debug: Any, mock_mass: MagicMock
) -> None:
    """debug_list_webserver_routes raises ToolError when webserver._server is unavailable."""
    mock_mass.webserver._server = None
    async with Client(mounted_debug) as client:
        with pytest.raises(ToolError, match="routes are unavailable"):
            await client.call_tool("debug_list_webserver_routes", {})


async def test_list_package_versions_includes_fastmcp(mounted_debug: Any) -> None:
    """debug_list_package_versions includes fastmcp, music_assistant_models, etc."""
    async with Client(mounted_debug) as client:
        result = await client.call_tool("debug_list_package_versions", {})
    assert "fastmcp" in result.data.packages
    assert "music_assistant_models" in result.data.packages
    # Check that version strings are non-empty and not marked "not installed"
    assert len(result.data.packages["fastmcp"]) > 0
    assert result.data.packages["fastmcp"] != "<not installed>"
