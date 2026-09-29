"""Request-policy visibility middleware for FastMCP components."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from fastmcp.exceptions import NotFoundError, PromptError, ResourceError, ToolError
from fastmcp.server.middleware import Middleware

if TYPE_CHECKING:
    from fastmcp.server.middleware.middleware import CallNext, MiddlewareContext

    from .policy import PolicySnapshot
    from .resource_authorization import ResourceAuthorizer


ComponentKind = Literal["tool", "resource", "prompt"]
TagsLookup = Callable[[ComponentKind, str], Awaitable[set[str] | None]]


class TagFilterMiddleware(Middleware):  # type: ignore[misc, unused-ignore]
    """
    Filter FastMCP component tags through the current request policy snapshot.

    ``Middleware`` is typed as ``Any`` upstream; under
    ``disallow_subclassing_any`` we suppress the misc-rule on the class
    line rather than every method.
    """

    def __init__(
        self,
        lookup_component_tags: TagsLookup,
        policy_provider: Callable[[], PolicySnapshot],
        resource_authorizer: ResourceAuthorizer | None = None,
        prompts_enabled_provider: Callable[[], bool] | None = None,
    ) -> None:
        """
        Initialise the middleware.

        :param lookup_component_tags: async ``(kind, key) -> set[str] | None``
            that resolves a tool name / resource URI / prompt name back to its
            tag set. Returns ``None`` when the component does not exist (treat
            as blocked: a stale cached name from a prior permission set must
            not slip through).
        """
        super().__init__()
        self._lookup = lookup_component_tags
        self._policy = policy_provider
        self._resource_authorizer = resource_authorizer
        self._prompts_enabled = prompts_enabled_provider or (lambda: True)

    # ── filtered listings ────────────────────────────────────────────────────

    async def on_list_tools(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Sequence[Any]],
    ) -> Sequence[Any]:
        """Drop tools whose tags are all disabled."""
        items = await call_next(context)
        return [t for t in items if self._is_visible("tool", t)]

    async def on_list_resources(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Sequence[Any]],
    ) -> Sequence[Any]:
        """Drop resources whose tags are all disabled."""
        items = await call_next(context)
        return [r for r in items if await self._resource_is_visible(r)]

    async def on_list_resource_templates(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Sequence[Any]],
    ) -> Sequence[Any]:
        """Drop resource templates whose tags are all disabled."""
        items = await call_next(context)
        return [r for r in items if await self._resource_is_visible(r)]

    async def on_list_prompts(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Sequence[Any]],
    ) -> Sequence[Any]:
        """Drop prompts whose tags are all disabled."""
        items = await call_next(context)
        return [p for p in items if self._is_visible("prompt", p)]

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
        if self._resource_authorizer is None:
            await self._reject_if_hidden("resource", uri)
            return await call_next(context)
        tags = await self._lookup("resource", uri)
        if tags == set():
            # Untagged catalog infrastructure performs its own request-bound
            # dynamic authorization and remains permanently addressable.
            return await call_next(context)
        # Unknown/cached URIs go through the authorizer with a fixed unknown
        # capability so their denial is audited without recording the URI.
        request = await self._resource_authorizer.authorize(uri, tags or set())
        if request is None:  # pragma: no cover - raising direct authorization is contractual
            raise ResourceError("Resource is not permitted")
        from .resource_authorization import (  # noqa: PLC0415
            bind_resource_request,
            reset_resource_request,
        )

        token = bind_resource_request(request)
        try:
            return await call_next(context)
        finally:
            reset_resource_request(token)

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

    def _is_visible(self, kind: ComponentKind, component: Any) -> bool:
        tags = {str(t) for t in (getattr(component, "tags", None) or set())}
        if kind == "prompt":
            return self._prompts_enabled()
        if not tags:
            return True
        from .policy import PolicyMode  # noqa: PLC0415

        policy = self._policy()
        if kind == "resource":
            return any(policy.mode(tag) is PolicyMode.ALLOW for tag in tags)
        return any(policy.mode(tag) is not PolicyMode.DENY for tag in tags)

    async def _resource_is_visible(self, component: Any) -> bool:
        tags = {str(t) for t in (getattr(component, "tags", None) or set())}
        if not tags:
            return True
        # Listings are filtered only by the request policy snapshot.  Full
        # identity, scope and target authorization belongs to the actual read
        # path; doing it here makes an unauthenticated list request hide every
        # otherwise-visible resource and performs unnecessary MA lookups.
        return self._is_visible("resource", component)

    async def _reject_if_hidden(self, kind: ComponentKind, key: str) -> set[str]:
        if not key:
            return set()
        tags = await self._lookup(kind, key)
        if tags is None:
            # Component doesn't exist (or is itself disabled at the FastMCP layer).
            # Surface a NotFoundError so the SDK returns the spec-correct
            # "method-not-allowed" / "not-found" path rather than 500.
            msg = f"{kind.capitalize()} {key!r} not found"
            raise NotFoundError(msg)
        if kind == "prompt":
            if not self._prompts_enabled():
                raise PromptError(f"Prompt {key!r} is currently disabled by configuration")
            return tags
        if tags:
            from .policy import PolicyMode  # noqa: PLC0415

            modes = [self._policy().mode(tag) for tag in tags]
            visible = (
                any(mode is PolicyMode.ALLOW for mode in modes)
                if kind == "resource"
                else any(mode is not PolicyMode.DENY for mode in modes)
            )
            if visible:
                return tags
            msg = f"{kind.capitalize()} {key!r} is not allowed by request policy"
            raise self._ERROR_BY_KIND[kind](msg)
        return tags
