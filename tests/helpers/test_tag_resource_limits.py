"""Resource limits for audio metadata inspection."""

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock
from unittest.mock import MagicMock

import pytest
from music_assistant_models.errors import InvalidDataError

from music_assistant.helpers import tags


def _id3_header(size: int, version: int = 3) -> bytes:
    return (
        b"ID3" + bytes((version, 0, 0)) + bytes((size >> shift) & 0x7F for shift in (21, 14, 7, 0))
    )


@pytest.mark.parametrize("version", [2, 3, 4])
def test_oversized_id3_rejected_before_parsers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    """A huge declared tag must not reach either memory-intensive parser."""
    source = tmp_path / "oversized.mp3"
    source.write_bytes(_id3_header(161481653, version))
    probe = MagicMock()
    mutagen = MagicMock()
    monkeypatch.setattr(subprocess, "check_output", probe)
    monkeypatch.setattr(tags, "parse_tags_mutagen", mutagen)

    with pytest.raises(InvalidDataError, match="ID3 metadata is too large"):
        tags.parse_tags(str(source))

    probe.assert_not_called()
    mutagen.assert_not_called()


def test_oversized_id3_closes_opener_descriptor(tmp_path: Path) -> None:
    """Rejected metadata still closes a provider's confined file descriptor."""
    source = tmp_path / "oversized.mp3"
    source.write_bytes(_id3_header(tags.MAX_ID3_TAG_SIZE + 1))
    descriptor = os.open(source, os.O_RDONLY)

    with pytest.raises(InvalidDataError, match="ID3 metadata is too large"):
        tags.parse_tags("provider-file.mp3", opener=lambda _path, _flags: descriptor)

    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(descriptor)


@pytest.mark.parametrize("header", [b"", b"RIFF123456", _id3_header(16 * 1024 * 1024)])
def test_small_or_other_headers_continue_to_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, header: bytes
) -> None:
    """The header guard leaves ordinary validation to the audio parser."""
    source = tmp_path / "audio"
    source.write_bytes(header)
    probe = MagicMock(return_value=b'{"streams": []}')
    monkeypatch.setattr(subprocess, "check_output", probe)

    with pytest.raises(InvalidDataError, match="Not an audio file"):
        tags.parse_tags(str(source))

    probe.assert_called_once()


def test_invalid_id3_size_rejected(tmp_path: Path) -> None:
    """Invalid synchsafe size bytes must not bypass the size guard."""
    source = tmp_path / "invalid.mp3"
    source.write_bytes(b"ID3\x03\x00\x00\xff\x00\x00\x00")
    with pytest.raises(InvalidDataError, match="Invalid ID3 tag size"):
        tags.parse_tags(str(source))


def test_probe_timeout_is_a_file_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stalled probe becomes a recoverable error and releases its worker slot."""
    probe = MagicMock(side_effect=subprocess.TimeoutExpired("ffprobe", tags.FFPROBE_TIMEOUT))
    monkeypatch.setattr(subprocess, "check_output", probe)
    for _ in range(3):
        with pytest.raises(InvalidDataError, match="audio inspection timed out"):
            tags.parse_tags("https://example.invalid/audio.mp3")
    assert probe.call_args.kwargs["timeout"] == tags.FFPROBE_TIMEOUT
    args = probe.call_args.args[0]
    assert args[args.index("-max_alloc") + 1] == str(tags.MAX_ID3_TAG_SIZE)
    assert args[args.index("-threads") + 1] == "1"


def test_probe_concurrency_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Independent callers cannot run more than two probes at once."""
    lock = Lock()
    two_started = Event()
    release = Event()
    active = peak = 0

    def probe(*_args: object, **_kwargs: object) -> bytes:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 2:
                two_started.set()
        try:
            assert release.wait(timeout=5)
            return b'{"streams": []}'
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(subprocess, "check_output", probe)
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [
            executor.submit(tags.parse_tags, "https://example.invalid/a.mp3") for _ in range(6)
        ]
        try:
            assert two_started.wait(timeout=5)
            assert peak == 2
        finally:
            release.set()
        for future in futures:
            with pytest.raises(InvalidDataError, match="Not an audio file"):
                future.result(timeout=5)
    assert peak == 2
