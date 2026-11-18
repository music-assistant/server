"""
Pages a provider serves, behind a short-lived address bound to a user and a provider instance.

A provider declares its page in its manifest. The ``providers/open_page`` command hands the
calling user an address under ``/provider_page/<session>/``; the server re-checks the user,
scope and instance on every request to it, and serves every response of that route sandboxed
(an opaque origin), so a page can not read the app's token or storage even though it shares the
server's host and port.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from aiohttp import hdrs, web
from music_assistant_models.errors import ActionUnavailable

from music_assistant.controllers.webserver.helpers.auth_middleware import (
    has_scope,
    user_auth_context,
)

if TYPE_CHECKING:
    from music_assistant_models.auth import Scope

    from music_assistant.controllers.webserver import WebserverController
    from music_assistant.models.provider import Provider

PROVIDER_PAGE_ROUTE: Final = "provider_page"
# a page address lapses after this long without a request, and after the max age in any case
PAGE_IDLE_TIMEOUT: Final = 15 * 60
PAGE_MAX_AGE: Final = 12 * 3600
# opening a page once more drops the oldest address of that user beyond this many
MAX_PAGES_PER_USER: Final = 20
# no allow-same-origin: the page gets an opaque origin, so it can not read the app's storage or
# token; no allow-downloads (usage policy)
PAGE_CSP: Final = (
    "sandbox allow-scripts allow-forms allow-popups allow-modals; frame-ancestors 'none'"
)
PAGE_HEADERS: Final = {
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
    # the page's own requests come from its opaque origin; they carry no credentials, the
    # secret address is the credential
    "Access-Control-Allow-Origin": "*",
}
# headers that act on the server's host or origin rather than on the page's own document, so
# the sandbox does not contain them: cookies, wiping the app's storage (Clear-Site-Data), https
# forced on every port of the host (Strict-Transport-Security), traffic for the origin sent
# elsewhere (Alt-Svc), a wider service worker scope (Service-Worker-Allowed) and client hints
# remembered for the origin (Accept-CH, Critical-CH)
ORIGIN_WIDE_HEADERS: Final = (
    "Set-Cookie",
    "Clear-Site-Data",
    "Strict-Transport-Security",
    "Alt-Svc",
    "Service-Worker-Allowed",
    "Accept-CH",
    "Critical-CH",
)
# headers that would have a browser report the page's address elsewhere
REPORTING_HEADERS: Final = (
    "Content-Security-Policy-Report-Only",
    "Report-To",
    "Reporting-Endpoints",
    "NEL",
)
REPORTING_DIRECTIVES: Final = frozenset({"report-uri", "report-to"})
PREFLIGHT_HEADERS: Final = {
    "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE",
    "Access-Control-Allow-Headers": "Content-Type",
}


@dataclass
class _PageSession:
    """A page address handed to one user for one loaded provider instance."""

    user_id: str
    provider: Provider
    scope: Scope
    created: float
    last_used: float
    # drops the address at its max age
    max_age_timer: asyncio.TimerHandle
    # the requests to the page still being served, a WebSocket or streamed response included
    requests: set[asyncio.Task[object]] = field(default_factory=set)


class ProviderPageManager:
    """Issue and serve the short-lived addresses of the pages providers serve."""

    def __init__(self, webserver: WebserverController) -> None:
        """
        Initialize the provider page manager.

        :param webserver: The webserver controller instance.
        """
        self.webserver = webserver
        self.mass = webserver.mass
        self._sessions: dict[str, _PageSession] = {}

    def close(self) -> None:
        """Drop all page addresses."""
        self._drop(lambda _session: True)

    def create_address(self, user_id: str, provider: Provider, scope: Scope) -> str:
        """
        Create a fresh address of the page of a loaded provider instance, for one user.

        Returns the address relative to the server's base url, which opens the page's root. It
        never serves another instance loaded later under the same instance id.

        :param user_id: The user the address is for.
        :param provider: The provider instance that serves the page.
        :param scope: The scope of the page, which every request to it re-checks.
        """
        now = time.monotonic()
        self._drop(lambda session: _expired(session, now))
        own = [key for key, session in self._sessions.items() if session.user_id == user_id]
        # the dict keeps insertion order, so the oldest come first
        for key in own[: max(0, len(own) - MAX_PAGES_PER_USER + 1)]:
            self._drop_session(key)
        session_id = secrets.token_urlsafe(32)
        timer = self.mass.loop.call_later(PAGE_MAX_AGE, self._drop_session, session_id)
        self._sessions[session_id] = _PageSession(user_id, provider, scope, now, now, timer)
        return f"{PROVIDER_PAGE_ROUTE}/{session_id}/"

    def drop_user(self, user_id: str) -> None:
        """
        Drop all page addresses of a user, and close the requests still being served on them.

        :param user_id: The user whose addresses to drop.
        """
        self._drop(lambda session: session.user_id == user_id)

    def drop_instance(self, instance_id: str) -> None:
        """
        Drop all page addresses of a provider instance, and close the requests still being served.

        :param instance_id: The provider instance whose addresses to drop.
        """
        self._drop(lambda session: session.provider.instance_id == instance_id)

    async def handle_request(self, request: web.Request) -> web.StreamResponse:
        """
        Serve a request to a provider page, after checking its address, user, scope and instance.

        :param request: The request to the page route.
        """
        session_id = request.match_info["session"]
        session = self._sessions.get(session_id)
        now = time.monotonic()
        if session is None or _expired(session, now):
            if session is not None:
                self._drop_session(session_id)
            return _expired_response()
        user = await self.webserver.auth.get_user(session.user_id)
        if user is None or not has_scope(user, session.scope):
            return web.Response(status=403, text="You can no longer open this page.")
        provider = next(
            (
                provider
                for provider in self.mass.get_providers_for_user(user)
                if provider.instance_id == session.provider.instance_id
            ),
            None,
        )
        if provider is not None and provider is not session.provider:
            # the instance was reloaded or replaced since the address was handed out
            self._drop_session(session_id)
            return _expired_response()
        if provider is None or not provider.available:
            return web.Response(status=503, text="This page is not available right now.")
        session.last_used = now
        if request.method == hdrs.METH_OPTIONS:
            # the CORS preflight of the page's own requests from its opaque origin
            return web.Response(status=204, headers=PREFLIGHT_HEADERS)
        task = asyncio.current_task()
        assert task is not None  # a request is always served in a task
        session.requests.add(task)
        try:
            with user_auth_context(user):
                return await provider.handle_page_request(request, request.match_info["tail"])
        except ActionUnavailable:
            return web.Response(status=404, text="This provider has no page.")
        finally:
            session.requests.discard(task)
            session.last_used = time.monotonic()

    async def on_response_prepare(self, request: web.Request, response: web.StreamResponse) -> None:
        """
        Force the security headers of a provider page on every response of its route.

        :param request: The request answered.
        :param response: The response about to send its headers.
        """
        if not request.path.startswith(f"/{PROVIDER_PAGE_ROUTE}/"):
            return
        # a policy of the provider is kept next to ours, which it can then only narrow further
        policies = [
            policy
            for value in response.headers.getall(hdrs.CONTENT_SECURITY_POLICY, [])
            if (policy := _without_reporting(value))
        ]
        response.headers.popall(hdrs.CONTENT_SECURITY_POLICY, None)
        for policy in [*policies, PAGE_CSP]:
            response.headers.add(hdrs.CONTENT_SECURITY_POLICY, policy)
        for key, value in PAGE_HEADERS.items():
            response.headers[key] = value
        for key in (*ORIGIN_WIDE_HEADERS, *REPORTING_HEADERS):
            response.headers.popall(key, None)

    def _drop(self, matches: Callable[[_PageSession], bool]) -> None:
        """Drop the page addresses whose session matches."""
        for key in [key for key, session in self._sessions.items() if matches(session)]:
            self._drop_session(key)

    def _drop_session(self, session_id: str) -> None:
        """Drop a page address, if it still exists, and close the requests still served on it."""
        if (session := self._sessions.pop(session_id, None)) is None:
            return
        session.max_age_timer.cancel()
        for task in session.requests:
            task.cancel()


def _expired(session: _PageSession, now: float) -> bool:
    """Return whether a page address has lapsed; one still serving a request is not idle."""
    if now - session.created > PAGE_MAX_AGE:
        return True
    return not session.requests and now - session.last_used > PAGE_IDLE_TIMEOUT


def _expired_response() -> web.Response:
    """Return the answer to an address that has lapsed or was dropped."""
    return web.Response(
        status=410, text="This page has expired, open it again from Music Assistant."
    )


def _without_reporting(policy: str) -> str:
    """Return a content security policy without the directives that report elsewhere."""
    return "; ".join(
        directive
        for raw in policy.split(";")
        if (directive := raw.strip())
        and directive.split(maxsplit=1)[0].lower() not in REPORTING_DIRECTIVES
    )
