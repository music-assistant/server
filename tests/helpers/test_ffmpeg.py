"""Tests for the ffmpeg helper module."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

from music_assistant.helpers.ffmpeg import FFMpeg

_PCM_FORMAT = AudioFormat(
    content_type=ContentType.PCM_S16LE, sample_rate=44100, bit_depth=16, channels=2
)


# -- _log_reader_task (decode-error flood guard) --


class _FakeStream:
    """Stand-in for a StreamReader/StreamWriter: already closed/at EOF, nothing to drain."""

    def is_closing(self) -> bool:
        return True

    def at_eof(self) -> bool:
        return True


class _FakeProc:
    """Minimal stand-in for asyncio.subprocess.Process — just enough for close() to run."""

    def __init__(self) -> None:
        self.pid = 12345
        self.returncode: int | None = None
        self.stdin: _FakeStream | None = _FakeStream()
        self.stdout = _FakeStream()

    async def communicate(self) -> tuple[bytes, bytes]:
        self.returncode = 0
        return b"", b""

    def send_signal(self, _sig: int) -> None:
        pass


class _FakeProcRacingExit(_FakeProc):
    """A no-stdin process that has already exited, so send_signal raises ProcessLookupError."""

    def __init__(self) -> None:
        super().__init__()
        # no stdin routes close() down the send_signal(SIGINT) branch
        self.stdin = None

    def send_signal(self, _sig: int) -> None:
        raise ProcessLookupError("no such process")


async def test_log_reader_reports_decode_errors_once_and_aborts() -> None:
    """
    Crossing the decode-error threshold logs a single line and aborts the stream.

    Regression test: previously every stderr line was re-promoted to ERROR for the
    rest of the process once 50 "Invalid data" lines were seen, flooding the log
    with thousands of lines for a single corrupted file.
    """
    ffmpeg = FFMpeg(audio_input="-", input_format=_PCM_FORMAT, output_format=_PCM_FORMAT)

    async def fake_stderr() -> AsyncGenerator[str]:
        for _ in range(60):
            yield "Invalid data found when processing input"
        # noise that a genuinely corrupted stream keeps emitting after the threshold;
        # none of this should reach ERROR level under the fix
        for _ in range(20):
            yield "Reserved bit set."

    ffmpeg.iter_stderr = fake_stderr  # type: ignore[method-assign]

    error_lines: list[str] = []
    ffmpeg.logger.error = lambda msg, *args: error_lines.append(msg % args if args else msg)  # type: ignore[method-assign]

    await ffmpeg._log_reader_task()
    assert ffmpeg._abort_task is not None
    await ffmpeg._abort_task

    assert error_lines == ["Excessive decode errors (50+) for this stream; aborting"]
    assert ffmpeg.closed


async def test_log_reader_below_threshold_does_not_abort() -> None:
    """A handful of decode errors, well under the threshold, triggers no report or abort."""
    ffmpeg = FFMpeg(audio_input="-", input_format=_PCM_FORMAT, output_format=_PCM_FORMAT)

    async def fake_stderr() -> AsyncGenerator[str]:
        for _ in range(10):
            yield "Invalid data found when processing input"
        yield "Reserved bit set."

    ffmpeg.iter_stderr = fake_stderr  # type: ignore[method-assign]

    error_lines: list[str] = []
    ffmpeg.logger.error = lambda msg, *args: error_lines.append(msg % args if args else msg)  # type: ignore[method-assign]

    await ffmpeg._log_reader_task()

    assert error_lines == []
    assert ffmpeg._abort_task is None
    assert not ffmpeg.closed


async def test_log_reader_abort_does_not_self_deadlock() -> None:
    """
    The detached abort task can close() the reader's own process without a self-await.

    Regression test for the deadlock this PR reintroduces abort-on-close around:
    close() does ``await asyncio.wait_for(self._stderr_reader_task, 5)``, and
    ``_stderr_reader_task`` here is wired to the very task running
    ``_log_reader_task`` — the same setup ``start()`` uses in production. If the
    abort were awaited inline from within ``_log_reader_task`` instead of via a
    detached task, that task would be awaiting itself, which asyncio turns into
    ``RuntimeError: Task cannot await on itself`` rather than a hang.
    """
    ffmpeg = FFMpeg(audio_input="-", input_format=_PCM_FORMAT, output_format=_PCM_FORMAT)
    ffmpeg.proc = _FakeProc()  # type: ignore[assignment]

    async def fake_stderr() -> AsyncGenerator[str]:
        for _ in range(50):
            yield "Invalid data found when processing input"

    ffmpeg.iter_stderr = fake_stderr  # type: ignore[method-assign]

    reader_task = asyncio.create_task(ffmpeg._log_reader_task())
    ffmpeg._stderr_reader_task = reader_task

    await asyncio.wait_for(reader_task, timeout=2)
    assert ffmpeg._abort_task is not None
    await asyncio.wait_for(ffmpeg._abort_task, timeout=2)

    assert ffmpeg.closed


async def test_abort_survives_send_signal_racing_process_exit() -> None:
    """
    The fire-and-forget abort must not crash if the process exits before it is signalled.

    close() sends SIGINT to no-stdin processes, which raises ProcessLookupError when the
    process already exited between the returncode check and the signal. The abort task is
    never awaited by a caller, so that race must be swallowed inside close() rather than
    escaping as an untracked "Task exception was never retrieved".
    """
    ffmpeg = FFMpeg(audio_input="-", input_format=_PCM_FORMAT, output_format=_PCM_FORMAT)
    ffmpeg.proc = _FakeProcRacingExit()  # type: ignore[assignment]

    async def fake_stderr() -> AsyncGenerator[str]:
        for _ in range(50):
            yield "Invalid data found when processing input"

    ffmpeg.iter_stderr = fake_stderr  # type: ignore[method-assign]

    reader_task = asyncio.create_task(ffmpeg._log_reader_task())
    ffmpeg._stderr_reader_task = reader_task

    await asyncio.wait_for(reader_task, timeout=2)
    assert ffmpeg._abort_task is not None
    # must complete without propagating ProcessLookupError
    await asyncio.wait_for(ffmpeg._abort_task, timeout=2)

    assert ffmpeg.closed
