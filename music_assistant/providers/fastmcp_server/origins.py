"""
Origin allowlist helpers shared between the MCP bridge and the Connect Wizard.

Both ``provider/http_bridge.py`` (the MCP endpoint) and
``provider/connect/mount.py`` (the wizard) enforce the same Origin policy on
browser-issued requests. The helpers live in this small standalone module so
both consumers can import them directly — previously the wizard reached into
``provider.http_bridge`` via ``importlib`` to look up name-mangled private
symbols, which meant a future rename in the bridge module silently broke the
wizard mount at runtime with no test to catch it.

**Allowlist rule.** A request's ``Origin`` is accepted if its normalised
``scheme://host[:port]`` is in the computed allowlist. The allowlist always
includes:

* ``http://localhost``, ``http://127.0.0.1``, ``http://[::1]`` — bare loopback
  (default-port form).
* The normalised origin of ``mass.webserver.base_url``. If the base URL is
  ``http://``, the ``https://`` mirror is added too — MA may be behind a
  TLS-terminating reverse proxy.
* For every loopback host plus the configured ``publish_ip``, both
  ``http://`` and ``https://`` on the MA port (``http://localhost:8095``,
  ``https://localhost:8095``, etc.). Browsers serialize the port in Origin
  even for loopback connections.
* Any operator-supplied origin from the ``extra_allowed_origins`` CSV config.

A missing ``Origin`` header is accepted (stdio MCP clients, curl). ``Origin:
null`` is rejected unless explicitly in the allowlist (covers sandboxed
iframes / ``file://`` pages).

A second tier (:func:`is_origin_allowed_for_request`) adds a Home-Assistant-
ingress fallback: when the request arrives on MA's trusted ingress socket and
carries an ``X-Forwarded-Host`` matching the browser's ``Origin``, accept it
even if the origin isn't in the static allowlist. This removes the need to
hand-edit ``extra_allowed_origins`` every time HA's public hostname changes.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from aiohttp import web

    from music_assistant.mass import MusicAssistant

LOGGER = logging.getLogger(__name__)


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _normalize_origin(origin: str) -> str | None:
    """
    Return ``scheme://host[:port]`` lower-cased, default-port stripped, or None.

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


def _port_from_base_url(base_url: str) -> int | None:
    """Return the explicit port from a URL, or ``None`` when it's the scheme default."""
    if not base_url:
        return None
    parts = urlsplit(base_url)
    if parts.port is not None and parts.port != _DEFAULT_PORTS.get(parts.scheme.lower()):
        return parts.port
    return None


def compute_origin_allowlist(mass: MusicAssistant, extra_origins_csv: str = "") -> frozenset[str]:
    """
    Build the set of accepted ``Origin`` values for the MCP endpoint.

    See the module docstring for the full rule. The set is computed once at
    mount time and consulted on every request — call sites should cache the
    result rather than recompute per request.
    """
    allow: set[str] = {
        "http://localhost",
        "http://127.0.0.1",
        "http://[::1]",
    }

    base_url = str(mass.webserver.base_url or "")
    base_norm = _normalize_origin(base_url)
    if base_norm:
        allow.add(base_norm)
        # Same host on https is acceptable when MA is behind a TLS-terminating
        # proxy. No symmetric http-mirror for https base_url — an attacker
        # cannot induce browsers to downgrade.
        if base_norm.startswith("http://"):
            allow.add("https://" + base_norm[len("http://") :])

    # Browsers send the MA port in Origin even for loopback access. Adding
    # both schemes on the MA port keeps the allowlist consistent across
    # direct-loopback and TLS-proxy access patterns.
    base_port = _port_from_base_url(base_url)
    if base_port:
        for loopback in ("localhost", "127.0.0.1", "[::1]"):
            allow.add(f"http://{loopback}:{base_port}")
            allow.add(f"https://{loopback}:{base_port}")

    publish_ip = str(mass.webserver.publish_ip or "")
    if publish_ip:
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


def _is_origin_allowed(origin: str | None, allowlist: frozenset[str]) -> bool:
    """
    Return True if the request's ``Origin`` should be accepted.

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


def is_origin_allowed_for_request(
    request: web.Request,
    allowlist: frozenset[str],
) -> bool:
    """
    Origin check with a Home-Assistant-ingress fallback.

    Applies :func:`_is_origin_allowed` first. When that rejects, accept the
    request if **all** of the following hold:

    * the request arrived on the trusted ingress socket Music Assistant
      verifies via :func:`is_request_from_ingress` (so we are not trusting
      attacker-supplied headers); and
    * the request carries an ``X-Forwarded-Host`` set by HA; and
    * the browser's ``Origin`` matches
      ``<X-Forwarded-Proto or request.scheme>://<X-Forwarded-Host>``.

    This removes the need for HA add-on users to copy their public hostname
    into the ``extra_allowed_origins`` config every time the URL changes.
    """
    origin = request.headers.get("Origin")
    if _is_origin_allowed(origin, allowlist):
        return True
    if origin is None:
        return False  # _is_origin_allowed already returned True above; defensive

    forwarded_host = request.headers.get("X-Forwarded-Host")
    if not forwarded_host:
        return False

    try:
        from music_assistant.controllers.webserver.helpers.auth_middleware import (  # noqa: PLC0415
            is_request_from_ingress,
        )
    except ImportError, ModuleNotFoundError:
        # ``music_assistant`` is a dev-only / test-extras dep here; absent in
        # the bare provider venv. Fail closed without log noise.
        return False
    except Exception:
        # Anything else (e.g. partial module init breakage upstream) is a real
        # surprise — log so it's debuggable, then fail closed.
        LOGGER.exception("Connect Wizard: unexpected error importing ingress helper")
        return False
    try:
        if not is_request_from_ingress(request):
            return False
    except Exception:
        # MA may evolve the request-app shape; log so a future breakage isn't
        # silently a 403 with no hint as to why.
        LOGGER.exception("Connect Wizard: is_request_from_ingress raised")
        return False

    # Default to the aiohttp transport scheme (canonical aiohttp API) rather
    # than a hard-coded "https" so an unsecured local HA installation still
    # works when X-Forwarded-Proto is omitted. Multi-value X-Forwarded-Host
    # (``ha.example.com, internal.lan``) intentionally fails normalisation
    # below → reject; supporting it would mean trusting whichever hop the
    # proxy listed last, which is rarely what you want.
    forwarded_proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    forwarded_origin = _normalize_origin(f"{forwarded_proto}://{forwarded_host}")
    if forwarded_origin is None:
        return False
    return _normalize_origin(origin) == forwarded_origin
