"""
Identifier matching: decides whether two sides stand for the same physical device.

The matcher is a pure function over what each side reports about its device. It knows
nothing about players, providers or config, and every veto and precedence rule lives in
the one table below. One side can be made up of several parts: what each protocol of a
player reported about the device.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, NamedTuple

from music_assistant_models.enums import IdentifierType

from music_assistant.constants import MAIN_ZONE
from music_assistant.helpers.util import (
    is_locally_administered_mac,
    is_valid_mac_address,
    normalize_mac_for_matching,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

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

    A differing strong identifier rules a match out first, and a side that reports no
    zone is in the main zone of its chassis. After that any shared identifier is a match.
    A shared IP address alone is the last resort, and never ties two native devices that
    each have a hardware MAC address, as one host can carry several of them.

    :param a: What one side reports about its device.
    :param b: What the other side reports about its device.
    """
    return matches_device(a, (b,))


def matches_device(candidate: DeviceIdentity, parts: Iterable[DeviceIdentity]) -> bool:
    """
    Return whether an identity stands for the device that several identities describe together.

    The rules of identifiers_match apply to the parts as one merged identity, so a
    strong identifier one part holds rules the candidate out whichever part shares
    another identifier with it. A domain veto only counts the values that parts of
    the candidate's own domains reported.

    :param candidate: What one protocol reports about its device.
    :param parts: What each protocol of a player reported about the device of that player.
    """
    parts = tuple(parts)
    values = _with_missing(_comparable_values(candidate))
    merged, same_domain = _merged_values(candidate, parts)
    if _vetoed(values, _with_missing(merged), same_domain):
        return False
    for identifier_type, rule in _RULES.items():
        if rule.identifies and values[identifier_type] & merged[identifier_type]:
            return True
    if not values[IdentifierType.IP_ADDRESS] & merged[IdentifierType.IP_ADDRESS]:
        return False
    return not (
        candidate.native
        and has_hardware_mac(candidate)
        and any(part.native for part in parts)
        and any(has_hardware_mac(part) for part in parts)
    )


def vetoes_device(candidate: DeviceIdentity, parts: Iterable[DeviceIdentity]) -> bool:
    """
    Return whether a differing strong identifier rules out that an identity is a device.

    Only what both sides report is compared: a protocol that reports no zone is not ruled
    out by the zone of the player it was attached to.

    :param candidate: What one protocol reports about its device.
    :param parts: What each protocol of a player reported about the device of that player.
    """
    merged, same_domain = _merged_values(candidate, tuple(parts))
    return _vetoed(_comparable_values(candidate), merged, same_domain)


def has_hardware_mac(identity: DeviceIdentity) -> bool:
    """Return whether an identity holds a usable, globally unique MAC address."""
    return any(
        identifier_type == IdentifierType.MAC_ADDRESS
        and is_valid_mac_address(value)
        and not is_locally_administered_mac(value)
        for identifier_type, value in identity.identifiers
    )


class _Veto(Enum):
    """When differing values of one identifier type rule out a match."""

    NEVER = auto()
    SAME_DOMAIN = auto()  # only between sides that share a protocol domain
    ANY_DOMAIN = auto()


class _Rule(NamedTuple):
    """How one identifier type takes part in matching."""

    veto: _Veto
    identifies: bool  # a shared value is a match on its own
    # what a side that reports none of the type counts as when a player is chosen for a
    # protocol, for a type that vetoes across domains
    missing: str | None = None


# In order of reliability. Every veto is checked before any shared value may match.
# A UUID or serial number only vetoes within one domain: a Chromecast reports its cast uuid
# as UUID, DLNA the UDN, MusicCast and Bose their device id, so one device has several.
# An AirPlay or Cast id is unique per receiver, whichever protocol reports it.
# A zone tells the outputs of one chassis apart, which share its MAC address and IP. It
# identifies nothing on its own: every chassis has a first zone. A side that reports no
# zone is in the main zone, where the protocols of the chassis without a zone play.
# A MAC address never vetoes: a device has one per network interface, and some protocols
# report a variant of it.
# An IP address is no row: it ties two sides only as the last resort, see identifiers_match.
_RULES: dict[IdentifierType, _Rule] = {
    IdentifierType.MAC_ADDRESS: _Rule(_Veto.NEVER, identifies=True),
    IdentifierType.SERIAL_NUMBER: _Rule(_Veto.SAME_DOMAIN, identifies=True),
    IdentifierType.UUID: _Rule(_Veto.SAME_DOMAIN, identifies=True),
    IdentifierType.CAST_UUID: _Rule(_Veto.ANY_DOMAIN, identifies=True),
    IdentifierType.AIRPLAY_ID: _Rule(_Veto.ANY_DOMAIN, identifies=True),
    IdentifierType.ZONE: _Rule(_Veto.ANY_DOMAIN, identifies=False, missing=MAIN_ZONE),
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
        elif identifier_type in (IdentifierType.IP_ADDRESS, IdentifierType.ZONE):
            values[identifier_type].add(value)
        else:
            values[identifier_type].add(value.lower().replace(":", "").replace("-", ""))
    return values


def _with_missing(
    values: defaultdict[IdentifierType, set[str]],
) -> defaultdict[IdentifierType, set[str]]:
    """Return values with what a side that reports none of an identifier type counts as."""
    for identifier_type, rule in _RULES.items():
        if rule.missing is not None and not values[identifier_type]:
            values[identifier_type] = {rule.missing}
    return values


def _merged_values(
    candidate: DeviceIdentity, parts: tuple[DeviceIdentity, ...]
) -> tuple[defaultdict[IdentifierType, set[str]], defaultdict[IdentifierType, set[str]]]:
    """
    Return the values several identities hold together, per identifier type.

    Returns all of them, and the ones of the parts that share a domain with the candidate.
    """
    merged: defaultdict[IdentifierType, set[str]] = defaultdict(set)
    same_domain: defaultdict[IdentifierType, set[str]] = defaultdict(set)
    for part in parts:
        shares_domain = not candidate.domains.isdisjoint(part.domains)
        for identifier_type, part_values in _comparable_values(part).items():
            merged[identifier_type] |= part_values
            if shares_domain:
                same_domain[identifier_type] |= part_values
    return merged, same_domain


def _vetoed(
    values: defaultdict[IdentifierType, set[str]],
    merged: defaultdict[IdentifierType, set[str]],
    same_domain: defaultdict[IdentifierType, set[str]],
) -> bool:
    """Return whether a differing value of an identifier type with a veto rules a match out."""
    for identifier_type, rule in _RULES.items():
        if rule.veto == _Veto.NEVER:
            continue
        against = merged if rule.veto == _Veto.ANY_DOMAIN else same_domain
        if _differ(values[identifier_type], against[identifier_type]):
            return True
    return False


def _differ(values_a: set[str], values_b: set[str]) -> bool:
    """Return whether both sides hold values of one identifier type and share none."""
    return bool(values_a) and bool(values_b) and values_a.isdisjoint(values_b)
