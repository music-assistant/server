"""Helpers/utils for the AriaCast Receiver plugin."""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


def get_local_ip() -> str:
    """Get the local IP address of this machine."""
    try:
        # Create a socket to determine the local IP
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            # Connect to a remote address (doesn't actually send data)
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
