"""
Stream URL solver for the WEB_REMIX player (in-process V8 via mini-racer).

The authenticated web player hands out stream URLs that googlevideo refuses
until two values are transformed by the player's own JavaScript: the signature
(`s` in a `signatureCipher`) and the `n` throttle parameter. We run the
current player's base.js in a V8 isolate (mini-racer ships V8 as a wheel, so no
Node or browser is needed) and drive the player's own media-URL decorator, the
same code the browser runs, to get both values back. A static descramble of
the cipher functions remains as a fallback for older player families.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

if TYPE_CHECKING:
    import logging
    from collections.abc import Awaitable, Callable

_PORTAL_JS = Path(__file__).parent / "_portal.js"
# Running a ~3 MB base.js and one solve are CPU-bound but fast; bound them so a
# pathological player build can never wedge a worker thread.
_SETUP_TIMEOUT_SEC = 30.0
_DECORATE_TIMEOUT_SEC = 5.0
_DESCRAMBLE_TIMEOUT_SEC = 20.0
# The sign step bails on a too-short signature, so an n-only solve signs a dummy
# of real signature length and ignores the result.
_DUMMY_SIGNATURE = "A" * 100
# A build that failed to load is not downloaded and seeded again for this long, unless
# YouTube moves on to another build first.
_LOAD_RETRY_AFTER = 600


class CipherError(Exception):
    """Raised when a stream URL could not be solved."""


def mini_racer_available() -> bool:
    """Return True if the mini-racer (V8) runtime can be imported."""
    try:
        import py_mini_racer  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True


class PlayerCipher:
    """
    Solves stream URLs for one player build at a time.

    Holds a V8 isolate seeded with the current player's base.js. A new player
    build gets a fresh isolate, since base.js defines its functions globally.
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
        self._has_decorator = False
        # Set once a decorator-solved URL streamed from googlevideo for this build;
        # from then on decorator results are trusted without another probe.
        self._decorator_verified = False
        self._failed_player_id: str | None = None
        self._failed_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def player_id(self) -> str | None:
        """Return the id of the player build the isolate is seeded with."""
        return self._player_id

    def check_player(self, player_id: str) -> None:
        """
        Raise CipherError while the given build recently failed to load.

        Callers check this before downloading the build's ~3 MB base.js, so a build that
        can not be loaded is not fetched and seeded again for every play and preload.

        :param player_id: The player build id (from the base.js URL).
        """
        if (
            player_id == self._failed_player_id
            and time.monotonic() - self._failed_at < _LOAD_RETRY_AFTER
        ):
            raise CipherError(f"player {player_id} failed to load, not retrying it yet")

    async def load(self, player_id: str, base_js: str) -> None:
        """
        Seed a fresh isolate with the given player build.

        :param player_id: The player build id (from the base.js URL).
        :param base_js: The player's base.js source.
        """
        async with self._lock:
            if player_id == self._player_id and self._ctx is not None:
                return
            try:
                ctx, setup = await asyncio.to_thread(self._seed, base_js)
            except CipherError:
                self._failed_player_id, self._failed_at = player_id, time.monotonic()
                raise
            old, self._ctx = self._ctx, ctx
            self._player_id = player_id
            self._has_decorator = bool(setup.get("decorator"))
            self._decorator_verified = False
            if old is not None:
                old.close()
        self.logger.debug(
            "Loaded player %s (url decorator: %s)", player_id, setup.get("decorator") or "none"
        )

    async def resolve(
        self, fmt: dict[str, Any], stream_ok: Callable[[str], Awaitable[bool]]
    ) -> str:
        """
        Turn a player format into a ready-to-stream URL.

        :param fmt: An entry of streamingData.adaptiveFormats.
        :param stream_ok: Probes a URL against googlevideo; True when it streams.
        """
        if url := fmt.get("url"):
            # Pre-signed URL that still carries the raw `n`: solve only n.
            n_in = _query_value(url, "n")
            if not n_in:
                return str(url)
            solved = await self._call("__decorate", _DUMMY_SIGNATURE, n_in)
            if not solved.get("n"):
                raise CipherError(f"n-challenge: {solved.get('error', 'decorator returned no n')}")
            return _with_params(url, {"n": solved["n"]})

        cipher = parse_qs(fmt.get("signatureCipher") or "")
        if not cipher.get("url") or not cipher.get("s"):
            raise CipherError("format has neither a url nor a signatureCipher")
        base_url = cipher["url"][0]
        s_in = cipher["s"][0]
        sp = cipher.get("sp", ["sig"])[0]
        n_in = _query_value(base_url, "n")

        decorated: str | None = None
        if self._has_decorator:
            solved = await self._call("__decorate", s_in, n_in)
            if solved.get("sig") and (not n_in or solved.get("n")):
                decorated = _build(base_url, sp, solved["sig"], solved.get("n"))
                if self._decorator_verified:
                    return decorated
                if await stream_ok(decorated):
                    self._decorator_verified = True
                    return decorated
                self.logger.debug("Decorator URL for player %s did not stream", self._player_id)

        solved = await self._call("__descramble", s_in, n_in)
        if "error" not in solved:
            return _build(base_url, sp, solved["sig"], solved.get("n"))
        if decorated:
            # an unverified decorator URL beats failing outright: a one-off probe
            # failure (network blip) must not make the track unplayable
            return decorated
        raise CipherError(solved["error"])

    def close(self) -> None:
        """Release the V8 isolate."""
        if self._ctx is not None:
            self._ctx.close()
            self._ctx = None
            self._player_id = None

    async def _call(self, func: str, *args: Any) -> dict[str, Any]:
        timeout = _DECORATE_TIMEOUT_SEC if func == "__decorate" else _DESCRAMBLE_TIMEOUT_SEC
        async with self._lock:
            if self._ctx is None:
                raise CipherError("no player loaded")
            try:
                raw = await asyncio.to_thread(self._ctx.call, func, *args, timeout_sec=timeout)
            except Exception as err:
                # mini-racer raises its own exception types (timeout, JS errors)
                return {"error": f"{func} failed: {err}"}
        result = json.loads(raw) if isinstance(raw, str) else None
        if not isinstance(result, dict):
            return {"error": f"{func} returned {type(raw).__name__}"}
        return result

    def _seed(self, base_js: str) -> tuple[Any, dict[str, Any]]:
        from py_mini_racer import MiniRacer  # noqa: PLC0415

        ctx = MiniRacer()
        try:
            ctx.eval(self._portal_js)
            raw = ctx.call("__setup", base_js, timeout_sec=_SETUP_TIMEOUT_SEC)
        except Exception as err:
            ctx.close()
            raise CipherError(f"could not load player: {err}") from err
        return ctx, json.loads(raw)


def _query_value(url: str, key: str) -> str | None:
    return parse_qs(urlsplit(url).query).get(key, [None])[0]


def _with_params(url: str, params: dict[str, str]) -> str:
    parts = urlsplit(url)
    query = {k: v[-1] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
    query.update(params)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _build(base_url: str, sp: str, sig: str, n: str | None) -> str:
    params = {sp: sig}
    if n:
        params["n"] = n
    return _with_params(base_url, params)
