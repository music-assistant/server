"""Tests for the background tasks controller."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.background_task import TaskSchedule
from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import (
    MediaType,
    ProviderFeature,
    ProviderType,
    TaskScheduleType,
    TaskStatus,
)
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.provider import ProviderManifest

import music_assistant.controllers.music.media.playlists as playlists_module
from music_assistant.controllers.cache import CacheController
from music_assistant.controllers.config import ConfigController
from music_assistant.controllers.config.migrations import _migrate_metadata_maintenance_schedule
from music_assistant.controllers.metadata import MetaDataController
from music_assistant.controllers.metadata.constants import (
    MISSING_ARTIST_METADATA_SCAN_TASK_ID,
    PLAYLIST_METADATA_SCAN_TASK_ID,
    THUMB_CACHE_CLEANUP_TASK_ID,
)
from music_assistant.controllers.music import MusicController
from music_assistant.controllers.music.media.genres import GenreController
from music_assistant.controllers.music.media.playlists import PlaylistController
from music_assistant.controllers.tasks import (
    TasksController,
    get_current_task,
    get_current_task_id,
    report_current_task_failure,
    update_current_task_progress,
    update_current_task_progress_from_index,
    update_current_task_progress_text,
)
from music_assistant.controllers.tasks.constants import TASK_UPDATE_TIMER_ID
from music_assistant.controllers.webserver.helpers.auth_middleware import set_current_user
from music_assistant.helpers.datetime import local_clock_time_to_utc
from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider


async def _wait_for_task_status(
    controller: TasksController,
    task_id: str,
    *statuses: TaskStatus,
    timeout: float = 2.0,
) -> None:
    """Wait until a managed task reaches one of the expected statuses."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if controller.get_task(task_id).status in statuses:
            return
        await asyncio.sleep(0.01)
    msg = (
        f"Task {task_id} did not reach one of {[status.value for status in statuses]} "
        f"before timeout"
    )
    raise AssertionError(msg)


@pytest.fixture
async def tasks_controller(mass_minimal: MusicAssistant) -> AsyncGenerator[TasksController]:
    """Set up the background tasks controller on a minimal Music Assistant instance."""
    controller = TasksController(mass_minimal)
    mass_minimal.tasks = controller
    await controller.setup(await mass_minimal.config.get_core_config(controller.domain))
    controller.initialized.set()
    try:
        yield controller
    finally:
        mass_minimal.cancel_timer(TASK_UPDATE_TIMER_ID)
        await controller.close()


async def test_run_background_task(tasks_controller: TasksController) -> None:
    """Ad hoc background tasks should transition to success and capture context."""
    handler_started = asyncio.Event()
    seen_task_id: str | None = None

    async def handler() -> None:
        nonlocal seen_task_id
        current_task = get_current_task()
        assert current_task is not None
        seen_task_id = get_current_task_id()
        update_current_task_progress(42, "Processing playlist items")
        update_current_task_progress_text("Refreshing playlist")
        handler_started.set()

    task = tasks_controller.run_background_task(
        name="Add tracks to playlist",
        handler=handler,
        user_id="user-123",
    )

    await handler_started.wait()
    await _wait_for_task_status(tasks_controller, task.id, TaskStatus.SUCCESS)

    task = tasks_controller.get_task(task.id)
    assert seen_task_id == task.id
    assert task.status == TaskStatus.SUCCESS
    assert task.user_id == "user-123"
    assert task.last_run_user_id == "user-123"
    assert task.started_at is not None
    assert task.finished_at is not None
    assert task.progress == 42
    assert task.progress_text == "Refreshing playlist"
    assert any("Task started" in line for line in task.logs)
    assert any("Task completed successfully" in line for line in task.logs)


async def test_task_can_report_partial_success(tasks_controller: TasksController) -> None:
    """Task context helpers should surface progress and non-fatal failures."""

    async def handler() -> None:
        progress = update_current_task_progress_from_index(2, 4, "Matching playlist items")
        assert progress == 50
        report_current_task_failure("Skipped duplicate playlist item")

    task = tasks_controller.run_background_task(
        name="Update playlist",
        handler=handler,
        allow_retry=True,
    )

    await _wait_for_task_status(tasks_controller, task.id, TaskStatus.PARTIAL_SUCCESS)

    task = tasks_controller.get_task(task.id)
    assert task.status == TaskStatus.PARTIAL_SUCCESS
    assert task.allow_retry is True
    assert task.failure_count == 1
    assert task.failure_messages == ["Skipped duplicate playlist item"]
    assert task.progress == 50
    assert task.progress_text == "Matching playlist items"
    assert any("completed with 1 issue" in line for line in task.logs)


async def test_priority_task_runs_before_normal(tasks_controller: TasksController) -> None:
    """Priority tasks should be queued ahead of normal tasks."""
    execution_order: list[str] = []
    blocker = asyncio.Event()

    async def blocking_handler() -> None:
        await blocker.wait()

    async def make_handler(label: str) -> Callable[[], Awaitable[None]]:
        async def handler() -> None:
            execution_order.append(label)

        return handler

    # Limit concurrency to 1 so tasks queue up.
    tasks_controller._max_concurrent_tasks = 1

    # Start a blocking task to saturate concurrency.
    tasks_controller.run_background_task(
        name="blocker",
        handler=blocking_handler,
    )

    # Queue two normal tasks, then one priority task.
    normal_handler_1 = await make_handler("normal-1")
    normal_handler_2 = await make_handler("normal-2")
    priority_handler = await make_handler("priority")
    tasks_controller.run_background_task(name="normal-1", handler=normal_handler_1)
    tasks_controller.run_background_task(name="normal-2", handler=normal_handler_2)
    tasks_controller.run_background_task(name="priority", handler=priority_handler, priority=True)

    # Unblock — the priority task should run before the normal ones.
    blocker.set()
    await asyncio.sleep(0.1)

    assert execution_order[0] == "priority"


async def test_user_scoped_task_visibility(tasks_controller: TasksController) -> None:
    """Non-admin users should only see and access their own tasks."""

    async def handler() -> None:
        """No-op test handler."""

    user_task = tasks_controller.run_background_task(
        name="Add playlist tracks",
        handler=handler,
        user_id="user-123",
    )
    system_task = tasks_controller.run_background_task(
        name="Database cleanup",
        handler=handler,
    )

    all_tasks = tasks_controller.list_tasks_for_user(None)
    assert {task.id for task in all_tasks} >= {user_task.id}

    set_current_user(
        User(
            user_id="user-123",
            username="user123",
            role=UserRole.USER,
        )
    )
    try:
        visible_tasks = tasks_controller.list_tasks()
        assert [task.id for task in visible_tasks] == [user_task.id]
        assert tasks_controller.get_task(user_task.id).id == user_task.id
        with pytest.raises(InvalidDataError):
            tasks_controller.get_task(system_task.id)
    finally:
        set_current_user(None)


def _register_blocking_task(
    tasks_controller: TasksController,
    task_id: str,
    handler: Callable[[], Awaitable[None]],
) -> None:
    """Register and immediately queue a scheduled task with the given handler."""
    tasks_controller.register_scheduled_task(
        task_id=task_id,
        name="Test sync",
        handler=handler,
        schedule=TaskSchedule.hourly(every=12),
    )
    tasks_controller.run_task(task_id)


async def test_unregister_scheduled_task_and_wait_waits_for_running_task(
    tasks_controller: TasksController,
) -> None:
    """Unregistering with a wait should only return once the cancelled task unwound."""
    started = asyncio.Event()
    cleanup_finished = False

    async def handler() -> None:
        nonlocal cleanup_finished
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            # cleanup that yields to the event loop, like a sync closing its resources
            await asyncio.sleep(0.05)
            cleanup_finished = True

    _register_blocking_task(tasks_controller, "test_sync_task", handler)
    await asyncio.wait_for(started.wait(), timeout=2)

    assert await tasks_controller.unregister_scheduled_task_and_wait("test_sync_task") is True
    assert cleanup_finished is True
    assert "test_sync_task" not in tasks_controller._tasks


async def test_unregister_scheduled_task_and_wait_gives_up_after_timeout(
    tasks_controller: TasksController,
) -> None:
    """A task that ignores cancellation must not block the caller indefinitely."""
    started = asyncio.Event()
    unwound = asyncio.Event()

    async def handler() -> None:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            # cleanup that outlives the caller's patience
            await asyncio.sleep(0.3)
            unwound.set()
            raise

    _register_blocking_task(tasks_controller, "test_sync_task", handler)
    await asyncio.wait_for(started.wait(), timeout=2)

    unregistered = await tasks_controller.unregister_scheduled_task_and_wait(
        "test_sync_task", timeout=0.05
    )

    assert unregistered is False
    assert not unwound.is_set()
    # the task still finishes (and cleans itself up) on its own
    await asyncio.wait_for(unwound.wait(), timeout=2)
    await asyncio.sleep(0)
    assert "test_sync_task" not in tasks_controller._tasks


async def test_unregister_scheduled_task_and_wait_from_within_the_task(
    tasks_controller: TasksController,
) -> None:
    """A task that unregisters itself must not wait for itself."""
    unregistered: bool | None = None
    returned = asyncio.Event()

    async def handler() -> None:
        nonlocal unregistered
        # yield once so the managed task is fully registered before it cancels itself
        await asyncio.sleep(0)
        unregistered = await tasks_controller.unregister_scheduled_task_and_wait("test_sync_task")
        returned.set()
        await asyncio.sleep(30)

    _register_blocking_task(tasks_controller, "test_sync_task", handler)

    await asyncio.wait_for(returned.wait(), timeout=2)
    assert unregistered is True


async def test_unschedule_provider_sync_waits_for_running_sync(
    mass_minimal: MusicAssistant,
    tasks_controller: TasksController,
) -> None:
    """Unscheduling a provider sync should wait for an in-flight sync of that provider."""
    music = MusicController(mass_minimal)
    mass_minimal.music = music
    task_id = music._get_sync_task_id("test_provider--instance", MediaType.TRACK)
    started = asyncio.Event()
    cleanup_finished = False

    async def handler() -> None:
        nonlocal cleanup_finished
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            await asyncio.sleep(0.05)
            cleanup_finished = True

    _register_blocking_task(tasks_controller, task_id, handler)
    await asyncio.wait_for(started.wait(), timeout=2)

    await music.unschedule_provider_sync("test_provider--instance")

    assert cleanup_finished is True
    assert task_id not in tasks_controller._tasks


async def test_scheduled_task_state_is_restored(mass_minimal: MusicAssistant) -> None:
    """Scheduled tasks should restore their edited schedule and persisted runtime state."""
    controller = TasksController(mass_minimal)
    mass_minimal.tasks = controller
    await controller.setup(await mass_minimal.config.get_core_config(controller.domain))

    async def handler() -> None:
        """No-op test handler."""

    task = controller.register_scheduled_task(
        task_id="sync_spotify_artists",
        name="Sync artists for Spotify",
        handler=handler,
        schedule=TaskSchedule.hourly(every=3),
        initial_delay=1800,
    )
    controller.set_task_enabled(task.id, False)
    controller.update_task_schedule(
        task.id,
        TaskSchedule.weekly(days_of_week=[1, 3, 5], hour=7, minute=15),
    )
    task.status = TaskStatus.PARTIAL_SUCCESS
    task.last_run = datetime(2026, 3, 19, 5, 30, tzinfo=UTC)
    task.last_run_user_id = "admin-user"
    task.failure_count = 2
    task.failure_messages[:] = ["Album import failed", "Artwork lookup failed"]
    controller._persist_scheduled_task_state(controller._get_managed_task(task.id))

    persisted_states = mass_minimal.config.get("core/tasks/scheduled_task_states", {})
    assert isinstance(persisted_states, dict)
    assert task.id in persisted_states

    mass_minimal.cancel_timer(TASK_UPDATE_TIMER_ID)
    await controller.close()

    restored = TasksController(mass_minimal)
    mass_minimal.tasks = restored
    await restored.setup(await mass_minimal.config.get_core_config(restored.domain))
    try:
        restored_task = restored.register_scheduled_task(
            task_id="sync_spotify_artists",
            name="Sync artists for Spotify",
            handler=handler,
            schedule=TaskSchedule.hourly(every=6),
            initial_delay=1800,
        )

        assert restored_task.status == TaskStatus.PARTIAL_SUCCESS
        assert restored_task.last_run == datetime(2026, 3, 19, 5, 30, tzinfo=UTC)
        assert restored_task.last_run_user_id == "admin-user"
        assert restored_task.failure_count == 2
        assert restored_task.failure_messages == [
            "Album import failed",
            "Artwork lookup failed",
        ]
        assert restored_task.schedule is not None
        assert restored_task.schedule.enabled is False
        assert restored_task.schedule.type == TaskScheduleType.WEEKLY
        assert restored_task.schedule.days_of_week == [1, 3, 5]
        assert restored_task.schedule.hour == 7
        assert restored_task.schedule.minute == 15
        assert restored_task.next_run is None
    finally:
        mass_minimal.cancel_timer(TASK_UPDATE_TIMER_ID)
        await restored.close()


async def test_add_playlist_tracks_creates_and_runs_background_task(
    mass_minimal: MusicAssistant,
    tasks_controller: TasksController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Playlist controller should return and execute a managed background task."""
    playlist_controller = PlaylistController(mass_minimal)
    handler_called = asyncio.Event()

    async def fake_get_library_item(_db_playlist_id: int) -> SimpleNamespace:
        return SimpleNamespace(name="Test playlist")

    async def fake_handle_add_playlist_tracks(db_playlist_id: str | int, uris: list[str]) -> None:
        assert db_playlist_id == "42"
        assert uris == ["spotify://track/1", "spotify://track/2"]
        handler_called.set()

    monkeypatch.setattr(playlist_controller, "get_library_item", fake_get_library_item)
    monkeypatch.setattr(
        playlist_controller,
        "_handle_add_playlist_tracks",
        fake_handle_add_playlist_tracks,
    )
    monkeypatch.setattr(
        playlists_module,
        "get_current_user",
        lambda: SimpleNamespace(user_id="user-123"),
    )

    task = await playlist_controller.add_playlist_tracks(
        "42",
        ["spotify://track/1", "spotify://track/2"],
    )

    await handler_called.wait()
    await _wait_for_task_status(tasks_controller, task.id, TaskStatus.SUCCESS)

    task = tasks_controller.get_task(task.id)
    assert task.translation_key == "background_task.add_playlist_tracks"
    assert task.translation_args == ["Test playlist"]
    assert task.user_id == "user-123"
    assert task.last_run_user_id == "user-123"
    assert task.metadata == {
        "task_domain": "playlist_add_tracks",
        "playlist_id": "42",
        "playlist_name": "Test playlist",
        "item_count": 2,
    }


class DummyMusicProvider(MusicProvider):
    """Minimal music provider used for scheduling tests."""

    async def sync_library(self, media_type: MediaType) -> None:
        """No-op sync implementation for tests."""


async def test_schedule_provider_sync_registers_scheduled_background_tasks(
    mass_minimal: MusicAssistant,
    tasks_controller: TasksController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Music controller should register scheduled sync tasks for supported media types."""
    monkeypatch.setattr(
        mass_minimal.config,
        "get_provider_config_value",
        AsyncMock(return_value=True),
    )

    music = MusicController(mass_minimal)
    mass_minimal.music = music

    provider_config = ProviderConfig(
        values={},
        type=ProviderType.MUSIC,
        domain="test_provider",
        instance_id="test_provider--instance",
        name="Spotify",
    )
    monkeypatch.setattr(provider_config, "get_value", lambda *_args, **_kwargs: "GLOBAL")

    provider = DummyMusicProvider(
        mass_minimal,
        manifest=ProviderManifest(
            type=ProviderType.MUSIC,
            domain="test_provider",
            name="Test provider",
            description="Test provider",
            codeowners=["@music-assistant"],
        ),
        config=provider_config,
        supported_features={
            ProviderFeature.LIBRARY_ARTISTS,
            ProviderFeature.LIBRARY_ALBUMS,
        },
    )
    provider.available = True
    mass_minimal._providers[provider.instance_id] = provider

    await music.schedule_provider_sync(provider.instance_id)

    artists_task = tasks_controller.get_task(music._get_sync_task_id(provider, MediaType.ARTIST))
    albums_task = tasks_controller.get_task(music._get_sync_task_id(provider, MediaType.ALBUM))

    assert artists_task.status == TaskStatus.IDLE
    assert artists_task.translation_key == "background_task.sync_provider_artists"
    assert artists_task.translation_args == ["Spotify"]
    assert artists_task.metadata == {
        "task_domain": "music_sync",
        "provider_domain": "test_provider",
        "provider_instance": "test_provider--instance",
        "provider_name": "Spotify",
        "media_type": "artist",
    }
    assert artists_task.schedule == TaskSchedule.hourly(every=12)
    assert artists_task.next_run is not None
    assert artists_task.allow_retry is True

    assert albums_task.translation_key == "background_task.sync_provider_albums"
    assert albums_task.metadata["media_type"] == "album"
    assert albums_task.schedule == TaskSchedule.hourly(every=12)

    with pytest.raises(InvalidDataError):
        tasks_controller.get_task(music._get_sync_task_id(provider, MediaType.TRACK))


async def test_core_maintenance_tasks_register_nightly_schedules(
    mass_minimal: MusicAssistant,
    tasks_controller: TasksController,
) -> None:
    """Core maintenance controllers should register their recurring background tasks."""
    maintenance_hour, maintenance_minute = local_clock_time_to_utc(4, 0)
    cleanup_hour, cleanup_minute = local_clock_time_to_utc(5, 0)
    maintenance_schedule = TaskSchedule.daily(hour=maintenance_hour, minute=maintenance_minute)
    cleanup_schedule = TaskSchedule.daily(hour=cleanup_hour, minute=cleanup_minute)
    cache = CacheController(mass_minimal)
    mass_minimal.cache = cache
    cache._register_cleanup_task()

    music = MusicController(mass_minimal)
    mass_minimal.music = music
    db_cleanup_task = music._register_database_cleanup_task()
    provider_mapping_task = music._register_provider_mapping_correction_task()
    genre_scan_task = music.genres.register_scheduled_scan_task()

    metadata = MetaDataController(mass_minimal)
    mass_minimal.metadata = metadata
    metadata._register_maintenance_tasks()

    cache_task = tasks_controller.get_task("cache_database_cleanup")
    artist_scan_task = tasks_controller.get_task(MISSING_ARTIST_METADATA_SCAN_TASK_ID)
    playlist_scan_task = tasks_controller.get_task(PLAYLIST_METADATA_SCAN_TASK_ID)
    thumb_cleanup_task = tasks_controller.get_task(THUMB_CACHE_CLEANUP_TASK_ID)

    assert cache_task.translation_key == "background_task.cache_database_cleanup"
    assert cache_task.translation_owner == "core.cache"
    assert cache_task.schedule == maintenance_schedule
    assert cache_task.metadata == {"task_domain": "cache_database_cleanup"}

    assert db_cleanup_task.schedule == cleanup_schedule
    assert provider_mapping_task.translation_key == "background_task.correct_provider_mappings"
    assert provider_mapping_task.translation_owner == "core.music"
    assert provider_mapping_task.schedule == TaskSchedule.daily(
        every=30,
        hour=maintenance_hour,
        minute=maintenance_minute,
    )
    assert provider_mapping_task.metadata == {"task_domain": "music_provider_mapping_correction"}
    assert genre_scan_task.schedule == maintenance_schedule

    assert artist_scan_task.translation_key == "background_task.scan_missing_artist_metadata"
    assert artist_scan_task.translation_owner == "core.metadata"
    assert artist_scan_task.metadata == {"task_domain": "metadata_missing_artist_metadata_scan"}

    assert playlist_scan_task.translation_key == "background_task.refresh_playlist_metadata"
    assert playlist_scan_task.translation_owner == "core.metadata"
    assert playlist_scan_task.metadata == {"task_domain": "metadata_playlist_metadata_scan"}

    # Metadata maintenance tasks pick a random time spread across the full day
    # to avoid spiking the shared MusicBrainz mirror, but share one time per instance.
    assert artist_scan_task.schedule is not None
    assert artist_scan_task.schedule.type == TaskScheduleType.DAILY
    assert artist_scan_task.schedule.hour is not None
    assert artist_scan_task.schedule.minute is not None
    assert 0 <= artist_scan_task.schedule.hour <= 23
    assert 0 <= artist_scan_task.schedule.minute <= 59
    assert artist_scan_task.schedule == playlist_scan_task.schedule
    assert thumb_cleanup_task.schedule == artist_scan_task.schedule


async def test_music_sync_completion_queues_database_cleanup_background_task(
    mass_minimal: MusicAssistant,
    tasks_controller: TasksController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed sync task should queue database cleanup as a managed task."""
    cleanup_hour, cleanup_minute = local_clock_time_to_utc(5, 0)
    cleanup_schedule = TaskSchedule.daily(hour=cleanup_hour, minute=cleanup_minute)
    music = MusicController(mass_minimal)
    mass_minimal.music = music
    cleanup_started = asyncio.Event()

    async def fake_cleanup_database() -> None:
        cleanup_started.set()

    monkeypatch.setattr(music, "_cleanup_database", fake_cleanup_database)
    provider_config = ProviderConfig(
        values={},
        type=ProviderType.MUSIC,
        domain="test_provider",
        instance_id="test_provider--instance",
        name="Spotify",
    )
    monkeypatch.setattr(provider_config, "get_value", lambda *_args, **_kwargs: "GLOBAL")
    provider = DummyMusicProvider(
        mass_minimal,
        manifest=ProviderManifest(
            type=ProviderType.MUSIC,
            domain="test_provider",
            name="Test provider",
            description="Test provider",
            codeowners=["@music-assistant"],
        ),
        config=provider_config,
        supported_features={ProviderFeature.LIBRARY_ARTISTS},
    )

    sync_task = tasks_controller.run_background_task(
        task_id=music._get_sync_task_id(provider, MediaType.ARTIST),
        name=music._get_sync_task_name(provider, MediaType.ARTIST),
        handler=music._create_provider_sync_handler(provider, MediaType.ARTIST),
        metadata=music._get_sync_task_metadata(provider, MediaType.ARTIST),
    )

    await _wait_for_task_status(tasks_controller, sync_task.id, TaskStatus.SUCCESS)
    await cleanup_started.wait()
    await _wait_for_task_status(tasks_controller, "music_database_cleanup", TaskStatus.SUCCESS)

    task = tasks_controller.get_task("music_database_cleanup")
    assert task.translation_key == "background_task.database_cleanup"
    assert task.schedule == cleanup_schedule
    assert task.metadata == {
        "task_domain": "music_database_cleanup",
    }


async def test_genre_scan_queues_managed_background_task(
    mass_minimal: MusicAssistant,
    tasks_controller: TasksController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual genre scans should run as managed background tasks."""
    maintenance_hour, maintenance_minute = local_clock_time_to_utc(4, 0)
    maintenance_schedule = TaskSchedule.daily(hour=maintenance_hour, minute=maintenance_minute)
    genre_controller = GenreController(mass_minimal)
    mass_minimal.music = cast("Any", SimpleNamespace(active_sync_tasks=[]))
    monkeypatch.setattr(genre_controller, "_bulk_scan_unmapped_genres", AsyncMock(return_value=3))

    result = await genre_controller.scan_mappings()

    assert result["status"] == "triggered"
    await _wait_for_task_status(tasks_controller, "genre_mapping_scan", TaskStatus.SUCCESS)

    task = tasks_controller.get_task("genre_mapping_scan")
    assert task.translation_key == "background_task.scan_genre_mappings"
    assert task.schedule == maintenance_schedule
    assert task.metadata == {
        "task_domain": "genre_mapping_scan",
    }
    status = await genre_controller.get_scanner_status()
    assert status["running"] is False
    assert status["last_scan_mapped"] == 3


async def test_schedule_update_metadata_uses_managed_background_task(
    mass_minimal: MusicAssistant,
    tasks_controller: TasksController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scheduled metadata lookups should run through the tasks controller."""
    metadata = MetaDataController(mass_minimal)
    mass_minimal.metadata = metadata
    lookup_started = asyncio.Event()
    release_lookup = asyncio.Event()
    resolved_item = SimpleNamespace(
        name="Test Artist",
        media_type=MediaType.ARTIST,
        provider="library",
        uri="artist://library/123",
        metadata=SimpleNamespace(last_refresh=0),
    )

    async def fake_update_metadata(item: object, force_refresh: bool = False) -> object:
        assert item is resolved_item
        assert force_refresh is False
        lookup_started.set()
        await release_lookup.wait()
        return item

    monkeypatch.setattr(metadata, "update_metadata", fake_update_metadata)
    metadata.schedule_update_metadata(cast("Any", resolved_item))

    task_id = metadata._get_metadata_lookup_task_id(resolved_item.uri)
    await lookup_started.wait()

    task = tasks_controller.get_task(task_id)
    assert task.translation_key == "background_task.update_metadata"
    assert task.translation_owner == "core.metadata"
    assert task.metadata == {
        "task_domain": "metadata_lookup",
        "item_uri": resolved_item.uri,
    }

    release_lookup.set()
    deadline = asyncio.get_running_loop().time() + 2.0
    while asyncio.get_running_loop().time() < deadline:
        if tasks_controller.get_task(task_id).status == TaskStatus.SUCCESS:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("Metadata lookup task did not finish successfully")


def _legacy_maintenance_schedule_state() -> dict[str, Any]:
    """Build a persisted core/tasks config holding the legacy 04:00 metadata schedules."""
    return {
        "tasks": {
            "domain": "tasks",
            "scheduled_task_states": {
                "metadata_missing_artist_metadata_scan": {
                    "status": "idle",
                    "schedule": {"type": "daily", "enabled": True, "hour": 4, "minute": 0},
                },
                "metadata_playlist_metadata_scan": {
                    "status": "idle",
                    "schedule": {"type": "daily", "enabled": True, "hour": 4, "minute": 0},
                },
                "metadata_thumb_cache_cleanup": {
                    "status": "idle",
                    "schedule": {"type": "daily", "enabled": True, "hour": 4, "minute": 0},
                },
                "music_database_cleanup": {
                    "status": "idle",
                    "schedule": {"type": "daily", "enabled": True, "hour": 5, "minute": 0},
                },
            },
        }
    }


async def test_metadata_maintenance_schedule_migration_drops_legacy_state(
    mass_minimal: MusicAssistant,
) -> None:
    """The config migration should remove only the orphaned legacy metadata task state."""
    config = ConfigController(mass_minimal)
    config._data = {"core": _legacy_maintenance_schedule_state()}

    assert _migrate_metadata_maintenance_schedule(config._data) is True

    task_states = config._data["core"]["tasks"]["scheduled_task_states"]
    assert "metadata_missing_artist_metadata_scan" not in task_states
    assert "metadata_playlist_metadata_scan" not in task_states
    assert "metadata_thumb_cache_cleanup" not in task_states
    # Unrelated scheduled tasks must be left untouched.
    assert "music_database_cleanup" in task_states

    # Migration is idempotent: a second pass finds nothing left to remove.
    assert _migrate_metadata_maintenance_schedule(config._data) is False


async def test_metadata_maintenance_schedule_migration_noop_without_state(
    mass_minimal: MusicAssistant,
) -> None:
    """The migration should be a no-op when no persisted task state exists."""
    config = ConfigController(mass_minimal)
    config._data = {}
    assert _migrate_metadata_maintenance_schedule(config._data) is False
