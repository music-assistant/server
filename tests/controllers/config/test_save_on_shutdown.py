"""
Tests that config changes survive a real server shutdown.

These run against an actual MusicAssistant instance rather than a stub, because
the ordering that matters here is the server's own: ``stop()`` cancels the
tracked tasks - the save task among them - before it closes the controllers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

    from music_assistant.mass import MusicAssistant


async def test_immediate_save_survives_a_shutdown(
    mass_minimal: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A change saved immediately must be on disk after a stop, without errors."""
    mass_minimal.config.set("test/immediate", "value", immediate=True)

    await mass_minimal.stop()

    settings = json.loads(Path(mass_minimal.config.filename).read_text())
    assert settings["test"]["immediate"] == "value"
    assert not [record for record in caplog.records if record.levelname == "ERROR"]


async def test_debounced_save_survives_a_shutdown(
    mass_minimal: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A change still waiting out the debounce delay must be on disk after a stop."""
    mass_minimal.config.set("test/debounced", "value")

    await mass_minimal.stop()

    settings = json.loads(Path(mass_minimal.config.filename).read_text())
    assert settings["test"]["debounced"] == "value"
    assert not [record for record in caplog.records if record.levelname == "ERROR"]


async def test_shutdown_does_not_rewrite_unchanged_settings(
    mass_minimal: MusicAssistant,
) -> None:
    """A stop without config changes must leave the settings file alone."""
    mass_minimal.config.set("test/change", "value")
    await mass_minimal.config._async_save()
    written_at = Path(mass_minimal.config.filename).stat().st_mtime_ns

    await mass_minimal.stop()

    assert Path(mass_minimal.config.filename).stat().st_mtime_ns == written_at
