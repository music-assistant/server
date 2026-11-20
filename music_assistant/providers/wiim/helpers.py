"""Helpers for the WiiM/LinkPlay provider."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ChannelLayout
from pywiim.model_names import is_known_wiim_model
from wiim.consts import MANUFACTURER_AUDIO_PRO, MANUFACTURER_WIIM

from .constants import PLAYER_ID_PREFIX

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pywiim.models import DeviceInfo as PywiimDeviceInfo

# Manufacturers handled by the official WiiM/Linkplay SDK. Everything else that
# still speaks the LinkPlay API (e.g. Edifier) is driven by the generic backend.
OFFICIAL_MANUFACTURERS = (MANUFACTURER_WIIM, MANUFACTURER_AUDIO_PRO)

_HEX = re.compile(r"^[0-9a-fA-F]+$")

# The channel a speaker plays in a native stereo pair (getSlaveList channel, group_channel).
SET_CHANNEL_LEFT = 1
SET_CHANNEL_RIGHT = 2


def linkplay_group_compatible(
    first: PywiimDeviceInfo | None, second: PywiimDeviceInfo | None
) -> bool:
    """
    Return whether two generic LinkPlay devices can share a router-based multiroom group.

    Grouping is only allowed between devices that both use modern router-based multiroom
    and belong to the same, known WiiM multiroom (WMRM) major generation. Legacy Wi-Fi
    Direct devices are rejected because MA does not move a follower onto the master's
    private network, and a device whose generation cannot be determined is not grouped.

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
    return first_major is not None and first_major == second_major


def native_set_channel(slave: dict[str, Any]) -> int | None:
    """
    Return the channel of a slave in its leader's native set, None when the entry shows none.

    A native set is a stereo pair (or another multichannel set) made in the WiiM Home app. Its
    leader reports it this way only while the set plays alone, not while it is part of a
    larger multiroom group.

    :param slave: One entry of the slave list a leader reports.
    """
    channel = _int_field(slave, "group_channel")
    return channel if channel > 0 else None


def plays_set_channel(slave: dict[str, Any]) -> bool:
    """
    Return whether a slave plays the left or the right channel.

    :param slave: One entry of the slave list a leader reports.
    """
    return _int_field(slave, "channel") in (SET_CHANNEL_LEFT, SET_CHANNEL_RIGHT)


def native_set_layout(channels: list[int]) -> ChannelLayout | None:
    """
    Return the channel layout of the native set a leader leads, None when it leads none.

    :param channels: The set channels of the leader's slaves that belong to its set.
    """
    if not channels:
        return None
    if channels in ([SET_CHANNEL_LEFT], [SET_CHANNEL_RIGHT]):
        return ChannelLayout.STEREO
    return ChannelLayout.UNKNOWN


def is_official_manufacturer(manufacturer: str | None) -> bool:
    """
    Return whether a UPnP manufacturer belongs to the official WiiM/Audio Pro backend.

    :param manufacturer: The manufacturer string from the device's UPnP description.
    """
    if not manufacturer:
        return False
    manufacturer = manufacturer.lower()
    return any(official.lower() in manufacturer for official in OFFICIAL_MANUFACTURERS)


def is_official_device(manufacturer: str | None, model: str | None) -> bool:
    """
    Return whether a discovered device should be driven by the official WiiM/Audio Pro SDK.

    :param manufacturer: The manufacturer string from the device's UPnP description.
    :param model: The model name from the device's UPnP description.
    """
    if not manufacturer or not is_official_manufacturer(manufacturer):
        return False
    if MANUFACTURER_AUDIO_PRO.lower() in manufacturer.lower():
        return True
    # Generic LinkPlay OEM devices advertise the same Linkplay manufacturer as WiiM
    # products, so the model name is needed to tell them apart.
    return is_wiim_model(model)


def is_wiim_model(model: str | None) -> bool:
    """
    Return whether a UPnP model name identifies a WiiM product.

    :param model: The model name from the device's UPnP description.
    """
    if not model:
        return False
    # pywiim covers the raw firmware aliases such as "Muzo_Mini", the prefix check
    # covers the marketing names such as "WiiM Pro" and any future model.
    return is_known_wiim_model(model) or model.strip().lower().startswith("wiim")


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


def match_slave_uuid_to_player_id(
    slave_uuid: str | None, candidate_player_ids: Iterable[str]
) -> str | None:
    """
    Resolve a slave-list UUID to one of the given registered player ids.

    Both backends key their players on the UPnP UDN, so a slave reported in either the
    24-char HTTP or full 32-hex form is matched against the candidate ids by their
    normalized hex, spanning the official and generic backends.

    :param slave_uuid: The UUID of a slave device as reported in the slave list.
    :param candidate_player_ids: The player ids to match the slave against.
    """
    if not slave_uuid or (udn := linkplay_slave_uuid_to_udn(slave_uuid)) is None:
        return None
    target_hex = udn.removeprefix("uuid:").replace("-", "").upper()
    for player_id in candidate_player_ids:
        if not player_id.startswith(PLAYER_ID_PREFIX):
            continue
        candidate_hex = (
            player_id[len(PLAYER_ID_PREFIX) :].removeprefix("uuid:").replace("-", "").upper()
        )
        if candidate_hex == target_hex:
            return player_id
    return None


def _int_field(slave: dict[str, Any], key: str) -> int:
    """Return an integer field of a slave list entry, 0 when it is missing or no number."""
    try:
        return int(slave.get(key, 0))
    except TypeError, ValueError:
        return 0


def _wmrm_major(device_info: PywiimDeviceInfo) -> int | None:
    """Return the WiiM multiroom (WMRM) major generation, or None when unknown."""
    version = getattr(device_info, "wmrm_version", None)
    if not version:
        return None
    try:
        return int(str(version).split(".", 1)[0])
    except ValueError:
        return None
