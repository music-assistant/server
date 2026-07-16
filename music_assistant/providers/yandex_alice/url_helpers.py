"""
Public-HTTPS URL detection + validation helpers (#8).

Phase 0 of the v1.2.0 UX overhaul confirmed that the obvious
``mass.streams.base_url`` and ``mass.webserver.base_url`` attributes
return MA's *internal* listen address (e.g. ``http://172.22.0.2:8095``
inside Docker) — useless for Yandex's webhook because Yandex needs a
**public HTTPS URL** that resolves on the open internet.

So autodetect only succeeds when the user (or their MA install) has set
a globally reachable HTTPS Base URL on the MA core webserver settings.
That's the rare case; in the typical case we return None and let the
form show a "fill this in" hint.

We also export a fast HTTPS-validity check so the form can warn inline
when the user pastes ``http://`` or a private IP.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "is_public_https_url",
    "try_detect_any_base_url",
    "try_detect_public_https_url",
    "validate_external_base_url",
]


def _is_private_or_loopback_host(host: str) -> bool:
    """Return True if *host* is private/loopback/link-local — not reachable from Yandex."""
    if not host:
        return True
    if host.lower() in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # Hostname (not IP) — assume public; DNS may still fail at request time
        # but that's a different layer.
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def is_public_https_url(url: str) -> bool:
    """
    Return True iff *url* is HTTPS and points to a non-private host.

    Used by:

    - autodetect: filters out the internal docker / loopback addresses MA's
      ``streams.base_url`` typically returns;
    - inline form warning: tells the user their pasted URL won't reach
      Yandex without exposing diagnostics in the description text.
    """
    if not url:
        return False
    parsed = urlparse(url.strip())
    if parsed.scheme.lower() != "https":
        return False
    host = (parsed.hostname or "").strip()
    return bool(host) and not _is_private_or_loopback_host(host)


def validate_external_base_url(value: object) -> bool:
    """
    Pre-flight ``ConfigEntry(validate=...)`` for the External Base URL field.

    Empty is allowed (user can still set up later); non-empty must be HTTPS
    and not a private host.
    """
    if not isinstance(value, str):
        return False
    s = value.strip()
    if not s:
        return True  # empty is OK at form-load; auto-create dispatcher will refuse.
    return is_public_https_url(s)


def try_detect_public_https_url(mass: MusicAssistant) -> str | None:
    """
    Best-effort: return MA's public HTTPS webserver URL, or None.

    Reads ``mass.webserver.base_url`` — the URL where MA's HTTP/WS API
    is hosted. The Yandex Dialogs webhook lives on this same server
    (``/api/yandex_dialogs/webhook/<secret>``), so any URL Yandex needs
    must point at the **webserver**, not at ``mass.streams.base_url``
    which is a separate audio streamserver port.

    Returns the URL only when :func:`is_public_https_url` says it's a
    real public URL. In the typical Docker / HA Ingress / local-only
    setup webserver returns ``http://<internal-ip>:port`` and we
    return None.

    Safe to call from ``get_config_entries`` — purely attribute access,
    no config-controller locks.
    """
    webserver_obj = getattr(mass, "webserver", None)
    candidate = getattr(webserver_obj, "base_url", None) if webserver_obj else None
    if isinstance(candidate, str) and is_public_https_url(candidate):
        _LOGGER.debug("autodetected public HTTPS base URL: %r", candidate)
        return candidate.strip().rstrip("/")
    return None


def try_detect_any_base_url(mass: MusicAssistant) -> str | None:
    """
    Best-effort: return *any* base URL of MA's webserver, public or not.

    Falls back from :func:`try_detect_public_https_url` so the user
    sees a pre-filled value to edit even when MA is reachable only
    via a private IP / HTTP. Like the public-only variant, this
    intentionally only inspects ``mass.webserver.base_url`` — the
    webhook lives on the webserver, not on the streamserver.
    """
    public = try_detect_public_https_url(mass)
    if public:
        return public
    webserver_obj = getattr(mass, "webserver", None)
    candidate = getattr(webserver_obj, "base_url", None) if webserver_obj else None
    if isinstance(candidate, str) and candidate.strip():
        _LOGGER.debug("autodetected webserver base URL (non-public): %r", candidate)
        return candidate.strip().rstrip("/")
    return None
