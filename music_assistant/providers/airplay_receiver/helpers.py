"""Helpers/utils for the AirPlay Receiver plugin."""

from __future__ import annotations

import re
import shutil

from music_assistant.helpers.process import check_output

# the first release that hands its mDNS service over through the metadata pipe
MIN_SHAIRPORT_SYNC_VERSION = (5, 6, 3)


async def get_shairport_sync_binary() -> str:
    """
    Return the path of the installed shairport-sync binary.

    :raises RuntimeError: When shairport-sync is not installed, too old, built for AirPlay 2,
        or built without the pipe backend or metadata support.
    """
    min_version = ".".join(str(part) for part in MIN_SHAIRPORT_SYNC_VERSION)
    if not (binary := shutil.which("shairport-sync")):
        msg = f"shairport-sync is not installed: the AirPlay Receiver needs version {min_version}+."
        raise RuntimeError(msg)
    try:
        returncode, output = await check_output(binary, "--version")
    except OSError as err:
        raise RuntimeError(f"Unable to run shairport-sync: {err}") from err
    version = output.decode(errors="ignore").strip()
    if returncode != 0 or not _is_supported_version(version):
        msg = (
            f"shairport-sync {version} can not be used: the AirPlay Receiver needs a classic "
            f"AirPlay build (not AirPlay 2) of version {min_version}+, with the pipe backend "
            "and metadata support."
        )
        raise RuntimeError(msg)
    return binary


def _is_supported_version(version: str) -> bool:
    """
    Return whether a shairport-sync build can run the receivers.

    :param version: The version string the binary prints, e.g.
        "5.6.3-FFmpeg-OpenSSL-stdout-pipe-metadata-sysconfdir:/etc".
    """
    if not (match := re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", version)):
        return False
    release = tuple(int(part or 0) for part in match.groups())
    # the receivers need the audio pipe backend and metadata; an AirPlay 2 build pipes
    # 48 kHz 32-bit audio and may advertise a second service
    capabilities = set(version.split("-"))
    return (
        release >= MIN_SHAIRPORT_SYNC_VERSION
        and {"pipe", "metadata"} <= capabilities
        and "AirPlay2" not in capabilities
    )
