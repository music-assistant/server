"""
Live browser preview for the Hue Lights Sync provider.

Exposes a lightweight web page plus a Server-Sent-Events stream that mirrors the
EXACT per-channel frames the analyzer hands to the bridge each render tick. It is
a tuning/debug aid: open it next to the Music Assistant settings and adjust light
latency, strobe coverage, sensitivity etc. while watching the lights react here in
real time (and against the physical fixtures) instead of guessing blind.

The publish path is deliberately non-blocking (drop-on-full bounded queues) so the
30 Hz render loop is never stalled by a slow or stalled browser connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from hue_entertainment import LightChannel, LightColorCommand

    from .provider import HueEntertainmentProvider

LOGGER = logging.getLogger(__name__)

# Bounded per-subscriber buffer. ~7 areas * 30 Hz = ~210 small frames/s; a slow
# browser just drops the oldest rather than back-pressuring the render loop.
_QUEUE_MAX = 120


class PreviewServer:
    """Per-provider live preview: meta registry, SSE fan-out and the HTML page."""

    def __init__(self, provider: HueEntertainmentProvider) -> None:
        """
        Initialize the preview server.

        :param provider: The owning Hue Entertainment provider instance.
        """
        self.provider = provider
        self._subs: set[asyncio.Queue[str]] = set()
        # area_id -> {"name": str, "channels": [[channel_id, name], ...]}
        self._meta: dict[str, dict[str, Any]] = {}

    def register_area(self, area_id: str, name: str, channels: list[LightChannel]) -> None:
        """
        Record an entertainment area and its channels for the preview legend.

        :param area_id: The Hue entertainment area id.
        :param name: Human readable area name.
        :param channels: The ordered list of light channels in the area.
        """
        # Number repeated device names (gradient-strip segments) as "- section N".
        name_counts: dict[str, int] = {}
        for channel in channels:
            name_counts[channel.name] = name_counts.get(channel.name, 0) + 1
        seen: dict[str, int] = {}
        labelled: list[list[Any]] = []
        for channel in channels:
            base = channel.name or f"Channel {channel.channel_id}"
            if name_counts.get(channel.name, 0) > 1:
                seen[channel.name] = seen.get(channel.name, 0) + 1
                base = f"{base} - section {seen[channel.name]}"
            labelled.append([channel.channel_id, base])
        self._meta[area_id] = {"name": name, "channels": labelled}

    def publish(self, area_id: str, commands: list[LightColorCommand]) -> None:
        """
        Fan a rendered frame out to every connected preview client.

        :param area_id: The area the frame belongs to.
        :param commands: The exact 16-bit color commands sent to the bridge.
        """
        if not self._subs:
            return
        # 16-bit -> 8-bit for the browser; keep payload tiny.
        ch = [[c.channel_id, c.red >> 8, c.green >> 8, c.blue >> 8] for c in commands]
        payload = json.dumps({"type": "frame", "area": area_id, "ch": ch})
        line = f"data: {payload}\n\n"
        for queue in self._subs:
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(line)

    def publish_event(self, payload: dict[str, Any]) -> None:
        """Push an arbitrary JSON event (e.g. a calibration tick) to all clients."""
        if not self._subs:
            return
        line = f"data: {json.dumps(payload)}\n\n"
        for queue in self._subs:
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(line)

    def close(self) -> None:
        """Drop all subscribers (used on provider unload)."""
        for queue in self._subs:
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait("event: close\ndata: {}\n\n")
        self._subs.clear()
        self._meta.clear()

    async def serve_page(self, request: web.Request) -> web.Response:
        """Serve the preview HTML page with the per-provider capability token injected."""
        html = _PAGE_HTML.replace("__HUE_PREVIEW_TOKEN__", self.provider.preview_token)
        return web.Response(
            text=html,
            content_type="text/html",
            headers={"Cache-Control": "no-cache"},
        )

    async def serve_stream(self, request: web.Request) -> web.StreamResponse:
        """Serve the SSE stream of live frames (meta first, then frames)."""
        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await resp.prepare(request)
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._subs.add(queue)
        try:
            meta = json.dumps({"type": "meta", "areas": self._meta})
            await resp.write(f"data: {meta}\n\n".encode())
            while True:
                try:
                    line = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    await resp.write(b": keep-alive\n\n")  # comment ping
                    continue
                await resp.write(line.encode())
        except asyncio.CancelledError, ConnectionResetError, RuntimeError:
            pass
        finally:
            self._subs.discard(queue)
        return resp


def _load_page_html() -> str:
    """Load the bundled preview page, falling back to a stub if the resource is missing."""
    try:
        return (Path(__file__).parent / "resources" / "preview.html").read_text(encoding="utf-8")
    except OSError as err:
        LOGGER.warning("Hue live-preview page resource could not be read (%s); serving a stub", err)
        return (
            "<!doctype html><meta charset=utf-8><title>Hue Lights Sync</title>"
            "<body style='font-family:sans-serif;background:#111418;color:#e9eaef;padding:24px'>"
            "<h1>Live preview unavailable</h1>"
            "<p>The preview page resource could not be loaded.</p>"
        )


# Read once at import; a missing resource degrades to the stub instead of breaking
# the whole provider import.
_PAGE_HTML = _load_page_html()
