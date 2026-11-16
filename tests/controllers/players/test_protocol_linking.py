"""Tests for which player a protocol belongs to, and what a player of several protocols is."""

from __future__ import annotations

import re
import secrets
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import EventType, IdentifierType, PlayerType, ProtocolRole

from music_assistant.constants import (
    CONF_CACHED_ARP_MAC,
    CONF_CREATED_AT,
    CONF_HIDE_IN_UI,
    CONF_PLAYER_PROTOCOLS,
    CONF_PLAYERS,
    CONF_UNDERLYING_PLAYER_ID,
)
from music_assistant.controllers.players import PlayerController
from music_assistant.controllers.players.matching import DeviceIdentity, identifiers_match
from music_assistant.helpers.util import enrich_device_mac_address
from tests.common import MockProtocol, MockProvider, attach_mock, mock_player_mass
from tests.scenarios.harness import Role, Scenario

KITCHEN = {
    IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A0",
    IdentifierType.IP_ADDRESS: "192.168.1.20",
}
# what a protocol of the same device reports while its hardware address is not known yet
KITCHEN_ADDRESS = {IdentifierType.IP_ADDRESS: "192.168.1.20"}
DEN = {IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:B0", IdentifierType.IP_ADDRESS: "192.168.1.30"}
STUDY = {IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:C0", IdentifierType.IP_ADDRESS: "192.168.1.40"}
PLAYER_EVENTS = (EventType.PLAYER_ADDED, EventType.PLAYER_REMOVED)


def _server() -> tuple[PlayerController, MagicMock]:
    """Return a players controller on a mocked server, and that server."""
    mass = mock_player_mass()
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    controller = PlayerController(mass)
    mass.players = controller
    return controller, mass


def _edge(scenario: Scenario, protocol_id: str) -> str | None:
    """Return the id of the player a protocol is stored as belonging to."""
    record = scenario.mass.config.get_protocol_record(protocol_id)
    return record.get("player_id") if record else None


# What a protocol reports about its device for matching


@pytest.mark.parametrize(
    ("domain", "role", "identifiers", "native"),
    [
        pytest.param(
            "sonos",
            ProtocolRole.NATIVE,
            {
                IdentifierType.MAC_ADDRESS: "00:0E:58:12:34:56",
                IdentifierType.UUID: "RINCON_000E58123456",
            },
            True,
            id="native",
        ),
        pytest.param(
            "wiim",
            ProtocolRole.CONTROL,
            {IdentifierType.MAC_ADDRESS: "00:0E:58:12:34:56"},
            True,
            id="control",
        ),
        pytest.param(
            "airplay",
            ProtocolRole.AUDIO,
            {IdentifierType.AIRPLAY_ID: "ap000e58123456"},
            False,
            id="audio",
        ),
    ],
)
def test_identity_of_a_protocol_reports_its_device_and_its_role(
    domain: str,
    role: ProtocolRole,
    identifiers: dict[IdentifierType, str],
    native: bool,
) -> None:
    """A protocol reports its identifiers, its own domain and whether it stands for the device."""
    controller, mass = _server()
    protocol = MockProtocol(
        MockProvider(domain, mass=mass), "protocol_1", "Speaker", role, identifiers
    )

    assert controller._identity_of(protocol) == DeviceIdentity(
        identifiers=frozenset(identifiers.items()),
        domains=frozenset({domain}),
        native=native,
    )


class TestEnrichPlayerIdentifiers:
    """Tests for MAC address enrichment via ARP lookup."""

    @staticmethod
    def _protocol(domain: str, identifiers: dict[IdentifierType, str]) -> MockProtocol:
        """Return a protocol of the given domain that reports the given identifiers."""
        _, mass = _server()
        return MockProtocol(
            MockProvider(domain, mass=mass), f"{domain}_1", "Device", identifiers=identifiers
        )

    async def test_no_ip_skips_enrichment(self) -> None:
        """Test that enrichment is skipped when no IP address is available."""
        # Sendspin bridge with a MAC but no IP
        protocol = self._protocol("sendspin", {IdentifierType.MAC_ADDRESS: "62:E5:97:45:93:D3"})

        await enrich_device_mac_address(protocol.device_info)

        # MAC should remain unchanged (no IP = no ARP lookup)
        assert protocol.device_info.identifiers[IdentifierType.MAC_ADDRESS] == "62:E5:97:45:93:D3"

    async def test_valid_hardware_mac_skips_enrichment(self) -> None:
        """Test that a valid, non-locally-administered MAC skips enrichment."""
        protocol = self._protocol(
            "sonos",
            {
                IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A0",
                IdentifierType.IP_ADDRESS: "192.168.1.100",
            },
        )

        await enrich_device_mac_address(protocol.device_info)

        # MAC should remain unchanged (valid hardware MAC, not locally administered)
        assert protocol.device_info.identifiers[IdentifierType.MAC_ADDRESS] == "54:78:C9:E6:0D:A0"

    async def test_locally_administered_bit_difference_replaces_mac(self) -> None:
        """
        Test that ARP MAC replaces reported MAC when only the locally-administered bit differs.

        Example: AirPlay reports 56:78:C9:E6:0D:A0, ARP resolves 54:78:C9:E6:0D:A0.
        These differ only in bit 1 of the first octet (locally-administered bit).
        """
        protocol = self._protocol(
            "airplay",
            {
                IdentifierType.MAC_ADDRESS: "56:78:C9:E6:0D:A0",
                IdentifierType.IP_ADDRESS: "192.168.1.50",
            },
        )

        with patch(
            "music_assistant.helpers.util.resolve_real_mac_address",
            new_callable=AsyncMock,
            return_value="54:78:C9:E6:0D:A0",
        ):
            await enrich_device_mac_address(protocol.device_info)

        # MAC should be replaced with the ARP-resolved hardware MAC
        assert protocol.device_info.identifiers[IdentifierType.MAC_ADDRESS] == "54:78:C9:E6:0D:A0"

    async def test_completely_different_mac_replaced_by_arp(self) -> None:
        """
        Test that ARP result always replaces a locally-administered MAC.

        Even when the ARP MAC is completely different from the reported MAC
        (e.g., Apple devices with random private MACs), the ARP result is used
        because it's the hardware truth that reliably unifies protocols.
        Sendspin bridges match via AIRPLAY_ID/CAST_UUID, not MAC.
        """
        protocol = self._protocol(
            "airplay",
            {
                IdentifierType.MAC_ADDRESS: "62:E5:97:45:93:D3",
                IdentifierType.IP_ADDRESS: "192.168.1.200",
            },
        )

        with patch(
            "music_assistant.helpers.util.resolve_real_mac_address",
            new_callable=AsyncMock,
            return_value="C0:95:6D:51:34:E0",
        ):
            await enrich_device_mac_address(protocol.device_info)

        # MAC should be replaced with ARP result (hardware truth)
        assert protocol.device_info.identifiers[IdentifierType.MAC_ADDRESS] == "C0:95:6D:51:34:E0"

    async def test_no_reported_mac_uses_arp_result(self) -> None:
        """Test that ARP MAC is used when no MAC was reported at all."""
        protocol = self._protocol("chromecast", {IdentifierType.IP_ADDRESS: "192.168.1.75"})

        with patch(
            "music_assistant.helpers.util.resolve_real_mac_address",
            new_callable=AsyncMock,
            return_value="AA:BB:CC:DD:EE:FF",
        ):
            await enrich_device_mac_address(protocol.device_info)

        # MAC should be set from ARP since there was none before
        assert protocol.device_info.identifiers[IdentifierType.MAC_ADDRESS] == "AA:BB:CC:DD:EE:FF"

    async def test_invalid_mac_replaced_by_arp(self) -> None:
        """Test that an invalid MAC (00:00:00:00:00:00) is replaced by ARP result."""
        protocol = self._protocol(
            "dlna",
            {
                IdentifierType.MAC_ADDRESS: "00:00:00:00:00:00",
                IdentifierType.IP_ADDRESS: "192.168.1.60",
            },
        )

        with patch(
            "music_assistant.helpers.util.resolve_real_mac_address",
            new_callable=AsyncMock,
            return_value="11:22:33:44:55:66",
        ):
            await enrich_device_mac_address(protocol.device_info)

        # Invalid MAC should be replaced
        assert protocol.device_info.identifiers[IdentifierType.MAC_ADDRESS] == "11:22:33:44:55:66"

    async def test_arp_returns_none_no_change(self) -> None:
        """Test that MAC is unchanged when ARP lookup returns None."""
        protocol = self._protocol(
            "airplay",
            {
                IdentifierType.MAC_ADDRESS: "62:E5:97:45:93:D3",
                IdentifierType.IP_ADDRESS: "192.168.1.100",
            },
        )

        with patch(
            "music_assistant.helpers.util.resolve_real_mac_address",
            new_callable=AsyncMock,
            return_value=None,
        ):
            await enrich_device_mac_address(protocol.device_info)

        # MAC should remain unchanged
        assert protocol.device_info.identifiers[IdentifierType.MAC_ADDRESS] == "62:E5:97:45:93:D3"

    async def test_ipv6_mapped_ipv4_normalized(self) -> None:
        """Test that IPv6-mapped IPv4 addresses are normalized."""
        protocol = self._protocol(
            "airplay",
            {
                IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A0",
                IdentifierType.IP_ADDRESS: "::ffff:192.168.1.100",
            },
        )

        await enrich_device_mac_address(protocol.device_info)

        # IP should be normalized to IPv4
        assert protocol.device_info.identifiers[IdentifierType.IP_ADDRESS] == "192.168.1.100"


class TestEnrichAndMatchIntegration:
    """
    Integration tests for the interaction between MAC enrichment and identifier matching.

    These tests verify that Fix 3 (preserving original MAC when ARP resolves a
    completely different address) works correctly with the identifier matching system.
    """

    async def test_apple_device_sendspin_matches_via_airplay_id(self) -> None:
        """
        Test the full Apple device scenario end-to-end.

        1. AirPlay protocol reports private MAC 62:E5:97:45:93:D3
        2. ARP resolves to hardware MAC C0:95:6D:51:34:E0 (always used)
        3. Sendspin bridge matches via AIRPLAY_ID, not MAC
        """
        controller, mass = _server()
        # Apple TV: its own protocol is AirPlay
        airplay = MockProtocol(
            MockProvider("airplay", mass=mass),
            "ap62e5974593d3",
            "Apple TV",
            identifiers={
                IdentifierType.MAC_ADDRESS: "62:E5:97:45:93:D3",
                IdentifierType.AIRPLAY_ID: "62:E5:97:45:93:D3",
                IdentifierType.IP_ADDRESS: "192.168.1.200",
            },
        )

        # ARP enrichment replaces MAC with hardware truth
        with patch(
            "music_assistant.helpers.util.resolve_real_mac_address",
            new_callable=AsyncMock,
            return_value="C0:95:6D:51:34:E0",
        ):
            await enrich_device_mac_address(airplay.device_info)

        # MAC replaced with ARP result
        assert airplay.device_info.identifiers[IdentifierType.MAC_ADDRESS] == "C0:95:6D:51:34:E0"

        # Sendspin bridge with AIRPLAY_ID (how Sendspin actually matches)
        sendspin = MockProtocol(
            MockProvider("sendspin", mass=mass),
            "spb_62e5974593d3",
            "Apple TV (Sendspin)",
            ProtocolRole.AUDIO,
            identifiers={IdentifierType.AIRPLAY_ID: "62:E5:97:45:93:D3"},
        )

        # Identifiers match via AIRPLAY_ID
        assert identifiers_match(
            controller._identity_of(airplay), controller._identity_of(sendspin)
        )

    async def test_wiim_device_mac_replaced_still_matches(self) -> None:
        """
        Test that WiiM devices match after ARP enrichment.

        WiiM AirPlay reports 56:78:C9:E6:0D:A0 (locally administered), ARP resolves to
        54:78:C9:E6:0D:A0 (hardware). The MAC is always replaced with the ARP result.
        """
        controller, mass = _server()
        airplay = MockProtocol(
            MockProvider("airplay", mass=mass),
            "ap_5678c9e60da0",
            "WiiM Pro (AirPlay)",
            ProtocolRole.AUDIO,
            identifiers={
                IdentifierType.MAC_ADDRESS: "56:78:C9:E6:0D:A0",
                IdentifierType.IP_ADDRESS: "192.168.1.50",
            },
        )

        # ARP resolves MAC that differs only in locally-administered bit
        with patch(
            "music_assistant.helpers.util.resolve_real_mac_address",
            new_callable=AsyncMock,
            return_value="54:78:C9:E6:0D:A0",
        ):
            await enrich_device_mac_address(airplay.device_info)

        # MAC should be replaced (only bit difference)
        assert airplay.device_info.identifiers[IdentifierType.MAC_ADDRESS] == "54:78:C9:E6:0D:A0"

        # DLNA protocol also reports hardware MAC
        dlna = MockProtocol(
            MockProvider("dlna", mass=mass),
            "dlna_5478c9e60da0",
            "WiiM Pro (DLNA)",
            ProtocolRole.AUDIO,
            identifiers={IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A0"},
        )

        # Should match by MAC
        assert identifiers_match(controller._identity_of(airplay), controller._identity_of(dlna))


class TestMultiMACMatching:
    """Tests for multi-MAC matching (reported MAC vs ARP MAC)."""

    @staticmethod
    def _protocols(
        mac_a: str, mac_b: str
    ) -> tuple[PlayerController, MagicMock, MockProtocol, MockProtocol]:
        """Return the controller, its server and two protocols reporting the given MACs."""
        controller, mass = _server()
        protocol_a = MockProtocol(
            MockProvider("test", mass=mass),
            "protocol_a",
            "Protocol A",
            identifiers={IdentifierType.MAC_ADDRESS: mac_a},
        )
        protocol_b = MockProtocol(
            MockProvider("other", mass=mass),
            "protocol_b",
            "Protocol B",
            identifiers={IdentifierType.MAC_ADDRESS: mac_b},
        )
        return controller, mass, protocol_a, protocol_b

    def test_match_on_reported_mac_when_arp_mac_differs(self) -> None:
        """Two protocols match when one's reported MAC equals the other's ARP-resolved MAC."""
        # A has its ARP-resolved MAC in its identifiers, the MAC its provider reported apart
        controller, _, protocol_a, protocol_b = self._protocols(
            "AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"
        )
        protocol_a.set_reported_mac("AA:BB:CC:DD:EE:02")

        assert identifiers_match(
            controller._identity_of(protocol_a), controller._identity_of(protocol_b)
        )

    def test_match_on_reported_mac_reverse(self) -> None:
        """Match when protocol_b's reported MAC matches protocol_a's ARP MAC."""
        controller, _, protocol_a, protocol_b = self._protocols(
            "AA:BB:CC:DD:EE:02", "AA:BB:CC:DD:EE:01"
        )
        protocol_b.set_reported_mac("AA:BB:CC:DD:EE:02")

        assert identifiers_match(
            controller._identity_of(protocol_a), controller._identity_of(protocol_b)
        )

    def test_no_match_when_both_macs_differ(self) -> None:
        """No match when all MACs differ between protocols."""
        controller, _, protocol_a, protocol_b = self._protocols(
            "AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:03"
        )
        protocol_a.set_reported_mac("AA:BB:CC:DD:EE:02")
        protocol_b.set_reported_mac("AA:BB:CC:DD:EE:04")

        assert not identifiers_match(
            controller._identity_of(protocol_a), controller._identity_of(protocol_b)
        )

    def test_match_reported_mac_with_la_bit_normalization(self) -> None:
        """Reported MAC matching also applies LA bit normalization."""
        # A has its ARP MAC, the reported MAC has the LA bit set; B has the hardware MAC
        controller, _, protocol_a, protocol_b = self._protocols(
            "AA:BB:CC:DD:EE:01", "54:78:C9:E6:0D:A0"
        )
        protocol_a.set_reported_mac("56:78:C9:E6:0D:A0")  # LA variant

        assert identifiers_match(
            controller._identity_of(protocol_a), controller._identity_of(protocol_b)
        )

    def test_match_on_arp_mac_while_the_provider_reports_its_own(self) -> None:
        """A protocol keeps the ARP-resolved MAC when its provider reports its own MAC again."""
        controller, mass, protocol_a, protocol_b = self._protocols(
            "AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"
        )
        cached = {("protocol_a", CONF_CACHED_ARP_MAC): "AA:BB:CC:DD:EE:02"}
        mass.config.get_protocol_value = MagicMock(
            side_effect=lambda protocol_id, key, default=None: cached.get(
                (protocol_id, key), default
            )
        )

        assert protocol_a.identifiers == frozenset(
            {
                (IdentifierType.MAC_ADDRESS, "AA:BB:CC:DD:EE:01"),
                (IdentifierType.MAC_ADDRESS, "AA:BB:CC:DD:EE:02"),
            }
        )
        assert identifiers_match(
            controller._identity_of(protocol_a), controller._identity_of(protocol_b)
        )


def test_device_info_of_a_player_keeps_the_identifiers_of_its_own_protocol() -> None:
    """The identifiers another protocol of the player reports do not replace the native's."""
    _, mass = _server()
    player, _ = attach_mock(
        MockProvider("sonos", mass=mass),
        "sonos_123",
        "Sonos Speaker",
        identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
    )
    attach_mock(
        MockProvider("dlna", mass=mass),
        "dlna_123",
        "Sonos DLNA",
        role=ProtocolRole.AUDIO,
        player=player,
        identifiers={
            IdentifierType.MAC_ADDRESS: "11:22:33:44:55:66",
            IdentifierType.UUID: "uuid:dlna-123",
        },
    )

    identifiers = player._device_info.identifiers
    assert identifiers[IdentifierType.MAC_ADDRESS] == "AA:BB:CC:DD:EE:FF"
    # what only the other protocol reports is added
    assert identifiers[IdentifierType.UUID] == "uuid:dlna-123"


# The name of a player built from its protocols


def _name_of_player_with(*protocols: tuple[str, str]) -> str:
    """
    Return the published name of a player with only the given audio protocols.

    :param protocols: The domain of each protocol and the name it reports.
    """
    _, mass = _server()
    player = None
    for domain, name in protocols:
        player, _ = attach_mock(
            MockProvider(domain, mass=mass),
            f"{domain}_123456",
            name,
            role=ProtocolRole.AUDIO,
            player=player,
        )
    assert player is not None
    player.refresh_state(signal_event=False)
    return player.name


def test_name_of_a_protocol_only_player_prefers_its_cast_name() -> None:
    """Of the names the protocols of a device report, the Cast name is preferred."""
    assert (
        _name_of_player_with(("airplay", "Samsung TV"), ("chromecast", "Living Room Speaker"))
        == "Living Room Speaker"
    )


def test_name_of_a_protocol_only_player_skips_a_mac_address() -> None:
    """A name that looks like a MAC address is skipped for a real name of another protocol."""
    # AirPlay is preferred over DLNA whichever priority applies, so only skipping it helps
    assert (
        _name_of_player_with(("airplay", "AA:BB:CC:DD:EE:FF"), ("dlna", "Kitchen Speaker"))
        == "Kitchen Speaker"
    )


@pytest.mark.parametrize(
    ("domain", "technical_name"),
    [("sendspin", "sendspin_device_abc"), ("chromecast", "cc_123456")],
)
def test_name_of_a_protocol_only_player_skips_a_technical_id(
    domain: str, technical_name: str
) -> None:
    """A name that looks like a player id is skipped for a real name of another protocol."""
    assert _name_of_player_with((domain, technical_name), ("dlna", "Bedroom TV")) == "Bedroom TV"


def test_name_of_a_player_is_the_name_its_device_reports() -> None:
    """A usable name its device reports is the name of the player as it is."""
    _, mass = _server()
    player, _ = attach_mock(MockProvider("airplay", mass=mass), "ap_123456", "HomePod Mini")
    player.refresh_state(signal_event=False)

    assert player.name == "HomePod Mini"


def test_player_keeps_its_name_while_its_protocols_report_no_usable_one() -> None:
    """A player whose protocols only report technical names keeps the name it had."""
    _, mass = _server()
    player, protocol = attach_mock(
        MockProvider("squeezelite", mass=mass), "sq_123456", "Kitchen", role=ProtocolRole.AUDIO
    )
    player.refresh_state(signal_event=False)
    assert player.name == "Kitchen"

    protocol._attr_name = "AA:BB:CC:DD:EE:FF"
    player.refresh_state(signal_event=False)

    assert player.name == "Kitchen"
    assert all(
        "AA:BB:CC:DD:EE:FF" not in call.args
        for call in mass.config.set_player_default_name.call_args_list
    )


# Which player a protocol belongs to when it attaches, on the real controller


async def test_identity_of_a_player_keeps_what_its_protocols_that_are_away_reported(
    scenario: Scenario,
) -> None:
    """
    A player is the device of its protocols also by what a protocol that is away reported.

    What a protocol reported is stored with it, so its domain still counts for the player
    and a second renderer of its domain on the same host is told apart from it.
    """
    chromecast = await scenario.add_provider("chromecast")
    dlna = await scenario.add_provider("dlna")
    airplay = await scenario.add_provider("airplay")
    cast = chromecast.protocol(
        "cast_tv", "TV", role="audio", identifiers={**KITCHEN, IdentifierType.UUID: "cast-uuid"}
    )
    renderer = dlna.protocol(
        "dlna_tv", "TV", role="audio", identifiers={**KITCHEN, IdentifierType.UUID: "dlna-udn"}
    )
    audio = airplay.protocol("ap_tv", "TV", role="audio", identifiers=KITCHEN)
    for protocol in (cast, renderer, audio):
        await scenario.register(protocol)
    player_id = scenario.single_player(cast, renderer, audio).player_id
    await scenario.unregister(renderer)
    await scenario.unregister(audio)

    parts = scenario.mass.players._identity_parts(player_id)

    assert {domain for part in parts for domain in part.domains} == {
        "chromecast",
        "dlna",
        "airplay",
    }
    # without its address, which another device can have while this one is away
    stored_identifiers = {
        (IdentifierType.MAC_ADDRESS, KITCHEN[IdentifierType.MAC_ADDRESS]),
        (IdentifierType.UUID, "dlna-udn"),
    }
    assert (
        DeviceIdentity(
            identifiers=frozenset(stored_identifiers),
            domains=frozenset({"dlna"}),
            native=False,
        )
        in parts
    )

    # another renderer on the host of the TV, while the TV's own renderer is away
    other = dlna.protocol(
        "dlna_other",
        "Other renderer on the TV",
        role="audio",
        identifiers={**KITCHEN, IdentifierType.UUID: "other-udn"},
    )
    await scenario.register(other)
    await scenario.advance(15)
    await scenario.idle()

    assert scenario.player_of(other) not in (None, player_id)
    assert scenario.player_of(cast) == player_id


async def test_minted_player_id_is_free_as_a_player_and_as_a_config(scenario: Scenario) -> None:
    """A minted id is never one that is already taken, by a live player or by a config."""
    sonos = await scenario.add_provider("sonos")
    sendspin = await scenario.add_provider("sendspin")
    # a live player without a stored config: a session whose own id has the minted shape
    session = sendspin.protocol("pl0a0a0a0a0a0a", "Web Browser", session_only=True)
    await scenario.register(session)
    assert scenario.player_of(session) == "pl0a0a0a0a0a0a"
    assert scenario.mass.config.get(f"{CONF_PLAYERS}/pl0a0a0a0a0a0a") is None
    # a disabled player has a stored config and no live player
    stored = sonos.protocol("RINCON_DEN", "Den", identifiers=DEN)
    await scenario.register(stored)
    stored_id = scenario.single_player(stored).player_id
    await scenario.disable(stored_id)
    assert scenario.mass.players.get_player(stored_id) is None
    candidates = iter(["0a0a0a0a0a0a", stored_id.removeprefix("pl"), "0123456789ab"])
    token_hex = secrets.token_hex

    def _token_hex(nbytes: int | None = None) -> str:
        return next(candidates) if nbytes == 6 else token_hex(nbytes)

    study = sonos.protocol("RINCON_STUDY", "Study", identifiers=STUDY)
    with patch.object(secrets, "token_hex", _token_hex):
        await scenario.register(study)

    assert scenario.player_of(study) == "pl0123456789ab"


async def test_new_devices_get_distinct_minted_ids_in_creation_order(scenario: Scenario) -> None:
    """
    Devices that are new each get a minted id of their own, and their creation moment.

    The creation moments order the players also when they are created back to back, for
    a later merge of two of them to keep the older one.
    """
    sonos = await scenario.add_provider("sonos")
    first = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    second = sonos.protocol("RINCON_DEN", "Den", identifiers=DEN)

    await scenario.register(first)
    await scenario.register(second)

    player_ids = [scenario.player_of(first), scenario.player_of(second)]
    assert all(player_id and re.fullmatch(r"pl[0-9a-f]{12}", player_id) for player_id in player_ids)
    assert len(set(player_ids)) == 2
    created = [
        scenario.mass.config.get(f"{CONF_PLAYERS}/{player_id}/values/{CONF_CREATED_AT}")
        for player_id in player_ids
    ]
    assert all(isinstance(value, int) for value in created)
    assert created[0] < created[1]


async def test_protocol_only_player_keeps_its_id_when_its_device_returns_with_other_identifiers(
    scenario: Scenario,
) -> None:
    """
    A device returns to its player by what is stored, whatever identifiers it reports then.

    Regression test for music-assistant/support#5888: the id of a player of protocols was
    derived from whichever identifiers were known when it was created, so a device that
    came back with other identifiers got another id, which orphaned the entities API
    consumers (such as the Home Assistant integration) bind to it.
    """
    airplay = await scenario.add_provider("airplay")
    # at first only its address is known
    speaker = airplay.protocol("ap_1", "Living Room", role="audio", identifiers=KITCHEN_ADDRESS)
    await scenario.register(speaker)
    await scenario.advance(15)
    player_id = scenario.single_player(speaker).player_id
    scenario.set_present("ap_1", False)
    await scenario.restart()

    # it comes back with only a UUID to identify it by
    returned = scenario.provider("airplay").protocol(
        "ap_1",
        "Living Room",
        role="audio",
        identifiers={IdentifierType.UUID: "12345678-1234-1234-1234-123456789abc"},
    )
    await scenario.register(returned)

    assert scenario.player_of(returned) == player_id
    assert _edge(scenario, "ap_1") == player_id
    assert [player.player_id for player in scenario.visible_players()] == [player_id]


@pytest.mark.parametrize("player_state", ["offline", "disabled"])
async def test_protocol_stored_for_a_player_attaches_to_it_without_waiting(
    scenario: Scenario, player_state: str
) -> None:
    """
    A protocol that comes back goes to its stored player at once, not after a wait.

    The player exists from its config while none of its protocols is attached. A disabled
    player gets no protocol and no player is created for the protocol either.
    """
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    native = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    audio = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN_ADDRESS)
    await scenario.register(native)
    await scenario.register(audio)
    player_id = scenario.single_player(native, audio).player_id
    if player_state == "disabled":
        await scenario.disable(player_id)
    for protocol_id in ("RINCON_KITCHEN", "ap_kitchen"):
        scenario.set_present(protocol_id, False)
    await scenario.restart()
    scenario.clear_events()

    # it now reports nothing that ties it to its device: matched, it would wait and get a
    # player of its own
    returned = scenario.provider("airplay").protocol(
        "ap_kitchen",
        "Kitchen",
        role="audio",
        identifiers={IdentifierType.IP_ADDRESS: "192.168.1.77"},
    )
    await scenario.register(returned)

    if player_state == "offline":
        assert scenario.player_of(returned) == player_id
        assert [player.player_id for player in scenario.visible_players()] == [player_id]
        return
    # it does not wait for its device either: the provider just keeps the object
    assert scenario.mass.players.get_protocol("ap_kitchen") is None
    await scenario.advance(15)
    await scenario.idle()
    assert scenario.player_of(returned) is None
    assert scenario.visible_players() == []
    assert _edge(scenario, "ap_kitchen") == player_id
    assert scenario.events(*PLAYER_EVENTS) == []


async def test_protocol_stored_for_a_player_is_not_claimed_by_another_matching_player(
    scenario: Scenario,
) -> None:
    """A protocol returns to its stored player, also when another player shares its identifiers."""
    sonos = await scenario.add_provider("sonos")
    heos = await scenario.add_provider("heos")
    airplay = await scenario.add_provider("airplay")
    other = heos.protocol("heos_receiver", "Other Receiver", identifiers=DEN)
    await scenario.register(other)
    other_id = scenario.player_of(other)
    native = sonos.protocol("RINCON_KITCHEN", "Speaker", identifiers=KITCHEN)
    audio = airplay.protocol("ap_shared", "Speaker", role="audio", identifiers=KITCHEN)
    await scenario.register(native)
    await scenario.register(audio)
    player_id = scenario.single_player(native, audio).player_id
    assert other_id not in (None, player_id)
    # the other device turns out to report an identifier of the speaker
    await scenario.report(other, identifiers=KITCHEN)
    for protocol_id in ("RINCON_KITCHEN", "ap_shared"):
        scenario.set_present(protocol_id, False)
    await scenario.restart()

    # the other player is the first to match, and it is back before the speaker's protocols
    assert scenario.player_of(scenario.protocol("heos_receiver")) == other_id
    await scenario.register(scenario.protocol("ap_shared"))
    await scenario.register(scenario.protocol("RINCON_KITCHEN"))
    await scenario.idle()

    assert scenario.player_of(scenario.protocol("ap_shared")) == player_id
    assert scenario.player_of(scenario.protocol("RINCON_KITCHEN")) == player_id
    state = scenario.player(other_id)
    assert state is not None
    assert [protocol.protocol_id for protocol in state.protocols] == ["heos_receiver"]


async def test_protocol_stored_for_a_player_that_is_gone_is_matched_again(
    scenario: Scenario,
) -> None:
    """A protocol stored for a player that no longer exists is matched by its identifiers."""
    heos = await scenario.add_provider("heos")
    airplay = await scenario.add_provider("airplay")
    receiver = heos.protocol("heos_receiver", "Receiver", identifiers=KITCHEN)
    await scenario.register(receiver)
    receiver_id = scenario.player_of(receiver)
    scenario.mass.config.create_protocol_record(
        "ap_shared",
        provider=airplay.instance_id,
        player_id="pl000000000000",
        role=ProtocolRole.AUDIO,
        default_name="Speaker",
    )

    audio = airplay.protocol("ap_shared", "Speaker", role="audio", identifiers=KITCHEN)
    await scenario.register(audio)

    assert scenario.player_of(audio) == receiver_id
    assert _edge(scenario, "ap_shared") == receiver_id


@pytest.mark.parametrize("first_available", [True, False], ids=["available", "unavailable"])
async def test_protocol_of_a_domain_its_player_has_gets_a_player_of_its_own(
    scenario: Scenario, first_available: bool
) -> None:
    """
    A player holds one protocol per domain; the protocols of other domains share it.

    A second protocol of a domain the player has attached, also while that one is
    unavailable, becomes a player of its own, and both are stored with their player.
    """
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    dlna = await scenario.add_provider("dlna")
    chromecast = await scenario.add_provider("chromecast")
    native = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    first = airplay.protocol("ap_1", "AirPlay 1", role="audio", identifiers=KITCHEN)
    renderer = dlna.protocol("dlna_1", "DLNA", role="audio", identifiers=KITCHEN)
    cast = chromecast.protocol("cast_1", "Cast", role="audio", identifiers=KITCHEN)
    for protocol in (native, first, renderer, cast):
        await scenario.register(protocol)
    player_id = scenario.single_player(native, first, renderer, cast).player_id
    if not first_available:
        await scenario.report(first, available=False)

    second = airplay.protocol("ap_2", "AirPlay 2", role="audio", identifiers=KITCHEN)
    await scenario.register(second)
    await scenario.idle()

    second_id = scenario.player_of(second)
    assert second_id not in (None, player_id)
    assert scenario.player_of(first) == player_id
    assert (_edge(scenario, "ap_1"), _edge(scenario, "ap_2")) == (player_id, second_id)
    state = scenario.player(player_id)
    assert state is not None
    assert {protocol.protocol_id for protocol in state.protocols} == {
        "RINCON_KITCHEN",
        "ap_1",
        "dlna_1",
        "cast_1",
    }


async def test_waiting_protocols_of_one_domain_become_a_player_each(scenario: Scenario) -> None:
    """Protocols that wait together get one player per domain: one of each on the first."""
    airplay = await scenario.add_provider("airplay")
    dlna = await scenario.add_provider("dlna")
    first = airplay.protocol("ap_1", "Zone 1", role="audio", identifiers=KITCHEN_ADDRESS)
    second = airplay.protocol("ap_2", "Zone 2", role="audio", identifiers=KITCHEN_ADDRESS)
    renderer = dlna.protocol("dlna_1", "Zone 1", role="audio", identifiers=KITCHEN_ADDRESS)
    for protocol in (first, second, renderer):
        await scenario.register(protocol)

    await scenario.advance(15)
    await scenario.idle()

    assert None not in {scenario.player_of(protocol) for protocol in (first, second, renderer)}
    assert scenario.player_of(first) == scenario.player_of(renderer)
    assert scenario.player_of(second) != scenario.player_of(first)
    assert _edge(scenario, "ap_2") == scenario.player_of(second)
    assert len(scenario.visible_players()) == 2


async def test_protocol_stored_for_a_player_with_a_protocol_of_its_domain_gets_a_player_of_its_own(
    scenario: Scenario,
) -> None:
    """A stored player that has an attached protocol of the domain is no place for another."""
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    native = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    first = airplay.protocol("ap_1", "AirPlay 1", role="audio", identifiers=KITCHEN)
    await scenario.register(native)
    await scenario.register(first)
    player_id = scenario.single_player(native, first).player_id
    # a second AirPlay protocol stored for the same player, from an earlier session
    scenario.mass.config.create_protocol_record(
        "ap_2",
        provider=airplay.instance_id,
        player_id=player_id,
        role=ProtocolRole.AUDIO,
        default_name="AirPlay 2",
    )

    second = airplay.protocol("ap_2", "AirPlay 2", role="audio", identifiers=KITCHEN)
    await scenario.register(second)
    await scenario.idle()

    second_id = scenario.player_of(second)
    assert second_id not in (None, player_id)
    assert _edge(scenario, "ap_2") == second_id
    assert scenario.player_of(first) == player_id


@pytest.mark.parametrize(
    ("domain", "role", "macs"),
    [
        pytest.param(
            "squeezelite",
            "audio",
            ("B8:27:EB:00:00:01", "B8:27:EB:00:00:02", "B8:27:EB:00:00:03"),
            id="own_macs",
        ),
        pytest.param(
            "squeezelite",
            "native",
            ("B8:27:EB:00:00:01", "B8:27:EB:00:00:02", "B8:27:EB:00:00:03"),
            id="own_macs_native",
        ),
        # shairport-sync instances on one host, each with a locally administered MAC
        pytest.param(
            "airplay",
            "audio",
            ("02:A8:2C:F9:60:25", "5A:B3:58:62:EC:6C", "6E:11:22:33:44:55"),
            id="locally_administered_macs",
        ),
    ],
)
async def test_receivers_of_one_domain_on_one_host_are_one_player_each(
    scenario: Scenario, domain: str, role: Any, macs: tuple[str, ...]
) -> None:
    """
    Receivers of one protocol on one host, sharing its IP address, stay a player each.

    Also across a restart, each keeps its own player. Regression test for
    https://github.com/music-assistant/support/issues/5266: three squeezelite instances
    on one VM were combined into one player.
    """
    provider = await scenario.add_provider(domain)
    receivers = [
        provider.protocol(
            f"{domain}_zone_{index}",
            f"Zone {index}",
            role=role,
            identifiers={
                IdentifierType.MAC_ADDRESS: mac,
                IdentifierType.IP_ADDRESS: "192.168.1.50",
            },
        )
        for index, mac in enumerate(macs, start=1)
    ]
    for receiver in receivers:
        await scenario.register(receiver)
    await scenario.advance(15)
    await scenario.idle()
    player_ids = [scenario.player_of(receiver) for receiver in receivers]
    assert None not in player_ids
    assert len(set(player_ids)) == 3

    await scenario.restart()
    await scenario.advance(15)
    await scenario.idle()

    assert [scenario.player_of(scenario.protocol(r.protocol_id)) for r in receivers] == player_ids
    assert len(scenario.visible_players()) == 3


@pytest.mark.parametrize("domain_slot", ["free", "taken"])
async def test_protocol_that_shares_an_identifier_with_another_protocol_of_a_player_joins_it(
    scenario: Scenario, domain_slot: str
) -> None:
    """
    A protocol joins a player through what another protocol of the player reports.

    The native protocol of the device reports nothing that ties it to the new protocol,
    the AirPlay protocol on the same player does. A player that has the domain of the new
    protocol attached already is no place for it.
    """
    heos = await scenario.add_provider("heos")
    airplay = await scenario.add_provider("airplay")
    sendspin = await scenario.add_provider("sendspin")
    # the native protocol reports no identifiers at all
    native = heos.protocol("2140037800", "Receiver")
    await scenario.register(native)
    player_id = scenario.single_player(native).player_id
    denon = {
        IdentifierType.MAC_ADDRESS: "00:06:78:74:7B:16",
        IdentifierType.AIRPLAY_ID: "ap000678747b16",
    }
    for protocol_id in ("ap000678747b16", "sendspin_client"):
        scenario.mass.config.create_protocol_record(
            protocol_id,
            provider=(airplay if protocol_id.startswith("ap") else sendspin).instance_id,
            player_id=player_id,
            role=ProtocolRole.AUDIO,
            default_name="Receiver",
        )
    await scenario.register(
        airplay.protocol("ap000678747b16", "Receiver", role="audio", identifiers=denon)
    )
    if domain_slot == "taken":
        client = sendspin.protocol("sendspin_client", "Receiver", role="audio")
        await scenario.register(client)
        assert scenario.player_of(client) == player_id

    bridge = sendspin.protocol("spb_000678747b16", "Receiver", role="audio", identifiers=denon)
    await scenario.register(bridge)
    await scenario.idle()

    if domain_slot == "free":
        assert scenario.player_of(bridge) == player_id
    else:
        assert scenario.player_of(bridge) not in (None, player_id)


@pytest.mark.parametrize("role", ["native", "control"])
async def test_native_protocol_joins_the_player_its_audio_protocols_have(
    scenario: Scenario, role: Role
) -> None:
    """
    The protocol that stands for a device joins the player of its audio protocols.

    The player keeps its id, its name and its settings: nothing is moved or removed.
    """
    airplay = await scenario.add_provider("airplay")
    dlna = await scenario.add_provider("dlna")
    wiim = await scenario.add_provider("wiim")
    audio = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    renderer = dlna.protocol("dlna_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    await scenario.register(audio)
    await scenario.register(renderer)
    player_id = scenario.single_player(audio, renderer).player_id
    await scenario.configure_player(player_id, name="Living Room TV", **{CONF_HIDE_IN_UI: True})
    scenario.clear_events()

    native = wiim.protocol("wiim_kitchen", "Kitchen", role=role, identifiers=KITCHEN)
    await scenario.register(native)
    await scenario.idle()

    assert scenario.player_of(native) == player_id
    assert scenario.players_carrying(native, audio, renderer) == [scenario.single_player(native)]
    assert scenario.single_player(native).player_id == player_id
    state = scenario.player(player_id)
    assert state is not None
    assert state.name == "Living Room TV"
    assert state.hide_in_ui is True
    assert state.provider == wiim.instance_id
    assert scenario.events(*PLAYER_EVENTS) == []

    # the join is stored: after a restart the same player carries all three again
    await scenario.restart()
    await scenario.idle()

    carried = [
        scenario.protocol(protocol_id)
        for protocol_id in ("wiim_kitchen", "ap_kitchen", "dlna_kitchen")
    ]
    assert scenario.single_player(*carried).player_id == player_id
    state = scenario.player(player_id)
    assert state is not None
    assert state.name == "Living Room TV"
    assert state.hide_in_ui is True


async def test_audio_protocol_never_joins_a_group_player(scenario: Scenario) -> None:
    """A group player has no place for an audio protocol, even one that shares its identifiers."""
    chromecast = await scenario.add_provider("chromecast")
    airplay = await scenario.add_provider("airplay")
    group = chromecast.protocol("cast_group", "Speaker Group", identifiers=KITCHEN)
    group._set_reported(type=PlayerType.GROUP)
    await scenario.register(group)
    group_id = scenario.player_of(group)
    assert group_id is not None

    audio = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    await scenario.register(audio)
    await scenario.idle()

    assert scenario.player_of(audio) not in (None, group_id)


@pytest.mark.parametrize("returned_first", [True, False], ids=["returned_first", "new_first"])
async def test_restored_player_is_recognised_by_what_its_protocols_reported(
    scenario: Scenario, returned_first: bool
) -> None:
    """
    After a restart a new protocol of a device finds its player, before or after the others.

    Before the protocols of the player return, what they stored identifies the player.
    """
    airplay = await scenario.add_provider("airplay")
    await scenario.add_provider("sendspin")
    known = airplay.protocol("ap_tv", "Apple TV", role="audio", identifiers=KITCHEN)
    await scenario.register(known)
    player_id = scenario.single_player(known).player_id
    scenario.set_present("ap_tv", False)
    await scenario.restart()

    scenario.provider("sendspin").protocol(
        "sendspin_tv", "Apple TV", role="audio", identifiers=KITCHEN
    )
    order = ["ap_tv", "sendspin_tv"] if returned_first else ["sendspin_tv", "ap_tv"]
    for protocol_id in order:
        await scenario.register(scenario.protocol(protocol_id))
    await scenario.idle()

    assert scenario.player_of(scenario.protocol("ap_tv")) == player_id
    assert scenario.player_of(scenario.protocol("sendspin_tv")) == player_id
    state = scenario.player(player_id)
    assert state is not None
    assert (
        state.device_info.identifiers[IdentifierType.MAC_ADDRESS]
        == KITCHEN[IdentifierType.MAC_ADDRESS]
    )


# Protocols that ride on another protocol


async def test_derived_protocol_attaches_to_the_player_of_the_protocol_it_rides_on(
    scenario: Scenario,
) -> None:
    """A derived protocol without identifiers joins the player of its base, as derived from it."""
    heos = await scenario.add_provider("heos")
    airplay = await scenario.add_provider("airplay")
    sendspin = await scenario.add_provider("sendspin")
    native = heos.protocol("native_1", "AVR", identifiers=KITCHEN)
    base = airplay.protocol("ap_1", "AVR AirPlay", role="audio", identifiers=KITCHEN)
    await scenario.register(native)
    await scenario.register(base)
    player_id = scenario.single_player(native, base).player_id

    # the derived protocol has NO identifiers: the edge alone must resolve it
    bridge = sendspin.protocol("spb_1", "Bridge", role="audio", underlying_protocol_id="ap_1")
    await scenario.register(bridge)

    assert scenario.player_of(bridge) == player_id
    player = scenario.mass.players.get_player(player_id)
    assert player is not None
    # the derived output carries its base output, the other output does not
    outputs = {output.output_protocol_id: output for output in player._output_protocols}
    assert outputs["spb_1"].derived_from == "ap_1"
    assert outputs["ap_1"].derived_from is None


async def test_derived_protocol_riding_on_the_own_protocol_rides_on_the_native_output(
    scenario: Scenario,
) -> None:
    """A protocol derived from the own protocol of its player is derived from its native output."""
    sonos = await scenario.add_provider("sonos")
    sendspin = await scenario.add_provider("sendspin")
    native = sonos.protocol("RINCON_KITCHEN", "Dummy Output", identifiers=KITCHEN)
    await scenario.register(native)
    player_id = scenario.single_player(native).player_id

    bridge = sendspin.protocol(
        "spb_1", "Bridge", role="audio", underlying_protocol_id="RINCON_KITCHEN"
    )
    await scenario.register(bridge)

    assert scenario.player_of(bridge) == player_id
    player = scenario.mass.players.get_player(player_id)
    assert player is not None
    outputs = {output.output_protocol_id: output for output in player._output_protocols}
    assert outputs["spb_1"].derived_from == "native"


async def _bridges_of_one_domain(scenario: Scenario) -> tuple[str, Any, Any]:
    """
    Return a player with an AirPlay and a Cast protocol, each with a Sendspin bridge on it.

    The bridge on the Cast protocol attaches first, so the one on AirPlay finds the
    Sendspin domain of the player taken.
    """
    wiim = await scenario.add_provider("wiim")
    airplay = await scenario.add_provider("airplay")
    chromecast = await scenario.add_provider("chromecast")
    sendspin = await scenario.add_provider("sendspin")
    native = wiim.protocol("native_1", "WiiM", identifiers=KITCHEN)
    base_ap = airplay.protocol("ap_1", "WiiM AirPlay", role="audio", identifiers=KITCHEN)
    base_cast = chromecast.protocol("cast_1", "WiiM Cast", role="audio", identifiers=KITCHEN)
    for protocol in (native, base_ap, base_cast):
        await scenario.register(protocol)
    player_id = scenario.single_player(native, base_ap, base_cast).player_id
    bridge_cast = sendspin.protocol(
        "spb_cast", "Bridge", role="audio", underlying_protocol_id="cast_1"
    )
    bridge_ap = sendspin.protocol("spb_ap", "Bridge", role="audio", underlying_protocol_id="ap_1")
    await scenario.register(bridge_cast)
    await scenario.register(bridge_ap)
    return player_id, bridge_cast, bridge_ap


async def test_derived_protocol_waits_while_its_player_has_a_protocol_of_its_domain(
    scenario: Scenario,
) -> None:
    """A derived protocol gets no player while that of its base has its domain attached."""
    player_id, bridge_cast, bridge_ap = await _bridges_of_one_domain(scenario)
    await scenario.advance(60)

    assert scenario.player_of(bridge_cast) == player_id
    assert scenario.player_of(bridge_ap) is None
    assert len(scenario.visible_players()) == 1


@pytest.mark.parametrize("detached_first", [False, True], ids=["removed", "removed_while_away"])
async def test_waiting_derived_protocol_attaches_once_its_domain_is_free(
    scenario: Scenario, detached_first: bool
) -> None:
    """
    A derived protocol that waits for the domain of its player attaches once that is free.

    A device's bridges register in no fixed order, so a bridge that is removed right after
    start can hold the domain while the bridge that stays registers.
    """
    player_id, bridge_cast, bridge_ap = await _bridges_of_one_domain(scenario)
    if detached_first:
        await scenario.unregister(bridge_cast)

    await scenario.mass.players.remove_protocol("spb_cast")
    await scenario.idle()

    assert scenario.player_of(bridge_ap) == player_id


async def test_derived_edge_is_stored_with_the_protocol(scenario: Scenario) -> None:
    """The protocol a derived protocol rides on is stored with it, and cleared when revoked."""
    airplay = await scenario.add_provider("airplay")
    sendspin = await scenario.add_provider("sendspin")
    base = airplay.protocol("ap_1", "Kitchen", role="audio", identifiers=KITCHEN)
    await scenario.register(base)
    bridge = sendspin.protocol("spb_1", "Bridge", role="audio", underlying_protocol_id="ap_1")
    await scenario.register(bridge)
    config = scenario.mass.config

    # derived protocol: the edge is stored
    assert config.get_protocol_value("spb_1", CONF_UNDERLYING_PLAYER_ID) == "ap_1"
    # a protocol that rides on nothing stores no edge at all
    assert CONF_UNDERLYING_PLAYER_ID not in (config.get_protocol_record("ap_1") or {})["values"]

    # edge revoked (e.g. a bridge client turned web player): the stored value is cleared
    bridge._set_reported(underlying_protocol_id=None)
    await scenario.register(bridge)

    assert config.get_protocol_value("spb_1", CONF_UNDERLYING_PLAYER_ID) is None


async def test_derived_protocol_of_a_player_without_a_config_stores_nothing(
    scenario: Scenario,
) -> None:
    """A derived protocol of a player that only exists while connected leaves no config."""
    sendspin = await scenario.add_provider("sendspin")
    airplay = await scenario.add_provider("airplay")
    browser = sendspin.protocol("sendspin_web_1", "Web Browser", session_only=True)
    await scenario.register(browser)
    bridge = airplay.protocol(
        "ap_web_1", "Bridge", role="audio", underlying_protocol_id="sendspin_web_1"
    )

    await scenario.register(bridge)

    assert scenario.player_of(bridge) == "sendspin_web_1"
    assert scenario.mass.config.get_protocol_record("ap_web_1") is None
    assert scenario.mass.config.get(CONF_PLAYER_PROTOCOLS, {}) == {}
    assert scenario.mass.config.get(CONF_PLAYERS, {}) == {}
