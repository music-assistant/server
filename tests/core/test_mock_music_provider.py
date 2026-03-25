"""Tests for the MockMusicProvider."""

from unittest.mock import MagicMock

from music_assistant_models.enums import MediaType

from tests.support.fixture_factory import make_track
from tests.support.mock_music_provider import MockMusicProvider


async def test_mock_provider_returns_configured_tracks() -> None:
    """MockMusicProvider yields configured tracks from get_library_tracks."""
    mass_mock = MagicMock()
    provider = MockMusicProvider(
        mass=mass_mock,
        tracks=[make_track("1", "Song One"), make_track("2", "Song Two")],
    )
    results = [t async for t in provider.get_library_tracks()]
    assert len(results) == 2
    assert results[0].name == "Song One"


async def test_mock_provider_search() -> None:
    """MockMusicProvider search matches track names by substring."""
    mass_mock = MagicMock()
    provider = MockMusicProvider(
        mass=mass_mock,
        tracks=[make_track("1", "Matching Song")],
    )
    results = await provider.search("Matching", [MediaType.TRACK], limit=10)
    assert len(results.tracks) == 1


async def test_mock_provider_stream_details_not_found() -> None:
    """MockMusicProvider returns None for unknown item_id."""
    mass_mock = MagicMock()
    provider = MockMusicProvider(mass=mass_mock)
    result = await provider.get_stream_details("nonexistent")
    assert result is None


async def test_mock_provider_stream_details_fail_mode() -> None:
    """MockMusicProvider returns None when fail_stream=True."""
    mass_mock = MagicMock()
    provider = MockMusicProvider(mass=mass_mock, tracks=[make_track("1")], fail_stream=True)
    result = await provider.get_stream_details("1")
    assert result is None
