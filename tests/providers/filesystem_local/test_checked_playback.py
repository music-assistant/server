"""Tests that playing a filesystem provider file reads the very file it checked."""

import os
import subprocess
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import MultiPartPath, StreamDetails

from music_assistant.controllers.streams.audio import StreamsAudio
from music_assistant.controllers.streams.audio_buffer import _probe_source_format
from music_assistant.providers.filesystem_local import LocalFileSystemProvider
from music_assistant.providers.filesystem_local.cue import CueSheetHandler
from music_assistant.providers.filesystem_local.helpers import open_real_path

INSTANCE_ID = "filesystem_local--test"
PCM_FORMAT = AudioFormat(
    content_type=ContentType.PCM_S16LE, sample_rate=44100, bit_depth=16, channels=2
)
FLAC_FORMAT = AudioFormat(
    content_type=ContentType.FLAC, sample_rate=44100, bit_depth=16, channels=2
)

type Repoint = Callable[[str], None]


def _make_flac(path: Path, source: str, rate: int, channels: int, seconds: int = 1) -> None:
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"{source}:d={seconds}",
            "-ar", str(rate), "-ac", str(channels), str(path),
        ],
        check=True,
    )  # fmt: skip


def _make_provider(base_path: Path) -> LocalFileSystemProvider:
    provider = LocalFileSystemProvider.__new__(LocalFileSystemProvider)
    provider.base_path = str(base_path)
    provider.logger = MagicMock()
    provider.config = MagicMock(instance_id=INSTANCE_ID)
    provider.manifest = MagicMock(domain="filesystem_local")
    provider._cue = CueSheetHandler(provider)
    return provider


def _make_audio(provider: LocalFileSystemProvider) -> StreamsAudio:
    mass = MagicMock()
    mass.get_provider.return_value = provider
    mass.loop.time = MagicMock(return_value=0.0)
    audio = StreamsAudio(mass)
    mass.streams.audio = audio
    return audio


def _local_stream(path: str | list[MultiPartPath], **overrides: Any) -> StreamDetails:
    fields: dict[str, Any] = {
        "provider": INSTANCE_ID,
        "item_id": "track.flac",
        "audio_format": FLAC_FORMAT,
        "media_type": MediaType.TRACK,
        "stream_type": StreamType.LOCAL_FILE,
        "path": path,
        "duration": 1,
    }
    return StreamDetails(**(fields | overrides))


async def _collect(stream: AsyncGenerator[bytes]) -> bytes:
    return b"".join([chunk async for chunk in stream])


async def _play(audio: StreamsAudio, streamdetails: StreamDetails) -> bytes:
    return await _collect(audio._get_media_stream(streamdetails, PCM_FORMAT, 0, None, 1.0))


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """
    Create a folder of audible tracks reached through symlinks, and silent tracks outside it.

    Each symlink name has a silent outside counterpart for it to be re-pointed to.
    """
    base = tmp_path / "music"
    outside = tmp_path / "outside"
    base.mkdir()
    outside.mkdir()
    for name, seconds in (("track.flac", 1), ("part1.flac", 3), ("part2.flac", 3)):
        _make_flac(base / f"inside-{name}", "sine=f=440:r=44100", 44100, 2, seconds)
        _make_flac(outside / name, "anullsrc=r=8000:cl=mono", 8000, 1, seconds)
        (base / name).symlink_to(f"inside-{name}")
    return base


@pytest.fixture
def repoint_after_check(monkeypatch: pytest.MonkeyPatch, tree: Path) -> Repoint:
    """Return a function that arms a symlink to be re-pointed right after it passed the check."""
    armed: dict[str, Path] = {}

    def _open_then_repoint(real_base_path: str, path: str, flags: int = os.O_RDONLY) -> int:
        fd = open_real_path(real_base_path, path, flags)
        if (target := armed.pop(Path(path).name, None)) is not None:
            new_link = Path(path).with_name(f".{Path(path).name}.new")
            new_link.symlink_to(target)
            new_link.replace(path)
        return fd

    monkeypatch.setattr(
        "music_assistant.providers.filesystem_local.open_real_path", _open_then_repoint
    )

    def _arm(link_name: str) -> None:
        armed[link_name] = tree.parent / "outside" / link_name

    return _arm


async def test_playback_reads_the_checked_file(tree: Path, repoint_after_check: Repoint) -> None:
    """Playback decodes the file that passed the check: audible, not the silent one."""
    provider = _make_provider(tree)
    audio = _make_audio(provider)
    repoint_after_check("track.flac")

    pcm = await _play(audio, _local_stream(str(tree / "track.flac")))

    assert pcm.strip(b"\x00")


async def test_playback_refuses_a_file_leading_outside(tree: Path) -> None:
    """A stream path whose real location lies outside the folder is not played."""
    (tree / "elsewhere.flac").symlink_to(tree.parent / "outside" / "track.flac")
    audio = _make_audio(_make_provider(tree))

    with pytest.raises(MediaNotFoundError):
        await _play(audio, _local_stream(str(tree / "elsewhere.flac")))


async def test_playback_of_a_removed_file_reports_it_missing(tree: Path) -> None:
    """A file removed after it was queued is reported as missing media."""
    (tree / "inside-track.flac").unlink()
    audio = _make_audio(_make_provider(tree))

    with pytest.raises(MediaNotFoundError):
        await _play(audio, _local_stream(str(tree / "track.flac")))


async def test_multi_file_playback_reads_the_checked_parts(
    tree: Path, repoint_after_check: Repoint
) -> None:
    """Every part of a multi-file audiobook is the file that passed the check."""
    audio = _make_audio(_make_provider(tree))
    parts = [
        MultiPartPath(path=str(tree / name), duration=3) for name in ("part1.flac", "part2.flac")
    ]
    repoint_after_check("part1.flac")

    pcm = await _play(audio, _local_stream(parts, media_type=MediaType.AUDIOBOOK, duration=6))

    # the first part is the one that was re-pointed, so its first half second must be audible
    assert pcm[: PCM_FORMAT.pcm_sample_size // 2].strip(b"\x00")


async def test_cue_playback_reads_the_checked_file(
    tree: Path, repoint_after_check: Repoint
) -> None:
    """A track of a CUE sheet is cut from the audio file that passed the check."""
    provider = _make_provider(tree)
    streamdetails = StreamDetails(
        provider=INSTANCE_ID,
        item_id="album.cue#1",
        audio_format=PCM_FORMAT,
        media_type=MediaType.TRACK,
        stream_type=StreamType.CUSTOM,
        duration=1,
        data={
            "audio_relative_path": "track.flac",
            "start_seconds": 0,
            "original_format": FLAC_FORMAT.to_dict(),
        },
    )
    repoint_after_check("track.flac")

    pcm = await _collect(provider.get_audio_stream(streamdetails))

    assert pcm.strip(b"\x00")


async def test_overlay_reads_the_checked_file(tree: Path, repoint_after_check: Repoint) -> None:
    """A sound effect overlay is mixed from the file that passed the check."""
    provider = _make_provider(tree)
    provider.get_stream_details = AsyncMock(  # type: ignore[method-assign]
        return_value=_local_stream(str(tree / "track.flac"), media_type=MediaType.SOUND_EFFECT)
    )
    audio = _make_audio(provider)
    queue = SimpleNamespace(
        overlay_source=SimpleNamespace(provider=INSTANCE_ID, item_id="track.flac", uri="x")
    )
    repoint_after_check("track.flac")

    overlay = await audio._resolve_overlay_input(queue)  # type: ignore[arg-type]

    assert overlay is not None
    overlay_input, pass_fds = overlay
    try:
        assert Path(overlay_input).read_bytes() == (tree / "inside-track.flac").read_bytes()
    finally:
        for fd in pass_fds:
            os.close(fd)


async def test_format_probe_reads_the_checked_file(
    tree: Path, repoint_after_check: Repoint
) -> None:
    """The format detected for a source without one is that of the file that passed the check."""
    provider = _make_provider(tree)
    provider.acquire_stream_slot = MagicMock(  # type: ignore[method-assign]
        return_value=AsyncMock()
    )
    audio = _make_audio(provider)
    streamdetails = _local_stream(
        str(tree / "track.flac"), audio_format=AudioFormat(content_type=ContentType.UNKNOWN)
    )
    repoint_after_check("track.flac")

    await _probe_source_format(audio.mass, streamdetails, None)

    assert streamdetails.audio_format.sample_rate == 44100
    assert streamdetails.audio_format.channels == 2


async def test_opening_parts_stops_at_a_refused_one_without_leaking(tree: Path) -> None:
    """A refused part leaves no descriptor of the parts before it open."""
    (tree / "elsewhere.flac").symlink_to(tree.parent / "outside" / "track.flac")
    audio = _make_audio(_make_provider(tree))
    paths = [str(tree / "part1.flac"), str(tree / "elsewhere.flac")]
    open_before = len(list(Path("/proc/self/fd").iterdir()))

    with pytest.raises(MediaNotFoundError):
        await audio.open_local_files(_local_stream(paths[0]), paths)

    assert len(list(Path("/proc/self/fd").iterdir())) == open_before
