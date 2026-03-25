"""Tests for the MusicAssistantHarness."""

from music_assistant.mass import MusicAssistant
from tests.support.fixture_factory import make_track
from tests.support.harness import MusicAssistantHarness
from tests.support.mock_music_provider import MockMusicProvider


async def test_harness_wraps_mass(mass: MusicAssistant) -> None:
    """Harness holds a reference to the underlying MA instance."""
    harness = MusicAssistantHarness(mass)
    assert harness.mass is mass


async def test_harness_add_provider(mass: MusicAssistant) -> None:
    """Harness can register a mock provider with MA."""
    harness = MusicAssistantHarness(mass)
    provider = MockMusicProvider(mass=mass, tracks=[make_track()])
    await harness.add_provider(provider)
    assert mass.get_provider(provider.instance_id) is not None
