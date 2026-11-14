"""Tests for the AudioMuse-AI setup flow."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from music_assistant.providers.audiomuse_ai.constants import (
    CONF_API_TOKEN,
    CONF_BASE_URL,
    CONF_MEDIA_PROVIDER,
)
from music_assistant.providers.audiomuse_ai.setup_flow import run_setup


async def test_offers_music_providers_and_finishes_with_submitted_values() -> None:
    """The media provider picker excludes the library and the submitted values are persisted."""
    submitted = {
        CONF_BASE_URL: "http://audiomuse:8000",
        CONF_API_TOKEN: "token",
        CONF_MEDIA_PROVIDER: "jellyfin--test",
    }
    session = MagicMock()
    session.context.setup_data = {}
    session.mass.get_providers.return_value = [
        SimpleNamespace(name="Library", instance_id="library", domain="library"),
        SimpleNamespace(name="Jellyfin", instance_id="jellyfin--test", domain="jellyfin"),
    ]
    session.form = AsyncMock(return_value=submitted)
    session.finish = AsyncMock(return_value={"instance_id": "audiomuse_ai--test"})

    await run_setup(session)

    entries = {entry.key: entry for entry in session.form.await_args.args[0]}
    options = entries[CONF_MEDIA_PROVIDER].options
    assert [(option.title, option.value) for option in options] == [("Jellyfin", "jellyfin--test")]
    session.finish.assert_awaited_once_with(submitted)
