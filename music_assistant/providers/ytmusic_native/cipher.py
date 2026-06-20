"""
Native premium-audio cipher solver (in-process, mini-racer / V8).

Resolves the WEB_REMIX `signatureCipher` (itag 141/774, 256k) by running the
player's own base.js inside a bare V8 isolate (mini-racer) and computing the
`sig` and `n` transforms via an injected eval-portal. Pure Python dependency
(a pip wheel bundles V8) - no Node, no browser, no po_token (reverseengeneer.md
§8.1).

The whole path is best-effort: if mini-racer is unavailable or the player
structure can no longer be solved, the caller falls back to ANDROID_VR (~150k).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode, urlparse, urlsplit, urlunsplit

from .constants import BASE_JS_CACHE_TTL

if TYPE_CHECKING:
    import logging

    from .innertube import InnerTube

_PORTAL_JS = Path(__file__).parent / "_portal.js"
# V8 eval of a ~2.5 MB base.js and the per-track descramble are CPU-bound but
# fast; cap them so a pathological player can't wedge a worker thread.
_SETUP_TIMEOUT_SEC = 30.0
_SOLVE_TIMEOUT_SEC = 15.0


class CipherError(Exception):
    """Raised when the signature cipher could not be solved."""


def mini_racer_available() -> bool:
    """Return True if the mini-racer (V8) runtime can be imported."""
    try:
        import py_mini_racer  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True


class CipherSolver:
    """
    Solves and caches the WEB_REMIX player cipher for one provider instance.

    Holds a V8 isolate seeded with the current player's base.js (re-seeded when
    the player rotates or the cache expires) and runs the descrambler in a worker
    thread so the event loop is never blocked.
    """

    def __init__(self, logger: logging.Logger) -> None:
        """
        Initialize the solver.

        :param logger: Provider logger.
        """
        self.logger = logger
        self._portal_js = _PORTAL_JS.read_text(encoding="utf-8")
        self._ctx: Any = None
        self._player_id: str | None = None
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        """Return True if the V8 runtime is available to run the descrambler."""
        return mini_racer_available()

    async def resolve_url(self, signature_cipher: str, innertube: InnerTube) -> str:
        """
        Turn a `signatureCipher` value into a ready-to-stream media URL.

        :param signature_cipher: The format's `signatureCipher` (url + s + sp).
        :param innertube: Transport used to (re)fetch base.js for the current player.
        """
        cipher = parse_qs(signature_cipher)
        base_url = cipher["url"][0]
        s_value = cipher["s"][0]
        sp = cipher.get("sp", ["sig"])[0]
        n_orig = parse_qs(urlparse(base_url).query).get("n", [None])[0]
        async with self._lock:
            await self._ensure_base_js(innertube)
            result = await asyncio.to_thread(self._descramble, s_value, n_orig)
        if "error" in result:
            raise CipherError(result["error"])
        return _rebuild_url(base_url, sp, result["sig"], result.get("n"))

    # ----------------- private -----------------

    async def _ensure_base_js(self, innertube: InnerTube) -> None:
        fresh = (
            self._ctx is not None
            and self._player_id == innertube.player_id
            and (time.time() - self._fetched_at) < BASE_JS_CACHE_TTL
        )
        if fresh:
            return
        base_js = await innertube.fetch_base_js()
        await asyncio.to_thread(self._seed, base_js)
        self._player_id = innertube.player_id
        self._fetched_at = time.time()

    def _seed(self, base_js: str) -> None:
        from py_mini_racer import MiniRacer  # noqa: PLC0415

        ctx = MiniRacer()
        ctx.eval(self._portal_js)
        ctx.call("__setup", base_js, timeout_sec=_SETUP_TIMEOUT_SEC)
        self._ctx = ctx

    def _descramble(self, s_value: str, n_orig: str | None) -> dict[str, Any]:
        if self._ctx is None:
            return {"error": "cipher isolate not initialised"}
        result = self._ctx.call("__descramble", s_value, n_orig, timeout_sec=_SOLVE_TIMEOUT_SEC)
        if not isinstance(result, dict):
            return {"error": f"descrambler returned {type(result).__name__}"}
        return result


def _rebuild_url(base_url: str, sp: str, sig: str, n_out: str | None) -> str:
    parts = urlsplit(base_url)
    query = parse_qs(parts.query, keep_blank_values=True)
    query[sp] = [sig]
    if n_out:
        query["n"] = [n_out]
    new_query = urlencode({k: v[-1] for k, v in query.items()})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))
