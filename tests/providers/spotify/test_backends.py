"""
Tests for the Spotify provider's playback backend selection and wiring.

The playback backend is an explicit per-instance choice stored in setup_data:
configs predating the choice (key unset) must stay on librespot, "soloist"
selects the single-track Soloist backend. The per-backend concurrency budget
and librespot's URI translation are locked down here as well.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self
from unittest.mock import MagicMock

from music_assistant.providers.spotify.backends.librespot import LibrespotBackend
from music_assistant.providers.spotify.backends.soloist import SoloistSingleTrackBackend
from music_assistant.providers.spotify.constants import (
    BACKEND_LIBRESPOT,
    BACKEND_SOLOIST,
    CONF_PLAYBACK_BACKEND,
)
from music_assistant.providers.spotify.provider import SpotifyProvider

if TYPE_CHECKING:
    import asyncio
    from collections.abc import AsyncGenerator
    from pathlib import Path

    import pytest


def test_realtime_declaration_follows_the_backend() -> None:
    """Soloist declares realtime delivery; librespot can read ahead."""
    librespot = LibrespotBackend(_make_provider({}))
    soloist = SoloistSingleTrackBackend(_make_provider({CONF_PLAYBACK_BACKEND: "soloist"}))
    assert librespot.is_realtime is False
    assert soloist.is_realtime is True


def test_backend_defaults_to_librespot() -> None:
    """A config without a stored backend choice (pre-split install) stays on librespot."""
    prov = _make_provider({})
    assert isinstance(prov._create_backend(), LibrespotBackend)


def test_backend_soloist_is_selected() -> None:
    """A stored soloist choice selects the Soloist single-track backend."""
    prov = _make_provider({CONF_PLAYBACK_BACKEND: BACKEND_SOLOIST})
    assert isinstance(prov._create_backend(), SoloistSingleTrackBackend)


def test_max_concurrent_streams_follows_the_backend() -> None:
    """Librespot (or an unset choice) allows two parallel fetches, soloist only one."""
    assert _make_provider({}).max_concurrent_streams == 2
    assert _make_provider({CONF_PLAYBACK_BACKEND: BACKEND_LIBRESPOT}).max_concurrent_streams == 2
    assert _make_provider({CONF_PLAYBACK_BACKEND: BACKEND_SOLOIST}).max_concurrent_streams == 1


async def test_librespot_receives_the_translated_uri(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The canonical spotify:track: URI is translated to librespot's spotify:// scheme."""
    backend = _make_librespot_backend(tmp_path)
    captured = _install_fake_librespot_process(monkeypatch)
    chunks = [chunk async for chunk in backend.stream_spotify_uri("spotify:track:xyz", 0)]
    assert chunks == [b"ogg"]
    args = captured[0]
    assert args[args.index("--single-track") + 1] == "spotify://track:xyz"
    assert "--start-position" not in args


async def test_librespot_seek_adds_start_position(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A nonzero seek position is passed to librespot as --start-position."""
    backend = _make_librespot_backend(tmp_path)
    captured = _install_fake_librespot_process(monkeypatch)
    async for _chunk in backend.stream_spotify_uri("spotify:track:xyz", 42):
        pass
    args = captured[0]
    assert args[args.index("--start-position") + 1] == "42"


def _make_provider(setup_data: dict[str, Any]) -> SpotifyProvider:
    """Return a SpotifyProvider (bypassing __init__) with the given setup_data."""
    prov = object.__new__(SpotifyProvider)
    config = MagicMock(instance_id="spotify--test")
    config.get_value = MagicMock(return_value=None)
    config.values = {}
    prov.config = config
    prov.manifest = MagicMock(domain="spotify")
    prov.logger = MagicMock()
    prov.available = True
    mass = MagicMock()
    # get_setup_value reads the live setup_data blob from the store
    mass.config.get = MagicMock(return_value=setup_data)
    mass.config.get_raw_provider_config_value = MagicMock(return_value=None)
    # the store keeps values encrypted; decrypt is an identity map for the test
    mass.config.decrypt_string = MagicMock(side_effect=lambda value: value)
    prov.mass = mass
    return prov


def _make_librespot_backend(tmp_path: Path) -> LibrespotBackend:
    """Return a stream-ready LibrespotBackend with a stubbed binary and cache dir."""
    prov = _make_provider({})
    prov.cache_dir = str(tmp_path / "cache")
    backend = LibrespotBackend(prov)
    backend._librespot_bin = "/bin/librespot"
    return backend


def _install_fake_librespot_process(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Replace AsyncProcess in the librespot backend, returning the captured argv lists."""
    captured: list[list[str]] = []

    class _FakeProcess:
        """AsyncProcess stand-in yielding one ogg chunk and exiting cleanly."""

        def __init__(self, args: list[str], **_kwargs: Any) -> None:
            captured.append(args)
            self.returncode = 0
            self.proc = None
            self._stderr_task: asyncio.Task[None] | None = None

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_exc_info: object) -> None:
            # consume the attached stderr reader so no pending task leaks a warning
            if self._stderr_task is not None:
                await self._stderr_task

        def attach_stderr_reader(self, task: asyncio.Task[None]) -> None:
            self._stderr_task = task

        async def iter_stderr(self) -> AsyncGenerator[str]:
            lines: tuple[str, ...] = ()
            for line in lines:
                yield line

        async def iter_chunked(self, _n: int = 64000) -> AsyncGenerator[bytes]:
            yield b"ogg"

    monkeypatch.setattr(
        "music_assistant.providers.spotify.backends.librespot.AsyncProcess", _FakeProcess
    )
    return captured
