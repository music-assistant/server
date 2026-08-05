"""
Tests for the debounced save of the persistent settings storage.

Saves are debounced by a timer, so on server stop the controller has to decide
whether anything still needs writing: skip the write when the data on disk is
already up to date, but never skip one that is genuinely still pending.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable, Coroutine
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from music_assistant.controllers.config.controller import ConfigController


class _FakeMass:
    """Minimal MusicAssistant stub that tracks every task the controller creates."""

    def __init__(self, storage_path: Path) -> None:
        self.storage_path = str(storage_path)
        self.tasks: list[asyncio.Task[Any]] = []
        self._loop = asyncio.get_running_loop()
        self.loop = SimpleNamespace(call_later=self._loop.call_later, create_task=self._track)

    def create_task(
        self, target: Callable[..., Coroutine[Any, Any, Any]], *args: Any, **kwargs: Any
    ) -> asyncio.Task[Any]:
        """Create a task from a coroutine function, as the real server does."""
        return self._track(target(*args, **kwargs))

    def _track(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        # eager start, like the real server: the save already runs up to its first
        # suspension before the caller gets the task back
        task = asyncio.Task(coro, loop=self._loop, eager_start=True)
        self.tasks.append(task)
        return task


def _make_controller(tmp_path: Path) -> tuple[ConfigController, _FakeMass]:
    mass = _FakeMass(tmp_path)
    controller = ConfigController(mass)  # type: ignore[arg-type]
    controller.initialized = True
    return controller, mass


def _set_without_delay(controller: ConfigController, key: str, value: Any) -> None:
    """Change a setting with the debounce delay reduced to zero."""
    with patch("music_assistant.controllers.config.controller.DEFAULT_SAVE_DELAY", 0):
        controller.set(key, value)


async def _wait_for_save_task(mass: _FakeMass) -> None:
    """Wait until the save timer has fired and started its task."""
    async with asyncio.timeout(5):
        while not mass.tasks:
            await asyncio.sleep(0)


async def _await_save_task(mass: _FakeMass) -> None:
    """Wait for the scheduled save to run to completion."""
    await _wait_for_save_task(mass)
    await asyncio.gather(*mass.tasks)
    mass.tasks.clear()


async def _stalled_json_dumps(*_args: Any, **_kwargs: Any) -> str:
    """Stand in for the serialization step of a save that is still busy when the server stops."""
    await asyncio.Event().wait()
    return ""


async def _cancel_save_tasks(mass: _FakeMass) -> None:
    """Cancel the running save tasks, as the server does on stop."""
    for task in mass.tasks:
        task.cancel()
    await asyncio.gather(*mass.tasks, return_exceptions=True)
    mass.tasks.clear()


async def test_close_skips_save_when_nothing_changed(tmp_path: Path) -> None:
    """A stop without config changes may not rewrite the settings file."""
    controller, mass = _make_controller(tmp_path)
    _set_without_delay(controller, "generation", 1)
    await _await_save_task(mass)

    with patch.object(controller, "_save_to_disk") as save_to_disk:
        await controller.close()

    save_to_disk.assert_not_called()
    assert json.loads(Path(controller.filename).read_text()) == {"generation": 1}


async def test_save_timer_handle_is_cleared_when_it_fires(tmp_path: Path) -> None:
    """The timer handle may not outlive the timer it belongs to."""
    controller, mass = _make_controller(tmp_path)
    _set_without_delay(controller, "generation", 1)
    await _await_save_task(mass)

    assert controller._timer_handle is None


async def test_close_skips_save_after_immediate_save(tmp_path: Path) -> None:
    """An immediate save leaves nothing behind for the stop to write."""
    controller, mass = _make_controller(tmp_path)
    controller.set("generation", 1, immediate=True)
    await _await_save_task(mass)

    with patch.object(controller, "_save_to_disk") as save_to_disk:
        await controller.close()

    save_to_disk.assert_not_called()
    assert json.loads(Path(controller.filename).read_text()) == {"generation": 1}


async def test_close_skips_save_after_startup_migration(tmp_path: Path) -> None:
    """A migration during load writes the settings file, so the stop must not repeat it."""
    controller, _ = _make_controller(tmp_path)
    Path(controller.filename).write_text(json.dumps({"generation": 1}))
    with patch(
        "music_assistant.controllers.config.controller.migrate", new=AsyncMock(return_value=True)
    ):
        await controller._load()

    with patch.object(controller, "_save_to_disk") as save_to_disk:
        await controller.close()

    save_to_disk.assert_not_called()


async def test_close_saves_change_that_is_still_debounced(tmp_path: Path) -> None:
    """A change made within the debounce delay must survive a stop."""
    controller, mass = _make_controller(tmp_path)
    controller.set("generation", 1)

    await controller.close()

    assert not mass.tasks
    assert json.loads(Path(controller.filename).read_text()) == {"generation": 1}


async def test_close_saves_change_whose_save_task_was_cancelled(tmp_path: Path) -> None:
    """
    A change must survive a stop that cancels the save task before closing.

    The server cancels all tracked tasks before it closes the controllers, so a
    save that was already scheduled is cancelled and can only complete here.
    """
    controller, mass = _make_controller(tmp_path)
    with patch(
        "music_assistant.controllers.config.controller.async_json_dumps", new=_stalled_json_dumps
    ):
        _set_without_delay(controller, "generation", 1)
        await _wait_for_save_task(mass)
        await _cancel_save_tasks(mass)
    assert not Path(controller.filename).exists()

    await controller.close()

    assert json.loads(Path(controller.filename).read_text()) == {"generation": 1}


async def test_close_saves_immediate_save_that_was_cancelled(tmp_path: Path) -> None:
    """An immediate save that did not finish before the stop must still be written."""
    controller, mass = _make_controller(tmp_path)
    with patch(
        "music_assistant.controllers.config.controller.async_json_dumps", new=_stalled_json_dumps
    ):
        controller.set("generation", 1, immediate=True)
        await _cancel_save_tasks(mass)
    assert not Path(controller.filename).exists()

    await controller.close()

    assert json.loads(Path(controller.filename).read_text()) == {"generation": 1}


async def test_close_saves_change_whose_save_failed(tmp_path: Path) -> None:
    """A save that could not be written must be retried on stop."""
    controller, mass = _make_controller(tmp_path)
    with patch.object(controller, "_save_to_disk", side_effect=OSError("no space left on device")):
        _set_without_delay(controller, "generation", 1)
        await _wait_for_save_task(mass)
        await asyncio.gather(*mass.tasks, return_exceptions=True)
        mass.tasks.clear()
    assert not Path(controller.filename).exists()

    await controller.close()

    assert json.loads(Path(controller.filename).read_text()) == {"generation": 1}


async def test_close_cancels_the_pending_save_timer(tmp_path: Path) -> None:
    """The stop may not leave a timer behind that still fires a save afterwards."""
    controller, mass = _make_controller(tmp_path)
    _set_without_delay(controller, "generation", 1)

    await controller.close()
    await asyncio.sleep(0)

    assert controller._timer_handle is None
    assert not mass.tasks


async def test_close_saves_change_made_while_a_save_was_writing(tmp_path: Path) -> None:
    """A change made after a save took its snapshot is not in that write, so the stop must do it."""
    controller, mass = _make_controller(tmp_path)
    save_to_disk = controller._save_to_disk
    writing = threading.Event()
    release = threading.Event()

    def blocking_save_to_disk(json_data: str) -> None:
        writing.set()
        release.wait(5)
        save_to_disk(json_data)

    with patch.object(controller, "_save_to_disk", new=blocking_save_to_disk):
        controller.set("first", 1, immediate=True)
        await asyncio.to_thread(writing.wait, 5)
        controller.set("second", 2)
        release.set()
        await asyncio.gather(*mass.tasks)
        mass.tasks.clear()

    await controller.close()

    assert json.loads(Path(controller.filename).read_text()) == {"first": 1, "second": 2}
