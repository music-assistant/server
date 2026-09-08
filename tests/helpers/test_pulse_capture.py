"""
Tests for the pulse_capture helper.

No real PulseAudio is involved: the daemon process and the libpulse controller
are replaced by fakes patched into the pulse_capture module namespace. The fake
daemon creates the native socket file on start so readiness polling completes,
and the fake controller records every load_module/unload_module/volume call.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import ClassVar
from unittest.mock import Mock

import pytest

from music_assistant.helpers import pulse_capture
from music_assistant.helpers.pulse_capture import (
    MAX_RAW_VOLUME_PCT,
    PA_VOLUME_NORM,
    PipeSink,
    PulseCaptureServer,
)


class FakeAsyncProcess:
    """Fake AsyncProcess that mimics the pulseaudio daemon."""

    instances: ClassVar[list[FakeAsyncProcess]] = []

    def __init__(
        self,
        args: list[str],
        stdin: bool | int | None = None,
        stdout: bool | int | None = None,
        stderr: bool | int | None = False,
        name: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        """Record the launch parameters."""
        self.args = args
        self.name = name
        self.env = env or {}
        self.returncode: int | None = None
        self.closed = False
        self._exited = asyncio.Event()
        FakeAsyncProcess.instances.append(self)

    async def start(self) -> None:
        """Create the native socket file, like the real daemon would."""
        runtime_dir = Path(self.env["PULSE_RUNTIME_PATH"])
        runtime_dir.mkdir(parents=True, exist_ok=True)
        (runtime_dir / "native").touch()

    async def iter_stderr(self) -> AsyncGenerator[str]:
        """Yield nothing and block until the daemon exits."""
        await self._exited.wait()
        lines: list[str] = []
        for line in lines:
            yield line

    async def close(self) -> None:
        """Terminate the fake daemon."""
        self.closed = True
        if self.returncode is None:
            self.returncode = 0
        self._exited.set()

    def crash(self) -> None:
        """Simulate an unexpected daemon exit."""
        self.returncode = 1
        self._exited.set()


class FakeVolumeController:
    """Fake PAVolumeController recording all calls."""

    instances: ClassVar[list[FakeVolumeController]] = []

    def __init__(self, server: str | None = None) -> None:
        """Record the server address used to connect."""
        self.server = server
        self.load_module_calls: list[tuple[str, str]] = []
        self.unload_module_calls: list[int] = []
        self.volume_calls: list[tuple[str, float]] = []
        self.closed = False
        self._next_module_index = 1
        FakeVolumeController.instances.append(self)

    def load_module(self, module_name: str, argument: str) -> int | None:
        """Record the call and hand out a unique module index."""
        self.load_module_calls.append((module_name, argument))
        index = self._next_module_index
        self._next_module_index += 1
        return index

    def unload_module(self, module_index: int) -> bool:
        """Record the call."""
        self.unload_module_calls.append(module_index)
        return True

    def set_sink_volume_raw(self, sink_name: str, volume_pct: float, channels: int = 2) -> bool:
        """Record the call."""
        self.volume_calls.append((sink_name, volume_pct))
        return True

    def close(self) -> None:
        """Mark the controller closed."""
        self.closed = True


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> PulseCaptureServer:
    """Return a PulseCaptureServer wired to the fakes, on a tmp cache dir."""
    FakeAsyncProcess.instances = []
    FakeVolumeController.instances = []
    monkeypatch.setattr(pulse_capture, "AsyncProcess", FakeAsyncProcess)
    monkeypatch.setattr(pulse_capture, "PAVolumeController", FakeVolumeController)
    monkeypatch.setattr(pulse_capture, "_READY_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(pulse_capture, "_RESTART_BACKOFF_INITIAL", 0.01)
    mass = Mock()
    mass.cache_path = str(tmp_path)
    return PulseCaptureServer(mass)


async def _wait_for(condition: Callable[[], bool], timeout: float = 2.0) -> None:
    """Poll until condition holds or the timeout expires."""
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


async def test_refcounted_lifecycle(server: PulseCaptureServer) -> None:
    """First acquire starts the daemon, last release stops it, double release is safe."""
    assert not FakeAsyncProcess.instances

    await server.acquire()
    assert len(FakeAsyncProcess.instances) == 1
    proc = FakeAsyncProcess.instances[0]
    assert not proc.closed
    assert proc.args[0] == "pulseaudio"
    assert "--daemonize=no" in proc.args
    assert "-n" in proc.args
    assert "--exit-idle-time=-1" in proc.args
    assert server.generation == 1
    # connectivity was verified against the private socket
    assert FakeVolumeController.instances[0].server == server.server_address

    # second acquire does not start another daemon
    await server.acquire()
    assert len(FakeAsyncProcess.instances) == 1

    # first release keeps the daemon running
    await server.release()
    assert not proc.closed

    # last release stops the daemon and removes the private dir
    # (read via the class list: mypy would consider a re-read of the earlier
    # `assert not proc.closed`-narrowed expression unreachable)
    await server.release()
    assert FakeAsyncProcess.instances[0].closed
    assert FakeVolumeController.instances[0].closed
    assert not Path(server.server_address.removeprefix("unix:")).parent.exists()

    # extra release is a no-op
    await server.release()
    assert len(FakeAsyncProcess.instances) == 1


async def test_daemon_env_isolation(server: PulseCaptureServer, tmp_path: Path) -> None:
    """The daemon env is private and os.environ is never mutated."""
    environ_before = dict(os.environ)
    await server.acquire()
    try:
        assert dict(os.environ) == environ_before

        private_dir = str(tmp_path / "pulse_capture")
        proc = FakeAsyncProcess.instances[0]
        assert proc.env["XDG_RUNTIME_DIR"] == private_dir
        assert proc.env["PULSE_RUNTIME_PATH"] == private_dir
        assert proc.env["PULSE_STATE_PATH"] == private_dir
        # PA writes an auth cookie under HOME and refuses to start when it is
        # unwritable, as it is when the container runs as a uid with no passwd entry
        assert proc.env["HOME"] == private_dir
        # the generated config loads only the native protocol on the private socket
        config_text = (Path(private_dir) / "pulse_capture.pa").read_text(encoding="utf-8")
        assert config_text.startswith("load-module module-native-protocol-unix ")
        assert f"socket={private_dir}/native" in config_text
        assert "auth-anonymous=1" in config_text

        child_env = server.child_env("my_sink")
        assert child_env["PULSE_SERVER"] == f"unix:{private_dir}/native"
        assert child_env["PULSE_SINK"] == "my_sink"
        # merged over the regular subprocess env, not replacing it
        assert child_env["PATH"] == os.environ["PATH"]
        assert dict(os.environ) == environ_before
    finally:
        await server.release()


async def test_unique_sinks_and_module_args(server: PulseCaptureServer) -> None:
    """Each sink gets a unique name/FIFO and correct module-pipe-sink args."""
    await server.acquire()
    try:
        sink1 = await PipeSink.create(server, "soloist")
        sink2 = await PipeSink.create(server, "soloist")
        assert sink1.sink_name != sink2.sink_name
        assert sink1.fifo_path != sink2.fifo_path
        assert sink1.sink_name.startswith("soloist_")
        assert sink1.fifo_path.parent == Path(server.server_address.removeprefix("unix:")).parent

        controller = FakeVolumeController.instances[-1]
        module_name, argument = controller.load_module_calls[0]
        assert module_name == "module-pipe-sink"
        assert f"sink_name={sink1.sink_name}" in argument
        assert f"file={sink1.fifo_path}" in argument
        assert "format=s32le" in argument
        assert "rate=44100" in argument
        assert "channels=2" in argument
    finally:
        await server.release()


async def test_set_volume_forwards_raw_percentage(server: PulseCaptureServer) -> None:
    """set_volume passes the raw percentage (also above 100) to the controller."""
    await server.acquire()
    try:
        sink = await PipeSink.create(server, "soloist")
        await sink.set_volume(400.0)
        await sink.set_volume(85.5)
        controller = FakeVolumeController.instances[-1]
        assert controller.volume_calls == [(sink.sink_name, 400.0), (sink.sink_name, 85.5)]
    finally:
        await server.release()


def test_set_sink_volume_raw_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    """The raw setter maps percentage linearly onto PA_VOLUME_NORM, with clamping."""
    controller = pulse_capture.PAVolumeController.__new__(pulse_capture.PAVolumeController)
    applied: list[tuple[str, int, int]] = []

    def fake_apply(sink_name: str, pa_volume: int, channels: int) -> bool:
        applied.append((sink_name, pa_volume, channels))
        return True

    monkeypatch.setattr(controller, "_apply_sink_volume", fake_apply)

    assert controller.set_sink_volume_raw("sink", 100.0) is True
    assert applied[-1] == ("sink", PA_VOLUME_NORM, 2)
    # linear (no taper): 50% is exactly half of PA_VOLUME_NORM
    controller.set_sink_volume_raw("sink", 50.0)
    assert applied[-1][1] == PA_VOLUME_NORM // 2
    # values above 100% amplify linearly (reciprocal cubic compensation)
    controller.set_sink_volume_raw("sink", 400.0)
    assert applied[-1][1] == PA_VOLUME_NORM * 4
    # clamped to the sane maximum and to zero
    controller.set_sink_volume_raw("sink", 1_000_000.0)
    assert applied[-1][1] == round(PA_VOLUME_NORM * MAX_RAW_VOLUME_PCT / 100.0)
    controller.set_sink_volume_raw("sink", -5.0)
    assert applied[-1][1] == 0


async def test_suspend_resume_use_pactl(
    server: PulseCaptureServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """suspend/resume invoke pactl against the private server."""
    calls: list[tuple[tuple[str, ...], dict[str, str] | None, float | None]] = []

    async def fake_check_output(
        *args: str, env: dict[str, str] | None = None, timeout: float | None = None
    ) -> tuple[int, bytes]:
        calls.append((args, env, timeout))
        return 0, b""

    monkeypatch.setattr(pulse_capture, "check_output", fake_check_output)
    await server.acquire()
    try:
        sink = await PipeSink.create(server, "soloist")
        await sink.suspend()
        await sink.resume()
        assert calls[0][0] == (
            "pactl",
            "--server",
            server.server_address,
            "suspend-sink",
            sink.sink_name,
            "1",
        )
        assert calls[1][0][-1] == "0"
        assert calls[0][1] is not None
        assert calls[0][1]["PULSE_SERVER"] == server.server_address
    finally:
        await server.release()


async def test_unload_idempotent_and_fifo_cleanup(server: PulseCaptureServer) -> None:
    """Unload removes the module once and cleans up the FIFO file."""
    await server.acquire()
    try:
        sink = await PipeSink.create(server, "soloist")
        # the real pipe-sink module creates the FIFO; simulate that
        sink.fifo_path.touch()

        await sink.unload()
        controller = FakeVolumeController.instances[-1]
        assert controller.unload_module_calls == [1]
        assert not sink.fifo_path.exists()

        await sink.unload()
        assert controller.unload_module_calls == [1]
    finally:
        await server.release()


async def test_crash_restart_bumps_generation(server: PulseCaptureServer) -> None:
    """An unexpected daemon exit triggers a restart with a new generation."""
    await server.acquire()
    try:
        stale_sink = await PipeSink.create(server, "soloist")
        assert server.generation == 1
        old_controller = FakeVolumeController.instances[-1]

        FakeAsyncProcess.instances[0].crash()
        await _wait_for(lambda: server.generation == 2)

        # a fresh daemon and controller are in place, the old controller is gone
        assert len(FakeAsyncProcess.instances) == 2
        assert not FakeAsyncProcess.instances[1].closed
        assert old_controller.closed
        new_controller = FakeVolumeController.instances[-1]
        assert new_controller is not old_controller

        # a sink from before the crash skips the module unload (module died
        # with the old daemon; the index would target the wrong module now)
        await stale_sink.unload()
        assert not new_controller.unload_module_calls

        # creating a sink against the restarted daemon works
        sink = await PipeSink.create(server, "soloist")
        assert new_controller.load_module_calls
        assert sink.sink_name.startswith("soloist_")
    finally:
        await server.release()


async def test_release_during_restart_backoff(
    server: PulseCaptureServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Releasing while the supervisor waits to restart stops cleanly."""
    monkeypatch.setattr(pulse_capture, "_RESTART_BACKOFF_INITIAL", 60.0)
    await server.acquire()
    proc = FakeAsyncProcess.instances[0]
    proc.crash()
    # give the supervisor a chance to enter its backoff sleep
    await asyncio.sleep(0.05)
    await server.release()
    assert server.generation == 1
    assert len(FakeAsyncProcess.instances) == 1


def test_get_pulse_capture_server_is_shared(tmp_path: Path) -> None:
    """The accessor returns one shared instance per mass object."""
    mass = Mock()
    mass.cache_path = str(tmp_path)
    first = pulse_capture.get_pulse_capture_server(mass)
    assert pulse_capture.get_pulse_capture_server(mass) is first
    other_mass = Mock()
    other_mass.cache_path = str(tmp_path)
    assert pulse_capture.get_pulse_capture_server(other_mass) is not first
    # a second mass must not evict the first mass's server (it may hold a
    # running daemon that would otherwise leak unreferenced)
    assert pulse_capture.get_pulse_capture_server(mass) is first
