"""Generic atomic JSON file storage helpers for the Library Automations plugin."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

import aiofiles

from music_assistant.helpers.json import json_dumps, json_loads


async def read_json(path: str) -> dict[str, Any]:
    """Read a JSON file and return its contents."""
    async with aiofiles.open(path, encoding="utf-8") as fh:
        return cast("dict[str, Any]", json_loads(await fh.read()))


def _atomic_write(path: str, payload: str) -> None:
    """Write payload to a temp file and atomically replace path, cleaning up on failure."""
    tmp = Path(f"{path}.tmp")
    replaced = False
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(payload)
        tmp.replace(path)
        replaced = True
    finally:
        # On any failure mid-write, don't leave a stray temp file behind to accumulate.
        if not replaced:
            with suppress(OSError):
                tmp.unlink(missing_ok=True)


async def write_json(path: str, data: dict[str, Any]) -> None:
    """Write data as JSON to a file atomically (temp file + replace, off the event loop)."""
    # The whole write+rename+cleanup runs in one thread so a cancelled await can't race the
    # rename against cleanup; the rename itself is atomic, so the destination is never truncated.
    payload = json_dumps(data, indent=True)
    await asyncio.to_thread(_atomic_write, path, payload)
