"""Helpers for the WiiM/LinkPlay provider."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from wiim.consts import MANUFACTURER_AUDIO_PRO, MANUFACTURER_WIIM

from .constants import PLAYER_ID_PREFIX

if TYPE_CHECKING:
    from pywiim.models import DeviceInfo as PywiimDeviceInfo

    from music_assistant.models.player import Player

# Manufacturers handled by the official WiiM/Linkplay SDK. Everything else that
# still speaks the LinkPlay API (e.g. Edifier) is driven by the generic backend.
OFFICIAL_MANUFACTURERS = (MANUFACTURER_WIIM, MANUFACTURER_AUDIO_PRO)

_HEX = re.compile(r"^[0-9a-fA-F]+$")


def is_in_mixed_group(player: Player) -> bool:
    """
    Return whether a player is in an externally-created cross-backend (mixed) group.

    A group is mixed when this player leads a group that includes a member on another
    backend, or another registered player on a different backend currently lists this
    player as a member. Such groups are read-only until cross-backend grouping is
    supported, so grouping is withdrawn while it holds. The check reads only the players
    already registered by the provider, so it needs no extra device requests.

    :param player: The player to check, expected to carry a ``linkplay_backend`` marker.
    """
    own_backend = getattr(player, "linkplay_backend", None)
    own_members = player._attr_group_members
    if own_members and own_members[0] == player.player_id:
        for member_id in own_members[1:]:
            member = player.mass.players.get_player(member_id)
            if member is not None and getattr(member, "linkplay_backend", None) != own_backend:
                return True
    for other in player.provider.players:
        if other is player or getattr(other, "linkplay_backend", None) == own_backend:
            continue
        other_members = other._attr_group_members
        if (
            other_members
            and other_members[0] == other.player_id
            and player.player_id in other_members[1:]
        ):
            return True
    return False


def linkplay_group_compatible(
    first: PywiimDeviceInfo | None, second: PywiimDeviceInfo | None
) -> bool:
    """
    Return whether two generic LinkPlay devices can share a router-based multiroom group.

    Grouping is only allowed between devices that both use modern router-based multiroom
    and belong to the same WiiM multiroom (WMRM) major generation. Legacy Wi-Fi Direct
    devices are rejected because MA does not move a follower onto the master's private
    network. Unknown WMRM generations are treated leniently once router-based is confirmed.

    :param first: The cached device info of one device, if known.
    :param second: The cached device info of the other device, if known.
    """
    if first is None or second is None:
        return False
    if getattr(first, "needs_wifi_direct_multiroom", False) or getattr(
        second, "needs_wifi_direct_multiroom", False
    ):
        return False
    first_major = _wmrm_major(first)
    second_major = _wmrm_major(second)
    return not (
        first_major is not None and second_major is not None and first_major != second_major
    )


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


def _wmrm_major(device_info: PywiimDeviceInfo) -> int | None:
    """Return the WiiM multiroom (WMRM) major generation, or None when unknown."""
    version = getattr(device_info, "wmrm_version", None)
    if not version:
        return None
    try:
        return int(str(version).split(".", 1)[0])
    except ValueError:
        return None
