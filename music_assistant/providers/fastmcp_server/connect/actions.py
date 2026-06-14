"""ACTION-handler: mints a bootstrap token and signals the wizard URL to the frontend.

Triggered by the ``open_connect`` ``ConfigEntryType.ACTION`` button defined in
:mod:`provider.config`. Mirrors the Spotify-provider OAuth pattern (signal an
``EventType.AUTH_SESSION`` event whose ``data`` is the URL the MA frontend
should ``window.open``).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from ._revoke import list_user_tokens, revoke_token_by_id

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

LOGGER = logging.getLogger(__name__)

# Short-lived plumbing tokens the wizard mints on each open / page load. These
# auto-expire after 30 days; until then they clutter the user's token list.
# Garbage-collect them before minting a new one.
_GC_NAMES = ("MCP — wizard bootstrap", "MCP — wizard session")


async def handle_open_connect_action(
    mass: MusicAssistant,
    *,
    current_user: Any,
    mount_path: str,
    base_url: str = "",
    session_id: str | None = None,
    external_base_url: str | None = None,
) -> None:
    """Open the Connect Wizard in the user's browser via MA's auth-session signal.

    :param mass: MusicAssistant instance.
    :param current_user: The authenticated MA ``User`` invoking the action, or
        ``None`` when no user context is available — in which case the wizard
        is opened without a bootstrap token and falls back to its login form.
    :param mount_path: HTTP path prefix where the MCP server is mounted.
    :param base_url: Deprecated — kept in the signature for backwards
        compatibility; ignored. Pass ``external_base_url`` instead.
    :param session_id: ``session_id`` echoed by the MA frontend in the action's
        ``values``. Must be passed back verbatim as the ``AUTH_SESSION`` event
        ``object_id`` so the EditProvider view actually opens the URL — frontend
        ignores AUTH_SESSION events whose object_id does not match its session.
    :param external_base_url: Externally reachable base URL (scheme + host +
        optional ingress path prefix) to prepend to the wizard URL. When
        omitted, falls back to a path-only URL that the browser resolves
        against its own origin.
    """
    del base_url  # kept in signature for backwards compatibility; ignored

    bootstrap: str | None = None
    if current_user is not None:
        # GC any prior wizard plumbing rows for this user before minting a
        # new bootstrap, via the sanctioned auth API. Best-effort: lookup
        # failures inside list_user_tokens return []; individual revoke
        # failures are swallowed inside revoke_token_by_id. Per-client
        # tokens (MCP — <Client>) are not touched.
        for tok in await list_user_tokens(mass, current_user):
            if tok.name in _GC_NAMES:
                await revoke_token_by_id(mass, current_user, tok.token_id)

        try:
            bootstrap = await mass.webserver.auth.create_token(
                user=current_user,
                name="MCP — wizard bootstrap",
                is_long_lived=False,
            )
        except Exception:
            LOGGER.exception("Connect Wizard: failed to mint bootstrap token")
            bootstrap = None

    mount = "/" + mount_path.strip("/")
    if external_base_url:
        # Fully-qualified URL — required under HA add-on ingress, where the
        # MA frontend lives at ``https://<ha>/<slug>/`` and ``window.open``
        # on a path starting with ``/`` would drop the ingress prefix.
        url = f"{external_base_url.rstrip('/')}{mount}/connect"
    else:
        # Path-only fallback — browser resolves against its own origin. Works
        # for direct access; loses any reverse-proxy / ingress path prefix.
        url = f"{mount}/connect"
    if bootstrap:
        # Carry the bootstrap in the URL fragment, not the query string.
        # Fragments are never sent to the server (so they don't appear in
        # aiohttp access logs or any reverse-proxy log), and they're not
        # sent in the Referer header on cross-origin navigation. Query
        # strings were leaking the bootstrap into both.
        url = f"{url}#bootstrap={quote(bootstrap, safe='')}"

    object_id = session_id or f"mcp-connect-{secrets.token_urlsafe(8)}"
    _signal_auth_session(mass, session_id=object_id, url=url)

    # Hold the action response open briefly. MA frontend's EditProvider sets
    # ``loading=true`` while awaiting our response (which keeps the overlay
    # mounted) and, on receiving AUTH_SESSION, schedules a 100 ms setTimeout
    # that grabs ``<a id="auth">`` from inside that overlay and clicks it.
    # Without this delay the response races back first, ``loading`` flips to
    # false, the overlay (and the anchor inside it) unmounts, and the
    # frontend throws ``Cannot read properties of null (reading
    # 'setAttribute')`` — the user sees nothing happen. 500 ms gives the
    # frontend enough time to follow the link before we let the overlay close.
    await asyncio.sleep(0.5)


def _signal_auth_session(mass: MusicAssistant, *, session_id: str, url: str) -> None:
    """Publish the wizard URL via MA's ``EventType.AUTH_SESSION`` signal.

    The MA frontend subscribes to ``AUTH_SESSION`` events and ``window.open``-s
    the carried URL — same mechanism the Spotify, Audible, QQMusic providers
    use for OAuth redirect. Never raises: if the event bus rejects the call we
    log the exception and the failure path (the user can re-trigger the action
    manually) — the URL itself is **not** logged because it carries the
    short-lived bootstrap token in its URL fragment.
    """
    from music_assistant_models.enums import EventType  # noqa: PLC0415

    try:
        mass.signal_event(EventType.AUTH_SESSION, object_id=session_id, data=url)
    except Exception:
        LOGGER.exception("Connect Wizard: signal_event failed")
