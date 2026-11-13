"""Tests for the core Music Assistant server object."""

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

from music_assistant_models.enums import EventType
from music_assistant_models.player_queue import PlayerQueue

from music_assistant.constants import MASS_LOGGER_NAME
from music_assistant.controllers.player_queues.constants import (
    CACHE_CATEGORY_PLAYER_QUEUE_STATE,
)
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.storage import StorageController
from music_assistant.mass import MusicAssistant
from tests.conftest import full_mass_context

if TYPE_CHECKING:
    import pytest
    from aiohttp import ClientSession
    from music_assistant_models.config_entries import CoreConfig
    from music_assistant_models.event import MassEvent


async def test_start_and_stop_server(mass: MusicAssistant) -> None:
    """Test that music assistant starts and stops cleanly."""
    domains = frozenset(p.domain for p in mass.get_provider_manifests())
    core_providers = frozenset(
        (
            "builtin",
            "cache",
            "discovery",
            "metadata",
            "music",
            "player_queues",
            "players",
            "streams",
        )
    )
    assert domains.issuperset(core_providers)


async def test_core_controller_setup_can_use_the_http_sessions(tmp_path: Path) -> None:
    """The core controllers can use the shared http sessions in their setup."""
    sessions: list[ClientSession] = []
    setup = StorageController.setup

    async def setup_using_the_sessions(self: StorageController, config: CoreConfig) -> None:
        sessions.extend((self.mass.http_session, self.mass.http_session_no_ssl))
        await setup(self, config)

    with patch.object(StorageController, "setup", setup_using_the_sessions):
        async with full_mass_context(tmp_path) as mass:
            assert len(sessions) == 2
            assert sessions[0] is mass.http_session
            assert sessions[1] is mass.http_session_no_ssl


async def test_runtime_dir_lives_while_the_server_runs(tmp_path: Path) -> None:
    """The runtime dir is created on start and removed on stop."""
    async with full_mass_context(tmp_path) as mass:
        runtime_path = Path(mass.runtime_path)
        assert runtime_path.is_dir()
    assert not runtime_path.exists()


async def test_server_info(mass: MusicAssistant) -> None:
    """Test that the server info advertises the webserver details."""
    server_info = mass.get_server_info()
    assert server_info.name is not None
    assert "Music Assistant" in server_info.name
    assert server_info.internal_url == mass.webserver.base_url
    assert server_info.external_url is None
    assert server_info.has_remote_access is False
    # the test environment is not the official release image
    assert server_info.unsupported_install is True


async def test_server_info_reports_official_build(mass: MusicAssistant) -> None:
    """Test that the official release image is reported as a supported installation."""
    mass.build_info = {"version": "2.11.0"}

    assert mass.get_server_info().unsupported_install is False


async def test_events(mass: MusicAssistant) -> None:
    """Test that events sent by signal_event can be seen by subscribe."""
    filters: list[tuple[EventType | tuple[EventType, ...] | None, str | tuple[str, ...] | None]] = [
        (None, None),
        (EventType.UNKNOWN, None),
        ((EventType.UNKNOWN, EventType.PLAYER_ADDED), None),
        (None, "myid1"),
        (None, ("myid1", "myid2")),
        (EventType.UNKNOWN, "myid1"),
    ]

    for event_filter, id_filter in filters:
        flag = False

        def _ev(event: MassEvent) -> None:
            assert event.event == EventType.UNKNOWN
            assert event.data == "mytestdata"
            assert event.object_id == "myid1"
            nonlocal flag
            flag = True

        remove_cb = mass.subscribe(_ev, event_filter, id_filter)

        mass.signal_event(EventType.UNKNOWN, "myid1", "mytestdata")
        await asyncio.sleep(0)
        assert flag is True

        flag = False
        remove_cb()
        mass.signal_event(EventType.UNKNOWN)
        await asyncio.sleep(0)
        assert flag is False


async def test_create_task_failure_logged(
    mass_minimal: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that a failed task without awaiter is logged, also without debug logging."""

    async def _boom() -> None:
        raise ValueError("boom")

    with caplog.at_level(logging.INFO, logger=MASS_LOGGER_NAME):
        mass_minimal.create_task(_boom)
        # allow the task's done callback to run
        await asyncio.sleep(0)

    assert any(
        "boom" in record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
    )


async def test_create_task_names_only_on_request(mass_minimal: MusicAssistant) -> None:
    """Test that a task is named only by an explicit name, never by its task_id."""

    async def _work() -> None:
        return

    named = mass_minimal.create_task(_work(), task_name="provider_loaded_some_provider")
    assert named.get_name() == "provider_loaded_some_provider"

    # task ids key on arguments such as image urls, so they must stay out of the name
    keyed = mass_minimal.create_task(_work(), task_id="palette_fetch_p1_current_https://x/y?sig=1")
    assert "https://x/y?sig=1" not in keyed.get_name()

    await asyncio.gather(named, keyed)


async def test_create_task_replacement_stays_tracked(mass_minimal: MusicAssistant) -> None:
    """Test that a finished task does not untrack a replacement with the same task_id."""
    task_id = "test_replacement"
    release = asyncio.Event()

    async def _instant() -> None:
        return

    async def _blocked() -> None:
        await release.wait()

    # a task that never suspends is already finished when create_task returns,
    # so its done callback is still queued while the caller continues
    first = mass_minimal.create_task(_instant(), task_id=task_id)
    assert first.done()
    second = mass_minimal.create_task(_blocked(), task_id=task_id)
    assert second is not first
    assert mass_minimal._tracked_tasks[task_id] is second

    # allow the first task's done callback to run
    await asyncio.sleep(0)

    assert mass_minimal._tracked_tasks.get(task_id) is second
    # a later caller must join the in-flight task instead of starting a duplicate
    assert mass_minimal.create_task(_blocked(), task_id=task_id) is second

    release.set()
    await second
    assert task_id not in mass_minimal._tracked_tasks


async def test_create_task_abort_existing_tracks_replacement(
    mass_minimal: MusicAssistant,
) -> None:
    """Test that a task aborted in favour of a replacement does not untrack it."""
    task_id = "test_abort_existing"

    async def _blocked() -> None:
        await asyncio.Event().wait()

    first = mass_minimal.create_task(_blocked(), task_id=task_id)
    second = mass_minimal.create_task(_blocked(), task_id=task_id, abort_existing=True)
    assert second is not first

    # the aborted task runs its done callback only once the cancellation is delivered
    await asyncio.wait((first,))
    assert first.cancelled()
    assert mass_minimal._tracked_tasks.get(task_id) is second

    second.cancel()
    await asyncio.wait((second,))
    assert task_id not in mass_minimal._tracked_tasks


async def test_stop_persists_a_recent_queue_change(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A queue change still waiting out the save delay is persisted when the server stops."""
    queue_id = "shutdown_queue"
    queue = PlayerQueue(
        queue_id=queue_id, active=False, display_name="Queue", available=True, items=0
    )
    mass.player_queues._queue_data[queue_id] = PlayerQueueData(queue=queue)
    queue.current_index = 3
    mass.player_queues.signal_update(queue_id)

    unload_provider = mass.unload_provider

    async def unload_provider_removing_the_player(instance_id: str) -> None:
        # unloading a player provider unregisters its players, which removes their queues
        mass.player_queues.on_player_remove(queue_id, permanent=False)
        await unload_provider(instance_id)

    saved: list[dict[str, Any]] = []
    cache_set = mass.cache.set

    async def record_cache_set(key: str, data: Any, **kwargs: Any) -> None:
        if key == queue_id and kwargs.get("category") == CACHE_CATEGORY_PLAYER_QUEUE_STATE:
            saved.append(data)
        await cache_set(key, data, **kwargs)

    monkeypatch.setattr(mass, "unload_provider", unload_provider_removing_the_player)
    monkeypatch.setattr(mass.cache, "set", record_cache_set)

    await mass.stop()

    assert saved
    assert saved[-1]["queue"]["current_index"] == 3


async def test_stop_logs_a_failed_provider_unload(
    mass_minimal: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A provider that fails to unload is logged and does not stop the others from unloading."""
    mass_minimal._providers.update({"broken": MagicMock(), "healthy": MagicMock()})
    unloaded: list[str] = []

    async def unload_provider(instance_id: str) -> None:
        mass_minimal._providers.pop(instance_id)
        if instance_id == "broken":
            raise RuntimeError("unload went wrong")
        unloaded.append(instance_id)

    with patch.object(mass_minimal, "unload_provider", unload_provider):
        await mass_minimal.stop()

    assert unloaded == ["healthy"]
    assert [
        record
        for record in caplog.records
        if "broken" in record.getMessage() and "unload went wrong" in record.getMessage()
    ]


async def test_stop_cancels_tracked_timers(mass_minimal: MusicAssistant) -> None:
    """A timer scheduled through call_later, also during the shutdown, does not fire after a stop."""
    callback = MagicMock()
    mass_minimal.call_later(0.05, callback, task_id="before_stop")
    config_close = mass_minimal.config.close

    async def close_scheduling_a_timer() -> None:
        mass_minimal.call_later(0.05, callback, task_id="during_stop")
        await config_close()

    with patch.object(mass_minimal.config, "close", close_scheduling_a_timer):
        await mass_minimal.stop()
    await asyncio.sleep(0.1)

    callback.assert_not_called()
    assert not mass_minimal._tracked_timers


async def test_stop_closes_the_queues_while_tasks_and_timers_still_run(
    mass: MusicAssistant,
) -> None:
    """Stopping the queues' playback still has the running tasks and pending timers."""
    listener = mass.create_task(asyncio.Event().wait(), task_id="test_listener")
    mass.call_later(60, MagicMock(), task_id="test_player_update")
    alive_at_queue_close: list[bool] = []
    queues_close = mass.player_queues.close

    async def record_queues_close() -> None:
        alive_at_queue_close.append(
            not listener.done() and "test_player_update" in mass._tracked_timers
        )
        await queues_close()

    with patch.object(mass.player_queues, "close", record_queues_close):
        await mass.stop()

    assert alive_at_queue_close == [True]
    assert listener.cancelled()
