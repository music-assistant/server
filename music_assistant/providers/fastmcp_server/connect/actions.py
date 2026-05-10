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
from urllib.parse import urlencode

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

LOGGER = logging.getLogger(__name__)


async def handle_open_connect_action(
    mass: MusicAssistant,
    *,
    current_user: Any,
    mount_path: str,
    base_url: str,
    session_id: str | None = None,
) -> None:
    """Open the Connect Wizard in the user's browser via MA's auth-session signal.

    :param mass: MusicAssistant instance.
    :param current_user: The authenticated MA ``User`` invoking the action, or
        ``None`` when no user context is available — in which case the wizard
        is opened without a bootstrap token and falls back to its login form.
    :param mount_path: HTTP path prefix where the MCP server is mounted.
    :param base_url: Public base URL of MA (no trailing slash).
    :param session_id: ``session_id`` echoed by the MA frontend in the action's
        ``values``. Must be passed back verbatim as the ``AUTH_SESSION`` event
        ``object_id`` so the EditProvider view actually opens the URL — frontend
        ignores AUTH_SESSION events whose object_id does not match its session.
    """
    bootstrap: str | None = None
    if current_user is not None:
        try:
            bootstrap = await mass.webserver.auth.create_token(
                user=current_user,
                name="MCP — wizard bootstrap",
                is_long_lived=False,
            )
        except Exception:
            LOGGER.exception("Connect Wizard: failed to mint bootstrap token")
            bootstrap = None

    # Path-only URL — the MA frontend assigns this to ``<a href>`` and the
    # browser resolves it against the current location (the user's URL bar
    # origin). Using ``base_url`` here would break in Docker / HA add-on
    # deployments where MA reports an internal IP (e.g. ``http://172.21.0.2``)
    # that the user's browser cannot reach.
    del base_url  # kept in signature for backwards compatibility; ignored
    mount = "/" + mount_path.strip("/")
    url = f"{mount}/connect"
    if bootstrap:
        url = f"{url}?{urlencode({'bootstrap': bootstrap})}"

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
    short-lived bootstrap token in its query string.
    """
    from music_assistant_models.enums import EventType  # noqa: PLC0415

    try:
        mass.signal_event(EventType.AUTH_SESSION, object_id=session_id, data=url)
    except Exception:
        LOGGER.exception("Connect Wizard: signal_event failed")
