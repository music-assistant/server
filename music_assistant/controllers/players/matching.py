"""
Identifier matching: decides whether two sides stand for the same physical device.

The matcher is a pure function over what each side reports about its device. It knows
nothing about players, providers or config, and every veto and precedence rule lives in
the one table below.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import Enum, auto
from typing import NamedTuple

from music_assistant_models.enums import IdentifierType

from music_assistant.helpers.util import (
    is_locally_administered_mac,
    is_valid_mac_address,
    normalize_mac_for_matching,
)

type Identifier = tuple[IdentifierType, str]


@dataclass(frozen=True, slots=True, kw_only=True)
class DeviceIdentity:
    """
    What one side of a match reports about its device.

    :param identifiers: Every identifier reported, as (type, value) pairs. A device with
        two MAC addresses or several UUIDs holds one pair per value.
    :param domains: The protocol domains that reported the identifiers.
    :param native: Whether the native protocol of the device is among them.
    """

    identifiers: frozenset[Identifier]
    domains: frozenset[str]
    native: bool


def identifiers_match(a: DeviceIdentity, b: DeviceIdentity) -> bool:
    """
    Return whether two identities stand for the same physical device.

    A differing strong identifier rules a match out first. After that any shared
    identifier is a match. A shared IP address alone is the last resort, and never ties
    two native devices that each have a hardware MAC address, as one host can carry
    several of them.

    :param a: What one side reports about its device.
    :param b: What the other side reports about its device.
    """
    values_a = _comparable_values(a)
    values_b = _comparable_values(b)
    share_domain = not a.domains.isdisjoint(b.domains)
    for identifier_type, rule in _RULES.items():
        vetoes = rule.veto == _Veto.ANY_DOMAIN or (rule.veto == _Veto.SAME_DOMAIN and share_domain)
        if vetoes and _differ(values_a[identifier_type], values_b[identifier_type]):
            return False
    for identifier_type, rule in _RULES.items():
        if rule.identifies and values_a[identifier_type] & values_b[identifier_type]:
            return True
    if not values_a[IdentifierType.IP_ADDRESS] & values_b[IdentifierType.IP_ADDRESS]:
        return False
    return not (a.native and b.native and _has_hardware_mac(a) and _has_hardware_mac(b))


class _Veto(Enum):
    """When differing values of one identifier type rule out a match."""

    NEVER = auto()
    SAME_DOMAIN = auto()  # only between sides that share a protocol domain
    ANY_DOMAIN = auto()


class _Rule(NamedTuple):
    """How one identifier type takes part in matching."""

    veto: _Veto
    identifies: bool  # a shared value is a match on its own


# In order of reliability. Every veto is checked before any shared value may match.
# A UUID or serial number only vetoes within one domain: a Chromecast reports its cast uuid
# as UUID, DLNA the UDN, MusicCast and Bose their device id, so one device has several.
# An AirPlay or Cast id is unique per receiver, whichever protocol reports it.
# A MAC address never vetoes: a device has one per network interface, and some protocols
# report a variant of it.
# An IP address is no row: it ties two sides only as the last resort, see identifiers_match.
_RULES: dict[IdentifierType, _Rule] = {
    IdentifierType.MAC_ADDRESS: _Rule(_Veto.NEVER, identifies=True),
    IdentifierType.SERIAL_NUMBER: _Rule(_Veto.SAME_DOMAIN, identifies=True),
    IdentifierType.UUID: _Rule(_Veto.SAME_DOMAIN, identifies=True),
    IdentifierType.CAST_UUID: _Rule(_Veto.ANY_DOMAIN, identifies=True),
    IdentifierType.AIRPLAY_ID: _Rule(_Veto.ANY_DOMAIN, identifies=True),
}


def _comparable_values(identity: DeviceIdentity) -> defaultdict[IdentifierType, set[str]]:
    """Return the values an identity holds per identifier type, as they are compared."""
    values: defaultdict[IdentifierType, set[str]] = defaultdict(set)
    for identifier_type, value in identity.identifiers:
        if identifier_type == IdentifierType.MAC_ADDRESS:
            if not is_valid_mac_address(value):
                # a null or broadcast MAC address identifies nothing
                continue
            # a protocol may report the hardware MAC address with the
            # locally-administered bit set
            values[identifier_type].add(normalize_mac_for_matching(value))
        elif identifier_type == IdentifierType.IP_ADDRESS:
            values[identifier_type].add(value)
        else:
            values[identifier_type].add(value.lower().replace(":", "").replace("-", ""))
    return values


def _differ(values_a: set[str], values_b: set[str]) -> bool:
    """Return whether both sides hold values of one identifier type and share none."""
    return bool(values_a) and bool(values_b) and values_a.isdisjoint(values_b)


def _has_hardware_mac(identity: DeviceIdentity) -> bool:
    """Return whether an identity holds a usable, globally unique MAC address."""
    return any(
        identifier_type == IdentifierType.MAC_ADDRESS
        and is_valid_mac_address(value)
        and not is_locally_administered_mac(value)
        for identifier_type, value in identity.identifiers
    )
