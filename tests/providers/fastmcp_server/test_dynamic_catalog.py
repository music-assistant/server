"""Contract tests for the permanent dynamic MA API catalog."""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from fastmcp import Client, Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken
from music_assistant_models.auth import AuthProviderType, Scope
from music_assistant_models.config_entries import ConfigActionResult, ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.providers.fastmcp_server import dynamic_serialization, meta_discovery
from music_assistant.providers.fastmcp_server.capabilities import Capability
from music_assistant.providers.fastmcp_server.catalog import (
    CatalogSnapshot,
    CatalogView,
    DynamicEntry,
    RequestCatalogContext,
)
from music_assistant.providers.fastmcp_server.catalog_pagination import (
    PaginationError,
    decode_cursor,
    encode_cursor,
)
from music_assistant.providers.fastmcp_server.command_profiles import (
    COMMAND_PROFILES,
    CURATED_PROFILE_MAPPINGS,
    CommandProfile,
)
from music_assistant.providers.fastmcp_server.dynamic_serialization import (
    _encoded_size,
    fit_json_envelope,
)
from music_assistant.providers.fastmcp_server.execution import DynamicAPIAdapter
from music_assistant.providers.fastmcp_server.meta_discovery import (
    DynamicAdapter,
    register_meta_discovery,
)
from music_assistant.providers.fastmcp_server.policy import (
    PolicyMode,
    PolicyProfile,
    policy_snapshot,
)
from music_assistant.providers.fastmcp_server.token_identity import TokenIdentity

_META_NAMES = {"search_tools", "call_tool", "get_tool_schema"}


class _TestDynamicAPIAdapter(DynamicAPIAdapter):  # type: ignore[misc, unused-ignore]
    """Adapter with a mutable test-only policy source."""

    _test_allowed_capabilities_provider: Callable[[], set[str]]


@dataclass
class _FakeAdapter:
    """Small adapter implementing the transform-facing dynamic API contract."""

    calls: list[tuple[str, dict[str, Any]]]

    async def base_snapshot(self) -> CatalogSnapshot:
        """Return the fake command as the immutable base catalog."""
        return CatalogSnapshot((1, "fake", ()), tuple(await self.visible_entries()))

    async def visible_catalog(self) -> CatalogView:
        """Return the fake command as visible to every request."""
        snapshot = await self.base_snapshot()
        return CatalogView(snapshot.fingerprint, snapshot.entries)

    async def catalog_context(self) -> RequestCatalogContext:
        """Return a same-generation fake request context."""
        snapshot = await self.base_snapshot()
        return RequestCatalogContext(snapshot, CatalogView(snapshot.fingerprint, snapshot.entries))

    async def visible_entries(self) -> list[DynamicEntry]:
        """Return one discoverable command."""
        return [
            DynamicEntry(
                name="ma_api:players/cmd/play",
                command="players/cmd/play",
                description="Start playback on a player.",
                input_schema={
                    "type": "object",
                    "properties": {"player_id": {"type": "string"}},
                    "required": ["player_id"],
                    "additionalProperties": False,
                },
                required_scope="players.control",
                allow_impersonation=False,
                handler=object(),
                search_aliases=("playback_play", "start music"),
                output_schema={"type": "object"},
                annotations={"readOnlyHint": False, "destructiveHint": False},
            )
        ]

    async def get_visible_entry(self, name: str) -> DynamicEntry | None:
        """Resolve the fake command by name."""
        return next(
            (entry for entry in await self.visible_entries() if entry.name == name),
            None,
        )

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
        """Record and acknowledge a fake call."""
        del fields, max_items, ctx
        self.calls.append((name, arguments))
        return {
            "command": name,
            "data": {"ok": True},
            "truncated": False,
            "returned_count": 1,
            "bytes": 11,
            "applied": {"mode": response_mode, "fields": [], "max_items": 25},
        }


def _server() -> tuple[FastMCP, _FakeAdapter]:
    """Build a root server with one legacy tool and one dynamic command."""
    mcp: FastMCP = FastMCP(name="dynamic-test")

    @mcp.tool(name="playback_play")
    async def legacy_play(player_id: str) -> None:
        """Legacy curated tool that must not remain callable."""
        del player_id

    adapter = _FakeAdapter(calls=[])
    register_meta_discovery(
        mcp,
        dynamic_adapter=adapter,
    )
    return mcp, adapter


async def test_tools_list_is_always_three_meta_tools() -> None:
    """The target surface never exposes the underlying curated catalog."""
    mcp, _adapter = _server()
    async with Client(mcp) as client:
        tools = await client.list_tools()
    assert {tool.name for tool in tools} == _META_NAMES


async def test_search_uses_alias_but_returns_canonical_ma_name() -> None:
    """Legacy terminology improves ranking without preserving the old name."""
    mcp, _adapter = _server()
    async with Client(mcp) as client:
        result = await client.call_tool("search_tools", {"query": "playback_play"})
    assert result.structured_content is not None
    assert result.structured_content["mode"] == "search"
    assert result.structured_content["items"] == [
        {
            "name": "ma_api:players/cmd/play",
            "description": "Start playback on a player.",
            "policy_mode": "confirm",
        }
    ]
    assert result.structured_content["total"] == 1
    assert result.structured_content["next_cursor"] is None
    assert result.structured_content["catalog_revision"]


async def test_search_tool_rejects_invalid_limit_with_stable_code() -> None:
    """The wire contract exposes invalid page-size errors without transport detail."""
    mcp, _adapter = _server()
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match=r"\[invalid_arguments\]"):
            await client.call_tool("search_tools", {"query": "music", "limit": 0})


@pytest.mark.parametrize("limit", ["2", 2.0, True])
async def test_search_tool_rejects_non_integer_limits_with_stable_code(limit: object) -> None:
    """The MCP boundary must not coerce non-integer page sizes."""
    mcp, _adapter = _server()
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match=r"\[invalid_arguments\]"):
            await client.call_tool("search_tools", {"query": "music", "limit": limit})


async def test_search_tool_rejects_malformed_cursor_with_stable_code() -> None:
    """The wire contract exposes malformed cursor errors with a stable code."""
    mcp, _adapter = _server()
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match=r"\[invalid_arguments\]"):
            await client.call_tool("search_tools", {"cursor": "not-json"})


async def test_search_tools_schema_advertises_pagination() -> None:
    """Clients can discover the complete paginated input and output contract."""
    mcp, _adapter = _server()
    async with Client(mcp) as client:
        tool = next(item for item in await client.list_tools() if item.name == "search_tools")
    assert set(tool.inputSchema["properties"]) == {
        "query",
        "cursor",
        "limit",
        "include_top_schema",
    }
    assert tool.outputSchema is not None
    assert {"mode", "items", "total", "next_cursor", "catalog_revision"} <= set(
        tool.outputSchema["properties"]
    )


async def test_call_tool_advertises_the_command_envelope_schema() -> None:
    """call_tool's MCP output schema is the response-budget envelope, not the MA type."""
    mcp, _adapter = _server()
    async with Client(mcp) as client:
        tool = next(item for item in await client.list_tools() if item.name == "call_tool")
    assert tool.outputSchema is not None
    assert {"command", "data", "truncated", "returned_count", "bytes", "applied"} <= set(
        tool.outputSchema["properties"]
    )
    assert tool.annotations is not None
    assert tool.annotations.readOnlyHint is False


async def test_search_can_include_only_the_top_result_schema() -> None:
    """Opt-in discovery embeds one schema without expanding the whole catalog."""
    mcp, _adapter = _server()
    async with Client(mcp) as client:
        result = await client.call_tool(
            "search_tools",
            {"query": "playback", "include_top_schema": True},
        )
    assert result.structured_content is not None
    items = result.structured_content["items"]
    assert items
    assert items[0]["schema"]["name"] == items[0]["name"]
    assert all("schema" not in item for item in items[1:])


@pytest.mark.parametrize(
    "arguments",
    [
        {"include_top_schema": True},
        {"query": "playback", "cursor": "opaque", "include_top_schema": True},
    ],
)
async def test_top_schema_requires_query_without_cursor(arguments: dict[str, object]) -> None:
    """The shortcut cannot expand catalog browsing or continuation pages."""
    mcp, _adapter = _server()
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match=r"\[invalid_arguments\]"):
            await client.call_tool("search_tools", arguments)


def _meta_service(adapter: DynamicAdapter) -> Any:
    """Construct the direct discovery service after proving it is exported."""
    service_type = getattr(meta_discovery, "MetaDiscoveryService", None)
    assert service_type is not None
    return service_type(adapter)


def _catalog_entry(name: str, description: str) -> DynamicEntry:
    """Build a minimal entry for direct discovery-index tests."""
    return DynamicEntry(
        name=name,
        command=name.removeprefix("ma_api:"),
        description=description,
        input_schema={"type": "object", "properties": {}},
        required_scope=None,
        allow_impersonation=False,
        handler=object(),
    )


def _catalog_snapshot(count: int = 7) -> CatalogSnapshot:
    """Build a deterministically ordered discovery catalog."""
    entries = tuple(
        _catalog_entry(f"ma_api:music/command_{index:02d}", f"Music command {index}")
        for index in range(count)
    )
    return CatalogSnapshot(
        (1, "test", tuple((entry.command, index) for index, entry in enumerate(entries))),
        entries,
    )


class _SnapshotAdapter:
    """Expose one immutable base snapshot and matching visible view."""

    def __init__(self, snapshot: CatalogSnapshot) -> None:
        self.snapshot = snapshot

    async def base_snapshot(self) -> CatalogSnapshot:
        """Return the test snapshot."""
        return self.snapshot

    async def visible_catalog(self) -> CatalogView:
        """Make all test entries visible."""
        return CatalogView(self.snapshot.fingerprint, self.snapshot.entries)

    async def catalog_context(self) -> RequestCatalogContext:
        """Return the fixture snapshot and its matching visible view."""
        return RequestCatalogContext(
            self.snapshot,
            CatalogView(self.snapshot.fingerprint, self.snapshot.entries),
        )

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
        """Satisfy the discovery adapter protocol; these tests never execute calls."""
        del name, arguments, response_mode, fields, max_items, ctx
        raise NotImplementedError


async def test_empty_query_browses_alphabetical_catalog_without_descriptions() -> None:
    """Catalog pages expose every visible command name in stable order."""
    service = _meta_service(_SnapshotAdapter(_catalog_snapshot()))
    first = await service.discover("", limit=3)
    second = await service.discover(cursor=first["next_cursor"], limit=3)
    third = await service.discover(cursor=second["next_cursor"], limit=3)
    assert first["mode"] == second["mode"] == third["mode"] == "catalog"
    assert first["total"] == second["total"] == third["total"] == 7
    assert [item["name"] for page in (first, second, third) for item in page["items"]] == [
        f"ma_api:music/command_{index:02d}" for index in range(7)
    ]
    assert all(
        set(item) == {"name", "policy_mode"}
        for page in (first, second, third)
        for item in page["items"]
    )
    assert third["next_cursor"] is None


async def test_ranked_search_pages_have_no_duplicates_or_gaps() -> None:
    """Search continuations preserve the ranked result sequence."""
    service = _meta_service(_SnapshotAdapter(_catalog_snapshot()))
    first = await service.discover("music command", limit=2)
    second = await service.discover(cursor=first["next_cursor"], limit=2)
    assert first["mode"] == second["mode"] == "search"
    assert first["total"] == 7
    assert len({item["name"] for item in first["items"] + second["items"]}) == 4
    assert all("description" in item for item in first["items"] + second["items"])


async def test_cursor_accepts_matching_query_and_rejects_conflicting_query() -> None:
    """A continuation accepts normalized text but rejects another search."""
    service = _meta_service(_SnapshotAdapter(_catalog_snapshot()))
    first = await service.discover("music", limit=2)
    resumed = await service.discover(" MUSIC ", cursor=first["next_cursor"], limit=2)
    assert resumed["items"]
    with pytest.raises(PaginationError) as exc_info:
        await service.discover("players", cursor=first["next_cursor"])
    assert exc_info.value.code == "invalid_cursor"


async def test_catalog_change_invalidates_cursor() -> None:
    """A base-catalog revision cannot resume an earlier page sequence."""
    adapter = _SnapshotAdapter(_catalog_snapshot())
    service = _meta_service(adapter)
    first = await service.discover(limit=2)
    adapter.snapshot = _catalog_snapshot(8)
    with pytest.raises(PaginationError) as exc_info:
        await service.discover(cursor=first["next_cursor"])
    assert exc_info.value.code == "catalog_changed"


async def test_impossible_cursor_offset_is_rejected() -> None:
    """A validly encoded but out-of-range offset cannot yield an empty page."""
    service = _meta_service(_SnapshotAdapter(_catalog_snapshot(2)))
    first = await service.discover(limit=1)
    state = decode_cursor(cast("str", first["next_cursor"]))
    forged = encode_cursor(replace(state, offset=99))
    with pytest.raises(PaginationError) as exc_info:
        await service.discover(cursor=forged)
    assert exc_info.value.code == "invalid_cursor"


async def test_visibility_change_invalidates_cursor_without_leaking_hidden_name() -> None:
    """Visibility changes invalidate pages while the next browse hides removed commands."""

    class _VisibilityAdapter(_SnapshotAdapter):
        restricted = False

        async def catalog_context(self) -> RequestCatalogContext:
            entries = self.snapshot.entries[:-1] if self.restricted else self.snapshot.entries
            return RequestCatalogContext(
                self.snapshot,
                CatalogView(self.snapshot.fingerprint, entries),
            )

    adapter = _VisibilityAdapter(_catalog_snapshot())
    service = _meta_service(adapter)
    first = await service.discover(limit=2)
    hidden_name = adapter.snapshot.entries[-1].name
    adapter.restricted = True
    with pytest.raises(PaginationError) as exc_info:
        await service.discover(cursor=first["next_cursor"])
    assert exc_info.value.code == "catalog_changed"
    restricted = await service.discover(limit=50)
    assert hidden_name not in {item["name"] for item in restricted["items"]}


async def test_search_uses_one_request_catalog_context() -> None:
    """Search never composes a view and snapshot from different generations."""
    first = CatalogSnapshot(
        (1, "test", (("music/first", 1),)),
        (_catalog_entry("ma_api:music/first", "Original collection."),),
    )
    second = CatalogSnapshot(
        (1, "test", (("music/replacement", 2),)),
        (_catalog_entry("ma_api:music/replacement", "Replacement collection."),),
    )

    class _ChangingAdapter(_SnapshotAdapter):
        async def base_snapshot(self) -> CatalogSnapshot:
            raise AssertionError("discovery must not read the base snapshot separately")

        async def visible_catalog(self) -> CatalogView:
            raise AssertionError("discovery must not read the request view separately")

        async def catalog_context(self) -> RequestCatalogContext:
            return RequestCatalogContext(
                second,
                CatalogView(second.fingerprint, second.entries),
            )

    service = _meta_service(_ChangingAdapter(first))
    assert (await service.discover("replacement"))["items"] == [
        {
            "name": "ma_api:music/replacement",
            "description": "Replacement collection.",
            "policy_mode": "confirm",
        }
    ]


async def test_parallel_searches_contend_for_one_awaitable_index_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Followers wait on the cold-build lock instead of starting duplicate builds."""
    snapshot = CatalogSnapshot(
        (1, "test", (("music/search", 1),)),
        (_catalog_entry("ma_api:music/search", "Search music."),),
    )
    service = _meta_service(_SnapshotAdapter(snapshot))
    build_index = getattr(service, "_build_index", None)
    assert build_index is not None
    started = asyncio.Event()
    release = asyncio.Event()
    attempts = 0

    async def delayed_build(candidate: CatalogSnapshot) -> Any:
        nonlocal attempts
        attempts += 1
        started.set()
        await release.wait()
        return await build_index(candidate)

    monkeypatch.setattr(service, "_build_index", delayed_build)
    leader = asyncio.create_task(service.discover("search"))
    await started.wait()
    followers = [asyncio.create_task(service.discover("search")) for _index in range(19)]
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(leader, *followers)
    assert attempts == 1
    assert service.index_build_count == 1


async def test_failed_index_build_releases_waiters_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient index failure does not poison later discovery searches."""
    snapshot = CatalogSnapshot(
        (1, "test", (("music/search", 1),)),
        (_catalog_entry("ma_api:music/search", "Search music."),),
    )
    service = _meta_service(_SnapshotAdapter(snapshot))
    build_index = getattr(service, "_build_index", None)
    assert build_index is not None
    attempts = 0

    async def fail_once(candidate: CatalogSnapshot) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient index failure")
        return await build_index(candidate)

    monkeypatch.setattr(service, "_build_index", fail_once)
    outcomes = await asyncio.gather(
        service.discover("search"), service.discover("search"), return_exceptions=True
    )
    first, second = outcomes
    assert isinstance(first, RuntimeError)
    assert not isinstance(second, BaseException)
    assert second["items"] == [
        {
            "name": "ma_api:music/search",
            "description": "Search music.",
            "policy_mode": "confirm",
        }
    ]
    assert await service.discover("search") == second
    assert attempts == 2


def test_search_index_does_not_expose_mutable_token_counters() -> None:
    """Callers cannot mutate cached BM25 term frequencies between requests."""
    snapshot = CatalogSnapshot(
        (1, "test", (("music/search", 1),)),
        (_catalog_entry("ma_api:music/search", "Search music."),),
    )
    index = meta_discovery._build_search_index(snapshot)
    with pytest.raises(TypeError):
        cast("dict[str, tuple[str, ...]]", index.documents)["ma_api:other"] = ()
    frequencies = cast("dict[str, dict[str, int]]", index.frequencies)
    with pytest.raises(TypeError):
        frequencies["ma_api:music/search"]["search"] = 99


async def test_parallel_search_builds_one_index() -> None:
    """Concurrent searches share one immutable base-snapshot index."""

    async def search(album: str) -> list[str]:
        """Search albums."""
        return [album]

    adapter = _real_adapter(_handler("music/search", search))
    service = _meta_service(adapter)
    await asyncio.gather(*(service.discover("album") for _index in range(20)))
    assert service.index_build_count == 1


async def test_registry_change_rebuilds_discovery_index_immediately() -> None:
    """A new registry fingerprint invalidates the cached search index."""

    async def existing() -> None:
        """Existing endpoint."""
        return

    async def new_command() -> None:
        """Expose a new endpoint."""
        return

    adapter = _real_adapter(_handler("music/browse", existing))
    service = _meta_service(adapter)
    before = await service.discover("search")
    adapter.mass.command_handlers["music/search"] = _handler("music/search", new_command)
    after = await service.discover("search")
    assert before["items"] == []
    assert after["items"][0]["name"] == "ma_api:music/search"
    assert service.index_build_count == 2


async def test_dynamic_schema_is_returned_on_demand() -> None:
    """A dynamic command exposes its real input schema only when requested."""
    mcp, _adapter = _server()
    async with Client(mcp) as client:
        result = await client.call_tool("get_tool_schema", {"tool_name": "ma_api:players/cmd/play"})
    assert result.data["name"] == "ma_api:players/cmd/play"
    assert result.data["kind"] == "ma_api"
    assert result.data["inputSchema"]["required"] == ["player_id"]
    assert "risk" not in result.data
    assert result.data["outputSchema"]["required"] == [
        "command",
        "data",
        "truncated",
        "returned_count",
        "bytes",
        "applied",
    ]
    assert result.data["dataSchema"] == {"type": "object"}
    assert result.data["annotations"]["readOnlyHint"] is False


async def test_call_tool_routes_dynamic_name() -> None:
    """The meta proxy forwards canonical names and response options."""
    mcp, adapter = _server()
    async with Client(mcp) as client:
        result = await client.call_tool(
            "call_tool",
            {
                "name": "ma_api:players/cmd/play",
                "arguments": {"player_id": "kitchen"},
            },
        )
    payload = (
        result.structured_content if isinstance(result.structured_content, dict) else result.data
    )
    assert payload["data"] == {"ok": True}
    assert adapter.calls == [("ma_api:players/cmd/play", {"player_id": "kitchen"})]


async def test_old_curated_name_is_not_callable() -> None:
    """Legacy public names are a breaking migration, not hidden aliases."""
    mcp, _adapter = _server()
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match=r"\[invalid_arguments\]"):
            await client.call_tool("call_tool", {"name": "playback_play", "arguments": {}})
        with pytest.raises(ToolError):
            await client.call_tool("playback_play", {"player_id": "kitchen"})


async def test_meta_catalog_stays_under_three_kib() -> None:
    """The permanent public schemas stay inside the agreed context budget."""
    mcp, _adapter = _server()
    async with Client(mcp) as client:
        tools = await client.list_tools()
    payload = "".join(tool.model_dump_json() for tool in tools).encode()
    assert len(payload) <= 4096


def test_fake_handler_signature_is_stable() -> None:
    """Guard accidental widening of the fake adapter call contract."""
    assert list(inspect.signature(_FakeAdapter.call).parameters) == [
        "self",
        "name",
        "arguments",
        "response_mode",
        "fields",
        "max_items",
        "ctx",
    ]


def _handler(command: str, target: Any, scope: str = "library.read") -> Any:
    """Build the stable subset of MA's APICommandHandler contract."""
    return SimpleNamespace(
        command=command,
        signature=inspect.signature(target),
        type_hints=target.__annotations__,
        target=target,
        authenticated=True,
        required_scope=scope,
        allow_impersonation=False,
        alias=False,
    )


def _real_adapter(
    handler: Any,
    *,
    scope_checker: Any = None,
    allowed_capabilities: set[str] | None = None,
    user: Any = None,
    audit_sink: Any = None,
) -> _TestDynamicAPIAdapter:
    """Build an authenticated adapter around one fake MA handler."""
    mass = MagicMock()
    mass.command_handlers = {handler.command: handler}
    user = user or MagicMock(user_id="u1", enabled=True, role="admin")
    mass.webserver.auth.authenticate_with_token = AsyncMock(return_value=user)
    mass.webserver.auth.get_token_id_from_token = AsyncMock(return_value="u1")
    token = AccessToken(token="secret", client_id="u1", scopes=[])
    adapter: _TestDynamicAPIAdapter = _TestDynamicAPIAdapter(
        mass,
        auth_required_provider=lambda: True,
        token_provider=lambda: token,
        scope_checker=scope_checker or (lambda _user, _scope: True),
        policy_provider=lambda _bearer: policy_snapshot(
            PolicyProfile.CUSTOM,
            {
                str(capability): (
                    PolicyMode.ALLOW
                    if str(capability) in adapter._test_allowed_capabilities_provider()
                    else PolicyMode.DENY
                )
                for capability in Capability
            },
        ),
        default_policy_provider=lambda: policy_snapshot(PolicyProfile.SAFE_QUERIES),
        identity_provider=lambda _bearer: TokenIdentity(str(user.user_id), "u1"),
        audit_sink=audit_sink,
    )
    adapter._test_allowed_capabilities_provider = lambda: (
        allowed_capabilities
        if allowed_capabilities is not None
        else {str(capability) for capability in Capability}
    )
    return adapter


def _bypass_ma_argument_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep authorization tests independent of MA parser optional dependencies."""
    monkeypatch.setattr(
        "music_assistant.providers.fastmcp_server.dynamic_signatures.CompiledSignature.parse",
        lambda _signature, arguments: dict(arguments),
    )


async def test_impersonation_resolves_a_builtin_ma_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy string user identifier is scoped to MA's built-in auth provider."""
    adapter = _real_adapter(_handler("music/search", lambda: None))
    expected_user = MagicMock(user_id="listener")
    resolve = AsyncMock(return_value=expected_user)
    monkeypatch.setattr(
        "music_assistant.controllers.webserver.helpers.auth_middleware.resolve_impersonated_user",
        resolve,
    )

    result = await adapter._resolve_impersonated_user(None, "listener")

    assert result is expected_user
    resolve.assert_awaited_once_with(adapter.mass, AuthProviderType.BUILTIN, "listener")


async def test_adapter_discovers_handler_and_compiles_schema() -> None:
    """The runtime registry becomes a canonical ma_api catalog entry."""

    async def search(query: str, limit: int = 5) -> list[str]:
        """Search the library."""
        return [query] * limit

    adapter = _real_adapter(_handler("music/search", search))
    entries = await adapter.visible_entries()
    assert [entry.name for entry in entries] == ["ma_api:music/search"]
    assert entries[0].input_schema["required"] == ["query"]
    assert entries[0].input_schema["properties"]["limit"]["default"] == 5


async def test_adapter_has_no_dynamic_risk_gate() -> None:
    """A classified command is not hidden behind the removed v1 risk switches."""

    async def play(player_id: str) -> None:
        del player_id

    handler = _handler("players/cmd/play", play, "players.control")
    assert len(await _real_adapter(handler).visible_entries()) == 1


async def test_adapter_observes_registry_changes_without_restart() -> None:
    """Registering and replacing handlers updates the same live adapter."""

    async def first() -> str:
        return "first"

    async def second(value: int) -> int:
        return value

    adapter = _real_adapter(_handler("music/browse", first))
    assert [entry.name for entry in await adapter.visible_entries()] == ["ma_api:music/browse"]
    adapter.mass.command_handlers = {"music/search": _handler("music/search", second)}
    entries = await adapter.visible_entries()
    assert [entry.name for entry in entries] == ["ma_api:music/search"]
    assert entries[0].input_schema["required"] == ["value"]


async def test_concurrent_catalog_reads_compile_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent cold readers share one compiled registry snapshot."""

    async def search(search_query: str) -> list[str]:
        return [search_query]

    adapter = _real_adapter(_handler("music/search", search))
    compile_spy = MagicMock(wraps=adapter._compile_entry)
    monkeypatch.setattr(adapter, "_compile_entry", compile_spy)
    snapshots = await asyncio.gather(*(adapter.base_snapshot() for _index in range(20)))
    assert all(snapshot is snapshots[0] for snapshot in snapshots)
    assert snapshots[0].entries[0].name == "ma_api:music/search"
    assert compile_spy.call_count == 1


async def test_registry_replacement_changes_fingerprint_without_restart() -> None:
    """Replacing a live handler invalidates the cached base snapshot."""

    async def search(search_query: str) -> list[str]:
        return [search_query]

    async def replacement(search_query: str, limit: int = 5) -> list[str]:
        return [search_query] * limit

    adapter = _real_adapter(_handler("music/search", search))
    first = await adapter.base_snapshot()
    replacement_handler = _handler("music/search", replacement)
    adapter.mass.command_handlers["music/search"] = replacement_handler
    second = await adapter.base_snapshot()
    assert second.fingerprint != first.fingerprint
    assert second.entries[0].handler is replacement_handler


async def test_security_and_schema_descriptors_invalidate_catalog_snapshot() -> None:
    """Changing live scope or documentation must invalidate compiled binders and schemas."""

    async def search(search_query: str) -> list[str]:
        """Original catalog description."""
        return [search_query]

    handler = _handler("music/search", search)
    adapter = _real_adapter(handler)
    first = await adapter.base_snapshot()

    handler.required_scope = "library.write"
    search.__doc__ = "Updated catalog description."
    second = await adapter.base_snapshot()

    assert second.fingerprint != first.fingerprint
    assert second.entries[0].description == "Updated catalog description."
    assert second.entries[0].required_scope == "library.write"
    assert second.entries[0].compiled_signature is not first.entries[0].compiled_signature


async def test_catalog_snapshot_has_immutable_constant_time_name_lookup() -> None:
    """Schema and execution lookup must use the snapshot's immutable name map."""

    async def search(search_query: str) -> list[str]:
        return [search_query]

    snapshot = await _real_adapter(_handler("music/search", search)).base_snapshot()

    assert snapshot.by_name["ma_api:music/search"] is snapshot.entries[0]
    with pytest.raises(TypeError):
        snapshot.by_name["ma_api:music/other"] = snapshot.entries[0]  # type: ignore[index, unused-ignore]


async def test_request_catalog_context_uses_one_registry_generation() -> None:
    """A request view and its base snapshot must be captured from one generation."""

    async def search(search_query: str) -> list[str]:
        return [search_query]

    adapter = _real_adapter(_handler("music/search", search))
    context = await adapter.catalog_context()

    assert context.snapshot.fingerprint == context.view.fingerprint
    assert context.view.by_name["ma_api:music/search"].name == "ma_api:music/search"


async def test_registry_validity_changes_fingerprint_and_base_diagnostics() -> None:
    """Empty valid and invalid registries never share cached availability state."""

    async def search(search_query: str) -> list[str]:
        return [search_query]

    adapter = _real_adapter(_handler("music/search", search))
    adapter.mass.command_handlers = {}
    valid = await adapter.base_snapshot()
    assert await adapter.visible_entries() == []
    assert adapter.diagnostics() == {
        "available": True,
        "registry_type": "dict",
        "handlers_seen": 0,
        "handlers_visible": 0,
        "incompatible_handlers": (),
        "last_error": None,
    }

    adapter.mass.command_handlers = []
    invalid = await adapter.base_snapshot()
    assert invalid.fingerprint != valid.fingerprint
    assert await adapter.visible_entries() == []
    assert adapter.diagnostics() == {
        "available": False,
        "registry_type": "list",
        "handlers_seen": 0,
        "handlers_visible": 0,
        "incompatible_handlers": (),
        "last_error": "mass.command_handlers is not a mapping",
    }

    adapter.mass.command_handlers = {}
    restored = await adapter.base_snapshot()
    assert restored.fingerprint == valid.fingerprint
    assert restored is not valid
    assert await adapter.visible_entries() == []
    assert adapter.diagnostics()["available"] is True


async def test_cached_snapshot_keeps_visibility_request_specific() -> None:
    """A shared descriptor never leaks one user's scope visibility to another."""

    async def search(search_query: str) -> list[str]:
        return [search_query]

    handler = _handler("music/search", search)
    allowed = SimpleNamespace(user_id="allowed", enabled=True, scopes={"library.read"})
    denied = SimpleNamespace(user_id="denied", enabled=True, scopes=set())
    users = {user.user_id: user for user in (allowed, denied)}
    current_token: contextvars.ContextVar[AccessToken] = contextvars.ContextVar("current_token")
    mass = MagicMock(command_handlers={handler.command: handler})
    mass.webserver.auth.authenticate_with_token = AsyncMock(
        side_effect=lambda _bearer: users[current_token.get().client_id]
    )
    mass.webserver.auth.get_token_id_from_token = AsyncMock(
        side_effect=lambda _bearer: current_token.get().client_id
    )
    adapter = DynamicAPIAdapter(
        mass,
        auth_required_provider=lambda: True,
        token_provider=current_token.get,
        scope_checker=lambda user, scope: scope in user.scopes,
        policy_provider=lambda _bearer: policy_snapshot(PolicyProfile.TRUSTED),
        default_policy_provider=lambda: policy_snapshot(PolicyProfile.SAFE_QUERIES),
        identity_provider=lambda _bearer: TokenIdentity(
            current_token.get().client_id, current_token.get().client_id
        ),
    )

    async def catalog_for(user_id: str) -> Any:
        token = current_token.set(AccessToken(token="secret", client_id=user_id, scopes=[]))
        try:
            return await adapter.visible_catalog()
        finally:
            current_token.reset(token)

    allowed_view, denied_view = await asyncio.gather(catalog_for("allowed"), catalog_for("denied"))
    snapshot = await adapter.base_snapshot()
    assert allowed_view.fingerprint == denied_view.fingerprint == snapshot.fingerprint
    assert [entry.name for entry in allowed_view.entries] == [
        entry.name for entry in snapshot.entries
    ]
    assert allowed_view.entries[0].policy_mode is PolicyMode.ALLOW
    assert allowed_view.entries[0].handler is snapshot.entries[0].handler
    assert denied_view.entries == ()
    base_diagnostics = adapter.diagnostics()
    assert base_diagnostics["handlers_visible"] == 1
    await catalog_for("denied")
    assert adapter.diagnostics() == base_diagnostics


async def test_failed_snapshot_build_does_not_poison_future_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exceptional cold build releases concurrent readers and remains retryable."""

    async def search(search_query: str) -> list[str]:
        return [search_query]

    adapter = _real_adapter(_handler("music/search", search))
    compile_entry = adapter._compile_entry
    attempts = 0

    def fail_once(command: str, handler: Any, decision: Any) -> DynamicEntry:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient compile failure")
        return compile_entry(command, handler, decision)

    monkeypatch.setattr(adapter, "_compile_entry", fail_once)
    outcomes = await asyncio.gather(
        adapter.base_snapshot(), adapter.base_snapshot(), return_exceptions=True
    )
    assert isinstance(outcomes[0], RuntimeError)
    assert isinstance(outcomes[1], CatalogSnapshot)
    assert outcomes[1].entries[0].name == "ma_api:music/search"
    assert (await adapter.base_snapshot()) is outcomes[1]
    assert attempts == 2


async def test_cancelled_snapshot_builder_and_waiter_leave_later_reads_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation never leaves the catalog lock held or poisons its next reader."""

    async def search(search_query: str) -> list[str]:
        return [search_query]

    adapter = _real_adapter(_handler("music/search", search))
    compile_snapshot = adapter._compile_snapshot
    attempts = 0

    def cancel_once(capture: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise asyncio.CancelledError
        return compile_snapshot(capture)

    monkeypatch.setattr(adapter, "_compile_snapshot", cancel_once)
    with pytest.raises(asyncio.CancelledError):
        await adapter.base_snapshot()

    await adapter._snapshot_lock.acquire()
    waiter = asyncio.create_task(adapter.base_snapshot())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    adapter._snapshot_lock.release()

    snapshot = await adapter.base_snapshot()
    assert snapshot.entries[0].name == "ma_api:music/search"
    assert attempts == 2


async def test_adapter_skips_structurally_incompatible_handlers() -> None:
    """One malformed registry entry cannot disable the MCP endpoint."""
    adapter = _real_adapter(SimpleNamespace(command="broken", target=lambda: None))
    assert await adapter.visible_entries() == []


async def test_adapter_executes_strictly_and_bounds_result() -> None:
    """Calls reject extra args and cap list/string output in compact mode."""

    async def values(prefix: str) -> list[str]:
        return [prefix * 3000 for _index in range(40)]

    adapter = _real_adapter(_handler("music/search", values))
    ctx = MagicMock(session_id="session-1")
    with pytest.raises(ToolError, match=r"\[invalid_arguments\]"):
        await adapter.call(
            "ma_api:music/search",
            {"prefix": "x", "typo": True},
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=ctx,
        )
    result = await adapter.call(
        "ma_api:music/search",
        {"prefix": "x"},
        response_mode="compact",
        fields=None,
        max_items=None,
        ctx=ctx,
    )
    assert result["truncated"] is True
    assert result["returned_count"] <= 25
    assert result["bytes"] <= 12_288


async def test_play_media_schema_excludes_dynamic_radio_arguments() -> None:
    """Radio stations play directly without ambiguous dynamic-radio flags."""

    async def play_media(
        queue_id: str,
        media: str,
        radio_mode: bool = False,
    ) -> None:
        del queue_id, media, radio_mode

    adapter = _real_adapter(_handler("player_queues/play_media", play_media))
    entry = (await adapter.visible_entries())[0]
    properties = entry.input_schema["properties"]

    assert "media" in properties
    assert "uri" in properties
    assert "radio" not in properties
    assert "radio_mode" not in properties


@pytest.mark.parametrize("argument", ["radio", "radio_mode"])
async def test_play_media_rejects_dynamic_radio_arguments(argument: str) -> None:
    """Hidden dynamic-radio arguments cannot bypass the published contract."""
    calls: list[tuple[str, str, bool]] = []

    async def play_media(
        queue_id: str,
        media: str,
        radio_mode: bool = False,
    ) -> None:
        calls.append((queue_id, media, radio_mode))

    adapter = _real_adapter(_handler("player_queues/play_media", play_media))

    with pytest.raises(ToolError, match=r"\[invalid_arguments\]"):
        await adapter.call(
            "ma_api:player_queues/play_media",
            {
                "queue_id": "living-room",
                "media": "siriusxm://radio/real-jazz",
                argument: True,
            },
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=MagicMock(),
        )

    assert calls == []


@pytest.mark.parametrize("media_argument", ["media", "uri"])
async def test_play_media_passes_radio_uri_without_dynamic_mode(media_argument: str) -> None:
    """A Radio URI reaches the native handler with its default direct-play mode."""
    calls: list[tuple[str, str, bool]] = []

    async def play_media(
        queue_id: str,
        media: str,
        radio_mode: bool = False,
    ) -> None:
        calls.append((queue_id, media, radio_mode))

    adapter = _real_adapter(_handler("player_queues/play_media", play_media))

    await adapter.call(
        "ma_api:player_queues/play_media",
        {"queue_id": "living-room", media_argument: "siriusxm://radio/real-jazz"},
        response_mode="compact",
        fields=None,
        max_items=None,
        ctx=MagicMock(),
    )

    assert calls == [("living-room", "siriusxm://radio/real-jazz", False)]


async def test_config_action_result_localizes_in_dynamic_response() -> None:
    """Native config action messages use MA's translation resolver in MCP output."""

    async def invoke() -> ConfigActionResult:
        return ConfigActionResult(
            translation_key="cleanup.result",
            translation_owner="core.cache",
            translation_args=[3],
        )

    adapter = _real_adapter(_handler("config/core/invoke_action", invoke, "config.core.write"))
    adapter.mass.translations.get_translation.side_effect = lambda key, owner=None, params=None: (
        f"{key}|{owner}|{','.join(params or ())}"
    )

    response = await adapter.call(
        "ma_api:config/core/invoke_action",
        {},
        response_mode="full",
        fields=None,
        max_items=None,
        ctx=MagicMock(),
    )

    assert response["data"] == {
        "message": "config_actions.cleanup.result|core.cache|3",
        "open_url": None,
    }


async def test_config_action_open_url_survives_dynamic_response() -> None:
    """A one-shot action URL reaches MCP clients without translation metadata."""

    async def invoke() -> ConfigActionResult:
        return ConfigActionResult(open_url="https://ma.example/result")

    adapter = _real_adapter(
        _handler("config/providers/invoke_action", invoke, "config.providers.write")
    )
    response = await adapter.call(
        "ma_api:config/providers/invoke_action",
        {},
        response_mode="full",
        fields=None,
        max_items=None,
        ctx=MagicMock(),
    )

    assert response["data"] == {
        "message": None,
        "open_url": "https://ma.example/result",
    }


async def test_empty_upstream_exception_names_type_and_command() -> None:
    """Empty upstream messages still produce an actionable command error."""

    async def search(search_query: str) -> list[str]:
        del search_query
        raise IndexError

    adapter = _real_adapter(_handler("music/search", search))
    with pytest.raises(ToolError, match=r"\[execution_failed\]"):
        await adapter.call(
            "ma_api:music/search",
            {"search_query": "missing"},
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=MagicMock(),
        )


async def test_adapter_hides_catalog_when_mcp_auth_is_disabled() -> None:
    """Disabling endpoint auth must not accidentally expose MA internals."""

    async def values() -> list[str]:
        return []

    handler = _handler("config/providers/reload", values, "config.providers.write")
    mass = MagicMock(command_handlers={handler.command: handler})
    adapter = DynamicAPIAdapter(
        mass,
        auth_required_provider=lambda: False,
        token_provider=lambda: None,
        scope_checker=lambda _user, _scope: True,
        policy_provider=lambda _bearer: policy_snapshot(PolicyProfile.SAFE_QUERIES),
        default_policy_provider=lambda: policy_snapshot(PolicyProfile.SAFE_QUERIES),
    )
    assert await adapter.visible_entries() == []


def test_request_view_is_the_same_catalog_generation() -> None:
    """A filtered view keeps the snapshot fingerprint and replaces only entries."""
    snapshot = CatalogSnapshot(
        (1, "gen", ()),
        (_catalog_entry("ma_api:music/search", "Search"),),
    )
    hidden = snapshot.with_entries(())

    assert hidden.fingerprint == snapshot.fingerprint
    assert hidden.entries == ()
    assert hidden.by_name == {}


def test_every_migrated_command_has_an_executable_profile() -> None:
    """The migration matrix is backed by profiles, not aliases alone."""
    assert set(CURATED_PROFILE_MAPPINGS.values()).issubset(COMMAND_PROFILES)
    for legacy, command in CURATED_PROFILE_MAPPINGS.items():
        profile = COMMAND_PROFILES[command]
        assert isinstance(profile, CommandProfile)
        assert legacy in profile.search_aliases
        assert profile.annotations
        assert profile.operation_override in {"read", "control", "write", "delete", "system"}
    assert COMMAND_PROFILES["providers"].compact_fields == (
        "instance_id",
        "domain",
        "type",
        "name",
        "available",
        "enabled",
        "last_error",
    )


async def test_profile_converts_arguments_and_projects_only_compact_mode() -> None:
    """Compatibility aliases parse strictly and full mode remains lossless."""
    seen: list[tuple[str, list[str] | None]] = []

    async def search(
        search_query: str, media_types: list[str] | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        seen.append((search_query, media_types))
        return {"tracks": [{"uri": "track://1", "name": "One", "provider_mappings": [1, 2, 3]}]}

    adapter = _real_adapter(_handler("music/search", search))
    compact = await adapter.call(
        "ma_api:music/search",
        {"query": "one", "media_types": "track"},
        response_mode="compact",
        fields=None,
        max_items=None,
        ctx=MagicMock(),
    )
    full = await adapter.call(
        "ma_api:music/search",
        {"search_query": "one", "media_types": ["track"]},
        response_mode="full",
        fields=None,
        max_items=None,
        ctx=MagicMock(),
    )
    assert seen == [("one", ["track"]), ("one", ["track"])]
    assert "provider_mappings" not in compact["data"]["tracks"][0]
    assert full["data"]["tracks"][0]["provider_mappings"] == [1, 2, 3]


def test_nested_response_lists_are_bounded_deterministically() -> None:
    """Nested collections obey the same compact item budget as root lists."""
    payload = {"groups": [{"items": list(range(40))} for _index in range(40)]}
    first = DynamicAPIAdapter._bounded_envelope(
        "ma_api:test", payload, response_mode="compact", fields=None, max_items=None
    )
    second = DynamicAPIAdapter._bounded_envelope(
        "ma_api:test", payload, response_mode="compact", fields=None, max_items=None
    )
    assert len(first["data"]["groups"]) == 25
    assert len(first["data"]["groups"][0]["items"]) == 25
    assert first == second
    assert first["truncated"] is True


def test_deep_response_is_truncated_before_python_recursion_limit() -> None:
    """Depth limiting happens before recursive normalization can exhaust Python."""
    payload: Any = "leaf"
    for _index in range(sys.getrecursionlimit() + 100):
        payload = [payload]

    result = DynamicAPIAdapter._bounded_envelope(
        "ma_api:test", payload, response_mode="compact", fields=None, max_items=None
    )

    nested = result["data"]
    for _index in range(6):
        assert isinstance(nested, list)
        nested = nested[0]
    assert nested == "[truncated]"
    assert result["truncated"] is True
    assert result["bytes"] <= 12_288


def test_response_normalization_stops_at_each_list_item_cap() -> None:
    """Discarded list suffixes are never serialized before item limiting."""
    serialized: list[int] = []

    @dataclass
    class Row:
        index: int

        def model_dump(self, *, mode: str) -> dict[str, int]:
            assert mode == "json"
            serialized.append(self.index)
            return {"index": self.index}

    payload = [Row(index) for index in range(100)]
    result = DynamicAPIAdapter._bounded_envelope(
        "ma_api:test", payload, response_mode="compact", fields=None, max_items=3
    )

    assert result["data"] == [{"index": 0}, {"index": 1}, {"index": 2}]
    assert result["total_count"] == 100
    assert result["truncated"] is True
    assert serialized == [0, 1, 2]


def test_response_normalization_emits_strict_json_scalars() -> None:
    """Surrogates and non-finite floats cannot escape into MCP JSON output."""
    result = DynamicAPIAdapter._bounded_envelope(
        "ma_api:test",
        {"text": "left\ud800right", "numbers": [float("nan"), float("inf"), -float("inf")]},
        response_mode="full",
        fields=None,
        max_items=None,
    )

    assert result["data"] == {
        "text": "left\ufffdright",
        "numbers": [None, None, None],
    }
    assert result["truncated"] is True
    encoded = json.dumps(
        result,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()
    assert result["bytes"] == len(encoded)


def test_large_search_envelope_keeps_mapping_shape_within_byte_budget() -> None:
    """Large SearchResults mappings shrink nested rows instead of becoming a string."""
    payload = {
        "tracks": [
            {
                "uri": f"provider://track/{index}",
                "name": f"Track {index} " + ("x" * 900),
                "media_type": "track",
                "artists": [{"uri": "provider://artist/1", "name": "Artist"}],
            }
            for index in range(30)
        ],
        "albums": [],
    }

    result = DynamicAPIAdapter._bounded_envelope(
        "ma_api:music/search",
        payload,
        response_mode="compact",
        fields=None,
        max_items=None,
        profile=COMMAND_PROFILES["music/search"],
    )

    assert isinstance(result["data"], dict)
    assert 0 < len(result["data"]["tracks"]) < 30
    assert result["data"]["albums"] == []
    assert result["truncated"] is True
    assert result["bytes"] <= 12_288


def test_byte_fitting_balances_equal_sibling_lists_by_original_policy() -> None:
    """Equal sibling lists lose suffix rows in traversal order, one row at a time."""
    payload = {
        "first": [f"a{index}:" + ("x" * 930) for index in range(10)],
        "second": [f"b{index}:" + ("y" * 930) for index in range(10)],
    }

    result = DynamicAPIAdapter._bounded_envelope(
        "ma_api:test",
        payload,
        response_mode="compact",
        fields=None,
        max_items=None,
    )

    assert result["data"] == {
        "first": payload["first"][:6],
        "second": payload["second"][:6],
    }
    assert result["truncated"] is True
    assert result["returned_count"] == 1
    assert result["bytes"] == _encoded_size(result)
    assert result["bytes"] <= 12_288


def test_byte_fitting_measures_trials_with_truncation_metadata() -> None:
    """The first fitting logical removal is retained at a one-byte boundary."""
    envelope = {
        "command": "ma_api:test",
        "data": ["x", "x"],
        "truncated": False,
        "returned_count": 2,
        "bytes": 0,
        "applied": {"mode": "compact", "fields": [], "max_items": 25},
    }

    fit_json_envelope(envelope, 142)

    assert envelope["data"] == ["x"]
    assert envelope["returned_count"] == 1
    assert envelope["truncated"] is True


def test_nested_sibling_response_uses_logarithmic_byte_fitting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Many one-row nested siblings do not trigger a full encoding per removal."""
    payload = {
        f"group-{index:03}": [{"items": [f"row-{index}:" + ("x" * 500)]}] for index in range(200)
    }
    encoded_size = _encoded_size
    measurements = 0

    def counted_size(value: Any) -> int:
        nonlocal measurements
        measurements += 1
        return int(encoded_size(value))

    monkeypatch.setattr(dynamic_serialization, "_encoded_size", counted_size)

    result = DynamicAPIAdapter._bounded_envelope(
        "ma_api:test",
        payload,
        response_mode="full",
        fields=None,
        max_items=None,
    )

    remaining = [index for index, group in enumerate(result["data"].values()) if group]
    assert remaining == list(range(81, 200))
    assert list(result["data"]) == list(payload)
    assert result["truncated"] is True
    assert result["returned_count"] == 1
    assert result["bytes"] == encoded_size(result)
    assert result["bytes"] <= 65_536
    assert measurements <= 16


def test_large_top_level_response_uses_logarithmic_byte_fitting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A large bounded response does not serialize once per removed row."""
    payload = [f"row-{index}:" + ("x" * 3000) for index in range(200)]
    encoded_size = _encoded_size
    measurements = 0

    def counted_size(value: Any) -> int:
        nonlocal measurements
        measurements += 1
        return int(encoded_size(value))

    monkeypatch.setattr(dynamic_serialization, "_encoded_size", counted_size)

    result = DynamicAPIAdapter._bounded_envelope(
        "ma_api:test",
        payload,
        response_mode="full",
        fields=None,
        max_items=None,
    )

    assert result["bytes"] <= 65_536
    assert result["data"]
    assert result["data"] == payload[: len(result["data"])]
    assert result["returned_count"] == len(result["data"])
    assert result["truncated"] is True
    assert result["bytes"] == encoded_size(result)
    assert measurements <= 16


@pytest.mark.parametrize(
    ("response_mode", "field_width", "byte_cap"),
    [("compact", 20_000, 12_288), ("full", 100_000, 65_536)],
)
def test_oversized_echoed_fields_use_a_bounded_shape_preserving_fallback(
    response_mode: str,
    field_width: int,
    byte_cap: int,
) -> None:
    """Oversized optional metadata cannot make a success envelope exceed its cap."""
    result = DynamicAPIAdapter._bounded_envelope(
        "ma_api:test",
        ["kept"],
        response_mode=response_mode,
        fields=["f" * field_width],
        max_items=None,
    )

    assert result["data"] == []
    assert result["applied"]["fields"] == []
    assert "total_count" not in result
    assert result["returned_count"] == 0
    assert result["truncated"] is True
    assert result["bytes"] == _encoded_size(result)
    assert result["bytes"] <= byte_cap


def test_oversized_mapping_without_lists_uses_empty_mapping_fallback() -> None:
    """A response with no reducible list retains its top-level JSON container type."""
    payload = {f"key-{index}": "x" * 3_000 for index in range(10)}

    result = DynamicAPIAdapter._bounded_envelope(
        "ma_api:test",
        payload,
        response_mode="compact",
        fields=None,
        max_items=None,
    )

    assert result["data"] == {}
    assert result["returned_count"] == 1
    assert result["truncated"] is True
    assert result["bytes"] == _encoded_size(result)
    assert result["bytes"] <= 12_288


def test_unshrinkable_envelope_metadata_raises_short_tool_error() -> None:
    """Required metadata that cannot fit is reported as an error, never as success."""
    with pytest.raises(ToolError, match="Response exceeds the compact byte budget") as error:
        DynamicAPIAdapter._bounded_envelope(
            "x" * 20_000,
            None,
            response_mode="compact",
            fields=None,
            max_items=None,
        )

    assert len(str(error.value).encode()) < 200


async def test_registry_incompatibility_is_reported_without_breaking_catalog() -> None:
    """Structural MA drift is isolated and leaves actionable diagnostics."""

    async def values() -> list[str]:
        return []

    valid = _handler("music/search", values)
    adapter = _real_adapter(valid)
    adapter.mass.command_handlers["broken"] = SimpleNamespace(target=None)
    assert [entry.name for entry in await adapter.visible_entries()] == ["ma_api:music/search"]
    diagnostics = adapter.diagnostics()
    assert diagnostics["available"] is True
    assert diagnostics["incompatible_handlers"] == ("broken",)
    assert diagnostics["last_error"] == "1 incompatible handler(s) skipped"
    adapter.mass.command_handlers = []
    assert await adapter.visible_entries() == []
    assert adapter.diagnostics()["last_error"] == "mass.command_handlers is not a mapping"


@pytest.mark.parametrize(
    "command",
    [
        "auth/future_dangerous_command",
        "dashboard/register",
        "dashboard/unregister",
    ],
)
async def test_denied_handlers_are_omitted_from_dynamic_health_diagnostics(command: str) -> None:
    """Intentional denylist exclusions do not look like MA compatibility failures."""

    async def operation() -> None:
        return None

    adapter = _real_adapter(_handler(command, operation, scope="admin"))
    adapter.mass.command_handlers["broken"] = SimpleNamespace(target=None)

    assert await adapter.visible_entries() == []
    assert adapter.diagnostics()["incompatible_handlers"] == ("broken",)
    assert adapter.diagnostics()["last_error"] == "1 incompatible handler(s) skipped"


async def test_hidden_auth_registry_churn_keeps_catalog_state_stable() -> None:
    """Hidden-only registry changes cannot affect diagnostics, revisions, or cursors."""

    async def first() -> None:
        return None

    async def second() -> None:
        return None

    async def hidden() -> None:
        return None

    adapter = _real_adapter(_handler("music/browse", first))
    adapter.mass.command_handlers["music/search"] = _handler("music/search", second)
    service = meta_discovery.MetaDiscoveryService(adapter)
    initial_view = await adapter.visible_catalog()
    initial_page = await service.discover("", limit=1)
    initial_diagnostics = adapter.diagnostics()
    assert initial_page["next_cursor"] is not None

    for hidden_handler in (
        _handler("auth/token/create", hidden, scope="admin"),
        _handler("auth/token/create", lambda: None, scope="admin"),
        None,
    ):
        if hidden_handler is None:
            adapter.mass.command_handlers.pop("auth/token/create")
        else:
            adapter.mass.command_handlers["auth/token/create"] = hidden_handler
        current_view = await adapter.visible_catalog()
        continued = await service.discover(cursor=initial_page["next_cursor"], limit=1)
        assert current_view.fingerprint == initial_view.fingerprint
        assert continued["catalog_revision"] == initial_page["catalog_revision"]
        assert adapter.diagnostics() == initial_diagnostics


@pytest.mark.parametrize("scope", [Scope.UNKNOWN, "future.scope", object()])
async def test_dynamic_catalog_rejects_unknown_scopes_before_ma_checker(scope: object) -> None:
    """Unknown scopes are neither visible nor executable, even for a permissive checker."""
    checked: list[object] = []
    called = False

    async def search() -> None:
        nonlocal called
        called = True

    def scope_checker(_user: Any, required_scope: object) -> bool:
        checked.append(required_scope)
        return True

    handler = _handler("music/search", search, cast("Any", scope))
    adapter = _real_adapter(handler, scope_checker=scope_checker)

    assert await adapter.visible_entries() == []
    with pytest.raises(ToolError, match=r"\[not_found_or_forbidden\]"):
        await adapter.call(
            "ma_api:music/search",
            {},
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=MagicMock(),
        )
    assert checked == []
    assert called is False


async def test_dynamic_catalog_passes_normalized_scope_to_ma_checker() -> None:
    """Known string scopes are normalized before MA authorization is delegated."""
    checked: list[Scope] = []

    async def search() -> None:
        return None

    def scope_checker(_user: Any, required_scope: Scope) -> bool:
        checked.append(required_scope)
        return True

    adapter = _real_adapter(
        _handler("music/search", search, "library.read"),
        scope_checker=scope_checker,
    )

    assert [entry.name for entry in await adapter.visible_entries()] == ["ma_api:music/search"]
    assert checked == [Scope.LIBRARY_READ]


@pytest.mark.parametrize(
    "command",
    [
        "auth/future_dangerous_command",
        "dashboard/register",
        "dashboard/unregister",
    ],
)
async def test_denied_handlers_stay_denied_when_reauthorized(command: str) -> None:
    """A cached entry cannot make an intentionally denied handler executable."""

    async def operation() -> None:
        return None

    adapter = _real_adapter(_handler("music/search", operation))
    entry = (await adapter.visible_entries())[0]
    handler = _handler(command, operation, scope="admin")
    adapter.mass.command_handlers = {command: handler}
    stale_entry = replace(entry, name=f"ma_api:{command}", command=command, handler=handler)

    with pytest.raises(ToolError, match="not found or not permitted"):
        adapter._reauthorize_entry(
            stale_entry,
            (AccessToken(token="secret", client_id="u1", scopes=[]), MagicMock()),
        )


async def test_execution_sets_and_restores_ma_auth_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native execution sets and restores MA's request-local identity context."""
    current_user: contextvars.ContextVar[Any] = contextvars.ContextVar("current_user")
    current_token: contextvars.ContextVar[Any] = contextvars.ContextVar("current_token")
    auth_middleware = SimpleNamespace(current_user=current_user, current_token=current_token)
    helpers = SimpleNamespace(auth_middleware=auth_middleware)
    monkeypatch.setitem(sys.modules, "music_assistant.controllers.webserver.helpers", helpers)

    async def whoami() -> str:
        return str(current_user.get().user_id)

    adapter = _real_adapter(_handler("music/browse", whoami))
    result = await adapter.call(
        "ma_api:music/browse",
        {},
        response_mode="compact",
        fields=None,
        max_items=None,
        ctx=MagicMock(),
    )
    assert result["data"] == "u1"
    with pytest.raises(LookupError):
        current_user.get()


async def test_schema_covers_enum_union_collections_and_impersonation() -> None:
    """Live type hints remain the source of truth for rich command schemas."""

    class Mode(StrEnum):
        ONE = "one"
        TWO = "two"

    async def typed(mode: Mode, values: list[int], optional: str | None = None) -> dict[str, int]:
        return {str(mode): len(values) + bool(optional)}

    handler = _handler("music/browse", typed)
    handler.type_hints = {
        "mode": Mode,
        "values": list[int],
        "optional": str | None,
        "return": dict[str, int],
    }
    handler.allow_impersonation = True
    entry = (await _real_adapter(handler).visible_entries())[0]
    assert entry.input_schema["properties"]["mode"]["enum"] == ["one", "two"]
    assert entry.input_schema["properties"]["values"]["items"]["type"] == "integer"
    assert entry.input_schema["properties"]["optional"]["anyOf"]
    assert entry.input_schema["properties"]["user"]["type"] == "string"
    assert entry.output_schema is not None


async def test_disabled_user_and_transport_commands_are_hidden() -> None:
    """Authentication state and transport exclusions are fail-closed."""

    async def operation() -> None:
        return None

    disabled = SimpleNamespace(user_id="u1", enabled=False)
    adapter = _real_adapter(_handler("music/read", operation), user=disabled)
    assert await adapter.visible_entries() == []
    transport = _real_adapter(_handler("dashboard/register", operation))
    assert await transport.visible_entries() == []


@pytest.mark.parametrize(
    "command",
    [
        "auth/token/create",
        "auth/token/revoke",
        "auth/tokens",
        "auth/user/create",
        "auth/join_codes",
        "auth/future_dangerous_command",
    ],
)
async def test_auth_command_prefix_is_never_discoverable(command: str) -> None:
    """System access cannot expose current or future authentication commands."""

    async def operation() -> None:
        return None

    handler = _handler(command, operation, scope="admin")
    adapter = _real_adapter(handler)

    assert await adapter.visible_entries() == []
    assert await adapter.get_visible_entry(f"ma_api:{command}") is None


async def test_denied_auth_command_cannot_be_called_directly() -> None:
    """A cached auth command name cannot bypass catalog compilation."""
    called = False

    async def mint_token() -> str:
        nonlocal called
        called = True
        return "full-scope-token"

    adapter = _real_adapter(_handler("auth/token/create", mint_token, scope="admin"))
    ctx = SimpleNamespace(
        elicit=AsyncMock(return_value=SimpleNamespace(action="accept", data=True))
    )

    with pytest.raises(ToolError, match=r"\[not_found_or_forbidden\]"):
        await adapter.call(
            "ma_api:auth/token/create",
            {},
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=cast("Context", ctx),
        )
    assert called is False


async def test_native_command_requires_its_live_permission_tag() -> None:
    """Native handlers cannot bypass the provider's existing permission toggles."""

    async def operation() -> None:
        return None

    handler = _handler("music/search", operation, "library.read")
    assert await _real_adapter(handler, allowed_capabilities=set()).visible_entries() == []
    visible = await _real_adapter(
        handler, allowed_capabilities={str(Capability.QUERY_LIBRARY)}
    ).visible_entries()
    assert [entry.name for entry in visible] == ["ma_api:music/search"]


@pytest.mark.parametrize(
    ("command", "parameter", "user_filter"),
    [
        ("players/get", "player_id", "player_filter"),
    ],
)
async def test_invocation_rejects_targets_outside_user_filters(
    command: str, parameter: str, user_filter: str
) -> None:
    """Direct dynamic invocation preserves MA player and provider filters."""
    called = False

    async def operation(**kwargs: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        return kwargs

    operation.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        [inspect.Parameter(parameter, inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    )
    handler = _handler(command, operation, "players.read")
    handler.type_hints = {parameter: str, "return": dict[str, Any]}
    filters: dict[str, list[str]] = {"player_filter": [], "provider_filter": []}
    filters[user_filter] = [f"allowed-{user_filter.removesuffix('_filter')}"]
    user = SimpleNamespace(
        user_id="u1",
        username="limited",
        enabled=True,
        role="user",
        **filters,
    )
    adapter = _real_adapter(handler, user=user)
    with pytest.raises(ToolError, match="not permitted"):
        await adapter.call(
            f"ma_api:{command}",
            {parameter: "blocked-target"},
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=MagicMock(),
        )
    assert called is False


async def test_admin_scope_is_not_restricted_by_user_filters() -> None:
    """Admin calls retain MA's Scope.ALL exemption from target filters."""

    async def get_player(player_id: str) -> str:
        return player_id

    admin = SimpleNamespace(
        user_id="u1",
        username="admin",
        enabled=True,
        role="admin",
        player_filter=["other-player"],
        provider_filter=["other-provider"],
    )
    adapter = _real_adapter(_handler("players/get", get_player, "players.read"), user=admin)
    result = await adapter.call(
        "ma_api:players/get",
        {"player_id": "kitchen"},
        response_mode="compact",
        fields=None,
        max_items=None,
        ctx=MagicMock(),
    )
    assert result["data"] == "kitchen"


async def test_sync_coroutine_and_generator_handlers_close_cleanly() -> None:
    """The dispatcher supports all MA execution shapes and closes generators."""
    closed = False

    def sync_value() -> str:
        return "sync"

    async def coroutine_value() -> str:
        return "async"

    async def generated() -> Any:
        nonlocal closed
        try:
            for index in range(250):
                yield index
        finally:
            closed = True

    for name, target, expected in (
        ("music/sync", sync_value, "sync"),
        ("music/search", coroutine_value, "async"),
    ):
        adapter = _real_adapter(_handler(name, target))
        result = await adapter.call(
            f"ma_api:{name}",
            {},
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=MagicMock(),
        )
        assert result["data"] == expected

    adapter = _real_adapter(_handler("music/browse", generated))
    result = await adapter.call(
        "ma_api:music/browse",
        {},
        response_mode="full",
        fields=None,
        max_items=None,
        ctx=MagicMock(),
    )
    assert result["returned_count"] == 200
    assert closed is True


async def test_impersonation_keeps_discovery_conservatively_confirm() -> None:
    """An otherwise prompt-free command advertises confirmation when impersonation exists."""
    handler = _handler("music/search", lambda: None)
    handler.allow_impersonation = True
    entry = (await _real_adapter(handler).visible_entries())[0]
    assert entry.policy_mode is PolicyMode.CONFIRM


async def test_queue_delete_has_no_classifier_owned_confirmation() -> None:
    """Delete execution is governed by its capability, not a mandatory classifier prompt."""
    called = False

    async def clear(queue_id: str) -> None:
        nonlocal called
        del queue_id
        called = True

    adapter = _real_adapter(
        _handler("player_queues/clear", clear, "queues.control"),
        allowed_capabilities={str(Capability.DELETE_QUEUE)},
    )
    entry = (await adapter.visible_entries())[0]
    assert entry.decision is not None
    assert entry.decision.required_capabilities == frozenset({str(Capability.DELETE_QUEUE)})
    await adapter.call(
        "ma_api:player_queues/clear",
        {"queue_id": "kitchen"},
        response_mode="compact",
        fields=None,
        max_items=None,
        ctx=MagicMock(),
    )
    assert called is True


async def test_playlist_provider_alias_is_filtered_before_confirmation() -> None:
    """Profile alias conversion cannot bypass a restricted provider filter."""
    called = False

    async def create_playlist(name: str, provider_instance_or_domain: str) -> dict[str, str]:
        nonlocal called
        called = True
        return {"name": name, "provider": provider_instance_or_domain}

    user = SimpleNamespace(
        user_id="u1",
        username="limited",
        enabled=True,
        role="user",
        player_filter=[],
        provider_filter=["allowed-provider"],
    )
    adapter = _real_adapter(
        _handler(
            "music/playlists/create_playlist",
            create_playlist,
            "library.write",
        ),
        allowed_capabilities={str(Capability.EDIT_PLAYLISTS)},
        user=user,
    )
    with pytest.raises(ToolError, match="not permitted"):
        await adapter.call(
            "ma_api:music/playlists/create_playlist",
            {"name": "Blocked", "provider_instance_id": "blocked-provider"},
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=MagicMock(),
        )
    assert called is False


@pytest.mark.parametrize("revoked", ["capability", "scope"])
async def test_native_live_authorization_revocation_prevents_elicitation(
    revoked: str,
) -> None:
    """Native live capability and scope revocation fail before execution."""
    called = False
    tag_checks = 0
    scope_checks = 0

    async def write() -> None:
        nonlocal called
        called = True

    adapter = _real_adapter(
        _handler("music/write", write, "library.write"),
        allowed_capabilities={str(Capability.EDIT_LIBRARY)},
    )

    def capabilities_provider() -> set[str]:
        nonlocal tag_checks
        tag_checks += 1
        return (
            {str(Capability.EDIT_LIBRARY)} if revoked != "capability" or tag_checks == 1 else set()
        )

    def scope_checker(_user: Any, _scope: Any) -> bool:
        nonlocal scope_checks
        scope_checks += 1
        return revoked != "scope" or scope_checks == 1

    adapter._test_allowed_capabilities_provider = capabilities_provider
    adapter._scope_checker = scope_checker
    with pytest.raises(ToolError, match="not permitted"):
        await adapter.call(
            "ma_api:music/write",
            {},
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=MagicMock(),
        )
    assert called is False


@pytest.mark.parametrize("revoked", ["handler", "capability", "scope"])
async def test_native_authorization_revoked_between_checks_prevents_execution(
    revoked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Authorization changes at the call boundary are rechecked before invocation."""
    called: list[str] = []
    state = {"capability": True, "scope": True}

    async def write() -> None:
        called.append("stale")

    async def replacement() -> None:
        called.append("replacement")

    handler = _handler("music/sync", write, "library.write")
    adapter = _real_adapter(
        handler,
        allowed_capabilities={str(Capability.EDIT_LIBRARY)},
    )
    adapter._test_allowed_capabilities_provider = lambda: (
        {str(Capability.EDIT_LIBRARY)} if state["capability"] else set()
    )
    adapter._scope_checker = lambda _user, _scope: state["scope"]

    async def revoke_during_confirmation(*_args: Any, **_kwargs: Any) -> None:
        if revoked == "handler":
            adapter.mass.command_handlers[handler.command] = _handler(
                handler.command, replacement, "library.write"
            )
        else:
            state[revoked] = False

    confirmation = AsyncMock(side_effect=revoke_during_confirmation)
    monkeypatch.setattr(adapter, "_confirm", confirmation)

    with pytest.raises(ToolError, match="not permitted"):
        await adapter.call(
            "ma_api:music/sync",
            {},
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=MagicMock(),
        )
    confirmation.assert_awaited_once()
    assert called == []


@pytest.mark.parametrize("replacement", [None, "disabled"])
async def test_fresh_authentication_rejects_removed_or_disabled_user_after_confirmation(
    replacement: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user that changes while confirming cannot execute with stale authentication."""
    _bypass_ma_argument_parser(monkeypatch)
    called = False
    initial_user = SimpleNamespace(user_id="u1", enabled=True, role="admin")
    current_user: Any = initial_user

    async def reload_provider() -> None:
        nonlocal called
        called = True

    adapter = _real_adapter(
        _handler("config/providers/reload", reload_provider, "config.providers.write"),
        allowed_capabilities={str(Capability.CONFIG_WRITE_PROVIDER)},
        user=initial_user,
    )
    adapter.mass.webserver.auth.get_user = AsyncMock(side_effect=lambda _user_id: current_user)
    adapter.mass.webserver.auth.authenticate_with_token = AsyncMock(
        side_effect=lambda _token: current_user
    )

    async def change_user(*_args: Any, **_kwargs: Any) -> None:
        nonlocal current_user
        current_user = (
            None
            if replacement is None
            else SimpleNamespace(user_id="u1", enabled=False, role="admin")
        )

    monkeypatch.setattr(adapter, "_confirm", AsyncMock(side_effect=change_user))

    with pytest.raises(ToolError, match="Authentication is required"):
        await adapter.call(
            "ma_api:config/providers/reload",
            {},
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=MagicMock(),
        )
    assert called is False


async def test_revoked_bearer_token_after_confirmation_prevents_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-confirm authorization rejects a token that MA no longer accepts."""
    _bypass_ma_argument_parser(monkeypatch)
    called = False
    audit_records: list[Any] = []

    async def reload_provider() -> None:
        nonlocal called
        called = True

    adapter = _real_adapter(
        _handler("config/providers/reload", reload_provider, "config.providers.write"),
        allowed_capabilities={str(Capability.CONFIG_WRITE_PROVIDER)},
        audit_sink=audit_records.append,
    )
    adapter.mass.webserver.auth.authenticate_with_token = AsyncMock(return_value=None)

    with pytest.raises(ToolError, match="not found or is not permitted"):
        await adapter.call(
            "ma_api:config/providers/reload",
            {},
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=MagicMock(),
        )
    assert [record.outcome for record in audit_records] == ["authorization.denied"]
    assert called is False


async def test_valid_bearer_revalidation_uses_the_fresh_user_after_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-confirm execution uses the user returned by MA token validation."""
    _bypass_ma_argument_parser(monkeypatch)
    called = False
    fresh_user = SimpleNamespace(user_id="u1", enabled=True, role="admin")

    async def reload_provider() -> None:
        nonlocal called
        called = True

    adapter = _real_adapter(
        _handler("config/providers/reload", reload_provider, "config.providers.write"),
        allowed_capabilities={str(Capability.CONFIG_WRITE_PROVIDER)},
    )
    adapter.mass.webserver.auth.authenticate_with_token = AsyncMock(return_value=fresh_user)

    await adapter.call(
        "ma_api:config/providers/reload",
        {},
        response_mode="compact",
        fields=None,
        max_items=None,
        ctx=MagicMock(),
    )
    assert adapter.mass.webserver.auth.authenticate_with_token.await_count >= 2
    assert called is True


async def test_post_confirmation_revalidation_rejects_a_different_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bearer token cannot switch the adapter to another MA user identity."""
    _bypass_ma_argument_parser(monkeypatch)
    called = False

    async def reload_provider() -> None:
        nonlocal called
        called = True

    adapter = _real_adapter(
        _handler("config/providers/reload", reload_provider, "config.providers.write"),
        allowed_capabilities={str(Capability.CONFIG_WRITE_PROVIDER)},
    )
    adapter.mass.webserver.auth.authenticate_with_token = AsyncMock(
        return_value=SimpleNamespace(user_id="other-user", enabled=True, role="admin")
    )

    with pytest.raises(ToolError, match="not found or is not permitted"):
        await adapter.call(
            "ma_api:config/providers/reload",
            {},
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=MagicMock(),
        )
    assert called is False


async def test_target_filter_revoked_during_confirmation_prevents_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-confirm authorization re-applies the current user's target filters."""
    _bypass_ma_argument_parser(monkeypatch)
    called = False
    current_user: Any = SimpleNamespace(
        user_id="u1",
        enabled=True,
        role="user",
        player_filter=["kitchen"],
        provider_filter=[],
    )

    async def clear(queue_id: str) -> None:
        nonlocal called
        del queue_id
        called = True

    adapter = _real_adapter(
        _handler("player_queues/clear", clear, "queues.control"),
        allowed_capabilities={str(Capability.DELETE_QUEUE)},
        user=current_user,
    )
    adapter.mass.webserver.auth.get_user = AsyncMock(side_effect=lambda _user_id: current_user)
    adapter.mass.webserver.auth.authenticate_with_token = AsyncMock(
        side_effect=lambda _token: current_user
    )

    async def revoke_filter(*_args: Any, **_kwargs: Any) -> None:
        nonlocal current_user
        current_user = SimpleNamespace(
            user_id="u1",
            enabled=True,
            role="user",
            player_filter=["living-room"],
            provider_filter=[],
        )

    monkeypatch.setattr(adapter, "_confirm", AsyncMock(side_effect=revoke_filter))

    with pytest.raises(ToolError, match=r"\[not_found_or_forbidden\]"):
        await adapter.call(
            "ma_api:player_queues/clear",
            {"queue_id": "kitchen"},
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=MagicMock(),
        )
    assert called is False


async def test_secret_tag_revoked_during_confirmation_prevents_config_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The request-dependent secret guard runs again after the prompt returns."""
    _bypass_ma_argument_parser(monkeypatch)
    called = False
    state = {"secret": True}

    async def save_provider_config(
        provider_domain: str,
        values: dict[str, Any],
        instance_id: str | None = None,
    ) -> None:
        nonlocal called
        del provider_domain, values, instance_id
        called = True

    adapter = _real_adapter(
        _handler("config/providers/save", save_provider_config, "config.providers.write"),
        allowed_capabilities=set(),
    )
    adapter._test_allowed_capabilities_provider = lambda: {
        str(Capability.CONFIG_WRITE_PROVIDER),
        *({str(Capability.CONFIG_WRITE_SECRET)} if state["secret"] else set()),
    }
    adapter.mass.config.get_provider_config_entries = AsyncMock(
        return_value=[ConfigEntry(key="token", type=ConfigEntryType.SECURE_STRING, label="Token")]
    )

    async def revoke_secret(*_args: Any, **_kwargs: Any) -> None:
        state["secret"] = False

    monkeypatch.setattr(adapter, "_confirm", AsyncMock(side_effect=revoke_secret))

    with pytest.raises(ToolError, match=r"\[not_found_or_forbidden\]"):
        await adapter.call(
            "ma_api:config/providers/save",
            {
                "provider_domain": "demo",
                "instance_id": "demo--1",
                "values": {"token": "secret"},
            },
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=MagicMock(),
        )
    assert called is False


@pytest.mark.parametrize(
    ("command", "arguments", "getter_name", "getter_arguments"),
    [
        (
            "config/providers/get_value",
            {"instance_id": "demo--1", "key": "token"},
            "get_provider_config_entries",
            ("demo--1",),
        ),
        (
            "config/core/get_value",
            {"domain": "webserver", "key": "token"},
            "get_core_config_entries",
            ("webserver",),
        ),
        (
            "config/players/get_value",
            {"player_id": "kitchen", "key": "token"},
            "get_player_config_entries",
            ("kitchen",),
        ),
    ],
)
async def test_secure_config_value_is_reclassified_after_confirmation_before_serialization(
    command: str,
    arguments: dict[str, str],
    getter_name: str,
    getter_arguments: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refreshed config schema controls whether a native result is serialized masked."""
    _bypass_ma_argument_parser(monkeypatch)
    raw_secret = "super-secret-encrypted-token"
    confirmed = False

    async def get_value(
        key: str,
        instance_id: str | None = None,
        domain: str | None = None,
        player_id: str | None = None,
    ) -> str:
        assert {"instance_id": instance_id, "domain": domain, "player_id": player_id} == {
            "instance_id": arguments.get("instance_id"),
            "domain": arguments.get("domain"),
            "player_id": arguments.get("player_id"),
        }
        assert key == "token"
        return raw_secret

    adapter = _real_adapter(
        _handler(
            command,
            get_value,
            {
                "providers": "config.providers.read",
                "core": "config.core.read",
                "players": "config.players.read",
            }[command.split("/")[1]],
        ),
        allowed_capabilities={str(Capability.CONFIG_READ)},
    )
    schema_getter = AsyncMock(
        side_effect=lambda *_args: [
            ConfigEntry(
                key="token",
                type=(ConfigEntryType.SECURE_STRING if confirmed else ConfigEntryType.STRING),
                label="Token",
            )
        ]
    )
    setattr(adapter.mass.config, getter_name, schema_getter)

    async def confirm(*_args: Any, **_kwargs: Any) -> frozenset[str]:
        nonlocal confirmed
        confirmed = True
        return frozenset()

    monkeypatch.setattr(adapter, "_confirm", AsyncMock(side_effect=confirm))

    result = await adapter.call(
        f"ma_api:{command}",
        arguments,
        response_mode="full",
        fields=None,
        max_items=None,
        ctx=MagicMock(),
    )

    assert result["data"] == "this_value_is_encrypted"
    assert raw_secret not in json.dumps(result)
    assert schema_getter.await_count == 4
    schema_getter.assert_has_awaits(
        [
            call(*getter_arguments),
            call(*getter_arguments),
            call(*getter_arguments),
            call(*getter_arguments),
        ]
    )


async def test_secure_config_value_is_reclassified_after_execution_before_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A value made secure during execution is masked before serialization."""
    _bypass_ma_argument_parser(monkeypatch)
    raw_secret = "secret-created-during-execution"
    secure = False

    async def get_value(key: str, instance_id: str) -> str:
        nonlocal secure
        assert key == "token"
        assert instance_id == "demo--1"
        secure = True
        await asyncio.sleep(0)
        return raw_secret

    adapter = _real_adapter(
        _handler("config/providers/get_value", get_value, "config.providers.read"),
        allowed_capabilities={str(Capability.CONFIG_READ)},
    )
    schema_getter = AsyncMock(
        side_effect=lambda *_args: [
            ConfigEntry(
                key="token",
                type=ConfigEntryType.SECURE_STRING if secure else ConfigEntryType.STRING,
                label="Token",
            )
        ]
    )
    adapter.mass.config.get_provider_config_entries = schema_getter

    result = await adapter.call(
        "ma_api:config/providers/get_value",
        {"instance_id": "demo--1", "key": "token"},
        response_mode="full",
        fields=None,
        max_items=None,
        ctx=MagicMock(),
    )

    assert result["data"] == "this_value_is_encrypted"
    assert raw_secret not in json.dumps(result)
    assert schema_getter.await_count == 4


async def test_config_value_that_stops_being_secure_during_execution_stays_masked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A result classified secure before execution remains masked afterward."""
    _bypass_ma_argument_parser(monkeypatch)
    raw_secret = "secret-before-schema-change"
    secure = True

    async def get_value(key: str, domain: str) -> str:
        nonlocal secure
        assert key == "token"
        assert domain == "webserver"
        secure = False
        return raw_secret

    adapter = _real_adapter(
        _handler("config/core/get_value", get_value, "config.core.read"),
        allowed_capabilities={str(Capability.CONFIG_READ)},
    )
    adapter.mass.config.get_core_config_entries = AsyncMock(
        side_effect=lambda *_args: [
            ConfigEntry(
                key="token",
                type=ConfigEntryType.SECURE_STRING if secure else ConfigEntryType.STRING,
                label="Token",
            )
        ]
    )

    result = await adapter.call(
        "ma_api:config/core/get_value",
        {"domain": "webserver", "key": "token"},
        response_mode="full",
        fields=None,
        max_items=None,
        ctx=MagicMock(),
    )

    assert result["data"] == "this_value_is_encrypted"
    assert raw_secret not in json.dumps(result)


async def test_config_value_postflight_schema_failure_never_serializes_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed postflight classification rejects the result without exposing it."""
    _bypass_ma_argument_parser(monkeypatch)
    raw_secret = "secret-with-missing-postflight-schema"

    async def get_value(key: str, player_id: str) -> str:
        assert key == "token"
        assert player_id == "kitchen"
        return raw_secret

    adapter = _real_adapter(
        _handler("config/players/get_value", get_value, "config.players.read"),
        allowed_capabilities={str(Capability.CONFIG_READ)},
    )
    visible_entry = ConfigEntry(key="token", type=ConfigEntryType.STRING, label="Token")
    adapter.mass.config.get_player_config_entries = AsyncMock(
        side_effect=[[visible_entry], [visible_entry], RuntimeError("schema disappeared")]
    )

    with pytest.raises(ToolError, match=r"\[execution_failed\]") as error:
        await adapter.call(
            "ma_api:config/players/get_value",
            {"player_id": "kitchen", "key": "token"},
            response_mode="full",
            fields=None,
            max_items=None,
            ctx=MagicMock(),
        )

    assert raw_secret not in str(error.value)


async def test_flow_category_revoked_during_confirmation_prevents_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flow rechecks its exact provider/player permission after confirmation."""
    _bypass_ma_argument_parser(monkeypatch)
    called = False
    state = {"provider": True}

    async def submit_flow(flow_id: str, values: dict[str, Any]) -> None:
        nonlocal called
        del flow_id, values
        called = True

    adapter = _real_adapter(
        _handler("config/flows/submit", submit_flow),
        allowed_capabilities=set(),
    )
    adapter._test_allowed_capabilities_provider = lambda: {
        str(Capability.CONFIG_WRITE_PLAYER),
        *({str(Capability.CONFIG_WRITE_PROVIDER)} if state["provider"] else set()),
    }
    adapter.mass.config.get_setup_flow_required_scope = lambda _flow_id: "config.providers.write"
    step = SimpleNamespace(
        entries=[ConfigEntry(key="name", type=ConfigEntryType.STRING, label="Name")]
    )
    adapter.mass.config.get_setup_flow = AsyncMock(return_value=step)
    adapter.mass.config._setup_flows = {
        "provider-flow": SimpleNamespace(
            session=SimpleNamespace(current_step=step),
        )
    }

    async def revoke_provider_category(*_args: Any, **_kwargs: Any) -> None:
        state["provider"] = False

    monkeypatch.setattr(adapter, "_confirm", AsyncMock(side_effect=revoke_provider_category))

    with pytest.raises(ToolError, match=r"\[not_found_or_forbidden\]"):
        await adapter.call(
            "ma_api:config/flows/submit",
            {"flow_id": "provider-flow", "values": {"name": "Kitchen"}},
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=MagicMock(),
        )
    assert called is False


async def test_player_only_tag_executes_a_player_setup_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catalog any-of visibility still permits the matching live flow category."""
    _bypass_ma_argument_parser(monkeypatch)
    called = False

    async def submit_flow(flow_id: str, values: dict[str, Any]) -> None:
        nonlocal called
        del flow_id, values
        called = True

    adapter = _real_adapter(
        _handler("config/flows/submit", submit_flow),
        allowed_capabilities={str(Capability.CONFIG_WRITE_PLAYER)},
    )
    adapter.mass.config.get_setup_flow_required_scope = lambda _flow_id: "config.players.write"
    step = SimpleNamespace(
        entries=[ConfigEntry(key="name", type=ConfigEntryType.STRING, label="Name")]
    )
    adapter.mass.config.get_setup_flow = AsyncMock(return_value=step)
    adapter.mass.config._setup_flows = {
        "player-flow": SimpleNamespace(session=SimpleNamespace(current_step=step))
    }

    await adapter.call(
        "ma_api:config/flows/submit",
        {"flow_id": "player-flow", "values": {"name": "Kitchen"}},
        response_mode="compact",
        fields=None,
        max_items=None,
        ctx=MagicMock(),
    )
    assert called is True


async def test_provider_setup_flow_rejects_player_only_tag_before_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catalog visibility does not let a player capability invoke a provider flow."""
    _bypass_ma_argument_parser(monkeypatch)

    async def submit_flow(flow_id: str, values: dict[str, Any]) -> None:
        del flow_id, values

    adapter = _real_adapter(
        _handler("config/flows/submit", submit_flow),
        allowed_capabilities={str(Capability.CONFIG_WRITE_PLAYER)},
    )
    adapter.mass.config.get_setup_flow_required_scope = lambda _flow_id: "config.providers.write"
    adapter.mass.config.get_setup_flow = AsyncMock(
        return_value=SimpleNamespace(
            entries=[ConfigEntry(key="name", type=ConfigEntryType.STRING, label="Name")]
        )
    )

    with pytest.raises(ToolError, match=r"\[not_found_or_forbidden\]"):
        await adapter.call(
            "ma_api:config/flows/submit",
            {"flow_id": "provider-flow", "values": {"name": "Kitchen"}},
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=MagicMock(),
        )


async def test_native_config_secret_denial_precedes_confirmation_and_target() -> None:
    """Native config secret preflight rejects before elicitation or mutation."""
    called = False

    async def save_provider_config(
        provider_domain: str,
        values: dict[str, Any],
        instance_id: str | None = None,
    ) -> None:
        nonlocal called
        del provider_domain, values, instance_id
        called = True

    adapter = _real_adapter(
        _handler(
            "config/providers/save",
            save_provider_config,
            "config.providers.write",
        ),
        allowed_capabilities={str(Capability.CONFIG_WRITE_PROVIDER)},
    )
    adapter.mass.config.get_provider_config_entries = AsyncMock(
        return_value=[ConfigEntry(key="token", type=ConfigEntryType.SECURE_STRING, label="Token")]
    )
    with pytest.raises(ToolError, match=r"\[not_found_or_forbidden\]"):
        await adapter.call(
            "ma_api:config/providers/save",
            {
                "provider_domain": "demo",
                "instance_id": "demo--1",
                "values": {"token": "secret"},
            },
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=MagicMock(),
        )
    assert called is False


async def test_impersonation_is_authorized_before_confirmation_and_execution() -> None:
    """A caller without impersonation scope cannot elicit or run as another user."""
    called = False

    async def operation() -> None:
        nonlocal called
        called = True

    caller = SimpleNamespace(
        user_id="u1",
        username="caller",
        enabled=True,
        role="guest",
        player_filter=[],
        provider_filter=[],
    )
    target = SimpleNamespace(
        user_id="u2",
        username="target",
        enabled=True,
        role="guest",
        player_filter=[],
        provider_filter=[],
    )
    handler = _handler("music/search", operation, "library.read")
    handler.allow_impersonation = True
    adapter = _real_adapter(handler, user=caller)
    adapter.mass.webserver.auth.get_user = AsyncMock(
        side_effect=lambda identifier: caller if identifier == "u1" else target
    )
    adapter.mass.webserver.auth.get_user_by_username = AsyncMock(return_value=None)
    with pytest.raises(ToolError, match=r"\[execution_failed\]"):
        await adapter.call(
            "ma_api:music/search",
            {"user": "u2"},
            response_mode="compact",
            fields=None,
            max_items=None,
            ctx=MagicMock(),
        )
    assert called is False
