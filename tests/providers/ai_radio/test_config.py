"""Unit tests for AI Radio provider config entries."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from music_assistant.providers.ai_radio.config import get_config_entries


def _make_mass(base_url: str) -> Any:
    """Build a minimal mass stub exposing webserver.base_url."""
    return cast("Any", SimpleNamespace(webserver=SimpleNamespace(base_url=base_url)))


def test_web_ui_url_entry_carries_runtime_url_via_translation_params() -> None:
    """Web UI URL config entry must render via translation_params, not description."""
    mass = _make_mass("http://localhost:8095")

    entries = asyncio.run(get_config_entries(mass))

    entry = next(entry for entry in entries if entry.key == "web_ui_url")
    assert entry.translation_params == ["http://localhost:8095/#/ai-radio"]
    assert entry.description is None
