"""Setup flow for the OneDrive filesystem provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.providers.filesystem_cloud.base import run_cloud_setup

from .auth import authorize

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession


async def run_setup(session: SetupSession) -> None:
    """Run the OneDrive setup flow: content type, OAuth credentials and root folder."""
    await run_cloud_setup(session, authorize)
