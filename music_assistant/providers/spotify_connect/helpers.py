"""Helpers/utils for the Spotify Connect provider."""

from __future__ import annotations

import hashlib
import shutil

GO_LIBRESPOT_BINARY = "go-librespot"


def get_go_librespot_binary() -> str:
    """
    Locate the go-librespot binary on the system PATH.

    In the official Docker image and Home Assistant add-on the binary is installed
    automatically; manual installs need it on PATH (e.g. ``brew install go-librespot``
    on macOS, or a release from https://github.com/devgianlu/go-librespot/releases).

    :return: Absolute path to the go-librespot executable.
    :raises RuntimeError: When the binary cannot be found on PATH.
    """
    if binary := shutil.which(GO_LIBRESPOT_BINARY):
        return binary
    msg = (
        "go-librespot binary not found on PATH. Install it (e.g. `brew install go-librespot` "
        "on macOS, or grab a release from https://github.com/devgianlu/go-librespot/releases) "
        "and make sure it is reachable on PATH."
    )
    raise RuntimeError(msg)


def generate_device_id(identity_key: str) -> str:
    """
    Derive a stable Spotify device id (40 hex chars) from a daemon's identity key.

    Passing a fixed ``device_id`` to go-librespot keeps the Spotify Connect device
    identity stable across daemon restarts, so the Spotify app keeps recognising it
    as the same speaker instead of spawning a fresh device each time.

    :param identity_key: The daemon's unique identity key.
    """
    return hashlib.sha256(identity_key.encode()).hexdigest()[:40]
