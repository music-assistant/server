"""Tests for the shairport-sync metadata pipe reader."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import pathlib
import time
from collections.abc import Callable
from typing import Any

import pytest

from music_assistant.providers.airplay_receiver.metadata import MdnsService, MetadataReader


@pytest.fixture
def pipe_path(tmp_path: pathlib.Path) -> str:
    """Create a metadata FIFO like the provider does."""
    path = str(tmp_path / "metadata_pipe")
    os.mkfifo(path)
    return path


def _item(item_type: str, code: str, data: bytes = b"") -> bytes:
    """Encode a metadata item the way shairport-sync writes it to the pipe."""
    item = (
        f"<item><type>{item_type.encode().hex()}</type>"
        f"<code>{code.encode().hex()}</code><length>{len(data)}</length>"
    )
    if data:
        item += f'\n<data encoding="base64">\n{base64.b64encode(data).decode()}</data>'
    return f"{item}</item>\n".encode()


async def _write(pipe_path: str, data: bytes, timeout: float = 5.0) -> None:
    """Write data in a single write, using a short-lived FIFO writer."""
    # O_NONBLOCK so a regression leaving the FIFO readerless fails the test instead of
    # hanging it: opening for write raises ENXIO while no reader is attached.
    async with asyncio.timeout(timeout):
        while True:
            try:
                fd = os.open(pipe_path, os.O_WRONLY | os.O_NONBLOCK)
            except OSError:
                await asyncio.sleep(0.05)
                continue
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            return


async def _wait_for(condition: Callable[[], bool], timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


async def test_pipe_is_open_when_start_returns(pipe_path: str) -> None:
    """start() attaches to the FIFO so shairport-sync never finds it without a reader."""
    reader = MetadataReader(pipe_path, logging.getLogger("test"), None)
    await reader.start()
    try:
        # Opening for write fails with ENXIO while no reader is attached.
        fd = os.open(pipe_path, os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
    finally:
        await reader.stop()


async def test_active_state_marker_delivered(pipe_path: str) -> None:
    """An active state begin marker is delivered as a play_state update."""
    updates: list[dict[str, Any]] = []
    reader = MetadataReader(pipe_path, logging.getLogger("test"), updates.append)
    await reader.start()
    try:
        await _write(pipe_path, _item("ssnc", "abeg"))
        await _wait_for(lambda: bool(updates))
        assert updates == [{"play_state": "playing"}]
    finally:
        await reader.stop()


async def test_mdns_service_hand_over_is_delivered(pipe_path: str) -> None:
    """The mDNS service shairport-sync hands over (svrg) is delivered for publishing."""
    updates: list[dict[str, Any]] = []
    reader = MetadataReader(pipe_path, logging.getLogger("test"), updates.append)
    await reader.start()
    payload = (
        b"service=_raop._tcp\nname=0A1B2C3D4E5F@Kitchen | Music Assistant\nport=7123\n"
        b"txt=am=ShairportSync\ntxt=md=0,1,2\ntxt=pw=false\n"
    )
    try:
        await _write(pipe_path, _item("ssnc", "svrg", payload))
        await _wait_for(lambda: bool(updates))
        assert updates == [
            {
                "mdns_service": MdnsService(
                    service_type="_raop._tcp",
                    name="0A1B2C3D4E5F@Kitchen | Music Assistant",
                    port=7123,
                    txt={"am": "ShairportSync", "md": "0,1,2", "pw": "false"},
                )
            }
        ]
    finally:
        await reader.stop()


async def test_malformed_mdns_service_hand_over_is_ignored(
    pipe_path: str, caplog: pytest.LogCaptureFixture
) -> None:
    """An svrg item without a port publishes nothing."""
    updates: list[dict[str, Any]] = []
    reader = MetadataReader(pipe_path, logging.getLogger("test"), updates.append)
    await reader.start()
    try:
        payload = b"service=_raop._tcp\nname=0A1B2C3D4E5F@Kitchen\n"
        await _write(pipe_path, _item("ssnc", "svrg", payload))
        await _wait_for(lambda: "malformed mDNS service" in caplog.text)
        assert not updates
    finally:
        await reader.stop()


async def test_markers_after_writer_close(pipe_path: str) -> None:
    """Accept markers from a writer that connects after an earlier writer closed."""
    updates: list[dict[str, Any]] = []
    reader = MetadataReader(pipe_path, logging.getLogger("test"), updates.append)
    await reader.start()
    try:
        await _write(pipe_path, _item("ssnc", "abeg"))
        await _wait_for(lambda: len(updates) == 1)
        await _write(pipe_path, _item("ssnc", "aend"))
        await _wait_for(lambda: len(updates) == 2)
        assert updates == [{"play_state": "playing"}, {"play_state": "stopped"}]
    finally:
        await reader.stop()


async def test_marker_between_items_in_one_read_is_delivered(pipe_path: str) -> None:
    """A marker that arrives in one read between other items is delivered, in order."""
    updates: list[dict[str, Any]] = []
    reader = MetadataReader(pipe_path, logging.getLogger("test"), updates.append)
    await reader.start()
    try:
        await _write(
            pipe_path,
            _item("ssnc", "pvol", b"-15.00,0.00,0.00,0.00")
            + _item("ssnc", "abeg")
            + _item("ssnc", "mdst", b"1234"),
        )
        await _wait_for(lambda: len(updates) == 3)
        assert updates == [{"volume": 50}, {"play_state": "playing"}, {"metadata_start": True}]
    finally:
        await reader.stop()


async def test_reader_stays_idle_after_writer_close(pipe_path: str) -> None:
    """A FIFO with no writers left must not keep the event loop busy."""
    updates: list[dict[str, Any]] = []
    reader = MetadataReader(pipe_path, logging.getLogger("test"), updates.append)
    await reader.start()
    try:
        await _write(pipe_path, _item("ssnc", "abeg"))
        # The marker arriving proves the reader consumed it and the writer is gone.
        await _wait_for(lambda: bool(updates))
        cpu_before = time.process_time()
        await asyncio.sleep(0.5)
        # Only epoll reports a writerless FIFO readable forever, so this guard is
        # meaningful on Linux and passes trivially on macOS/kqueue.
        assert time.process_time() - cpu_before < 0.1
    finally:
        await reader.stop()


async def test_reader_keeps_pipe_attached_across_restart(
    pipe_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected read error restarts the loop without ever detaching from the FIFO."""
    updates: list[dict[str, Any]] = []
    reader = MetadataReader(pipe_path, logging.getLogger("test"), updates.append)
    real_read = os.read
    fail_once = True

    def flaky_read(fd: int, size: int) -> bytes:
        nonlocal fail_once
        if fd == reader._fd and fail_once:
            fail_once = False
            raise ValueError("unexpected error")
        return real_read(fd, size)

    monkeypatch.setattr(os, "read", flaky_read)
    await reader.start()
    try:
        await _write(pipe_path, _item("ssnc", "abeg"))
        await _wait_for(lambda: not fail_once)
        # Well inside the restart backoff: shairport-sync must still find a reader here.
        await asyncio.sleep(0.2)
        os.close(os.open(pipe_path, os.O_WRONLY | os.O_NONBLOCK))
        await _write(pipe_path, _item("ssnc", "aend"))
        await _wait_for(lambda: updates[-1:] == [{"play_state": "stopped"}], timeout=5.0)
    finally:
        await reader.stop()
