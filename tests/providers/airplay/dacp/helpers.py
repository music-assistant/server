"""Helpers for replaying DACP requests through the AirPlay handler."""

from __future__ import annotations

import asyncio
import base64
import json
from unittest.mock import AsyncMock, MagicMock

from music_assistant.providers.airplay.provider import AirPlayProvider


def build_dacp_request(
    path: str,
    active_remote: str | None = "123",
    *,
    method: str = "POST",
    body: str = "",
    headers: dict[str, str] | None = None,
) -> bytes:
    """
    Build raw DACP request bytes for replay.

    :param path: Request path, e.g. "/ctrl-int/1/play".
    :param active_remote: Active-Remote header value; omitted from the request when None.
    :param method: HTTP method.
    :param body: Request body.
    :param headers: Extra headers to include.
    """
    hdrs: dict[str, str] = {}
    if active_remote is not None:
        hdrs["Active-Remote"] = active_remote
    if headers:
        hdrs.update(headers)
    lines = [f"{method} {path} HTTP/1.1"]
    lines += [f"{key}: {value}" for key, value in hdrs.items()]
    request = "\r\n".join(lines) + "\r\n\r\n" + body
    return request.encode("utf-8")


def load_capture(path: str) -> list[bytes]:
    """
    Parse AIRPLAY_DACP_CAPTURE lines from a verbose log file into raw request bytes.

    Parses real-device capture logs for future replay fixtures; landed ahead of its
    first consumer (the capture-mode task).

    :param path: Path to a log file containing AIRPLAY_DACP_CAPTURE lines.
    """
    marker = "AIRPLAY_DACP_CAPTURE "
    requests: list[bytes] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if marker not in line:
                continue
            payload = json.loads(line.split(marker, 1)[1])
            requests.append(base64.b64decode(payload["raw_b64"]))
    return requests


async def replay(provider: AirPlayProvider, raw: bytes) -> bytes:
    """
    Feed raw request bytes through the real DACP handler and return the response bytes.

    :param provider: Real AirPlayProvider instance (built with a mock mass).
    :param raw: Raw DACP request bytes.
    """
    reader = asyncio.StreamReader()
    reader.feed_data(raw)
    reader.feed_eof()
    writer = MagicMock()
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()
    await provider._handle_dacp_request(reader, writer)
    return writer.write.call_args.args[0] if writer.write.called else b""
