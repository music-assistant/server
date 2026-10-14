"""Tests for the Apple Music library sync schedule."""

from unittest.mock import MagicMock

from music_assistant_models.background_task import BackgroundTask, TaskSchedule
from music_assistant_models.enums import MediaType, TaskStatus

from music_assistant.providers.apple_music.constants import CONF_SYNC_SCHEDULE_MIGRATED
from music_assistant.providers.apple_music.provider import AppleMusicProvider


def _make_provider(
    migrated: list[str], tasks: list[BackgroundTask]
) -> tuple[AppleMusicProvider, MagicMock]:
    mass = MagicMock()
    mass.music.library_supported.return_value = True
    mass.config.get_raw_provider_config_value.return_value = migrated
    mass.tasks.get_tasks_by_metadata.return_value = tasks
    provider = object.__new__(AppleMusicProvider)
    provider.mass = mass
    provider.config = MagicMock(instance_id="apple_music--test")
    return provider, mass


def _task(media_type: MediaType, schedule: TaskSchedule) -> BackgroundTask:
    return BackgroundTask(
        id=f"music_sync_apple_music--test_{media_type.value}",
        name=media_type.value,
        status=TaskStatus.IDLE,
        schedule=schedule,
        metadata={"media_type": media_type.value},
    )


def test_default_library_sync_schedule_is_daily() -> None:
    """Library sync defaults to every 24 hours."""
    provider, _ = _make_provider(migrated=[], tasks=[])

    schedule = provider.get_default_library_sync_schedule(MediaType.TRACK)

    assert schedule == TaskSchedule.hourly(every=24)


def test_legacy_sync_schedule_migrated_once() -> None:
    """Only schedules still on the former 12 hour default move to 24 hours."""
    legacy = _task(MediaType.TRACK, TaskSchedule.hourly(every=12))
    custom = _task(MediaType.ALBUM, TaskSchedule.hourly(every=6))
    provider, mass = _make_provider(migrated=[], tasks=[legacy, custom])

    provider._migrate_legacy_sync_schedules()

    mass.tasks.update_task_schedule.assert_called_once_with(
        legacy.id, TaskSchedule.hourly(every=24)
    )
    mass.config.set_raw_provider_config_value.assert_called_once_with(
        "apple_music--test", CONF_SYNC_SCHEDULE_MIGRATED, ["track", "album"]
    )


def test_sync_schedule_migration_skips_migrated_media_types() -> None:
    """A deliberate 12 hour schedule survives, while a newly registered media type migrates."""
    own = _task(MediaType.TRACK, TaskSchedule.hourly(every=12))
    turned_on = _task(MediaType.PLAYLIST, TaskSchedule.hourly(every=12))
    provider, mass = _make_provider(migrated=["track"], tasks=[own, turned_on])

    provider._migrate_legacy_sync_schedules()

    mass.tasks.update_task_schedule.assert_called_once_with(
        turned_on.id, TaskSchedule.hourly(every=24)
    )
    mass.config.set_raw_provider_config_value.assert_called_once_with(
        "apple_music--test", CONF_SYNC_SCHEDULE_MIGRATED, ["track", "playlist"]
    )
