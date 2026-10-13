"""Party plugin adapter: QR rendering for the kiosk overlay."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import io
import logging
import time
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from aiohttp import web
from music_assistant_models.errors import MusicAssistantError

if TYPE_CHECKING:
    from .provider import WebKioskProvider

logger = logging.getLogger(__name__)

PARTY_CACHE_TTL = 10.0
PARTY_CALL_TIMEOUT = 5.0


class PartyInfo(NamedTuple):
    """Active-party details resolved from the MA Party plugin."""

    join_url: str
    name: str | None
    qr_text: str | None
    qr_version: str


class PartyAdapter:
    """Answer whether a party is active and render its join QR code."""

    def __init__(self, provider: WebKioskProvider) -> None:
        """Initialize the adapter."""
        self.provider = provider
        self.cache: tuple[float, PartyInfo | None] | None = None

    async def get_active_party(self) -> PartyInfo | None:
        """
        Return details of the active party, or None when no party is active.

        Never raises: a broken or slow Party plugin degrades to "no party" so
        the kiosk keeps working. Results are cached briefly.
        """
        now = time.monotonic()
        if self.cache is not None and now - self.cache[0] < PARTY_CACHE_TTL:
            return self.cache[1]
        info: PartyInfo | None = None
        try:
            party = cast("Any", self.provider.mass.get_provider("party"))
            if party is not None:
                join_url = await asyncio.wait_for(party.get_party_url(), PARTY_CALL_TIMEOUT)
                if join_url:
                    config = await asyncio.wait_for(party.get_party_config(), PARTY_CALL_TIMEOUT)
                    info = PartyInfo(
                        join_url=join_url,
                        name=getattr(config, "party_name", None),
                        qr_text=getattr(config, "qr_text", None),
                        qr_version=hashlib.sha256(join_url.encode()).hexdigest()[:12],
                    )
        except MusicAssistantError, RuntimeError, TimeoutError:
            logger.warning("Party plugin status check failed", exc_info=True)
        self.cache = (now, info)
        return info

    async def handle_qr(self, request: web.Request) -> web.Response:
        """Serve the guest join URL as a QR code image."""
        party = await self.get_active_party()
        if party is None:
            return web.Response(status=404, text="No active party")
        kind = "png" if request.path.endswith(".png") else "svg"
        body = await asyncio.to_thread(render_qr, party.join_url, kind)
        return web.Response(
            body=body,
            content_type="image/png" if kind == "png" else "image/svg+xml",
            headers={"Cache-Control": "no-store"},
        )


@functools.lru_cache(maxsize=4)
def render_qr(join_url: str, kind: str) -> bytes:
    """
    Render the join URL as a QR image (blocking on a miss; run in a worker thread).

    Results are memoized — the output only changes when the join code rotates.
    """
    import segno  # noqa: PLC0415

    buf = io.BytesIO()
    segno.make(join_url, error="m").save(buf, kind=kind, scale=8)
    return buf.getvalue()
