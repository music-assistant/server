"""Atomic JSON storage helpers for the Rotation playlists."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from pathlib import Path
from typing import Any

from music_assistant.constants import MASS_LOGGER_NAME
from music_assistant.helpers.json import json_dumps, json_loads

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.rotation_playlist")


def read_json(path: str) -> dict[str, Any]:
    """
    Read a JSON object from a file; a missing or broken file gives an empty dict.

    :param path: The file to read.
    """
    try:
        data = json_loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as err:
        LOGGER.warning("Unable to read %s, starting empty: %s", path, err)
        return {}
    if not isinstance(data, dict):
        LOGGER.warning("Unexpected content in %s, starting empty", path)
        return {}
    return data


async def write_json(path: str, data: dict[str, Any]) -> None:
    """
    Write data as JSON to a file atomically, off the event loop.

    :param path: The file to write.
    :param data: The JSON data.
    """
    await asyncio.to_thread(_atomic_write, path, json_dumps(data, indent=True))


def _atomic_write(path: str, payload: str) -> None:
    tmp = Path(f"{path}.tmp")
    replaced = False
    try:
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)
        replaced = True
    finally:
        if not replaced:
            with suppress(OSError):
                tmp.unlink(missing_ok=True)
