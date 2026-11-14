"""Identify the official Music Assistant release builds."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Final

from music_assistant.helpers.app_vars import has_bundled_app_vars

# written by the Dockerfile of the official container image, which the Home Assistant app runs too
BUILD_INFO_FILE: Final[Path] = Path("/app/build_info.json")


async def get_official_build_info() -> dict[str, str] | None:
    """
    Return the build info of the official release build the server runs from.

    Returns None for any other installation (e.g. a third-party package, a self-built image or
    a run from source), which is not supported.
    """
    return await asyncio.to_thread(_read_official_build_info)


def _read_official_build_info() -> dict[str, str] | None:
    # only the official release image carries both the bundled credentials and the build info
    if not has_bundled_app_vars():
        return None
    try:
        data = json.loads(BUILD_INFO_FILE.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return {str(key): str(value) for key, value in data.items()}
