"""ASGI ↔ aiohttp bridge for mounting FastMCP under MA's webserver.

FastMCP v3 exposes a Starlette-based ASGI app for streamable-HTTP transport.
MA's main webserver is aiohttp. This bridge translates a single aiohttp
``web.Request`` into ASGI ``scope/receive/send`` events and back into a
``web.StreamResponse``, so we can mount the MCP app under any path that
``mass.webserver.register_dynamic_route`` accepts (we use ``/mcp/v1/*``).

Streaming responses (SSE / chunked) are passed through verbatim so MCP
keep-alive heartbeats and tool-progress events reach the client without
buffering.

A second helper (:func:`mount_well_known`) registers a sibling route at
``/.well-known/oauth-protected-resource[/<mcp-path>]`` that serves the RFC
9728 protected-resource-metadata document — pointed to by FastMCP's
``WWW-Authenticate`` 401 header, so spec-compliant MCP clients (Claude
Desktop, Codex, ChatGPT Apps SDK) can discover the authorization server.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from aiohttp import web

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant.mass import MusicAssistant

LOGGER = logging.getLogger(__name__)


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _normalize_origin(origin: str) -> str | None:
    """Return ``scheme://host[:port]`` lower-cased, default-port stripped, or None.

    Rejects forms without scheme or netloc; preserves ``"null"`` verbatim so it
    can be matched against an explicit allowlist entry. IPv6 hosts are
    re-bracketed (``urlsplit`` strips the brackets via ``hostname``) so the
    canonical form matches a literal ``http://[::1]`` allowlist entry.
    """
    if not origin:
        return None
    if origin == "null":
        return "null"
    parts = urlsplit(origin)
    scheme = parts.scheme.lower()
    host = parts.hostname
    if not scheme or not host:
        return None
    host_lower = host.lower()
    # urlsplit's `.hostname` returns the bare IPv6 ("::1"); we need brackets
    # back ("[::1]") so f-string concatenation produces a valid Origin.
    bracketed_host = f"[{host_lower}]" if ":" in host_lower else host_lower
    port = parts.port
    if port is None or port == _DEFAULT_PORTS.get(scheme):
        return f"{scheme}://{bracketed_host}"
    return f"{scheme}://{bracketed_host}:{port}"


def _compute_origin_allowlist(mass: MusicAssistant, extra_origins_csv: str = "") -> frozenset[str]:
    """Build the set of accepted ``Origin`` values for the MCP endpoint.

    Always includes loopback variants (``http://localhost``, ``http://127.0.0.1``,
    ``http://[::1]``), the host derived from ``mass.webserver.base_url``, and the
    advertised ``mass.webserver.publish_ip``. Additional origins from config
    (CSV) are normalized and added.
    """
    allow: set[str] = {
        "http://localhost",
        "http://127.0.0.1",
        "http://[::1]",
    }

    base_url = str(getattr(mass.webserver, "base_url", "") or "")
    base_norm = _normalize_origin(base_url)
    if base_norm:
        allow.add(base_norm)
        # Same host on https is acceptable when MA is behind TLS-terminating proxy.
        if base_norm.startswith("http://"):
            allow.add("https://" + base_norm[len("http://") :])

    # Browsers send the MA port in Origin even for loopback access — add the
    # loopback variants on the MA port so a Origin like ``http://localhost:8095``
    # is accepted (the bare loopback entries above only match port 80).
    base_port = _port_from_base_url(base_url)
    if base_port:
        for loopback in ("localhost", "127.0.0.1", "[::1]"):
            allow.add(f"http://{loopback}:{base_port}")
            allow.add(f"https://{loopback}:{base_port}")

    publish_ip = str(getattr(mass.webserver, "publish_ip", "") or "")
    if publish_ip:
        # Derive port from base_url; fallback: no port (browsers send port if non-default).
        port = _port_from_base_url(base_url)
        suffix = f":{port}" if port else ""
        ip_lower = publish_ip.lower()
        # Bracket IPv6 literals so they match the way browsers serialize Origin.
        ip_token = f"[{ip_lower}]" if ":" in ip_lower else ip_lower
        allow.add(f"http://{ip_token}{suffix}")
        allow.add(f"https://{ip_token}{suffix}")

    for raw in (extra_origins_csv or "").split(","):
        norm = _normalize_origin(raw.strip())
        if norm:
            allow.add(norm)

    return frozenset(allow)


def _port_from_base_url(base_url: str) -> int | None:
    """Return the explicit port from a URL, or ``None`` when it's the scheme default."""
    if not base_url:
        return None
    parts = urlsplit(base_url)
    if parts.port is not None and parts.port != _DEFAULT_PORTS.get(parts.scheme.lower()):
        return parts.port
    return None


def _is_origin_allowed(origin: str | None, allowlist: frozenset[str]) -> bool:
    """Return True if the request's ``Origin`` should be accepted.

    Rules:

    * Missing ``Origin`` → allowed (stdio-style or non-browser MCP clients).
      Spec MUST applies to *present* Origin values.
    * ``Origin: null`` → allowed only if explicitly listed in the allowlist
      (some sandboxed iframes / file:// pages send it).
    * Any other value is normalized and matched literally.
    """
    if origin is None:
        return True
    norm = _normalize_origin(origin)
    if norm is None:
        return False
    return norm in allowlist


async def mount_into_mass(
    mass: MusicAssistant,
    mcp: Any,
    mount_path: str = "/mcp/v1",
    extra_origins_csv: str = "",
) -> Callable[[], None]:
    """Register the FastMCP streamable-HTTP ASGI app under MA's webserver.

    :param mass: MusicAssistant instance.
    :param mcp: FastMCP server instance whose ``http_app`` is exposed.
    :param mount_path: Path prefix on the MA webserver (default ``/mcp/v1``).
    :param extra_origins_csv: Comma-separated additional ``Origin`` values to
        accept beyond the auto-derived defaults (loopback + base_url +
        publish_ip). Use for reverse-proxy hostnames or HA ingress.
    :return: Callable that, when invoked, unregisters the route and shuts
        down the FastMCP ASGI lifespan.
    """
    # Tell FastMCP that its streamable-HTTP endpoint lives at ``mount_path``
    # (not the SDK's default ``/mcp``), so the internal Starlette router
    # matches the URL the request actually arrives with — without a prefix
    # strip in the bridge. With strip we'd hand FastMCP a bare ``/`` and its
    # router would 404 every request.
    asgi_app = _build_asgi_app(mcp, mount_path)
    allowlist = _compute_origin_allowlist(mass, extra_origins_csv)

    # Drive the ASGI lifespan ourselves — without it FastMCP's
    # StreamableHTTPSessionManager never enters its task group and the first
    # request fails with "Task group is not initialized." The lifespan loop
    # runs until shutdown is requested at unmount time.
    lifespan_state = await _start_asgi_lifespan(asgi_app)

    async def handler(request: web.Request) -> web.StreamResponse:
        origin = request.headers.get("Origin")
        if not _is_origin_allowed(origin, allowlist):
            LOGGER.warning(
                "MCP: rejected request with Origin=%r from %s (not in allowlist)",
                origin,
                request.remote,
            )
            return web.Response(status=403, text="Forbidden Origin")
        return await _asgi_to_aiohttp(asgi_app, request, strip_prefix="")

    unregister = mass.webserver.register_dynamic_route(f"{mount_path}/*", handler)

    def _unmount() -> None:
        with contextlib.suppress(Exception):
            unregister()
        # Schedule the lifespan shutdown — caller may be sync (MA's
        # ``unload``), so dispatch onto the running loop without blocking.
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_stop_asgi_lifespan(lifespan_state))
        else:  # pragma: no cover - belt-and-braces for unit-test contexts
            with contextlib.suppress(Exception):
                loop.run_until_complete(_stop_asgi_lifespan(lifespan_state))

    return _unmount


async def _start_asgi_lifespan(asgi_app: Any) -> dict[str, Any]:
    """Send ASGI ``lifespan.startup`` and keep the lifespan task running.

    Returns a state dict carrying the running task and the queues used to
    feed it ``lifespan.shutdown`` later. Re-raises a startup failure synchronously
    so caller sees the underlying exception immediately.
    """
    receive_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    send_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def receive() -> dict[str, Any]:
        return await receive_queue.get()

    async def send(message: dict[str, Any]) -> None:
        await send_queue.put(message)

    task = asyncio.create_task(
        asgi_app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send),
        name="mcp-asgi-lifespan",
    )

    # Trigger startup and wait for ack.
    await receive_queue.put({"type": "lifespan.startup"})
    ack = await asyncio.wait_for(send_queue.get(), timeout=30)
    if ack.get("type") == "lifespan.startup.failed":
        # Lifespan task aborted; surface its exception cleanly.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        msg = ack.get("message", "ASGI lifespan startup failed")
        raise RuntimeError(msg)
    if ack.get("type") != "lifespan.startup.complete":
        msg = f"Unexpected ASGI lifespan event during startup: {ack!r}"
        raise RuntimeError(msg)

    return {"task": task, "receive_queue": receive_queue, "send_queue": send_queue}


async def _stop_asgi_lifespan(state: dict[str, Any]) -> None:
    """Send ASGI ``lifespan.shutdown`` and await the lifespan task to finish."""
    receive_queue: asyncio.Queue[dict[str, Any]] = state["receive_queue"]
    send_queue: asyncio.Queue[dict[str, Any]] = state["send_queue"]
    task: asyncio.Task[Any] = state["task"]

    with contextlib.suppress(Exception):
        await receive_queue.put({"type": "lifespan.shutdown"})
        with contextlib.suppress(asyncio.TimeoutError):
            # Drain the shutdown ack but don't fail if the app skips it.
            await asyncio.wait_for(send_queue.get(), timeout=10)

    if not task.done():
        try:
            await asyncio.wait_for(task, timeout=10)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


def build_protected_resource_metadata(
    *,
    resource_uri: str,
    authorization_servers: list[str],
    scopes_supported: list[str] | None = None,
    resource_name: str | None = None,
) -> dict[str, Any]:
    """Construct the RFC 9728 OAuth 2.0 Protected Resource Metadata document.

    :param resource_uri: Canonical URI of this MCP server (matches the ``aud``
        claim in tokens issued for it).
    :param authorization_servers: Issuer URLs of authorization servers that
        produce valid tokens for ``resource_uri``.
    :param scopes_supported: Optional list of scopes advertised to clients.
    :param resource_name: Human-readable label.
    """
    metadata: dict[str, Any] = {
        "resource": resource_uri,
        "authorization_servers": list(authorization_servers),
        "bearer_methods_supported": ["header"],
    }
    if scopes_supported:
        metadata["scopes_supported"] = list(scopes_supported)
    if resource_name:
        metadata["resource_name"] = resource_name
    return metadata


async def mount_well_known(
    mass: MusicAssistant,
    *,
    mount_path: str,
    resource_uri: str,
    authorization_servers: list[str],
    scopes_supported: list[str] | Callable[[], list[str]] | None = None,
    resource_name: str | None = None,
) -> Callable[[], None]:
    """Register the Protected Resource Metadata endpoint on MA's webserver.

    Two paths are bound, both returning the same JSON:

    * ``/.well-known/oauth-protected-resource/<mount_path-without-leading-slash>``
      — the path FastMCP advertises in ``WWW-Authenticate`` 401 responses.
    * ``/.well-known/oauth-protected-resource`` — root fallback (RFC 9728
      §3.1 second form), so clients that strip the path component still find
      the document.

    :param scopes_supported: Either a static list (snapshot at mount time) or a
        zero-arg callable returning the current scope list. The callable form
        lets the document stay in sync with permission hot-swaps without a
        runtime rebuild — the body is regenerated on each request.
    :return: Callable that unregisters both routes when invoked.
    """

    def _resolve_scopes() -> list[str] | None:
        if callable(scopes_supported):
            return scopes_supported()
        return scopes_supported

    def _build_body() -> bytes:
        metadata = build_protected_resource_metadata(
            resource_uri=resource_uri,
            authorization_servers=authorization_servers,
            scopes_supported=_resolve_scopes(),
            resource_name=resource_name,
        )
        return json.dumps(metadata).encode()

    async def handler(_request: web.Request) -> web.Response:
        # Re-render per request — sub-ms json.dumps — so a closure over
        # self._config in MCPServerRuntime reflects permission hot-swaps.
        return web.Response(
            body=_build_body(),
            content_type="application/json",
            headers={"Cache-Control": "no-store"},
        )

    suffix = mount_path.lstrip("/")
    paths = [
        f"/.well-known/oauth-protected-resource/{suffix}",
        "/.well-known/oauth-protected-resource",
    ]
    unregister_fns: list[Callable[[], None]] = [
        mass.webserver.register_dynamic_route(p, handler, method="GET") for p in paths
    ]

    def _unregister_all() -> None:
        for fn in unregister_fns:
            with contextlib.suppress(Exception):
                fn()

    return _unregister_all


def _build_asgi_app(mcp: Any, mount_path: str = "/mcp") -> Any:
    """Return the streamable-HTTP ASGI app from FastMCP, accommodating v3 minor renames.

    ``mount_path`` is propagated as ``http_app(path=...)`` so FastMCP's
    Starlette router exposes the streamable endpoint at the same URL the
    aiohttp bridge forwards to it — preventing 404s when our outer mount
    differs from the SDK's default ``/mcp``. RFC 9728 metadata routes
    advertised by FastMCP are likewise rooted at this path.
    """
    if hasattr(mcp, "http_app"):
        return mcp.http_app(transport="streamable-http", path=mount_path)
    if hasattr(mcp, "streamable_http_app"):
        return mcp.streamable_http_app()
    if hasattr(mcp, "asgi_app"):
        return mcp.asgi_app()
    msg = "Could not find an ASGI app factory on FastMCP instance"
    raise RuntimeError(msg)


async def _asgi_to_aiohttp(  # noqa: PLR0915 - single-purpose ASGI bridge, splitting harms readability
    asgi_app: Any,
    request: web.Request,
    strip_prefix: str = "",
) -> web.StreamResponse:
    """Bridge a single aiohttp request through an ASGI app.

    The bridge supports streaming responses: ``http.response.body`` events
    with ``more_body=True`` are flushed to the client immediately, which is
    required for streamable-HTTP MCP transport.
    """
    scope = _build_scope(request, strip_prefix)
    body_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def receive() -> dict[str, Any]:
        return await body_queue.get()

    response_state: dict[str, Any] = {"started": False, "response": None, "disconnected": False}

    async def send(message: dict[str, Any]) -> None:
        msg_type = message.get("type")
        if response_state["disconnected"]:
            # Client gave up; suppress further ASGI sends so the app can wind
            # down without an exception cascade.
            return
        try:
            if msg_type == "http.response.start":
                status = int(message.get("status", 200))
                headers_list = message.get("headers", [])
                response = web.StreamResponse(status=status)
                for raw_name, raw_value in headers_list:
                    name = (
                        raw_name.decode("latin-1") if isinstance(raw_name, bytes) else str(raw_name)
                    )
                    value = (
                        raw_value.decode("latin-1")
                        if isinstance(raw_value, bytes)
                        else str(raw_value)
                    )
                    if name.lower() in {"transfer-encoding", "content-length"}:
                        continue
                    response.headers[name] = value
                await response.prepare(request)
                response_state["response"] = response
                response_state["started"] = True
            elif msg_type == "http.response.body":
                response = response_state["response"]
                if response is None:
                    msg = "ASGI app sent body before start"
                    raise RuntimeError(msg)
                body = message.get("body", b"")
                if body:
                    await response.write(body)
                if not message.get("more_body", False):
                    await response.write_eof()
        except (ConnectionResetError, ConnectionError, asyncio.CancelledError):
            # The other side closed the (SSE) stream. Mark the response as
            # disconnected and feed an ASGI ``http.disconnect`` upstream so
            # the app's keep-alive / ping loops can wind down cleanly. Logged
            # at debug because this is a normal, expected client behaviour
            # for long-lived streams.
            response_state["disconnected"] = True
            with contextlib.suppress(Exception):
                await body_queue.put({"type": "http.disconnect"})
            LOGGER.debug("MCP bridge: client closed stream during send (path=%s)", request.path)

    async def pump_request_body() -> None:
        try:
            async for chunk in request.content.iter_chunked(64 * 1024):
                await body_queue.put({"type": "http.request", "body": chunk, "more_body": True})
            await body_queue.put({"type": "http.request", "body": b"", "more_body": False})
        except (ConnectionResetError, ConnectionError, asyncio.CancelledError):
            response_state["disconnected"] = True
            with contextlib.suppress(Exception):
                await body_queue.put({"type": "http.disconnect"})
        except Exception:
            LOGGER.exception("MCP bridge: failed to pump request body")
            with contextlib.suppress(Exception):
                await body_queue.put({"type": "http.disconnect"})

    pump_task = asyncio.create_task(pump_request_body())
    try:
        await asgi_app(scope, receive, send)
    except (ConnectionResetError, ConnectionError, asyncio.CancelledError):
        # Client-driven disconnect during streaming — normal flow; don't
        # re-raise as 500.
        response_state["disconnected"] = True
        LOGGER.debug("MCP bridge: ASGI app cancelled by client disconnect")
    except Exception:
        LOGGER.exception("MCP bridge: ASGI app raised")
        if not response_state["started"]:
            return web.Response(status=500, text="Internal MCP bridge error")
        raise
    finally:
        pump_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await pump_task

    response = response_state["response"]
    if response is None:
        return web.Response(status=204)
    assert isinstance(response, web.StreamResponse)
    return response


def _build_scope(request: web.Request, strip_prefix: str) -> dict[str, Any]:
    """Convert an aiohttp request into a minimal ASGI HTTP scope dict."""
    raw_path = request.rel_url.raw_path
    if strip_prefix and raw_path.startswith(strip_prefix):
        raw_path = raw_path[len(strip_prefix) :]
    if not raw_path.startswith("/"):
        raw_path = "/" + raw_path

    headers: list[tuple[bytes, bytes]] = [
        (k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in request.headers.items()
    ]

    server_host = request.url.host or "localhost"
    server_port = request.url.port or (443 if request.url.scheme == "https" else 80)

    client_addr: tuple[str, int] | None = None
    peername = request.transport.get_extra_info("peername") if request.transport else None
    if peername and len(peername) >= 2:
        client_addr = (str(peername[0]), int(peername[1]))

    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": request.method,
        "scheme": request.url.scheme,
        "path": raw_path,
        "raw_path": raw_path.encode("latin-1"),
        "query_string": request.rel_url.raw_query_string.encode("latin-1"),
        "root_path": strip_prefix.rstrip("/"),
        "headers": headers,
        "server": (server_host, server_port),
        "client": client_addr,
    }
