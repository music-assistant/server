"""
Private helpers backing :mod:`provider.__init__`'s ``open_connect`` dispatch.

Lives in its own module so tests can import the helpers via a dotted path
(``from provider._init_helpers import …``) — the bare
``from provider import …`` form trips up the upstream-PR rewrite, which
only matches ``from provider.<sub> import …``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant.mass import MusicAssistant


def _sanitize_external_base_url(value: str | None) -> str | None:
    """
    Return ``value`` if it is a plausible ``http(s)://`` base URL, else ``None``.

    Defends against an admin pasting (or a misbehaving proxy injecting) a
    scheme-less or ``javascript:`` URL into the Connect Wizard link, which
    the MA frontend would feed straight to ``window.open``.
    """
    if not value:
        return None
    candidate = value.strip()
    if not candidate.lower().startswith(("http://", "https://")):
        LOGGER.warning(
            "Connect Wizard: ignoring external base URL with unsupported scheme: %r",
            candidate,
        )
        return None
    return candidate


def _detect_external_base_url(mass: MusicAssistant, current_user: Any) -> str | None:
    """
    Return the external base URL for the current user's active WS client.

    MA's :class:`WebsocketClientHandler` stores a per-connection ``base_url``
    derived from ``X-Forwarded-Host`` + ``X-Ingress-Path`` — exactly the
    prefix the Connect Wizard needs so ``window.open`` produces a working
    URL under Home Assistant add-on ingress. We pick the client whose
    authenticated user matches the invoker of the action.

    Returns ``None`` when nothing matches (e.g. action invoked outside the
    WS server, or no forward headers were captured).
    """
    if current_user is None:
        return None
    # ``webserver.clients`` is an internal collection (not part of the
    # documented MA surface), so a missing attribute is treated as "no
    # forwarded-host info available" rather than a hard failure.
    clients = getattr(mass.webserver, "clients", None) or ()

    def _user_id(user: Any) -> Any:
        return getattr(user, "user_id", None) or getattr(user, "username", None)

    target = _user_id(current_user)
    for client in clients:
        client_base = getattr(client, "base_url", None)
        if not client_base:
            continue
        # Prefer the (currently hypothetical) public name so a future MA rename
        # transparently takes over; fall back to the underscore-prefixed
        # internal attribute we rely on today.
        client_user = getattr(client, "authenticated_user", None) or getattr(
            client, "_authenticated_user", None
        )
        if client_user is None:
            continue
        if _user_id(client_user) == target:
            return str(client_base)
    return None


async def _dispatch_open_connect(
    mass: MusicAssistant,
    values: dict[str, ConfigValueType],
    setup_callback_path: str | None = None,
) -> str | None:
    """
    Mint a wizard bootstrap and return the Connect Wizard URL, or None on failure.

    URL resolution order: (1) auto-detect from the active WS client's
    ingress-aware ``base_url``; (2) explicit ``connect_external_url`` config
    override; (3) the server's advertised base URL; (4) a path-only fallback.

    :param mass: The Music Assistant instance.
    :param values: The effective MCP provider config values.
    :param setup_callback_path: Optional setup-flow callback path for the wizard
        to signal after generating a client configuration.
    """
    from .connect import handle_open_connect_action  # noqa: PLC0415
    from .constants import (  # noqa: PLC0415
        CONF_CONNECT_EXTERNAL_URL,
        CONF_MOUNT_PATH,
        DEFAULT_MOUNT_PATH,
    )

    mount_path = str(values.get(CONF_MOUNT_PATH) or DEFAULT_MOUNT_PATH)

    current_user: object | None = None
    try:
        from music_assistant.controllers.webserver.helpers.auth_middleware import (  # noqa: PLC0415
            get_current_user,
        )

        current_user = get_current_user()
    except Exception:
        LOGGER.debug("Connect Wizard: get_current_user lookup failed", exc_info=True)
        current_user = None

    external_base_url = _sanitize_external_base_url(_detect_external_base_url(mass, current_user))
    if not external_base_url:
        external_base_url = _sanitize_external_base_url(
            str(values.get(CONF_CONNECT_EXTERNAL_URL) or "")
        )
    if not external_base_url:
        external_base_url = _sanitize_external_base_url(
            str(getattr(mass.webserver, "base_url", "") or "")
        )

    try:
        return await handle_open_connect_action(
            mass,
            current_user=current_user,
            mount_path=mount_path,
            external_base_url=external_base_url,
            setup_callback_path=setup_callback_path,
        )
    except Exception:
        LOGGER.exception("Connect Wizard: open_connect action failed")
        return None
