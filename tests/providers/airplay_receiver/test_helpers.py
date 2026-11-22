"""Tests for finding a shairport-sync binary the AirPlay Receiver can run."""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, patch

import pytest

from music_assistant.providers.airplay_receiver.helpers import get_shairport_sync_binary

_HELPERS = "music_assistant.providers.airplay_receiver.helpers"
_BINARY = "/usr/local/bin/shairport-sync"


@pytest.mark.parametrize(
    "version",
    [
        pytest.param("5.6.3-FFmpeg-OpenSSL-stdout-pipe-metadata-sysconfdir:/etc", id="base-image"),
        pytest.param(
            "5.6.3-FFmpeg-libdaemon-OpenSSL-dns_sd-ao-PulseAudio-stdout-pipe-soxr-metadata"
            "-sysconfdir:/opt/homebrew/etc/shairport-sync",
            id="homebrew",
        ),
        pytest.param("5.7-dev-12-gabcdef0-FFmpeg-OpenSSL-pipe-metadata", id="development"),
    ],
)
async def test_a_recent_build_with_metadata_support_is_used(version: str) -> None:
    """A build that can hand its mDNS service over as metadata is used."""
    with (
        patch(f"{_HELPERS}.shutil.which", return_value=_BINARY),
        patch(f"{_HELPERS}.check_output", AsyncMock(return_value=(0, f"{version}\n".encode()))),
    ):
        assert await get_shairport_sync_binary() == _BINARY


@pytest.mark.parametrize(
    "version",
    [
        pytest.param("4.3.7-OpenSSL-tinysvcmdns-stdout-pipe-metadata-sysconfdir:/etc", id="old"),
        pytest.param("5.6.3-FFmpeg-OpenSSL-stdout-pipe-sysconfdir:/etc", id="no-metadata"),
        # PipeWire is an output backend of its own, not the audio pipe
        pytest.param("5.6.3-FFmpeg-OpenSSL-ALSA-PipeWire-metadata-sysconfdir:/etc", id="no-pipe"),
        # an AirPlay 2 build pipes 48 kHz 32-bit audio
        pytest.param(
            "5.6.3-AirPlay2-smi11-OpenSSL-Avahi-ALSA-stdout-pipe-soxr-metadata-sysconfdir:/etc",
            id="airplay2",
        ),
        # a build from a git checkout without tags reports its commit instead of a version
        pytest.param("7ce90d8-FFmpeg-OpenSSL-stdout-pipe-metadata-sysconfdir:/etc", id="commit"),
    ],
)
async def test_an_unsupported_build_is_refused_with_its_version(version: str) -> None:
    """A build that can not hand its mDNS service over is refused, naming what was found."""
    with (
        patch(f"{_HELPERS}.shutil.which", return_value=_BINARY),
        patch(f"{_HELPERS}.check_output", AsyncMock(return_value=(0, f"{version}\n".encode()))),
        pytest.raises(RuntimeError, match=re.escape(version)),
    ):
        await get_shairport_sync_binary()


async def test_a_missing_binary_is_refused() -> None:
    """Without shairport-sync on the PATH the receiver can not start."""
    with (
        patch(f"{_HELPERS}.shutil.which", return_value=None),
        pytest.raises(RuntimeError, match="not installed"),
    ):
        await get_shairport_sync_binary()
