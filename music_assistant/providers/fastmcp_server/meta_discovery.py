"""Permanent three-tool discovery surface for the dynamic MA API catalog."""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
import unicodedata
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, Any, Literal, Protocol, cast

from fastmcp import Context  # noqa: TC002  -- FastMCP resolves injected annotations at runtime.
from fastmcp.exceptions import NotFoundError, ToolError
from mcp.types import ToolAnnotations
from pydantic import WithJsonSchema

from .catalog import (
    CatalogFingerprint,
    CatalogSnapshot,
    CatalogView,
    DynamicEntry,
    RequestCatalogContext,
)
from .catalog_pagination import (
    CURSOR_VERSION,
    CursorState,
    DiscoveryItem,
    DiscoveryMode,
    DiscoveryPage,
    PaginationError,
    catalog_revision,
    decode_cursor,
    encode_cursor,
    normalize_query,
    resolve_limit,
)
from .catalog_resource import register_catalog_resource
from .errors import ToolFailureCode, tool_failure

if TYPE_CHECKING:
    from fastmcp import FastMCP

GET_TOOL_SCHEMA_NAME = "get_tool_schema"
CALL_TOOL_NAME = "call_tool"
SEARCH_TOOL_NAME = "search_tools"
_META_NAMES = {CALL_TOOL_NAME, SEARCH_TOOL_NAME, GET_TOOL_SCHEMA_NAME}
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_ARGUMENTS_CONTAINER_ERROR = "arguments must be an object or a JSON-encoded object"
_FIELDS_CONTAINER_ERROR = "fields must be an array of strings or a JSON-encoded array of strings"


def _reject_nonstandard_json_constant(value: str) -> None:
    """Reject JSON extensions such as NaN and Infinity."""
    raise ValueError(value)


def _normalize_arguments_container(value: Any) -> dict[str, Any]:
    """Accept a native object or decode exactly one JSON object layer."""
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value, parse_constant=_reject_nonstandard_json_constant)
        except ValueError:
            raise tool_failure(
                ToolFailureCode.INVALID_ARGUMENTS,
                _ARGUMENTS_CONTAINER_ERROR,
            ) from None
    if not isinstance(value, dict):
        raise tool_failure(
            ToolFailureCode.INVALID_ARGUMENTS,
            _ARGUMENTS_CONTAINER_ERROR,
        )
    return value


def _normalize_fields_container(value: Any) -> list[str] | None:
    """Accept a native string array or decode exactly one JSON array layer."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value, parse_constant=_reject_nonstandard_json_constant)
        except ValueError:
            raise tool_failure(
                ToolFailureCode.INVALID_ARGUMENTS,
                _FIELDS_CONTAINER_ERROR,
            ) from None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise tool_failure(
            ToolFailureCode.INVALID_ARGUMENTS,
            _FIELDS_CONTAINER_ERROR,
        )
    return value


def _discovery_policy_mode(entry: DynamicEntry) -> Literal["allow", "confirm"]:
    """Return the public mode after request visibility has excluded deny."""
    return cast("Literal['allow', 'confirm']", entry.policy_mode.value)


class DynamicAdapter(Protocol):
    """Direct discovery contract implemented by the MA dispatcher."""

    async def base_snapshot(self) -> CatalogSnapshot:
        """Return the immutable compiled catalog for the live registry."""

    async def visible_catalog(self) -> CatalogView:
        """Return entries visible for the current request."""

    async def catalog_context(self) -> RequestCatalogContext:
        """Return a base snapshot and request view from one generation."""

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        response_mode: str,
        fields: list[str] | None,
        max_items: int | None,
        ctx: Context,
    ) -> dict[str, Any]:
        """Execute an entry and return its bounded envelope."""


@dataclass(frozen=True, slots=True)
class SearchIndex:
    """Immutable token statistics for one base catalog fingerprint."""

    fingerprint: CatalogFingerprint
    documents: Mapping[str, tuple[str, ...]]
    frequencies: Mapping[str, Mapping[str, int]]
    document_frequencies: Mapping[str, int]
    average_length: float


def _tokens(value: str) -> list[str]:
    """Tokenize names and descriptions with Unicode-aware normalization."""
    normalized = unicodedata.normalize("NFKC", value).casefold().replace("/", " ")
    return _TOKEN_RE.findall(normalized.replace("_", " "))


def _build_search_index(snapshot: CatalogSnapshot) -> SearchIndex:
    """Compile immutable BM25 documents once for a base catalog snapshot."""
    documents: dict[str, tuple[str, ...]] = {}
    frequencies: dict[str, Mapping[str, int]] = {}
    document_frequencies: Counter[str] = Counter()
    for entry in snapshot.entries:
        document = tuple(_tokens(" ".join((entry.name, entry.description, *entry.search_aliases))))
        documents[entry.name] = document
        frequency = Counter(document)
        frequencies[entry.name] = MappingProxyType(dict(frequency))
        document_frequencies.update(frequency.keys())
    average_length = sum(map(len, documents.values())) / len(documents) if documents else 1.0
    return SearchIndex(
        fingerprint=snapshot.fingerprint,
        documents=MappingProxyType(documents),
        frequencies=MappingProxyType(frequencies),
        document_frequencies=MappingProxyType(dict(document_frequencies)),
        average_length=average_length or 1.0,
    )


def _rank(index: SearchIndex, query_tokens: list[str], *, allowed_names: set[str]) -> list[str]:
    """Rank only the current request's visible catalog intersection."""
    if not query_tokens:
        return []
    document_count = len(index.documents)
    if not document_count:
        return []
    normalized_query = " ".join(query_tokens)
    scored: list[tuple[float, str]] = []
    for name in allowed_names:
        document = index.documents.get(name)
        frequencies = index.frequencies.get(name)
        if document is None or frequencies is None:
            continue
        score = 0.0
        for token in query_tokens:
            frequency = frequencies.get(token, 0)
            if not frequency:
                continue
            document_frequency = index.document_frequencies.get(token, 0)
            inverse = math.log(
                1 + (document_count - document_frequency + 0.5) / (document_frequency + 0.5)
            )
            denominator = frequency + 1.5 * (1 - 0.75 + 0.75 * len(document) / index.average_length)
            score += inverse * frequency * 2.5 / denominator
        normalized_name = " ".join(_tokens(name))
        if normalized_query == normalized_name:
            score += 100.0
        elif normalized_name.startswith(normalized_query):
            score += 25.0
        if score > 0:
            scored.append((score, name))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [name for _score, name in scored]


class MetaDiscoveryService:
    """Search and schema lookup over the adapter's cached catalog snapshots."""

    def __init__(self, adapter: DynamicAdapter) -> None:
        """Initialise a request-safe cache for immutable catalog indexes."""
        self.adapter = adapter
        self._index: SearchIndex | None = None
        self._index_lock = asyncio.Lock()
        self.index_build_count = 0

    async def discover(
        self,
        query: str | None = None,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        include_top_schema: bool = False,
    ) -> DiscoveryPage:
        """Return one visible ranked-search or alphabetical-catalog page."""
        started = time.perf_counter()
        explicit_query = normalize_query(query)
        if include_top_schema and (not explicit_query or cursor is not None):
            raise PaginationError(
                "invalid_arguments",
                "include_top_schema requires a non-empty query and no cursor",
            )
        state = decode_cursor(cursor) if cursor is not None else None
        mode: DiscoveryMode
        if state is not None:
            if query is not None and explicit_query != state.query:
                raise PaginationError("invalid_cursor", "cursor query does not match query")
            mode = state.mode
            normalized_query = state.query
            offset = state.offset
        else:
            mode = "search" if explicit_query else "catalog"
            normalized_query = explicit_query
            offset = 0
        page_limit = resolve_limit(mode, limit)

        context = await self.adapter.catalog_context()
        snapshot = context.snapshot
        view = context.view
        visible = {entry.name: entry for entry in view.entries}
        revision = catalog_revision(snapshot.fingerprint, view.entries)
        if state is not None and state.revision != revision:
            raise PaginationError(
                "catalog_changed",
                "catalog changed; restart pagination without a cursor",
            )

        index: SearchIndex | None = None
        if mode == "search":
            index = await self._index_for(snapshot)
        ordered_items: list[DiscoveryItem]
        if mode == "search":
            if index is None:
                raise PaginationError("catalog_changed", "catalog search index is unavailable")
            names = _rank(index, _tokens(normalized_query), allowed_names=set(visible))
            ordered_items = [
                {
                    "name": name,
                    "description": visible[name].description,
                    "policy_mode": _discovery_policy_mode(visible[name]),
                }
                for name in names
            ]
        else:
            ordered_items = [
                {"name": name, "policy_mode": _discovery_policy_mode(visible[name])}
                for name in sorted(visible)
            ]

        total = len(ordered_items)
        if state is not None and offset >= total:
            raise PaginationError("invalid_cursor", "cursor offset is outside the result set")
        page_items = ordered_items[offset : offset + page_limit]
        if include_top_schema and page_items:
            top_entry = visible[page_items[0]["name"]]
            page_items[0]["schema"] = _schema_result(top_entry)
        next_offset = offset + len(page_items)
        next_cursor = (
            encode_cursor(
                CursorState(
                    version=CURSOR_VERSION,
                    mode=mode,
                    query=normalized_query,
                    offset=next_offset,
                    revision=revision,
                )
            )
            if next_offset < total
            else None
        )
        result: DiscoveryPage = {
            "mode": mode,
            "items": page_items,
            "total": total,
            "next_cursor": next_cursor,
            "catalog_revision": revision,
        }
        recorder = getattr(self.adapter, "record_performance", None)
        if callable(recorder):
            recorder((time.perf_counter() - started) * 1000)
        return result

    async def get_schema(self, tool_name: str) -> dict[str, Any]:
        """Return one current request-visible entry's complete schema descriptor."""
        entry = (await self.adapter.catalog_context()).view.by_name.get(tool_name)
        if entry is None:
            raise NotFoundError(f"Tool {tool_name!r} not found")
        return _schema_result(entry)

    async def _index_for(self, snapshot: CatalogSnapshot) -> SearchIndex:
        """Return the snapshot's index, building it once across concurrent callers."""
        if self._index is not None and self._index.fingerprint == snapshot.fingerprint:
            return self._index
        async with self._index_lock:
            if self._index is None or self._index.fingerprint != snapshot.fingerprint:
                self._index = await self._build_index(snapshot)
                self.index_build_count += 1
            return self._index

    async def _build_index(self, snapshot: CatalogSnapshot) -> SearchIndex:
        """Build index data synchronously behind the async singleflight lock."""
        return _build_search_index(snapshot)


def _schema_result(entry: DynamicEntry) -> dict[str, Any]:
    """Serialize the dynamic schema only after an exact visible-name lookup."""
    result: dict[str, Any] = {
        "name": entry.name,
        "kind": entry.name.split(":", 1)[0],
        "command": entry.command,
        "description": entry.description,
        "inputSchema": entry.input_schema,
        "requiredScope": entry.required_scope,
        "allowImpersonation": entry.allow_impersonation,
        "annotations": entry.annotations,
        "policy_mode": entry.policy_mode.value,
    }
    if entry.output_schema is not None:
        result["outputSchema"] = entry.output_schema
    return result


def register_meta_discovery(
    mcp: FastMCP,
    *,
    dynamic_adapter: DynamicAdapter,
) -> None:
    """Register the permanent direct three-tool discovery surface."""
    service = MetaDiscoveryService(dynamic_adapter)
    register_catalog_resource(mcp, service)

    @mcp.tool(
        name=SEARCH_TOOL_NAME,
        annotations=ToolAnnotations(
            title="Search tools",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def search_tools(
        query: str | None = None,
        cursor: str | None = None,
        limit: Annotated[Any, WithJsonSchema({"type": "integer"})] = None,
        include_top_schema: bool = False,
    ) -> DiscoveryPage:
        """Search visible commands or browse them with an opaque cursor."""
        try:
            return await service.discover(
                query,
                cursor=cursor,
                limit=limit,
                include_top_schema=include_top_schema,
            )
        except PaginationError as exc:
            code = (
                ToolFailureCode.CATALOG_CHANGED
                if exc.code == "catalog_changed"
                else ToolFailureCode.INVALID_ARGUMENTS
            )
            raise tool_failure(code, str(exc)) from exc

    @mcp.tool(
        name=GET_TOOL_SCHEMA_NAME,
        annotations=ToolAnnotations(
            title="Get tool schema",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def get_tool_schema(tool_name: str) -> dict[str, Any]:
        """
        Return the full schema for one catalogued tool.

        Use ``search_tools`` first to find candidate tool names, then fetch
        the schema of the one you intend to invoke via ``call_tool``.

        :param tool_name: Exact canonical ``ma_api:*`` name from ``search_tools``.
        """
        try:
            return await service.get_schema(tool_name)
        except NotFoundError as exc:
            raise tool_failure(
                ToolFailureCode.NOT_FOUND_OR_FORBIDDEN,
                "Tool was not found or is not permitted",
            ) from exc

    @mcp.tool(name=CALL_TOOL_NAME)  # type: ignore[untyped-decorator, unused-ignore]
    async def call_tool(
        name: str,
        arguments: Annotated[
            Any,
            WithJsonSchema({"type": "object", "additionalProperties": True}),
        ]
        | None = None,
        response_mode: str = "compact",
        fields: Annotated[
            Any,
            WithJsonSchema({"type": "array", "items": {"type": "string"}}),
        ]
        | None = None,
        max_items: int | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """
        Execute a canonical ``ma_api:*`` command found with ``search_tools``.

        :param name: Canonical ``ma_api:*`` name.
        :param arguments: Command arguments from get_tool_schema.
        :param response_mode: ``compact`` (default) or explicit ``full``.
        :param fields: Optional top-level fields to retain.
        :param max_items: Optional smaller item limit.
        """
        if not name.startswith("ma_api:"):
            raise tool_failure(
                ToolFailureCode.INVALID_ARGUMENTS,
                "Tool name must be a canonical ma_api command",
            )
        if ctx is None:  # pragma: no cover - FastMCP always injects Context
            raise ToolError("MCP request context is unavailable")
        return await dynamic_adapter.call(
            name,
            _normalize_arguments_container(arguments),
            response_mode=response_mode,
            fields=_normalize_fields_container(fields),
            max_items=max_items,
            ctx=ctx,
        )

    # Only the permanent discovery surface is exposed to model clients.
    mcp.disable(components={"tool"})
    mcp.enable(names=_META_NAMES, components={"tool"})
