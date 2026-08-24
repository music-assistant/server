"""
Mount the Connect Wizard endpoints onto MA's webserver.

Five routes are registered under ``<mount_path>/connect``; the returned
callable removes all of them when invoked (called from
:meth:`provider.server.MCPServerRuntime.stop`).
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from ..origins import compute_origin_allowlist, is_origin_allowed_for_request  # noqa: TID252
from .handlers import (
    WizardContext,
    make_exchange,
    make_info,
    make_login,
    make_mint_token,
    make_serve_page,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant.mass import MusicAssistant


async def mount_connect_wizard(
    mass: MusicAssistant,
    mount_path: str,
    *,
    enabled_tags_provider: Callable[[], list[str]],
    extra_origins_csv: str = "",
    trust_forwarded_proto: bool = False,
) -> Callable[[], None]:
    """
    Register the wizard routes and return a callable that unregisters them.

    :param mass: MusicAssistant instance.
    :param mount_path: HTTP path prefix where the MCP server is mounted
        (e.g. ``/mcp/v1``); wizard routes nest under ``<mount_path>/connect``.
    :param enabled_tags_provider: Zero-arg callable returning the list of
        currently-enabled permission tag strings; called per-request so
        permission hot-swaps surface in the UI without remount.
    :param extra_origins_csv: Comma-separated additional ``Origin`` values to
        accept beyond the auto-derived loopback + base_url + publish_ip set.
    :param trust_forwarded_proto: When True, accept a trusted reverse proxy's
        ``X-Forwarded-Proto: https`` as proof the public hop was HTTPS, so the
        credential-bearing endpoints work behind a TLS-terminating proxy.
    :return: Callable that, when invoked, unregisters every wizard route.
    """
    allowlist = compute_origin_allowlist(mass, extra_origins_csv)
    ctx = WizardContext(
        mass=mass,
        mount_path=mount_path,
        enabled_tags_provider=enabled_tags_provider,
        origin_check=lambda request: is_origin_allowed_for_request(request, allowlist),
        trust_forwarded_proto=trust_forwarded_proto,
    )

    base = "/" + mount_path.strip("/")
    routes: list[tuple[str, str]] = [
        (f"{base}/connect", "GET"),
        (f"{base}/connect/info", "GET"),
        (f"{base}/connect/exchange", "POST"),
        (f"{base}/connect/login", "POST"),
        (f"{base}/connect/token", "POST"),
    ]
    handlers = [
        make_serve_page(ctx),
        make_info(ctx),
        make_exchange(ctx),
        make_login(ctx),
        make_mint_token(ctx),
    ]

    unregister_fns: list[Callable[[], None]] = []
    for (path, method), handler in zip(routes, handlers, strict=True):
        unregister_fns.append(mass.webserver.register_dynamic_route(path, handler, method=method))

    def _unregister_all() -> None:
        for fn in unregister_fns:
            with contextlib.suppress(Exception):
                fn()

    return _unregister_all
