"""E2E test fixtures."""

import pytest

from music_assistant.mass import MusicAssistant
from tests.support.harness import MusicAssistantHarness


@pytest.fixture
async def harness(mass: MusicAssistant) -> MusicAssistantHarness:
    """Provide a MusicAssistantHarness wrapping a live MA instance."""
    return MusicAssistantHarness(mass)
