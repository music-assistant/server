"""
Pages a provider serves, behind a short-lived address bound to a user and a provider instance.

A provider answers an action with ``ConfigActionResult(open_page=<path>)``. The server turns
that into an address under ``/provider_page/<session>/`` for the calling user, re-checks the
user, scope and instance on every request to it, and serves every response of that route
sandboxed (an opaque origin), so a page can not read the app's token or storage even though it
shares the server's host and port.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from urllib.parse import unquote, urlsplit

from aiohttp import hdrs, web
from music_assistant_models.enums import ProviderTier
from music_assistant_models.errors import ActionUnavailable

from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_user,
    has_scope,
    user_auth_context,
)

if TYPE_CHECKING:
    from music_assistant_models.auth import Scope
    from music_assistant_models.config_entries import ConfigActionResult

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
PREFLIGHT_HEADERS: Final = {
    "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE",
    "Access-Control-Allow-Headers": "Content-Type",
}


@dataclass
class _PageSession:
    """A page address handed to one user for one provider instance."""

    user_id: str
    instance_id: str
    scope: Scope
    created: float
    last_used: float


class ProviderPageManager:
    """Issue and serve the short-lived addresses of the pages providers serve."""

    def __init__(self, webserver: WebserverController) -> None:
        """
        Initialize the provider page manager.

        :param webserver: The webserver controller instance.
        """
        self.webserver = webserver
        self.mass = webserver.mass
        self.logger = webserver.logger
        self._sessions: dict[str, _PageSession] = {}
        self._unsub_access_revoked: Callable[[], None] | None = None

    def setup(self) -> None:
        """Start dropping the page addresses of a user whose access is withdrawn."""
        self._unsub_access_revoked = self.webserver.auth.subscribe_user_access_revoked(
            lambda user: self.drop_user(user.user_id)
        )

    def close(self) -> None:
        """Drop all page addresses."""
        if self._unsub_access_revoked:
            self._unsub_access_revoked()
            self._unsub_access_revoked = None
        self._sessions.clear()

    def prepare_action_result(
        self, provider: Provider, result: ConfigActionResult, scope: Scope
    ) -> None:
        """
        Make an action result of a provider fit to hand to the calling user, in place.

        Replaces ``open_page`` with the page's address for the calling user, and drops an
        ``open_page`` that is not a relative path inside the page as well as a non-https
        ``open_url`` of a community provider, with a warning in the log.

        :param provider: The provider instance that answered the action.
        :param result: The result it answered.
        :param scope: The scope the action required, which every request to the page re-checks.
        """
        if (
            result.open_url
            and provider.manifest.tier == ProviderTier.COMMUNITY
            and urlsplit(result.open_url).scheme.lower() != "https"
        ):
            self.logger.warning(
                "Provider %s answered a url that is not https, which is not opened: %s",
                provider.instance_id,
                result.open_url,
            )
            result.open_url = None
        if result.open_page is None:
            return
        user = get_current_user()
        if user is None or not _is_page_path(result.open_page):
            self.logger.warning(
                "Provider %s answered a page that can not be opened: %s",
                provider.instance_id,
                result.open_page,
            )
            result.open_page = None
            return
        session_id = self._create_session(user.user_id, provider.instance_id, scope)
        result.open_page = f"{PROVIDER_PAGE_ROUTE}/{session_id}/{result.open_page}"

    def drop_user(self, user_id: str) -> None:
        """
        Drop all page addresses of a user.

        :param user_id: The user whose addresses to drop.
        """
        self._drop(lambda session: session.user_id == user_id)

    def drop_instance(self, instance_id: str) -> None:
        """
        Drop all page addresses of a provider instance.

        :param instance_id: The provider instance whose addresses to drop.
        """
        self._drop(lambda session: session.instance_id == instance_id)

    async def handle_request(self, request: web.Request) -> web.StreamResponse:
        """
        Serve a request to a provider page, after checking its address, user, scope and instance.

        :param request: The request to the page route.
        """
        session_id = request.match_info["session"]
        session = self._sessions.get(session_id)
        now = time.monotonic()
        if session is None or _expired(session, now):
            self._sessions.pop(session_id, None)
            return web.Response(
                status=410, text="This page has expired, open it again from Music Assistant."
            )
        user = await self.webserver.auth.get_user(session.user_id)
        if user is None or not has_scope(user, session.scope):
            return web.Response(status=403, text="You can no longer open this page.")
        provider = next(
            (
                provider
                for provider in self.mass.get_providers_for_user(user)
                if provider.instance_id == session.instance_id and provider.available
            ),
            None,
        )
        if provider is None:
            return web.Response(status=503, text="This page is not available right now.")
        session.last_used = now
        if request.method == hdrs.METH_OPTIONS:
            # the CORS preflight of the page's own requests from its opaque origin
            return web.Response(status=204, headers=PREFLIGHT_HEADERS)
        with user_auth_context(user):
            try:
                return await provider.handle_page_request(request, request.match_info["tail"])
            except ActionUnavailable:
                return web.Response(status=404, text="This provider has no page.")

    async def on_response_prepare(self, request: web.Request, response: web.StreamResponse) -> None:
        """
        Force the security headers of a provider page on every response of its route.

        :param request: The request answered.
        :param response: The response about to send its headers.
        """
        if not request.path.startswith(f"/{PROVIDER_PAGE_ROUTE}/"):
            return
        # added next to any policy of the provider, which can then only narrow it further
        response.headers.add(hdrs.CONTENT_SECURITY_POLICY, PAGE_CSP)
        for key, value in PAGE_HEADERS.items():
            response.headers[key] = value
        response.headers.popall(hdrs.SET_COOKIE, None)

    def _create_session(self, user_id: str, instance_id: str, scope: Scope) -> str:
        """Create a page address for the user and return its session id."""
        now = time.monotonic()
        self._drop(lambda session: _expired(session, now))
        own = [key for key, session in self._sessions.items() if session.user_id == user_id]
        # the dict keeps insertion order, so the oldest come first
        for key in own[: max(0, len(own) - MAX_PAGES_PER_USER + 1)]:
            del self._sessions[key]
        session_id = secrets.token_urlsafe(32)
        self._sessions[session_id] = _PageSession(user_id, instance_id, scope, now, now)
        return session_id

    def _drop(self, matches: Callable[[_PageSession], bool]) -> None:
        """Drop the page addresses whose session matches."""
        for key in [key for key, session in self._sessions.items() if matches(session)]:
            del self._sessions[key]


def _expired(session: _PageSession, now: float) -> bool:
    """Return whether a page address has lapsed."""
    return now - session.last_used > PAGE_IDLE_TIMEOUT or now - session.created > PAGE_MAX_AGE


def _is_page_path(path: str) -> bool:
    """Return whether the path stays inside the page: relative, without a scheme or a '..'."""
    # a URL parser drops tabs and newlines and reads a backslash as a slash, so a path with one
    # could still climb out of the page
    if not path.isprintable() or "\\" in path or path.startswith("/"):
        return False
    try:
        parts = urlsplit(path)
    except ValueError:
        return False
    if parts.scheme or parts.netloc:
        return False
    return ".." not in unquote(parts.path).split("/")
