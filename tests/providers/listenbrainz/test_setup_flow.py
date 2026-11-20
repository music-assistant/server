"""Tests for the ListenBrainz provider setup flow."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from music_assistant.constants import CONF_USERNAME
from music_assistant.models.setup_flow import SetupFlowContext
from music_assistant.providers.listenbrainz.setup_flow import run_setup


async def test_setup_collects_required_username() -> None:
    """The setup flow stores the required ListenBrainz username."""
    entries_seen: list[Any] = []

    async def form(entries: list[Any], **_kwargs: Any) -> dict[str, str]:
        entries_seen.extend(entries)
        return {CONF_USERNAME: "synthetic-user"}

    session: Any = SimpleNamespace(
        context=SetupFlowContext(
            kind="setup",
            reason="user",
            domain="listenbrainz",
            setup_data={},
        ),
        form=form,
        finish=AsyncMock(),
    )

    await run_setup(session)

    username = next(entry for entry in entries_seen if entry.key == CONF_USERNAME)
    assert username.required
    session.finish.assert_awaited_once_with({CONF_USERNAME: "synthetic-user"})
