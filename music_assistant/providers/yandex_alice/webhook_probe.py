"""
Outgoing webhook reachability probe (#11).

Used by ``CONF_ACTION_TEST_WEBHOOK`` to verify the public path Yandex will
take to reach our webhook before the user spends a Device Flow + skill
registration only to discover their reverse proxy is misconfigured. Tests
the same chain Yandex itself uses: DNS → TLS → HTTP path → handler reply.

Returns ``(ok, message)`` where the message is human-readable and ready
to drop straight into a ``ConfigEntry(LABEL)``.

Yandex Dialogs custom-skill webhooks always receive a JSON envelope with
a populated ``session.skill_id``. Our handler in ``dialogs.py`` rejects
mismatched skill_ids with HTTP 401 — that's the "reachable but rejecting
unknown skill" signal we look for here. HTTP 200 means our handler ran
the request through to dispatch (won't happen with the sentinel skill_id
we send), HTTP 401 means reachable, HTTP 4xx/5xx outside that means
either reverse-proxy or our handler crashed.
"""

from __future__ import annotations

import ipaddress
import json
import socket

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.resolver import ThreadedResolver

from .constants import DIALOG_WEBHOOK_BASE_PATH
from .url_helpers import is_public_https_url

__all__ = ["probe_webhook_reachability"]

_TEST_TIMEOUT_SECONDS = 5.0
_SENTINEL_SKILL_ID = "00000000-0000-0000-0000-000000000000"
_SENTINEL_SESSION_ID = "ma-test-reachability"
_SENTINEL_USER_ID = "ma-test-user"


class _NonPublicAddressError(OSError):
    """Raised when DNS points the outbound probe at a non-global address."""


class _PublicAddressResolver(AbstractResolver):
    """Resolve a host and reject every non-global answer before connecting."""

    def __init__(self, resolver: AbstractResolver | None = None) -> None:
        self._resolver = resolver or ThreadedResolver()

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        results = await self._resolver.resolve(host, port, family)
        for result in results:
            address = ipaddress.ip_address(result["host"])
            if not address.is_global:
                raise _NonPublicAddressError(f"{host!r} resolves to non-public address {address}")
        return results

    async def close(self) -> None:
        """Close the wrapped resolver."""
        await self._resolver.close()


def _build_test_envelope() -> dict[str, object]:
    """Minimal Yandex Dialogs custom-skill payload our handler will recognise."""
    return {
        "session": {
            "skill_id": _SENTINEL_SKILL_ID,
            "session_id": _SENTINEL_SESSION_ID,
            "user_id": _SENTINEL_USER_ID,
            "new": True,
            "user": {"user_id": _SENTINEL_USER_ID},
        },
        "request": {
            "type": "SimpleUtterance",
            "command": "ma reachability test",
            "original_utterance": "ma reachability test",
        },
        "version": "1.0",
    }


def _validate_inputs(base_url: str, webhook_secret: str) -> tuple[bool, str] | None:
    """
    Pre-network sanity check; returns failure tuple or None to continue.

    Rejects private / loopback / link-local hosts up front: Yandex's cloud
    cannot reach them, and the probe itself would falsely report
    ``localhost`` or ``192.168.x.x`` as "reachable" simply because the
    request loops back to the same machine.
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return False, "External base URL is empty — fill it in first."
    if not is_public_https_url(base):
        return (
            False,
            f"External base URL must be a public HTTPS endpoint Yandex can "
            f"reach over the internet (got: {base!r}). Private IPs, loopback "
            f"and link-local addresses are rejected.",
        )
    if not webhook_secret.strip():
        return False, "Webhook secret is empty — open the form once to auto-generate."
    return None


async def probe_webhook_reachability(base_url: str, webhook_secret: str) -> tuple[bool, str]:
    """
    POST a sentinel envelope and classify the response.

    Returns ``(reachable, message)``:

    - ``(True, "Webhook reachable (HTTP 401 — handler rejected unknown skill_id, expected)")``
      means the chain works end-to-end — Yandex traffic with the **real**
      skill_id will be accepted.
    - ``(False, "<diagnostic>")`` for anything else (DNS, TLS, timeout,
      connection refused, 5xx reverse-proxy, etc.). Each branch produces
      a specific message so the user knows what to fix.
    """
    pre = _validate_inputs(base_url, webhook_secret)
    if pre is not None:
        return pre

    base = base_url.strip().rstrip("/")
    url = f"{base}{DIALOG_WEBHOOK_BASE_PATH}/{webhook_secret.strip()}"
    payload = _build_test_envelope()
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=_TEST_TIMEOUT_SECONDS)
    connector = aiohttp.TCPConnector(resolver=_PublicAddressResolver())

    try:
        async with (
            aiohttp.ClientSession(timeout=timeout, connector=connector) as session,
            session.post(
                url,
                data=body,
                headers=headers,
                allow_redirects=False,
            ) as resp,
        ):
            status = resp.status
    except aiohttp.ClientConnectorDNSError as exc:
        if isinstance(exc.os_error, _NonPublicAddressError):
            return False, f"Blocked non-public DNS destination: {exc.os_error}"
        return False, f"DNS resolution failed for {base}: {exc}"
    except aiohttp.ClientSSLError as exc:
        return False, f"TLS / certificate error: {exc}"
    except aiohttp.ClientConnectorError as exc:
        return False, f"Connection error (no route / refused): {exc}"
    except TimeoutError:
        return False, f"Timed out after {_TEST_TIMEOUT_SECONDS:.0f}s — no response."
    except aiohttp.ClientError as exc:
        return False, f"HTTP client error: {exc}"

    if status == 401:
        return (
            True,
            "Webhook reachable (HTTP 401 — handler rejected the sentinel skill_id, "
            "which is the expected outcome).",
        )
    if status == 200:
        # Unlikely but possible: the user happened to register a skill with
        # all-zero UUID, or our handler regressed and accepts anything.
        return True, "Webhook reachable (HTTP 200)."
    if 300 <= status < 400:
        return False, f"HTTP {status} redirect rejected — the webhook URL must be final."
    if status == 404:
        return (
            False,
            f"HTTP 404 — webhook secret in config does not match the URL "
            f"({DIALOG_WEBHOOK_BASE_PATH}/...): no route registered. "
            "Make sure 'Enable dialog skill' is on and the provider is loaded.",
        )
    if 500 <= status < 600:
        return False, f"HTTP {status} — reverse proxy or upstream returned a server error."
    if 400 <= status < 500:
        return False, f"HTTP {status} — handler rejected the request (not a network problem)."
    return False, f"Unexpected HTTP status {status}."
