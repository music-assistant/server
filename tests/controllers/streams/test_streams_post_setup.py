"""Tests for the StreamsController post_setup ordering."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from music_assistant.controllers.streams.controller import StreamsController


async def test_post_setup_attaches_analysis_db_before_live_announcements() -> None:
    """The analysis database is attached before anything else post_setup starts."""
    calls: list[str] = []
    controller = MagicMock()
    controller._audio_analysis.setup_database = AsyncMock(
        side_effect=lambda: calls.append("setup_database")
    )
    controller.live_announcements.setup = MagicMock(
        side_effect=lambda: calls.append("live_announcements_setup")
    )

    await StreamsController.post_setup(controller)

    assert calls == ["setup_database", "live_announcements_setup"]
