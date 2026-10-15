"""Tests for the Apple Music library sync schedule."""

from unittest.mock import MagicMock

from music_assistant_models.background_task import TaskSchedule
from music_assistant_models.enums import MediaType

from music_assistant.providers.apple_music.provider import AppleMusicProvider


def test_default_library_sync_schedule_is_daily() -> None:
    """Library sync defaults to every 24 hours."""
    provider = object.__new__(AppleMusicProvider)
    provider.mass = MagicMock()

    schedule = provider.get_default_library_sync_schedule(MediaType.TRACK)

    assert schedule == TaskSchedule.hourly(every=24)
