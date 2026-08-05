"""
Unit tests for the output-device recovery in the Sendspin -> Local Audio bridge.

Every blocking device call is run inline against mock streams, so no test ever
opens a sound card and none of them depend on the host's wall clock. Cover six
things, for both writers (PulseAudio and sounddevice):

* mid-stream recovery: a write that fails closes the dead stream, reopens the
  device and carries on writing to the fresh one, with a reopened PA sink both
  re-arming the hardware-volume re-apply that the idle->RUNNING transition would
  otherwise let PulseAudio's own device-restore undo, and re-pinned to unity
  where this player's volume is applied in software instead;
* the reopen budget: a device that lasted beyond the guard window earns another
  reopen, while one that fails again inside it is not churned for the rest of the
  stream, and every writer starts with a budget of its own;
* giving up: a device that cannot be (re)opened -- the guard refusing, the reopen
  raising, the very first open failing, or the writer dying on something
  unclassified -- stops the feed and takes the bridge out of its Sendspin
  session, so the group stops holding the player on PLAYING for audio nobody can
  hear, and a device that opens but will not start is handed back rather than
  leaked;
* not giving up: a stream that simply ended, was stopped, or had its writer
  cancelled keeps its place in the session;
* leaving the session itself: the client is quiesced, never unregistered, an
  absent client is a silent no-op and a refusal is reported rather than raised;
* writer supersession: a writer replaced by a newer one can neither silence its
  replacement nor be the reason a teardown scheduled for itself stops it.
"""

import asyncio
import importlib
import time
from collections.abc import Callable, Coroutine
from contextlib import AbstractContextManager, suppress
from types import ModuleType
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.providers.local_audio.sendspin_bridge import (
    _DEVICE_REOPEN_GUARD_SECONDS,
    SendspinLocalAudioBridge,
)
from music_assistant.providers.sendspin.bridge_role import (
    BRIDGE_BIT_DEPTH,
    BRIDGE_CHANNELS,
    BRIDGE_SAMPLE_RATE,
)

_MODULE = "music_assistant.providers.local_audio.sendspin_bridge"

# An arbitrary reading of the bridge clock. Chunks are queued at exactly this
# instant, so they are due now: neither slept on nor dropped as late.
NOW_US = 4_200_000_000


def _import_sounddevice() -> ModuleType | None:
    """Return the sounddevice module, or None where the PortAudio library is missing."""
    try:
        return importlib.import_module("sounddevice")
    except OSError:
        # only the missing shared library is a reason to skip; anything else is
        # a real problem and silently skipping it would report green on nothing
        return None


# The sounddevice writer imports the module for real, and importing it fails
# outright on a host that has the Python package but not the PortAudio shared
# library it binds to. That writer is then untestable rather than broken.
_SOUNDDEVICE = _import_sounddevice()
needs_portaudio = pytest.mark.skipif(
    _SOUNDDEVICE is None, reason="the sounddevice writer needs the PortAudio shared library"
)

BACKENDS = [
    pytest.param("pulse", id="pulse"),
    pytest.param("sounddevice", id="sounddevice", marks=needs_portaudio),
]


def _run_inline(_executor: object, func: Callable[..., Any], *args: Any) -> asyncio.Future[Any]:
    """
    Stand in for ``loop.run_in_executor``, running the blocking call inline.

    The outcome is handed back on an already-completed future, so a device that
    raises on write really propagates into the writer awaiting it.
    """
    future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    try:
        future.set_result(func(*args))
    except Exception as err:
        future.set_exception(err)
    return future


def _schedule(target: Any, *_args: Any, eager_start: bool = True, **_kwargs: Any) -> Any:
    """
    Stand in for ``mass.create_task``, running real coroutines as real tasks.

    A target that a test patched away is only recorded, so what the bridge
    scheduled stays assertable without a coroutine ever being built.
    """
    if not asyncio.iscoroutine(target):
        return MagicMock()
    return asyncio.Task(target, loop=asyncio.get_running_loop(), eager_start=eager_start)


class _MonotonicClock:
    """A monotonic clock the test moves by hand, so the reopen guard window is exact."""

    def __init__(self) -> None:
        """Start from the current real reading, leaving the event loop's own timers sane."""
        self._now = time.monotonic()

    def __call__(self) -> float:
        """Return the current reading."""
        return self._now

    def advance(self, seconds: float) -> None:
        """Move the clock forward by the given number of seconds."""
        self._now += seconds


def _make_bridge(
    backend: str = "pulse", volume_controller: MagicMock | None = None
) -> SendspinLocalAudioBridge:
    """
    Build a streaming bridge on mocked infrastructure, bound to no real device.

    :param backend: The audio backend the writer dispatches on.
    :param volume_controller: A PA volume controller driving the sink's hardware
        volume, or None for the software-volume path.
    """
    mass = MagicMock()
    mass.loop.run_in_executor = MagicMock(side_effect=_run_inline)
    mass.create_task = MagicMock(side_effect=_schedule)
    provider = MagicMock()
    provider.mass = mass
    device_info = {
        "name": "alsa_output.pci-0000_00_1f.3.analog-stereo",
        "description": "Study Speakers",
        "pa_sink_name": "alsa_output.pci-0000_00_1f.3.analog-stereo",
        "index": 2,
        "sample_rate": BRIDGE_SAMPLE_RATE,
        "bit_depth": BRIDGE_BIT_DEPTH,
        "max_output_channels": BRIDGE_CHANNELS,
    }
    bridge = SendspinLocalAudioBridge(
        provider, device_info, MagicMock(), backend=backend, volume_controller=volume_controller
    )
    # Unity gain, so a chunk reaches the device as the exact bytes it was queued with.
    bridge._volume_level = 100
    bridge._is_streaming = True
    return bridge


def _make_device_stream(write_effect: Any = None) -> MagicMock:
    """
    Build a mock output stream standing in for an open PA sink or sound device.

    :param write_effect: What the blocking write does — an exception it raises,
        or a sequence of per-call outcomes.
    """
    stream = MagicMock()
    stream.write = MagicMock(side_effect=write_effect)
    return stream


def _patch_device_open(
    bridge: SendspinLocalAudioBridge, *streams: Any
) -> AbstractContextManager[Any]:
    """
    Patch the bridge's device-open call to hand out the given streams in order.

    An exception among them is raised by that open instead. Patching the call
    rather than the backend library keeps the tests running on any platform.
    """
    if bridge.backend == "pulse":
        return patch.object(bridge, "_get_pa_stream", AsyncMock(side_effect=list(streams)))
    return patch.object(bridge, "_open_sounddevice_stream", AsyncMock(side_effect=list(streams)))


def _device_error(bridge: SendspinLocalAudioBridge) -> Exception:
    """Return the failure the bridge's backend raises when its device goes away."""
    if bridge.backend == "pulse":
        return OSError("sink vanished")
    assert _SOUNDDEVICE is not None
    error: Exception = _SOUNDDEVICE.PortAudioError("device disconnected")
    return error


def _pcm_chunk(marker: int) -> bytes:
    """Build a short, distinguishable 16-bit stereo PCM payload."""
    return marker.to_bytes(2, "little") * BRIDGE_CHANNELS * 8


def _queue_chunks(bridge: SendspinLocalAudioBridge, *chunks: bytes) -> None:
    """Queue chunks that are all due right now, followed by the writer's stop sentinel."""
    for chunk in chunks:
        bridge._write_queue.put_nowait((NOW_US, chunk))
    bridge._write_queue.put_nowait(None)


def _install_writer(
    bridge: SendspinLocalAudioBridge, coro: Coroutine[Any, Any, None]
) -> asyncio.Task[None]:
    """Start the given coroutine as the writer task the bridge owns."""
    task = asyncio.create_task(coro)
    bridge._writer_task = task
    return task


async def _run_writer(bridge: SendspinLocalAudioBridge) -> None:
    """
    Run the audio writer to completion as the writer the bridge owns.

    The bridge clock is pinned to the instant the queued chunks are due, so the
    writer neither sleeps on them nor drops them as late.
    """
    with patch(f"{_MODULE}._now_us", return_value=NOW_US):
        await _install_writer(bridge, bridge._audio_writer())


async def _never_returns() -> None:
    """Stand in for a writer still feeding its stream."""
    await asyncio.Event().wait()


async def _settle() -> None:
    """Let every task the bridge scheduled reach its own next suspension."""
    for _ in range(5):
        await asyncio.sleep(0)


def _scheduled_targets(bridge: SendspinLocalAudioBridge) -> list[Any]:
    """Return everything the bridge handed to ``mass.create_task``."""
    return [call.args[0] for call in cast("MagicMock", bridge.mass).create_task.call_args_list]


# --- Mid-stream recovery: a dead device is replaced and the audio carries on ---


async def test_a_failed_pa_write_reopens_the_sink_and_keeps_writing() -> None:
    """
    A PA sink that dies mid-stream is replaced and the audio keeps flowing.

    The chunk that hit the dead sink goes with it, but every chunk after it
    reaches the fresh stream instead of being dropped for the rest of the track.
    """
    bridge = _make_bridge()
    dead = _make_device_stream(write_effect=OSError("connection terminated"))
    fresh = _make_device_stream()
    first, second = _pcm_chunk(1), _pcm_chunk(2)
    _queue_chunks(bridge, first, second)

    with (
        _patch_device_open(bridge, dead, fresh),
        patch.object(bridge, "_leave_sendspin_session", MagicMock()) as leave,
    ):
        await _run_writer(bridge)

    dead.write.assert_called_once_with(first)
    fresh.write.assert_called_once_with(second)
    # libpulse has no reconnect, so the failed connection is released outright
    dead.close.assert_called_once_with()
    leave.assert_not_called()


@needs_portaudio
async def test_a_failed_sounddevice_write_reopens_the_device_and_keeps_writing() -> None:
    """
    A sound device that drops out mid-stream is reopened and the audio keeps flowing.

    The chunk that hit the dead device goes with it, but every chunk after it
    reaches the fresh stream instead of being dropped for the rest of the track.
    """
    bridge = _make_bridge(backend="sounddevice")
    dead = _make_device_stream(write_effect=_device_error(bridge))
    fresh = _make_device_stream()
    first, second = _pcm_chunk(1), _pcm_chunk(2)
    _queue_chunks(bridge, first, second)

    with (
        _patch_device_open(bridge, dead, fresh),
        patch.object(bridge, "_leave_sendspin_session", MagicMock()) as leave,
    ):
        await _run_writer(bridge)

    dead.write.assert_called_once_with(first)
    fresh.write.assert_called_once_with(second)
    dead.close.assert_called_once_with()
    # a device that stopped responding can never finish playing its buffer out
    dead.stop.assert_not_called()
    leave.assert_not_called()


async def test_a_reopened_pa_sink_re_arms_the_hardware_volume_reapply() -> None:
    """
    A reopened sink has the cached hardware volume applied to it a second time.

    The fresh sink runs through idle->RUNNING again on its own first write, which
    is exactly when PulseAudio's device-restore can put back a stale level.
    """
    bridge = _make_bridge(volume_controller=MagicMock())
    dead = _make_device_stream(write_effect=[None, OSError("sink vanished")])
    fresh = _make_device_stream()
    _queue_chunks(bridge, _pcm_chunk(1), _pcm_chunk(2), _pcm_chunk(3))

    with (
        _patch_device_open(bridge, dead, fresh),
        patch.object(bridge, "_schedule_hardware_volume_reapply", MagicMock()) as reapply,
    ):
        await _run_writer(bridge)

    assert dead.write.call_count == 2
    fresh.write.assert_called_once_with(_pcm_chunk(3))
    assert reapply.call_count == 2


async def test_a_reopened_pa_sink_is_re_pinned_to_unity_without_hardware_volume() -> None:
    """
    A sink reopened on the software-volume path is pinned back to unity.

    Opening a stream can bleed its own volume into the sink's hardware volume,
    which on this path has to stay at unity or it quietly attenuates everything
    feeding through it.
    """
    bridge = _make_bridge()
    dead = _make_device_stream(write_effect=OSError("sink vanished"))
    fresh = _make_device_stream()
    _queue_chunks(bridge, _pcm_chunk(1), _pcm_chunk(2))

    with (
        _patch_device_open(bridge, dead, fresh),
        patch.object(bridge, "_reset_sink_volume", AsyncMock()) as reset_volume,
    ):
        await _run_writer(bridge)

    fresh.write.assert_called_once_with(_pcm_chunk(2))
    reset_volume.assert_awaited_once_with()


@needs_portaudio
async def test_a_stream_that_will_not_start_is_handed_back_to_portaudio() -> None:
    """
    A device that opens but refuses to start is released rather than leaked.

    PortAudio has already handed the device out by then, and a handle nobody
    gives back keeps every later attempt on that device failing.
    """
    bridge = _make_bridge(backend="sounddevice")
    stream = _make_device_stream()
    stream.start = MagicMock(side_effect=_device_error(bridge))

    with (
        patch("sounddevice.RawOutputStream", MagicMock(return_value=stream)),
        pytest.raises(Exception, match="device disconnected"),
    ):
        bridge._create_sounddevice_stream()

    stream.close.assert_called_once_with()


# --- The reopen budget ---------------------------------------------------------


async def test_a_device_that_lasted_beyond_the_guard_window_is_reopened_again() -> None:
    """
    A sink that played on for a while before failing again earns another reopen.

    Two unrelated dropouts within one stream are a device having a bad day, not
    one that is unusable, so the bridge keeps it playing.
    """
    bridge = _make_bridge()
    clock = _MonotonicClock()

    def _fail_after_the_guard_window(_data: bytes) -> None:
        clock.advance(_DEVICE_REOPEN_GUARD_SECONDS + 1)
        raise OSError("sink vanished again")

    first_dead = _make_device_stream(write_effect=OSError("sink vanished"))
    second_dead = _make_device_stream(write_effect=_fail_after_the_guard_window)
    fresh = _make_device_stream()
    _queue_chunks(bridge, _pcm_chunk(1), _pcm_chunk(2), _pcm_chunk(3))

    with (
        patch(f"{_MODULE}.time.monotonic", clock),
        _patch_device_open(bridge, first_dead, second_dead, fresh),
        patch.object(bridge, "_leave_sendspin_session", MagicMock()) as leave,
    ):
        await _run_writer(bridge)

    fresh.write.assert_called_once_with(_pcm_chunk(3))
    leave.assert_not_called()


async def test_a_device_failing_again_inside_the_guard_window_leaves_the_session() -> None:
    """
    A sink that will not stay open is taken out of the Sendspin session.

    Churning it for the rest of the stream would leave the group holding the
    player on PLAYING while it renders nothing at all.
    """
    bridge = _make_bridge()
    clock = _MonotonicClock()
    first_dead = _make_device_stream(write_effect=OSError("sink vanished"))
    second_dead = _make_device_stream(write_effect=OSError("sink vanished again"))
    spare = _make_device_stream()
    _queue_chunks(bridge, _pcm_chunk(1), _pcm_chunk(2), _pcm_chunk(3))

    with (
        patch(f"{_MODULE}.time.monotonic", clock),
        _patch_device_open(bridge, first_dead, second_dead, spare) as open_device,
        patch.object(bridge, "_leave_sendspin_session", MagicMock()) as leave,
    ):
        await _run_writer(bridge)

    # the refused reopen never reached the device
    assert open_device.call_count == 2
    assert bridge._is_streaming is False
    leave.assert_called_once_with()
    # scheduled, not merely constructed: an unscheduled coroutine never leaves
    assert leave.return_value in _scheduled_targets(bridge)


async def test_each_writer_starts_with_a_fresh_reopen_budget() -> None:
    """
    A device failure on the previous stream cannot condemn the device on this one.

    The guard measures how long a reopened device lasted, so a stamp carried over
    from an earlier stream would give up on the very first failure of a device
    that has been playing fine ever since.
    """
    bridge = _make_bridge()
    clock = _MonotonicClock()
    # a stamp left behind by a previous stream, well inside the guard window
    bridge._last_device_reopen = clock()
    dead = _make_device_stream(write_effect=OSError("sink vanished"))
    fresh = _make_device_stream()
    _queue_chunks(bridge, _pcm_chunk(1), _pcm_chunk(2))

    with (
        patch(f"{_MODULE}.time.monotonic", clock),
        _patch_device_open(bridge, dead, fresh),
        patch.object(bridge, "_leave_sendspin_session", MagicMock()) as leave,
    ):
        await _run_writer(bridge)

    fresh.write.assert_called_once_with(_pcm_chunk(2))
    leave.assert_not_called()


# --- Giving up on a device that cannot be (re)opened ---------------------------


@pytest.mark.parametrize("backend", BACKENDS)
async def test_a_device_that_cannot_be_reopened_leaves_the_session(backend: str) -> None:
    """A reopen the device itself refuses ends the stream and leaves the session."""
    bridge = _make_bridge(backend=backend)
    dead = _make_device_stream(write_effect=_device_error(bridge))
    _queue_chunks(bridge, _pcm_chunk(1), _pcm_chunk(2))

    with (
        _patch_device_open(bridge, dead, _device_error(bridge)),
        patch.object(bridge, "_leave_sendspin_session", MagicMock()) as leave,
    ):
        await _run_writer(bridge)

    assert bridge._is_streaming is False
    leave.assert_called_once_with()
    assert leave.return_value in _scheduled_targets(bridge)


@pytest.mark.parametrize("backend", BACKENDS)
async def test_a_device_that_never_opens_leaves_the_session(backend: str) -> None:
    """
    A device already gone when the stream starts leaves the session too.

    Nothing restarts the writer within a stream, so this player has no audio
    path at all for the whole of it.
    """
    bridge = _make_bridge(backend=backend)
    _queue_chunks(bridge, _pcm_chunk(1))

    with (
        _patch_device_open(bridge, _device_error(bridge)),
        patch.object(bridge, "_leave_sendspin_session", MagicMock()) as leave,
    ):
        await _run_writer(bridge)

    assert bridge._is_streaming is False
    leave.assert_called_once_with()
    assert leave.return_value in _scheduled_targets(bridge)


@pytest.mark.parametrize("backend", BACKENDS)
async def test_an_unexpected_writer_failure_leaves_the_session(backend: str) -> None:
    """
    A writer dying on something nobody anticipated takes the player with it.

    The device is this player's only audio path, so an unclassified failure is
    still silence, and the group must stop reporting it as playback. The device
    is handed back on the way out, whatever the writer died on.
    """
    bridge = _make_bridge(backend=backend)
    stream = _make_device_stream(write_effect=ValueError("unclassified device failure"))
    _queue_chunks(bridge, _pcm_chunk(1))

    with (
        _patch_device_open(bridge, stream),
        patch.object(bridge, "_leave_sendspin_session", MagicMock()) as leave,
    ):
        await _run_writer(bridge)

    assert bridge._is_streaming is False
    stream.close.assert_called_once_with()
    leave.assert_called_once_with()
    assert leave.return_value in _scheduled_targets(bridge)


async def test_giving_up_really_quiesces_the_sendspin_client() -> None:
    """
    A writer that gives up takes the client out of its session for real.

    Scheduling the leave and performing it are separate steps, so this drives
    the whole path end to end rather than either half of it.
    """
    bridge = _make_bridge()
    client = MagicMock()
    client.quiesce_to_solo_stopped = AsyncMock()
    bridge._sendspin_client = client
    _queue_chunks(bridge, _pcm_chunk(1))

    with _patch_device_open(bridge, OSError("sink vanished")):
        await _run_writer(bridge)
    await _settle()  # the leave is deferred off the writer

    client.quiesce_to_solo_stopped.assert_awaited_once_with()


@pytest.mark.parametrize("backend", BACKENDS)
async def test_a_device_that_never_finishes_opening_leaves_the_session(backend: str) -> None:
    """
    A device that hangs on open is given up on rather than waited for.

    Waiting holds the writer with nothing consuming its queue, so the chunks
    pile up behind a player that goes on reporting playback it cannot produce.
    """
    bridge = _make_bridge(backend=backend)
    opening: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    cast("MagicMock", bridge.mass).loop.run_in_executor = MagicMock(return_value=opening)
    _queue_chunks(bridge, _pcm_chunk(1))

    with (
        patch(f"{_MODULE}._DEVICE_OPEN_TIMEOUT_SECONDS", 0.01),
        patch.object(bridge, "_leave_sendspin_session", MagicMock()) as leave,
    ):
        await _run_writer(bridge)

    assert bridge._is_streaming is False
    leave.assert_called_once_with()


@needs_portaudio
async def test_a_cancelled_open_releases_the_device_it_still_gets() -> None:
    """
    A device that finishes opening after its writer is gone is closed, not orphaned.

    PortAudio hands the device out whether or not anyone is still waiting, and a
    handle nobody gives back makes every later open of that device fail.
    """
    bridge = _make_bridge(backend="sounddevice")
    stream = _make_device_stream()
    opening: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

    def _defer_the_open(
        executor: object, func: Callable[..., Any], *args: Any
    ) -> asyncio.Future[Any]:
        # the open takes no arguments; anything else is the release closing up
        return _run_inline(executor, func, *args) if args else opening

    cast("MagicMock", bridge.mass).loop.run_in_executor = MagicMock(side_effect=_defer_the_open)
    task = asyncio.create_task(bridge._open_sounddevice_stream())
    await _settle()
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    # the device only becomes available once nobody is waiting for it any more
    opening.set_result(stream)
    await _settle()

    stream.close.assert_called_once_with()


async def test_a_timed_out_open_releases_the_device_when_it_finally_arrives() -> None:
    """
    A device that opens long after the wait gave up is closed, not left behind.

    The open runs to completion in its own thread whatever the bridge does, and
    a sink nobody hands back keeps every later open of it failing.
    """
    bridge = _make_bridge()
    stream = _make_device_stream()
    opening: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

    def _defer_the_open(
        executor: object, func: Callable[..., Any], *args: Any
    ) -> asyncio.Future[Any]:
        # the open takes no arguments; anything else is the release closing up
        return _run_inline(executor, func, *args) if args else opening

    cast("MagicMock", bridge.mass).loop.run_in_executor = MagicMock(side_effect=_defer_the_open)

    with (
        patch(f"{_MODULE}._DEVICE_OPEN_TIMEOUT_SECONDS", 0.01),
        pytest.raises(TimeoutError),
    ):
        await bridge._get_pa_stream()

    opening.set_result(stream)
    await _settle()

    stream.close.assert_called_once_with()


async def test_abandoning_a_stream_defers_the_leave_off_the_writer() -> None:
    """
    Giving up stops the feed and hands the leave to a task of its own.

    Leaving ends the Sendspin stream, which unwinds straight back into the
    bridge's own teardown — that must not run inside the writer on its way out.
    """
    bridge = _make_bridge()
    # only the registered writer may give up, which is what every caller is
    bridge._writer_task = cast("asyncio.Task[None]", asyncio.current_task())

    with patch.object(bridge, "_leave_sendspin_session", MagicMock()) as leave:
        bridge._abandon_streaming()

    assert bridge._is_streaming is False
    leave.assert_called_once_with()
    create_task = cast("MagicMock", bridge.mass).create_task
    assert create_task.call_args.args[0] is leave.return_value
    assert create_task.call_args.kwargs["eager_start"] is False


async def test_a_writer_a_stream_start_installed_can_still_give_up() -> None:
    """
    A writer started by a real stream start is registered before it runs.

    Everything it touches on the bridge is keyed on being the registered
    writer, so a device failing at the very first open has to still take the
    player out of the session rather than leave it reporting playback.
    """
    bridge = _make_bridge()

    with (
        _patch_device_open(bridge, OSError("sink vanished")),
        patch.object(bridge, "_leave_sendspin_session", MagicMock()) as leave,
    ):
        bridge._on_bridge_stream_start()
        await _settle()

    assert bridge._is_streaming is False
    leave.assert_called_once_with()


async def test_a_superseded_writer_cannot_give_up_on_its_replacement() -> None:
    """
    A writer a newer stream replaced gives up on nothing.

    Leaving the session would stop the group for a stream that is playing
    perfectly well, and the flag it would clear now belongs to that stream.
    """
    bridge = _make_bridge()
    bridge._writer_task = MagicMock()  # a newer stream's writer

    with patch.object(bridge, "_leave_sendspin_session", MagicMock()) as leave:
        bridge._abandon_streaming()

    assert bridge._is_streaming is True
    leave.assert_not_called()


# --- Not giving up: an ordinary end of stream keeps the bridge in its session ---


@pytest.mark.parametrize("backend", BACKENDS)
async def test_a_stream_that_simply_ended_keeps_its_place_in_the_session(backend: str) -> None:
    """
    The stop sentinel is the normal end of a stream, not a device failure.

    Leaving on it would drop the player out of its group at the end of every
    single track.
    """
    bridge = _make_bridge(backend=backend)
    stream = _make_device_stream()
    _queue_chunks(bridge, _pcm_chunk(1))

    with (
        _patch_device_open(bridge, stream),
        patch.object(bridge, "_leave_sendspin_session", MagicMock()) as leave,
    ):
        await _run_writer(bridge)

    stream.write.assert_called_once_with(_pcm_chunk(1))
    assert bridge._is_streaming is False
    leave.assert_not_called()


async def test_a_writer_whose_stream_already_stopped_keeps_its_place_in_the_session() -> None:
    """
    A writer that finds streaming already switched off simply stops feeding.

    That is a teardown racing the queue, not a device that failed, so there is
    nothing for Sendspin to hear about.
    """
    bridge = _make_bridge()
    stream = _make_device_stream()
    _queue_chunks(bridge, _pcm_chunk(1))
    bridge._is_streaming = False

    with (
        _patch_device_open(bridge, stream),
        patch.object(bridge, "_leave_sendspin_session", MagicMock()) as leave,
    ):
        await _run_writer(bridge)

    stream.write.assert_not_called()
    leave.assert_not_called()


@needs_portaudio
async def test_a_stopped_stream_keeps_its_place_in_the_session() -> None:
    """A writer taken down by the teardown path has nothing to report to Sendspin."""
    bridge = _make_bridge(backend="sounddevice")
    stream = _make_device_stream()

    with (
        _patch_device_open(bridge, stream),
        patch.object(bridge, "_leave_sendspin_session", MagicMock()) as leave,
    ):
        task = _install_writer(bridge, bridge._audio_writer())
        await asyncio.sleep(0)  # let the writer park on the empty queue
        await bridge._stop_streaming()

    assert task.done()
    assert not task.cancelled()
    assert bridge._writer_task is None
    assert bridge._is_streaming is False
    stream.close.assert_called_once_with()
    leave.assert_not_called()


async def test_a_cancelled_writer_keeps_its_place_in_the_session() -> None:
    """
    A writer cancelled out from under a healthy sink is not a device failure.

    Cancellation is how the PA writer is torn down between streams, so leaving
    on it would un-group the player on every track change.
    """
    bridge = _make_bridge()
    stream = _make_device_stream()

    with (
        _patch_device_open(bridge, stream),
        patch.object(bridge, "_leave_sendspin_session", MagicMock()) as leave,
    ):
        task = _install_writer(bridge, bridge._audio_writer())
        await asyncio.sleep(0)  # let the writer park on the empty queue
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    assert bridge._is_streaming is False
    leave.assert_not_called()


# --- Leaving the Sendspin session ----------------------------------------------


async def test_leaving_the_session_quiesces_the_client_without_unregistering() -> None:
    """The bridge leaves a shared group (or stops a solo one) but stays registered."""
    bridge = _make_bridge()
    client = MagicMock()
    client.quiesce_to_solo_stopped = AsyncMock()
    bridge._sendspin_client = client

    await bridge._leave_sendspin_session()

    client.quiesce_to_solo_stopped.assert_awaited_once_with()
    # staying registered is what keeps the player around to be grouped again
    cast("MagicMock", bridge.sendspin_server).remove_client.assert_not_called()


async def test_leaving_the_session_without_a_client_is_a_silent_noop() -> None:
    """
    Giving up before registration completed has no session to leave.

    The call has to return without touching anything: swallowing an error from
    an absent client would look identical from the outside, so the absence of a
    complaint is what distinguishes the two.
    """
    bridge = _make_bridge()
    assert bridge._sendspin_client is None

    await bridge._leave_sendspin_session()

    cast("MagicMock", bridge.logger).warning.assert_not_called()


async def test_a_refused_quiesce_is_reported_and_contained() -> None:
    """
    A session that will not release the bridge is reported, never raised.

    The caller is a writer already on its way out, so an exception here would be
    swallowed by the task machinery and the silence would go unexplained.
    """
    bridge = _make_bridge()
    client = MagicMock()
    client.quiesce_to_solo_stopped = AsyncMock(side_effect=RuntimeError("no active session"))
    bridge._sendspin_client = client

    await bridge._leave_sendspin_session()

    cast("MagicMock", bridge.logger).warning.assert_called_once()


# --- Writer supersession: a replaced writer speaks for a stream that is gone ---


@pytest.mark.parametrize("backend", BACKENDS)
async def test_a_superseded_writer_does_not_silence_its_replacement(backend: str) -> None:
    """
    A writer left behind by a newer stream keeps its hands off the shared state.

    The streaming flag now belongs to its replacement, which reads it on every
    chunk; clearing it would silence a stream that is playing perfectly well.
    Its own stream is still its own to close.
    """
    bridge = _make_bridge(backend=backend)
    stream = _make_device_stream()
    newer_writer = MagicMock()
    bridge._writer_task = newer_writer
    bridge._write_queue.put_nowait(None)

    with _patch_device_open(bridge, stream):
        await bridge._audio_writer()

    assert bridge._is_streaming is True
    assert bridge._writer_task is newer_writer
    stream.close.assert_called_once_with()


async def test_a_stale_teardown_leaves_the_newer_writer_running() -> None:
    """
    A teardown scheduled for a replaced writer must not stop the one that replaced it.

    The stream end that scheduled it belongs to the previous stream; acting on it
    would stop the audio the new stream has only just started.
    """
    bridge = _make_bridge()
    newer_writer = _install_writer(bridge, _never_returns())

    await bridge._stop_streaming_locked(MagicMock())

    assert not newer_writer.done()
    assert not newer_writer.cancelled()
    assert bridge._writer_task is newer_writer
    assert bridge._is_streaming is True

    newer_writer.cancel()
    with suppress(asyncio.CancelledError):
        await newer_writer


async def test_a_teardown_stops_the_writer_it_was_scheduled_for() -> None:
    """The writer the stream end actually belongs to is cancelled and cleared."""
    bridge = _make_bridge()
    writer = _install_writer(bridge, _never_returns())

    await bridge._stop_streaming_locked(writer)

    assert writer.cancelled()
    assert bridge._writer_task is None
    assert bridge._is_streaming is False


async def test_a_stream_end_tears_down_the_writer_that_was_running() -> None:
    """
    The teardown a stream end schedules is bound to the writer running at the time.

    That binding is what lets a later teardown recognise itself as stale, so it
    has to name a writer — a teardown naming none matches nothing and quietly
    stops tearing anything down at all.
    """
    bridge = _make_bridge()
    writer = _install_writer(bridge, _never_returns())
    await _settle()

    bridge._on_bridge_stream_end()
    await _settle()

    assert writer.cancelled()
    assert bridge._writer_task is None
    assert bridge._is_streaming is False


async def test_a_stream_end_behind_a_running_teardown_leaves_the_newer_writer_alone() -> None:
    """
    A stream end whose teardown turns out to be stale leaves the streaming flag alone.

    Every chunk is checked against that flag, so clearing it on behalf of a
    stream that has already finished would silence the one that replaced it.
    """
    bridge = _make_bridge()
    old_writer = _install_writer(bridge, _never_returns())
    await _settle()

    await bridge._lock.acquire()  # a teardown is already in progress
    bridge._on_bridge_stream_end()
    await _settle()
    # a new stream installs its writer before the queued teardown gets the lock
    newer_writer = _install_writer(bridge, _never_returns())
    bridge._lock.release()
    await _settle()

    assert bridge._is_streaming is True
    assert bridge._writer_task is newer_writer
    assert not newer_writer.cancelled()

    for task in (old_writer, newer_writer):
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_a_timed_out_stop_cancels_the_writer_it_captured() -> None:
    """
    A writer that will not stop on its own is cancelled — and only that writer.

    The cancel comes after a wait, by which time a new stream may already have
    installed its own writer; cancelling that one would silence a stream that
    has only just started.
    """
    bridge = _make_bridge(backend="sounddevice")
    newer_writer = MagicMock()
    stubborn = _install_writer(bridge, _never_returns())
    await _settle()

    async def _install_the_replacement() -> None:
        await asyncio.sleep(0)
        bridge._writer_task = newer_writer

    with patch(f"{_MODULE}._WRITER_STOP_TIMEOUT_SECONDS", 0.01):
        replacing = asyncio.create_task(_install_the_replacement())
        await bridge._stop_streaming()
    await replacing

    assert stubborn.cancelled()
    assert bridge._writer_task is newer_writer
    newer_writer.cancel.assert_not_called()


async def test_a_teardown_does_not_disown_a_writer_installed_while_it_waits() -> None:
    """
    A writer installed while the teardown waits for its own keeps its registration.

    Stopping a writer takes an await, and a new stream can start within it. The
    registration then belongs to the replacement, and clearing it would leave
    that writer running unowned — invisible to the next teardown, and splitting
    the following stream's chunks with the writer started alongside it.
    """
    bridge = _make_bridge()
    newer_writer = MagicMock()

    async def _hand_over_when_stopped() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            bridge._writer_task = newer_writer
            raise

    old_writer = _install_writer(bridge, _hand_over_when_stopped())
    await asyncio.sleep(0)  # let the writer park before it is torn down

    await bridge._stop_streaming()

    assert old_writer.cancelled()
    assert bridge._writer_task is newer_writer
