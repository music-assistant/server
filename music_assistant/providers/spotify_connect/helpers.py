"""Helpers/utils for the Spotify Connect provider."""

from __future__ import annotations

import hashlib
import shutil

import ifaddr

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


def generate_device_id(instance_id: str) -> str:
    """
    Derive a stable Spotify device id (40 hex chars) from the provider instance id.

    Passing a fixed ``device_id`` to go-librespot keeps the Spotify Connect device
    identity stable across daemon restarts, so the Spotify app keeps recognising it
    as the same speaker instead of spawning a fresh device each time.

    :param instance_id: The provider instance id.
    """
    return hashlib.sha256(instance_id.encode()).hexdigest()[:40]


def interface_name_for_ip(ip: str) -> str | None:
    """
    Return the name of the network interface that holds the given IP, or None.

    go-librespot selects which interfaces to advertise the Spotify Connect device
    on by interface name, so callers map the streams server's bind IP to its
    interface to keep the advertisement on the right network.

    :param ip: The IPv4/IPv6 address to look up.
    """
    for adapter in ifaddr.get_adapters():
        for ip_config in adapter.ips:
            addr = ip_config.ip if isinstance(ip_config.ip, str) else ip_config.ip[0]
            if addr == ip:
                return adapter.name
    return None
