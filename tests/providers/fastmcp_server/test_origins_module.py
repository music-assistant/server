"""
Contract tests for ``provider.origins``.

Two contracts are pinned here:

1. The public helper names exist on ``provider.origins``. The Connect Wizard
   imports them directly (no ``importlib`` round-trip); a rename in the
   origins module would silently break wizard mount with an opaque
   ``RuntimeError`` if there were no test to catch it.

2. ``provider.http_bridge`` continues to re-export the historical
   underscore-prefixed names so existing call sites and any external tests
   keep working without churn.
"""
# mypy: disable-error-code="arg-type, no-untyped-def, type-arg, assignment, misc, attr-defined"

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from music_assistant.providers.fastmcp_server.origins import (
    _is_origin_allowed,
    _normalize_origin,
    _port_from_base_url,
    compute_origin_allowlist,
    is_origin_allowed_for_request,
)


def test_public_names_exist() -> None:
    """The two helpers the Connect Wizard imports must remain public."""
    assert callable(compute_origin_allowlist)
    assert callable(is_origin_allowed_for_request)


def test_http_bridge_re_exports_legacy_names() -> None:
    """
    Historical names on ``provider.http_bridge`` keep working for back-compat.

    The attribute-defined ignores below are deliberate: the re-exports use
    underscore-prefixed aliases so mypy treats them as private. This test
    is *exactly* the contract that asserts those aliases stay reachable.
    """
    from music_assistant.providers.fastmcp_server import http_bridge  # noqa: PLC0415

    assert http_bridge._compute_origin_allowlist is compute_origin_allowlist
    assert http_bridge._is_origin_allowed_for_request is is_origin_allowed_for_request
    assert http_bridge._normalize_origin is _normalize_origin
    assert http_bridge._port_from_base_url is _port_from_base_url
    assert http_bridge._is_origin_allowed is _is_origin_allowed


def test_compute_origin_allowlist_includes_loopback() -> None:
    """The allowlist always includes default-port loopback variants."""
    mass = MagicMock()
    mass.webserver.base_url = "http://localhost:8095"
    mass.webserver.publish_ip = "127.0.0.1"

    allow = compute_origin_allowlist(mass)
    assert "http://localhost" in allow
    assert "http://127.0.0.1" in allow
    assert "http://[::1]" in allow


def test_compute_origin_allowlist_adds_both_schemes_on_ma_port() -> None:
    """
    When MA runs on a non-default port, both http and https on that port are accepted.

    Browsers serialize the port in Origin even for loopback connections, and
    a TLS-terminating reverse proxy in front of MA produces https origins
    that the bare http allowlist would otherwise reject.
    """
    mass = MagicMock()
    mass.webserver.base_url = "http://localhost:8095"
    mass.webserver.publish_ip = "192.168.1.42"

    allow = compute_origin_allowlist(mass)
    # Loopback x {http, https} x MA port
    for host in ("localhost", "127.0.0.1", "[::1]"):
        assert f"http://{host}:8095" in allow, host
        assert f"https://{host}:8095" in allow, host
    # Configured publish_ip x {http, https} x MA port
    assert "http://192.168.1.42:8095" in allow
    assert "https://192.168.1.42:8095" in allow
    # base_url itself plus its https mirror
    assert "http://localhost:8095" in allow
    assert "https://localhost:8095" in allow


def test_compute_origin_allowlist_picks_up_extra_origins_csv() -> None:
    """Operator-supplied origins from config are normalised and added."""
    mass = MagicMock()
    mass.webserver.base_url = "http://localhost:8095"
    mass.webserver.publish_ip = ""

    allow = compute_origin_allowlist(
        mass, extra_origins_csv="https://ha.example.com, https://other.example "
    )
    assert "https://ha.example.com" in allow
    assert "https://other.example" in allow


def test_https_base_url_does_not_get_http_mirror() -> None:
    """An https base URL must NOT add an http downgrade to the allowlist."""
    mass = MagicMock()
    mass.webserver.base_url = "https://secure.example"
    mass.webserver.publish_ip = ""

    allow = compute_origin_allowlist(mass)
    assert "https://secure.example" in allow
    # No http mirror — attackers should not be able to downgrade.
    assert "http://secure.example" not in allow


def test_is_origin_allowed_for_request_passes_through_allowlist_hits() -> None:
    """A standard allowlist hit returns True without entering the ingress fallback."""
    mass = MagicMock()
    mass.webserver.base_url = "http://localhost:8095"
    mass.webserver.publish_ip = ""
    allow = compute_origin_allowlist(mass)

    request: Any = MagicMock()
    request.headers = {"Origin": "http://localhost:8095"}
    assert is_origin_allowed_for_request(request, allow) is True


def test_is_origin_allowed_for_request_rejects_unknown_no_forwarded_host() -> None:
    """An unknown origin without HA-ingress headers is rejected."""
    mass = MagicMock()
    mass.webserver.base_url = "http://localhost:8095"
    mass.webserver.publish_ip = ""
    allow = compute_origin_allowlist(mass)

    request: Any = MagicMock()
    request.headers = {"Origin": "https://evil.example"}
    assert is_origin_allowed_for_request(request, allow) is False
