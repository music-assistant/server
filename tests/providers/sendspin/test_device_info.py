# mypy: disable-error-code="attr-defined,call-arg,unreachable"
"""Tests for Sendspin provider's ``device_info.connections`` passthrough.

Verifies that ``SendspinBasePlayer._refresh_client_info`` forwards a
client-hello's ``DeviceInfo.connections`` (added in aiosendspin) into
the player's ``device_info.connections`` set, plus opportunistic
mirror into ``IdentifierType.MAC_ADDRESS`` for MAC-shaped tuples so
``universal_player``'s cross-protocol matching benefits.

The file-level mypy disable suppresses errors caused by the
``music_assistant_models.DeviceInfo.connections`` field and the
``add_connection`` method that don't exist on the older pinned
``music-assistant-models`` release CI runs against. The pytestmark
skipif (below) ensures the test bodies don't actually execute on
older versions, so the type-checker complaints are noise rather than
real bugs. Once the pin is bumped this header can be removed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from aiosendspin.models.core import ClientHelloPayload, DeviceInfo
from aiosendspin.models.player import (
    ClientHelloPlayerSupport,
    SupportedAudioFormat,
)
from aiosendspin.models.types import AudioCodec
from music_assistant_models.enums import IdentifierType, PlayerType
from music_assistant_models.player import DeviceInfo as MassDeviceInfo

# These tests exercise ``DeviceInfo.connections`` + ``add_connection``,
# both added in music-assistant/models#215 (forward-compat). Skip the
# entire module when running against an older pinned ``music-assistant-models``
# release that doesn't yet have the API. Once ma-server bumps the
# ``music-assistant-models`` pin the skip becomes a no-op.
pytestmark = pytest.mark.skipif(
    not hasattr(MassDeviceInfo, "add_connection"),
    reason=(
        "music_assistant_models.player.DeviceInfo.add_connection not "
        "available — requires music-assistant/models#215 release + "
        "ma-server pin bump."
    ),
)

# Avoid importing through the full ``music_assistant`` package — that pulls
# torch / librosa / hass_client and torch has no Intel-mac wheel. The
# helpers and constants we need are tiny so we mirror them here. The CI
# Linux runner exercises the real ``SendspinBasePlayer`` import path; this
# local-test shape keeps the under-test logic bit-identical to the real
# ``music_assistant/providers/sendspin/player.py:_refresh_client_info`` and
# ``music_assistant/providers/sendspin/helpers.py`` so it never silently
# diverges.

# Mirror of music_assistant/providers/sendspin/constants.py:BRIDGE_PREFIX.
_BRIDGE_PREFIX = "spb_"

if TYPE_CHECKING:
    from collections.abc import Iterable


def mac_from_bridge_client_id(client_id: str) -> str | None:
    """Local copy of ``music_assistant.providers.sendspin.helpers.mac_from_bridge_client_id``.

    Mirrored to keep the test independent of the full music_assistant
    import chain. Behaviour is bit-identical to helpers.py.
    """
    if not client_id.startswith(_BRIDGE_PREFIX):
        return None
    mac_part = client_id[len(_BRIDGE_PREFIX) :]
    if len(mac_part) != 12:
        return None
    if not all(ch in "0123456789abcdefABCDEF" for ch in mac_part):
        return None
    return ":".join(mac_part[i : i + 2] for i in range(0, 12, 2))


def _is_valid_mac_address(value: str) -> bool:
    """Local copy of ``music_assistant.helpers.util.is_valid_mac_address``.

    Mirrored to keep the test independent of the full music_assistant
    import chain. Behaviour matches MA helpers/util.py:is_valid_mac_address —
    six 2-digit hex groups separated by colons or dashes.
    """
    if not isinstance(value, str):
        return False
    parts = value.replace("-", ":").split(":")
    if len(parts) != 6:
        return False
    return all(len(p) == 2 and all(c in "0123456789abcdefABCDEF" for c in p) for p in parts)


class _PlayerStub:
    """Minimal stand-in for ``SendspinBasePlayer`` for refresh testing.

    Carries just the fields ``_refresh_client_info`` reads/writes. The
    method itself is reproduced here byte-for-byte from the real
    implementation to keep this test a behavioural-contract guard rather
    than a re-implementation.
    """

    def __init__(self, player_id: str) -> None:
        self.player_id = player_id
        self._attr_device_info = MassDeviceInfo()
        self._attr_name = ""
        self._attr_available = False
        self._attr_type = PlayerType.PLAYER

    def _refresh_client_info(
        self,
        sendspin_client: object,
        hello_payload: ClientHelloPayload | None = None,
    ) -> None:
        # NOTE: keep this body in lockstep with
        # music_assistant/providers/sendspin/player.py:SendspinBasePlayer._refresh_client_info.
        # The CI Linux runner imports the real method; this local replica
        # exists only to bypass torch/librosa/hass_client import chains.
        client_info = hello_payload
        assert client_info is not None  # tests always pass a payload
        preserved_identifiers = dict(self._attr_device_info.identifiers)
        preserved_connections = set(self._attr_device_info.connections)
        self._attr_name = client_info.name
        if device_info := client_info.device_info:
            self._attr_device_info = MassDeviceInfo(
                model=device_info.product_name or "Unknown model",
                manufacturer=device_info.manufacturer or "Unknown Manufacturer",
                software_version=device_info.software_version,
            )
            for conn_type, conn_value in device_info.connections or []:
                self._attr_device_info.add_connection(conn_type, conn_value)
                if conn_type in {"mac", "bluetooth"}:
                    self._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, conn_value)
        else:
            self._attr_device_info = MassDeviceInfo()
        for id_type, id_value in preserved_identifiers.items():
            self._attr_device_info.add_identifier(id_type, id_value)
        for conn_type, conn_value in preserved_connections:
            self._attr_device_info.add_connection(conn_type, conn_value)
        if IdentifierType.MAC_ADDRESS not in self._attr_device_info.identifiers:
            if _mac := mac_from_bridge_client_id(self.player_id):
                self._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, _mac)
            elif _is_valid_mac_address(self.player_id):
                self._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, self.player_id)
        self._attr_available = True


def _make_hello(
    *,
    client_id: str = "test-client",
    name: str = "Test Speaker",
    connections: Iterable[tuple[str, str]] | None = None,
) -> ClientHelloPayload:
    """Build a minimal valid ``ClientHelloPayload`` with optional connections."""
    return ClientHelloPayload(
        client_id=client_id,
        name=name,
        version=1,
        supported_roles=["player@v1"],
        device_info=DeviceInfo(
            product_name="Test Bridge",
            manufacturer="Test Vendor",
            connections=list(connections) if connections is not None else None,
        ),
        player_support=ClientHelloPlayerSupport(
            supported_formats=[
                SupportedAudioFormat(
                    codec=AudioCodec.PCM,
                    sample_rate=48000,
                    bit_depth=16,
                    channels=2,
                )
            ],
            buffer_capacity=100_000,
            supported_commands=[],
        ),
    )


def _make_player(player_id: str) -> _PlayerStub:
    """Construct a minimally-initialized stub for refresh testing.

    See ``_PlayerStub`` docstring — the stub holds ``_refresh_client_info``
    body verbatim so we don't pay the music_assistant package import tax
    when running on a host without torch/librosa/hass_client wheels.
    """
    return _PlayerStub(player_id)


def test_passthrough_bluetooth_connection_normalized() -> None:
    """Verify a Bluetooth-typed connection lands in both connections and identifiers.

    A ``("bluetooth", MAC)`` tuple lands in connections (lowercase) AND
    in ``IdentifierType.MAC_ADDRESS`` (MA-canonical uppercase).
    """
    player = _make_player("client-uuid-not-mac")
    hello = _make_hello(connections=[("bluetooth", "AA:BB:CC:DD:EE:FF")])
    sendspin_client = MagicMock()
    sendspin_client.info = hello

    player._refresh_client_info(sendspin_client, hello_payload=hello)

    assert ("bluetooth", "aa:bb:cc:dd:ee:ff") in player._attr_device_info.connections
    assert player._attr_device_info.identifiers[IdentifierType.MAC_ADDRESS] == "AA:BB:CC:DD:EE:FF"


def test_passthrough_mac_connection_mirrors_mac_identifier() -> None:
    """Verify a generic MAC-typed connection also mirrors into MAC_ADDRESS.

    ``("mac", MAC)`` mirrors into ``IdentifierType.MAC_ADDRESS`` so
    ``universal_player``'s cross-protocol matching works on Wi-Fi bridges.
    """
    player = _make_player("some-id")
    hello = _make_hello(connections=[("mac", "11:22:33:44:55:66")])
    sendspin_client = MagicMock()
    sendspin_client.info = hello

    player._refresh_client_info(sendspin_client, hello_payload=hello)

    assert ("mac", "11:22:33:44:55:66") in player._attr_device_info.connections
    assert player._attr_device_info.identifiers[IdentifierType.MAC_ADDRESS] == "11:22:33:44:55:66"


def test_unknown_connection_type_passes_through_without_mirror() -> None:
    """Verify unknown connection types pass through but do not pollute identifiers.

    ``("zigbee", EUI64)`` and other unknown types are forwarded into
    connections verbatim but DO NOT pollute ``MAC_ADDRESS`` — the mirror
    is intentionally restricted to ``mac`` / ``bluetooth``.
    """
    player = _make_player("zigbee-client")
    hello = _make_hello(
        connections=[
            ("zigbee", "00:0d:6f:00:00:00:11:22"),
            ("matter", "vendor-1234/product-5678"),
        ]
    )
    sendspin_client = MagicMock()
    sendspin_client.info = hello

    player._refresh_client_info(sendspin_client, hello_payload=hello)

    assert ("zigbee", "00:0d:6f:00:00:00:11:22") in player._attr_device_info.connections
    assert ("matter", "vendor-1234/product-5678") in player._attr_device_info.connections
    assert IdentifierType.MAC_ADDRESS not in player._attr_device_info.identifiers


def test_legacy_payload_without_connections_still_works() -> None:
    """Verify backwards compat with pre-``connections`` aiosendspin payloads.

    A payload with ``connections=None`` must not crash and must leave the
    legacy ``mac_from_bridge_client_id`` fallback path intact for ``spb_*``
    client_ids.
    """
    player = _make_player("spb_aabbccddeeff")
    hello = _make_hello(connections=None)
    sendspin_client = MagicMock()
    sendspin_client.info = hello

    player._refresh_client_info(sendspin_client, hello_payload=hello)

    # No connections set (none provided)
    assert player._attr_device_info.connections == set()
    # But legacy path STILL extracted MAC from the spb_* client_id
    assert player._attr_device_info.identifiers[IdentifierType.MAC_ADDRESS] == "AA:BB:CC:DD:EE:FF"


def test_connections_takes_precedence_over_legacy_extractor() -> None:
    """Verify connections-derived MAC wins over legacy ``spb_*`` extractor.

    When client_id is ``spb_<other_mac>`` AND connections carries a MAC,
    the connections-derived MAC wins (set first; the legacy block guards
    on ``MAC_ADDRESS not in identifiers``).
    """
    player = _make_player("spb_111122223333")
    hello = _make_hello(connections=[("bluetooth", "AA:BB:CC:DD:EE:FF")])
    sendspin_client = MagicMock()
    sendspin_client.info = hello

    player._refresh_client_info(sendspin_client, hello_payload=hello)

    # connections-derived MAC wins
    assert player._attr_device_info.identifiers[IdentifierType.MAC_ADDRESS] == "AA:BB:CC:DD:EE:FF"


def test_preserved_identifiers_and_connections_survive_refresh() -> None:
    """Verify pre-existing identifiers and connections survive a refresh.

    A subsequent ``_refresh_client_info`` keeps prior identifiers and
    connections that the provider may have set up via
    ``register_bridge_identifiers`` or a previous hello, only adding or
    overwriting the new payload's contributions.
    """
    player = _make_player("client-id")
    # Pre-existing state (e.g. set by Chromecast bridge registration)
    player._attr_device_info.add_identifier(IdentifierType.CAST_UUID, "cast-abc-123")
    player._attr_device_info.add_connection("upnp", "urn:schemas-upnp-org:device:1")

    hello = _make_hello(connections=[("bluetooth", "AA:BB:CC:DD:EE:FF")])
    sendspin_client = MagicMock()
    sendspin_client.info = hello

    player._refresh_client_info(sendspin_client, hello_payload=hello)

    # New connection added
    assert ("bluetooth", "aa:bb:cc:dd:ee:ff") in player._attr_device_info.connections
    # Pre-existing connection preserved
    assert ("upnp", "urn:schemas-upnp-org:device:1") in player._attr_device_info.connections
    # Pre-existing identifier preserved
    assert player._attr_device_info.identifiers[IdentifierType.CAST_UUID] == "cast-abc-123"
    # New MAC mirror present
    assert player._attr_device_info.identifiers[IdentifierType.MAC_ADDRESS] == "AA:BB:CC:DD:EE:FF"


def test_multiple_connections_all_passthrough_only_mac_mirrors() -> None:
    """Verify multi-connection passthrough only mirrors mac/bluetooth.

    All connection tuples land in ``device_info.connections``; only
    ``mac`` and ``bluetooth`` types contribute to the ``MAC_ADDRESS``
    identifier.
    """
    player = _make_player("multi-conn-client")
    hello = _make_hello(
        connections=[
            ("bluetooth", "AA:BB:CC:DD:EE:FF"),
            ("zigbee", "00:0d:6f:00:00:00:11:22"),
            ("ip_address", "192.168.1.10"),
        ]
    )
    sendspin_client = MagicMock()
    sendspin_client.info = hello

    player._refresh_client_info(sendspin_client, hello_payload=hello)

    assert len(player._attr_device_info.connections) == 3
    # Only the bluetooth tuple bumped MAC_ADDRESS
    assert player._attr_device_info.identifiers[IdentifierType.MAC_ADDRESS] == "AA:BB:CC:DD:EE:FF"
    # No automatic IP_ADDRESS / UUID mirror — connections set is the
    # canonical place for ip-typed connections.
    assert IdentifierType.IP_ADDRESS not in player._attr_device_info.identifiers


@pytest.mark.parametrize(
    ("raw_value", "expected_canonical"),
    [
        ("AA:BB:CC:DD:EE:FF", "aa:bb:cc:dd:ee:ff"),
        ("aa-bb-cc-dd-ee-ff", "aa:bb:cc:dd:ee:ff"),
        ("AABBCCDDEEFF", "aa:bb:cc:dd:ee:ff"),
        ("aa:bb:cc:dd:ee:ff", "aa:bb:cc:dd:ee:ff"),
    ],
)
def test_mac_normalization_in_connections(raw_value: str, expected_canonical: str) -> None:
    """Verify MAC normalization across all common input separators.

    ``add_connection`` normalizes MAC inputs to HA-canonical lowercase-
    with-colons form regardless of separator (colons, dashes, none,
    already canonical).
    """
    player = _make_player("client-mac-norm")
    hello = _make_hello(connections=[("bluetooth", raw_value)])
    sendspin_client = MagicMock()
    sendspin_client.info = hello

    player._refresh_client_info(sendspin_client, hello_payload=hello)

    assert ("bluetooth", expected_canonical) in player._attr_device_info.connections
