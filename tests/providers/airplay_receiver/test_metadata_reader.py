"""Tests for the shairport-sync metadata pipe reader."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import pytest

from music_assistant.providers.airplay_receiver.metadata import MetadataReader


@pytest.fixture
def pipe_path(tmp_path: Any) -> str:
    """Create a metadata FIFO like the provider does."""
    path = str(tmp_path / "metadata_pipe")
    os.mkfifo(path)
    return path


async def _write_marker(pipe_path: str, marker: str) -> None:
    """Write a marker using a short-lived FIFO writer."""

    def _write() -> None:
        fd = os.open(pipe_path, os.O_WRONLY)
        try:
            os.write(fd, f"{marker}\n".encode())
        finally:
            os.close(fd)

    await asyncio.get_event_loop().run_in_executor(None, _write)


async def _wait_for(condition: Any, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


async def test_hook_marker_delivered(pipe_path: str) -> None:
    """A sessioncontrol hook marker is delivered as a play_state update."""
    updates: list[dict[str, Any]] = []
    reader = MetadataReader(pipe_path, logging.getLogger("test"), updates.append)
    await reader.start()
    try:
        await _write_marker(pipe_path, "MA_PLAY_BEGIN")
        await _wait_for(lambda: updates)
        assert updates == [{"play_state": "playing"}]
    finally:
        await reader.stop()


async def test_markers_after_writer_close(pipe_path: str) -> None:
    """Accept markers from writers that connect after EOF."""
    updates: list[dict[str, Any]] = []
    reader = MetadataReader(pipe_path, logging.getLogger("test"), updates.append)
    await reader.start()
    try:
        await _write_marker(pipe_path, "MA_PLAY_BEGIN")
        await _wait_for(lambda: len(updates) == 1)
        await _write_marker(pipe_path, "MA_PLAY_END")
        await _wait_for(lambda: len(updates) == 2)
        assert updates == [{"play_state": "playing"}, {"play_state": "stopped"}]
    finally:
        await reader.stop()


async def test_reader_backs_off_on_eof(pipe_path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """After all writers closed, the reader backs off instead of spinning the loop."""
    reader = MetadataReader(pipe_path, logging.getLogger("test"), None)
    real_read = os.read
    read_counts: list[int] = []

    def counting_read(fd: int, size: int) -> bytes:
        if fd == reader._fd:
            read_counts.append(fd)
        return real_read(fd, size)

    monkeypatch.setattr(os, "read", counting_read)
    await reader.start()
    try:
        await _write_marker(pipe_path, "MA_PLAY_BEGIN")
        await asyncio.sleep(0.1)  # Wait for the reader to reach EOF.
        read_counts.clear()
        await asyncio.sleep(0.5)
        assert len(read_counts) < 20
    finally:
        await reader.stop()
