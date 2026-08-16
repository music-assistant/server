"""Helpers for the WiiM/LinkPlay provider."""

from __future__ import annotations

import re

from wiim.consts import MANUFACTURER_AUDIO_PRO, MANUFACTURER_WIIM

from .constants import PLAYER_ID_PREFIX

# Manufacturers handled by the official WiiM/Linkplay SDK. Everything else that
# still speaks the LinkPlay API (e.g. Edifier) is driven by the generic backend.
OFFICIAL_MANUFACTURERS = (MANUFACTURER_WIIM, MANUFACTURER_AUDIO_PRO)

_HEX = re.compile(r"^[0-9a-fA-F]+$")


def is_official_manufacturer(manufacturer: str | None) -> bool:
    """
    Return whether a UPnP manufacturer belongs to the official WiiM/Audio Pro backend.

    :param manufacturer: The manufacturer string from the device's UPnP description.
    """
    if not manufacturer:
        return False
    manufacturer = manufacturer.lower()
    return any(official.lower() in manufacturer for official in OFFICIAL_MANUFACTURERS)


def linkplay_slave_uuid_to_udn(slave_uuid: str) -> str | None:
    """
    Convert a LinkPlay slave-list UUID to its canonical UPnP UDN.

    Accepts both forms a slave list can report: the 24-character HTTP UUID (from
    which LinkPlay derives the UDN by appending the UUID's first 8 characters) and
    an already-full 32-character UPnP UDN (plain, dashed, or ``uuid:``-prefixed).
    Returns ``None`` when the input is not one of those hex forms.

    :param slave_uuid: The UUID of a slave device as reported in the slave list.
    """
    if not slave_uuid:
        return None
    hex_str = slave_uuid.strip().removeprefix("uuid:").replace("-", "")
    if not _HEX.match(hex_str):
        return None
    if len(hex_str) == 24:
        full = hex_str + hex_str[:8]
    elif len(hex_str) == 32:
        full = hex_str
    else:
        return None
    full = full.upper()
    formatted = f"{full[0:8]}-{full[8:12]}-{full[12:16]}-{full[16:20]}-{full[20:32]}"
    return f"uuid:{formatted}"


def linkplay_slave_uuid_to_player_id(slave_uuid: str) -> str | None:
    """
    Convert a LinkPlay slave-list UUID to a Music Assistant player id.

    :param slave_uuid: The UUID of a slave device as reported in the slave list.
    """
    if (udn := linkplay_slave_uuid_to_udn(slave_uuid)) is None:
        return None
    return f"{PLAYER_ID_PREFIX}{udn}"
