"""Compatibility-level tests for the permanent meta-discovery surface."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from mcp.shared.exceptions import McpError

from music_assistant.providers.fastmcp_server import meta_discovery
from music_assistant.providers.fastmcp_server.config import build_config_entries
from music_assistant.providers.fastmcp_server.constants import DEFAULT_MOUNT_PATH
from music_assistant.providers.fastmcp_server.dynamic_api import (
    CatalogSnapshot,
    CatalogView,
    DynamicEntry,
    RequestCatalogContext,
)
from music_assistant.providers.fastmcp_server.meta_discovery import register_meta_discovery
from music_assistant.providers.fastmcp_server.middleware import TagFilterMiddleware
from music_assistant.providers.fastmcp_server.policy import PolicyProfile, policy_snapshot
from music_assistant.providers.fastmcp_server.server import build_tag_lookup


@dataclass
class _Adapter:
    """Minimal catalog adapter for direct-tool integration tests."""

    _snapshot = CatalogSnapshot((1, "test", ()), ())

    async def base_snapshot(self) -> CatalogSnapshot:
        """Return the empty immutable base catalog."""
        return self._snapshot

    async def visible_catalog(self) -> CatalogView:
        """Return the empty request-filtered catalog."""
        return CatalogView(self._snapshot.fingerprint, ())

    async def catalog_context(self) -> RequestCatalogContext:
        """Return one empty same-generation request context."""
        return RequestCatalogContext(
            self._snapshot,
            CatalogView(self._snapshot.fingerprint, ()),
        )

    async def visible_entries(self) -> list[DynamicEntry]:
        """Return an empty dynamic catalog."""
        return []

    async def get_visible_entry(self, name: str) -> DynamicEntry | None:
        """Resolve no names."""
        del name
        return None

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        response_mode: str,
        fields: list[str] | None,
        max_items: int | None,
        ctx: Any,
    ) -> dict[str, Any]:
        """Reject calls because the adapter is intentionally empty."""
        del name, arguments, response_mode, fields, max_items, ctx
        raise AssertionError("unreachable")


@dataclass(frozen=True, slots=True)
class _RecordedCall:
    """One adapter invocation captured after MCP-boundary normalization."""

    name: str
    arguments: dict[str, Any]
    response_mode: str
    fields: list[str] | None
    max_items: int | None


class _RecordingAdapter(_Adapter):
    """Record call-tool inputs that crossed the real FastMCP transport."""

    def __init__(self) -> None:
        self.calls: list[_RecordedCall] = []

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        response_mode: str,
        fields: list[str] | None,
        max_items: int | None,
        ctx: Any,
    ) -> dict[str, Any]:
        """Capture normalized containers and return a minimal envelope."""
        del ctx
        self.calls.append(_RecordedCall(name, arguments, response_mode, fields, max_items))
        return {"ok": True}


def _recording_server() -> tuple[FastMCP, _RecordingAdapter]:
    """Build a call-tool server with an adapter-side transport probe."""
    mcp: FastMCP = FastMCP(name="call-tool-test")
    adapter = _RecordingAdapter()
    register_meta_discovery(mcp, dynamic_adapter=adapter)
    return mcp, adapter


async def test_registers_exactly_three_real_tools() -> None:
    """The direct MCP registration exposes no transform-time virtual tools."""
    mcp: FastMCP = FastMCP(name="test")

    @mcp.tool
    async def old_tool() -> None:
        """Former public tool."""

    register_meta_discovery(
        mcp,
        dynamic_adapter=_Adapter(),
    )
    async with Client(mcp) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert names == {"search_tools", "call_tool", "get_tool_schema"}


async def test_call_tool_decodes_json_encoded_containers_once() -> None:
    """Stringified top-level containers reach the adapter as native values."""
    mcp, adapter = _recording_server()
    async with Client(mcp) as client:
        await client.call_tool(
            "call_tool",
            {
                "name": "ma_api:music/search",
                "arguments": '{"query":"jazz","nested":"[1]"}',
                "fields": '["name","uri"]',
            },
        )

    assert adapter.calls == [
        _RecordedCall(
            "ma_api:music/search",
            {"query": "jazz", "nested": "[1]"},
            "compact",
            ["name", "uri"],
            None,
        )
    ]


@pytest.mark.parametrize(
    ("arguments", "fields", "expected_arguments", "expected_fields"),
    [
        ({"query": "jazz"}, ["name"], {"query": "jazz"}, ["name"]),
        (None, None, {}, None),
    ],
)
async def test_call_tool_preserves_native_containers_and_defaults(
    arguments: object,
    fields: object,
    expected_arguments: dict[str, Any],
    expected_fields: list[str] | None,
) -> None:
    """Compatibility decoding does not alter native values or None defaults."""
    mcp, adapter = _recording_server()
    async with Client(mcp) as client:
        await client.call_tool(
            "call_tool",
            {
                "name": "ma_api:music/search",
                "arguments": arguments,
                "fields": fields,
            },
        )

    assert adapter.calls[0].arguments == expected_arguments
    assert adapter.calls[0].fields == expected_fields


@pytest.mark.parametrize(
    ("parameter", "value", "message"),
    [
        (
            "arguments",
            '{"private":"secret-274-payload"',
            "arguments must be an object or a JSON-encoded object",
        ),
        (
            "arguments",
            "secret-274-payload",
            "arguments must be an object or a JSON-encoded object",
        ),
        (
            "arguments",
            "null",
            "arguments must be an object or a JSON-encoded object",
        ),
        (
            "arguments",
            '["secret-274-payload"]',
            "arguments must be an object or a JSON-encoded object",
        ),
        (
            "arguments",
            ["secret-274-payload"],
            "arguments must be an object or a JSON-encoded object",
        ),
        (
            "arguments",
            '{"private":"secret-274-payload","value":NaN}',
            "arguments must be an object or a JSON-encoded object",
        ),
        (
            "fields",
            '["name","secret-274-payload"',
            "fields must be an array of strings or a JSON-encoded array of strings",
        ),
        (
            "fields",
            "secret-274-payload",
            "fields must be an array of strings or a JSON-encoded array of strings",
        ),
        (
            "fields",
            "null",
            "fields must be an array of strings or a JSON-encoded array of strings",
        ),
        (
            "fields",
            '{"private":"secret-274-payload"}',
            "fields must be an array of strings or a JSON-encoded array of strings",
        ),
        (
            "fields",
            ["name", 274, "secret-274-payload"],
            "fields must be an array of strings or a JSON-encoded array of strings",
        ),
        (
            "fields",
            '["name",274,"secret-274-payload"]',
            "fields must be an array of strings or a JSON-encoded array of strings",
        ),
    ],
)
async def test_call_tool_rejects_invalid_containers_without_echoing_payload(
    parameter: str,
    value: object,
    message: str,
) -> None:
    """Invalid compatibility inputs produce stable redacted tool errors."""
    mcp, adapter = _recording_server()
    async with Client(mcp) as client:
        with pytest.raises(ToolError) as raised:
            await client.call_tool(
                "call_tool",
                {"name": "ma_api:music/search", parameter: value},
            )

    assert str(raised.value) == f"[invalid_arguments] {message}"
    assert "secret-274-payload" not in str(raised.value)
    assert "Traceback" not in str(raised.value)
    assert adapter.calls == []


async def test_call_tool_schema_keeps_container_only_contract() -> None:
    """Compatibility decoding does not advertise string-valued containers."""
    mcp, _adapter = _recording_server()
    async with Client(mcp) as client:
        tool = next(item for item in await client.list_tools() if item.name == "call_tool")

    arguments = tool.inputSchema["properties"]["arguments"]
    fields = tool.inputSchema["properties"]["fields"]
    assert {variant["type"] for variant in arguments["anyOf"]} == {"object", "null"}
    assert {variant["type"] for variant in fields["anyOf"]} == {"array", "null"}
    array_schema = next(variant for variant in fields["anyOf"] if variant["type"] == "array")
    assert array_schema["items"] == {"type": "string"}


async def test_catalog_resource_template_is_discoverable_and_matches_tool_browse() -> None:
    """The catalog resource exposes the same first page as empty tool browse."""
    mcp: FastMCP = FastMCP(name="test")
    register_meta_discovery(
        mcp,
        dynamic_adapter=_Adapter(),
    )
    async with Client(mcp) as client:
        templates = {str(item.uriTemplate) for item in await client.list_resource_templates()}
        tool_page = await client.call_tool("search_tools", {"query": "", "limit": 25})
        contents = await client.read_resource("catalog://commands?limit=25")
    payload = json.loads(next(item.text for item in contents if hasattr(item, "text")))
    assert "catalog://commands{?cursor,limit}" in templates
    assert tool_page.structured_content is not None
    assert payload["items"] == tool_page.structured_content["items"]
    assert payload["total"] == tool_page.structured_content["total"]
    assert payload["catalog_revision"] == tool_page.structured_content["catalog_revision"]
    assert payload["next_uri"] is None
    assert set(payload) == {
        "items",
        "total",
        "next_cursor",
        "next_uri",
        "catalog_revision",
    }


class _CatalogAdapter(_Adapter):
    """Expose a fixed multi-entry catalog for resource paging tests."""

    def __init__(self, entry_count: int) -> None:
        entries = tuple(
            DynamicEntry(
                name=f"ma_api:music/command_{index:02d}",
                command=f"music/command_{index:02d}",
                description=f"Music command {index}",
                input_schema={"type": "object", "properties": {}},
                required_scope=None,
                allow_impersonation=False,
                handler=object(),
            )
            for index in range(entry_count)
        )
        self._snapshot = CatalogSnapshot(
            (
                1,
                "catalog",
                tuple((entry.command, index + 1) for index, entry in enumerate(entries)),
            ),
            entries,
        )

    async def visible_catalog(self) -> CatalogView:
        """Make every catalog fixture entry visible."""
        return CatalogView(self._snapshot.fingerprint, self._snapshot.entries)

    async def catalog_context(self) -> RequestCatalogContext:
        """Return the fixed catalog and matching all-visible view."""
        return RequestCatalogContext(
            self._snapshot,
            CatalogView(self._snapshot.fingerprint, self._snapshot.entries),
        )


def _catalog_server(entry_count: int, *, middleware: bool = False) -> FastMCP:
    """Build a resource-enabled catalog server, optionally with empty permissions."""
    mcp: FastMCP = FastMCP(name="catalog-test")
    register_meta_discovery(
        mcp,
        dynamic_adapter=_CatalogAdapter(entry_count),
    )
    if middleware:
        mcp.add_middleware(
            TagFilterMiddleware(
                build_tag_lookup(mcp),
                lambda: policy_snapshot(PolicyProfile.SAFE_QUERIES),
            )
        )
    return mcp


async def test_catalog_resource_next_uri_reads_the_next_page() -> None:
    """The advertised resource URI resumes the alphabetical catalog page."""
    mcp = _catalog_server(entry_count=3)
    async with Client(mcp) as client:
        first_contents = await client.read_resource("catalog://commands?limit=2")
        first = json.loads(next(item.text for item in first_contents if hasattr(item, "text")))
        second_contents = await client.read_resource(first["next_uri"])
    second = json.loads(next(item.text for item in second_contents if hasattr(item, "text")))
    assert len(first["items"]) == 2
    assert len(second["items"]) == 1
    assert second["next_cursor"] is None
    assert second["next_uri"] is None
    assert [item["name"] for item in first["items"] + second["items"]] == sorted(
        item["name"] for item in first["items"] + second["items"]
    )


async def test_catalog_resource_rejects_search_cursor() -> None:
    """Search continuations cannot be replayed through the catalog resource."""
    mcp = _catalog_server(entry_count=3)
    async with Client(mcp) as client:
        search = await client.call_tool("search_tools", {"query": "music", "limit": 1})
        assert search.structured_content is not None
        with pytest.raises(McpError, match="invalid_cursor"):
            await client.read_resource(
                f"catalog://commands?{urlencode({'cursor': search.structured_content['next_cursor']})}"
            )


async def test_untagged_catalog_resource_survives_empty_tag_middleware() -> None:
    """The catalog resource remains visible as untagged infrastructure."""
    mcp = _catalog_server(entry_count=0, middleware=True)
    async with Client(mcp) as client:
        templates = {str(item.uriTemplate) for item in await client.list_resource_templates()}
        contents = await client.read_resource("catalog://commands")
    page = json.loads(next(item.text for item in contents if hasattr(item, "text")))
    assert "catalog://commands{?cursor,limit}" in templates
    assert page["items"] == []
    assert page["total"] == 0


def test_meta_discovery_service_is_a_direct_index_owner() -> None:
    """Search indexing belongs to a service, rather than a FastMCP transform."""
    assert getattr(meta_discovery, "MetaDiscoveryService", None) is not None


def test_dynamic_risk_gate_entries_are_removed(mock_mass: Any) -> None:
    """V2 command behavior no longer exposes legacy dynamic risk gates."""
    entries = {entry.key: entry for entry in build_config_entries(mock_mass, DEFAULT_MOUNT_PATH)}
    assert {
        "dynamic_api_read",
        "dynamic_api_control",
        "dynamic_api_write",
        "dynamic_api_system",
    }.isdisjoint(entries)


def test_dynamic_entry_type_carries_no_classifier_risk_gate() -> None:
    """Discovery descriptors do not expose the removed v1 risk class."""
    assert "risk" not in DynamicEntry.__dataclass_fields__
