"""Helpers for Sendspin provider."""

from __future__ import annotations

from music_assistant.providers.sendspin.constants import BRIDGE_PREFIX


def bridge_client_id_from_mac(mac: str) -> str:
    """Generate a Sendspin bridge client ID from a MAC address."""
    return f"{BRIDGE_PREFIX}{mac.replace(':', '').lower()}"


def mac_from_bridge_client_id(client_id: str) -> str | None:
    """Extract a MAC address from a Sendspin bridge client ID."""
    if not client_id.startswith(BRIDGE_PREFIX):
        return None
    mac_part = client_id[len(BRIDGE_PREFIX) :]
    if len(mac_part) != 12:
        return None
    # Reconstruct MAC address with colons
    return ":".join(mac_part[i : i + 2] for i in range(0, 12, 2))
