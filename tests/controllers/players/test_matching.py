"""Tests for the identifier matcher: one per rule, and one per reported case."""

from __future__ import annotations

import pytest
from music_assistant_models.enums import IdentifierType

from music_assistant.constants import MAIN_ZONE
from music_assistant.controllers.players.matching import (
    DeviceIdentity,
    identifiers_match,
    vetoes_device,
)

MAC = IdentifierType.MAC_ADDRESS
SERIAL = IdentifierType.SERIAL_NUMBER
UUID = IdentifierType.UUID
CAST_UUID = IdentifierType.CAST_UUID
AIRPLAY_ID = IdentifierType.AIRPLAY_ID
ZONE = IdentifierType.ZONE
IP = IdentifierType.IP_ADDRESS

HARDWARE_MAC = "54:78:C9:E6:0D:A0"
# the same MAC address with the locally-administered bit set, as AirPlay reports it
AIRPLAY_MAC = "56:78:C9:E6:0D:A0"
OTHER_MAC = "A8:BB:CC:DD:EE:FF"
RANDOM_MAC = "02:4E:52:A1:BC:6B"
HOST = "192.168.1.48"


def identity(
    domains: str | tuple[str, ...],
    identifiers: dict[IdentifierType, str | list[str]],
    *,
    native: bool = False,
) -> DeviceIdentity:
    """
    Build what one side reports about its device.

    :param domains: The protocol domain that reports it, or several for a player that
        holds protocols of more than one domain.
    :param identifiers: The identifiers it reports; a list reports several values of a type.
    :param native: Whether the native protocol of the device is among the reporters.
    """
    pairs = {
        (identifier_type, value)
        for identifier_type, values in identifiers.items()
        for value in ([values] if isinstance(values, str) else values)
    }
    return DeviceIdentity(
        identifiers=frozenset(pairs),
        domains=frozenset({domains} if isinstance(domains, str) else domains),
        native=native,
    )


def same_device(a: DeviceIdentity, b: DeviceIdentity) -> bool:
    """Return the verdict of the matcher, which never depends on the order of the sides."""
    verdict = identifiers_match(a, b)
    assert identifiers_match(b, a) is verdict
    return verdict


def test_shared_mac_matches_in_any_notation() -> None:
    """A MAC address matches regardless of case and separators."""
    assert same_device(
        identity("sonos", {MAC: "AA:BB:CC:DD:EE:FF"}),
        identity("airplay", {MAC: "aa-bb-cc-dd-ee-ff"}),
    )


def test_different_macs_do_not_match() -> None:
    """Two devices with nothing but different MAC addresses are two devices."""
    assert not same_device(
        identity("sonos", {MAC: HARDWARE_MAC}),
        identity("airplay", {MAC: OTHER_MAC}),
    )


def test_mac_differing_only_in_the_locally_administered_bit_matches() -> None:
    """A MAC address reported with the locally-administered bit set is the same device."""
    assert same_device(
        identity("dlna", {MAC: HARDWARE_MAC}),
        identity("airplay", {MAC: AIRPLAY_MAC}),
    )
    # only that one bit is ignored
    assert not same_device(
        identity("dlna", {MAC: HARDWARE_MAC}),
        identity("airplay", {MAC: "56:78:C9:E6:0D:A1"}),
    )


@pytest.mark.parametrize("unusable_mac", ["00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"])
def test_unusable_mac_identifies_nothing(unusable_mac: str) -> None:
    """A null or broadcast MAC address is no identity, so two devices reporting it differ."""
    assert not same_device(
        identity("dlna", {MAC: unusable_mac}),
        identity("airplay", {MAC: unusable_mac}),
    )


def test_any_of_several_macs_matches() -> None:
    """A device with two MAC addresses (reported and ARP-resolved) matches on either."""
    two_interfaces = identity("dlna", {MAC: ["AA:BB:CC:DD:EE:01", HARDWARE_MAC]})
    assert same_device(two_interfaces, identity("airplay", {MAC: "AA:BB:CC:DD:EE:01"}))
    assert same_device(two_interfaces, identity("airplay", {MAC: AIRPLAY_MAC}))
    assert not same_device(two_interfaces, identity("airplay", {MAC: OTHER_MAC}))


def test_differing_macs_never_veto() -> None:
    """Two network interfaces of one device have different MAC addresses."""
    assert same_device(
        identity("sonos", {MAC: HARDWARE_MAC, UUID: "RINCON_5478C9E60DA001400"}),
        identity("dlna", {MAC: OTHER_MAC, UUID: "RINCON_5478C9E60DA001400"}),
    )


@pytest.mark.parametrize("identifier_type", [SERIAL, UUID])
def test_shared_serial_or_uuid_matches_across_domains(identifier_type: IdentifierType) -> None:
    """A serial number or UUID two protocols agree on is a match, in any notation."""
    assert same_device(
        identity("sonos", {identifier_type: "RINCON_000E5812345601400"}),
        identity("dlna", {identifier_type: "rincon_000e5812345601400"}),
    )
    assert same_device(
        identity("raumfeld", {identifier_type: "4f8a0b6e-9c1d-4e2f-a3b4-c5d6e7f80910"}),
        identity("dlna", {identifier_type: "4F8A0B6E9C1D4E2FA3B4C5D6E7F80910"}),
    )


@pytest.mark.parametrize("identifier_type", [SERIAL, UUID])
def test_differing_serials_or_uuids_of_one_domain_veto(identifier_type: IdentifierType) -> None:
    """Two renderers of one protocol on one host (same MAC address and IP) are two devices."""
    assert not same_device(
        identity("dlna", {MAC: HARDWARE_MAC, IP: HOST, identifier_type: "renderer-1"}),
        identity("dlna", {MAC: HARDWARE_MAC, IP: HOST, identifier_type: "renderer-2"}),
    )


@pytest.mark.parametrize("identifier_type", [SERIAL, UUID])
def test_differing_serials_or_uuids_across_domains_do_not_veto(
    identifier_type: IdentifierType,
) -> None:
    """A Chromecast reports its cast uuid and DLNA its UDN: one device, tied by its MAC."""
    assert same_device(
        identity("chromecast", {MAC: HARDWARE_MAC, identifier_type: "cast-uuid"}),
        identity("dlna", {MAC: HARDWARE_MAC, identifier_type: "udn"}),
    )


def test_one_shared_domain_is_enough_for_the_same_domain_veto() -> None:
    """A player holding an AirPlay and a DLNA protocol is not the second renderer on its host."""
    player = identity(("airplay", "dlna"), {MAC: HARDWARE_MAC, IP: HOST, UUID: "renderer-1"})
    assert not same_device(
        player, identity("dlna", {MAC: HARDWARE_MAC, IP: HOST, UUID: "renderer-2"})
    )
    assert same_device(
        player, identity("chromecast", {MAC: HARDWARE_MAC, IP: HOST, UUID: "cast-uuid"})
    )


def test_a_type_only_one_side_reports_never_vetoes() -> None:
    """A side without a UUID is not ruled out by the UUID of the other side."""
    assert same_device(
        identity("dlna", {MAC: HARDWARE_MAC, UUID: "renderer-1"}),
        identity("dlna", {MAC: HARDWARE_MAC}),
    )


def test_a_shared_value_among_several_never_vetoes() -> None:
    """A player whose protocols report a UUID each shares one with each of those protocols."""
    # a Cast device reports its cast uuid as UUID, its DLNA renderer the UDN
    uuids = ["cast-uuid-1", "renderer-1"]
    assert same_device(
        identity(("chromecast", "dlna"), {UUID: uuids}), identity("dlna", {UUID: "renderer-1"})
    )
    # the renderer of another device on that host is vetoed although it shares the IP
    assert not same_device(
        identity(("chromecast", "dlna"), {UUID: uuids, IP: HOST}),
        identity("dlna", {UUID: "renderer-2", IP: HOST}),
    )


def test_shared_cast_uuid_matches_without_a_mac() -> None:
    """A Cast device without a usable MAC address and its Sendspin bridge share the cast uuid."""
    assert same_device(
        identity(
            "chromecast",
            {
                UUID: "88ef6168-67d7-3d08-fae8-b1b1709c1ed7",
                CAST_UUID: "88ef6168-67d7-3d08-fae8-b1b1709c1ed7",
                IP: HOST,
            },
        ),
        identity(
            "sendspin", {MAC: HARDWARE_MAC, CAST_UUID: "88ef6168-67d7-3d08-fae8-b1b1709c1ed7"}
        ),
    )


def test_differing_cast_uuids_veto_across_domains() -> None:
    """Two Cast receivers behind one MAC address and IP are two devices, whoever reports them."""
    assert not same_device(
        identity("chromecast", {MAC: HARDWARE_MAC, IP: HOST, CAST_UUID: "cast-1"}),
        identity("sendspin", {MAC: HARDWARE_MAC, IP: HOST, CAST_UUID: "cast-2"}),
    )


def test_shared_airplay_id_matches() -> None:
    """An AirPlay receiver and its Sendspin bridge share the AirPlay id, in any notation."""
    assert same_device(
        identity("airplay", {MAC: AIRPLAY_MAC, AIRPLAY_ID: "56:78:C9:E6:0D:A0", IP: HOST}),
        identity("sendspin", {AIRPLAY_ID: "5678c9e60da0"}),
    )


@pytest.mark.parametrize(
    "other_domains",
    ["airplay", "sendspin", ("airplay", "dlna")],
    ids=["receiver", "bridge", "player"],
)
def test_differing_airplay_ids_veto_the_shared_host(other_domains: str | tuple[str, ...]) -> None:
    """
    Several shairport-sync receivers on one host (one IP and MAC address) are one player each.

    Reported as music-assistant/support#6807 and music-assistant/support#5985. The veto holds
    against another receiver, against the bridge of another receiver and against the player
    another receiver was given.
    """
    assert not same_device(
        identity("airplay", {MAC: HARDWARE_MAC, IP: HOST, AIRPLAY_ID: "ap-kitchen"}),
        identity(other_domains, {MAC: HARDWARE_MAC, IP: HOST, AIRPLAY_ID: "ap-den"}),
    )


@pytest.mark.parametrize("output_domain", ["airplay", "sendspin", "bluesound"])
def test_differing_zones_veto_the_shared_chassis(output_domain: str) -> None:
    """
    The zones of a multi-zone amplifier (one MAC address and IP) are one player each.

    Reported as music-assistant/support#6847: the outputs of one zone landed on another
    zone of the chassis. The veto holds against an output of another protocol as well as
    against another zone of the same provider.
    """
    zone_1 = identity("bluesound", {MAC: HARDWARE_MAC, IP: HOST, ZONE: "1"}, native=True)
    assert not same_device(
        zone_1, identity(output_domain, {MAC: HARDWARE_MAC, IP: HOST, ZONE: "2"})
    )
    assert same_device(zone_1, identity(output_domain, {MAC: HARDWARE_MAC, IP: HOST, ZONE: "1"}))


def test_shared_zone_is_no_match_on_its_own() -> None:
    """The first zone of one chassis is not the first zone of another."""
    assert not same_device(
        identity("bluesound", {MAC: HARDWARE_MAC, ZONE: "1"}, native=True),
        identity("airplay", {MAC: OTHER_MAC, ZONE: "1"}),
    )


def test_a_side_without_a_zone_is_in_the_main_zone() -> None:
    """
    An output that reports no zone lands on the main zone of its chassis, never on another one.

    Reported as music-assistant/support#6847: the receiver of the chassis joined whichever
    zone was found first.
    """
    airplay = identity("airplay", {MAC: HARDWARE_MAC, IP: HOST})
    main_zone = identity("bluesound", {MAC: HARDWARE_MAC, IP: HOST, ZONE: MAIN_ZONE}, native=True)
    assert same_device(main_zone, airplay)
    assert not same_device(
        identity("bluesound", {MAC: HARDWARE_MAC, IP: HOST, ZONE: "2"}, native=True), airplay
    )
    # also on its shared IP alone
    assert not same_device(
        identity("bluesound", {IP: HOST, ZONE: "2"}, native=True), identity("dlna", {IP: HOST})
    )


def test_a_missing_zone_never_rules_out_the_player_a_protocol_was_attached_to() -> None:
    """An output without a zone that was attached to another zone stays there."""
    second_zone = identity("bluesound", {MAC: HARDWARE_MAC, IP: HOST, ZONE: "2"}, native=True)
    assert not vetoes_device(identity("airplay", {MAC: HARDWARE_MAC, IP: HOST}), [second_zone])
    assert vetoes_device(
        identity("airplay", {MAC: HARDWARE_MAC, IP: HOST, ZONE: "1"}), [second_zone]
    )


def test_shared_ip_matches_without_a_hardware_mac() -> None:
    """A shared IP is the last resort when a side has no MAC address or a randomised one."""
    assert same_device(identity("airplay", {IP: HOST}), identity("dlna", {IP: HOST}))
    assert same_device(
        identity("airplay", {MAC: HARDWARE_MAC, IP: HOST}),
        identity("dlna", {MAC: RANDOM_MAC, IP: HOST}),
    )
    assert same_device(
        identity("squeezelite", {MAC: HARDWARE_MAC, IP: HOST}, native=True),
        identity("snapcast", {IP: HOST}, native=True),
    )
    assert same_device(
        identity("squeezelite", {MAC: HARDWARE_MAC, IP: HOST}, native=True),
        identity("snapcast", {MAC: RANDOM_MAC, IP: HOST}, native=True),
    )


def test_shared_ip_matches_outputs_with_their_own_hardware_macs() -> None:
    """A device reporting another hardware MAC address per protocol is tied by its IP."""
    assert same_device(
        identity("dlna", {MAC: HARDWARE_MAC, IP: HOST}),
        identity("airplay", {MAC: OTHER_MAC, IP: HOST}),
    )
    assert same_device(
        identity("musiccast", {MAC: HARDWARE_MAC, IP: HOST}, native=True),
        identity("airplay", {MAC: OTHER_MAC, IP: HOST}),
    )


def test_shared_ip_never_ties_two_native_devices_with_hardware_macs() -> None:
    """Two native devices with a hardware MAC address each behind one IP are two devices."""
    assert not same_device(
        identity("squeezelite", {MAC: HARDWARE_MAC, IP: HOST}, native=True),
        identity("snapcast", {MAC: OTHER_MAC, IP: HOST}, native=True),
    )


def test_nothing_in_common_does_not_match() -> None:
    """Different IPs, or no identifiers at all, are no match."""
    assert not same_device(
        identity("airplay", {MAC: HARDWARE_MAC, IP: HOST}),
        identity("dlna", {MAC: RANDOM_MAC, IP: "192.168.1.99"}),
    )
    assert not same_device(identity("airplay", {}), identity("dlna", {}))
