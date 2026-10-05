"""
Tag-filter middleware: hide tools / resources / prompts whose tags are disabled.

FastMCP v3's built-in ``restrict_tag`` is scope-based authorization (token must
carry a specific OAuth scope). What we need here is **config-driven visibility**:
the operator toggles a permission boolean and the corresponding tools simply
disappear from listings — no error path, no permission-denied trace.

This middleware reads ``allowed_tags`` from a closure (so we can swap the set in
place when ``MCPServerProvider.update_config`` runs without rebuilding the
FastMCP server), and applies the rule:

* a component with **at least one** allowed tag is exposed
* a component with **no** tags is exposed (treat as always-on infrastructure)
* a component whose tags are **all** disabled is hidden / blocked

Listings are filtered post-hoc; direct invocations (``tools/call``,
``resources/read``, ``prompts/get``) look the component up by name/URI and
apply the same rule. A client that cached a tool name from an earlier
permission set therefore cannot reach a now-disabled tool.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from fastmcp.exceptions import NotFoundError, PromptError, ResourceError, ToolError
from fastmcp.server.middleware import Middleware

if TYPE_CHECKING:
    from fastmcp.server.middleware.middleware import CallNext, MiddlewareContext


ComponentKind = Literal["tool", "resource", "prompt"]
TagsLookup = Callable[[ComponentKind, str], Awaitable[set[str] | None]]


def tags_visible(tags: set[str] | None, allowed: set[str]) -> bool:
    """
    Apply the shared visibility rule for a component's tag set.

    ``None`` means the component is unknown (blocked); an empty set means
    untagged always-on infrastructure; otherwise at least one tag must be
    allowed.

    :param tags: The component's tag set, or ``None`` when it does not exist.
    :param allowed: The currently allowed tag set.
    """
    if tags is None:
        return False
    if not tags:
        return True
    return any(str(t) in allowed for t in tags)


class TagFilterMiddleware(Middleware):  # type: ignore[misc, unused-ignore]
    """
    Hide tools, resources, and prompts whose tags are not in ``allowed_tags``.

    ``Middleware`` is typed as ``Any`` upstream; under
    ``disallow_subclassing_any`` we suppress the misc-rule on the class
    line rather than every method.
    """

    def __init__(
        self,
        allowed_tags_provider: Callable[[], set[str]],
        lookup_component_tags: TagsLookup,
    ) -> None:
        """
        Initialise the middleware.

        :param allowed_tags_provider: zero-arg callable returning the *current*
            set of allowed tags. Wrapped in a callable so the operator can
            change permission flags without restarting the runtime.
        :param lookup_component_tags: async ``(kind, key) -> set[str] | None``
            that resolves a tool name / resource URI / prompt name back to its
            tag set. Returns ``None`` when the component does not exist (treat
            as blocked: a stale cached name from a prior permission set must
            not slip through).
        """
        super().__init__()
        self._allowed = allowed_tags_provider
        self._lookup = lookup_component_tags

    # ── filtered listings ────────────────────────────────────────────────────

    async def on_list_tools(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Sequence[Any]],
    ) -> Sequence[Any]:
        """Drop tools whose tags are all disabled."""
        items = await call_next(context)
        return [t for t in items if self._is_visible(t)]

    async def on_list_resources(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Sequence[Any]],
    ) -> Sequence[Any]:
        """Drop resources whose tags are all disabled."""
        items = await call_next(context)
        return [r for r in items if self._is_visible(r)]

    async def on_list_resource_templates(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Sequence[Any]],
    ) -> Sequence[Any]:
        """Drop resource templates whose tags are all disabled."""
        items = await call_next(context)
        return [r for r in items if self._is_visible(r)]

    async def on_list_prompts(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Sequence[Any]],
    ) -> Sequence[Any]:
        """Drop prompts whose tags are all disabled."""
        items = await call_next(context)
        return [p for p in items if self._is_visible(p)]

    # ── invocation guards ────────────────────────────────────────────────────

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        """Block calls to tools whose tag set has been disabled."""
        name = getattr(context.message, "name", "")
        await self._reject_if_hidden("tool", name)
        return await call_next(context)

    async def on_read_resource(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        """Block reads of resources whose tag set has been disabled."""
        uri = str(getattr(context.message, "uri", ""))
        await self._reject_if_hidden("resource", uri)
        return await call_next(context)

    async def on_get_prompt(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        """Block reads of prompts whose tag set has been disabled."""
        name = getattr(context.message, "name", "")
        await self._reject_if_hidden("prompt", name)
        return await call_next(context)

    # Error class chosen so the SDK reports the failure under the right RPC
    # method (tools/resources/prompts) rather than always as a tool error.
    _ERROR_BY_KIND: ClassVar[dict[ComponentKind, type[Exception]]] = {
        "tool": ToolError,
        "resource": ResourceError,
        "prompt": PromptError,
    }

    # ── helpers ──────────────────────────────────────────────────────────────

    def _is_visible(self, component: Any) -> bool:
        tags = {str(t) for t in (getattr(component, "tags", None) or set())}
        return tags_visible(tags, self._allowed())

    async def _reject_if_hidden(self, kind: ComponentKind, key: str) -> None:
        if not key:
            return
        tags = await self._lookup(kind, key)
        if tags is None:
            # Component doesn't exist (or is itself disabled at the FastMCP layer).
            # Surface a NotFoundError so the SDK returns the spec-correct
            # "method-not-allowed" / "not-found" path rather than 500.
            msg = f"{kind.capitalize()} {key!r} not found"
            raise NotFoundError(msg)
        if not tags_visible(tags, self._allowed()):
            msg = f"{kind.capitalize()} {key!r} is currently disabled by configuration"
            raise self._ERROR_BY_KIND[kind](msg)
