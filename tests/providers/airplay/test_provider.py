"""Unit tests for AirPlay provider manual discovery."""

from __future__ import annotations

import asyncio
import plistlib
import socket
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

if TYPE_CHECKING:
    from music_assistant_models.provider import ProviderManifest
from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceInfo

from music_assistant.constants import CONF_ENABLED, CONF_ENTRY_MANUAL_DISCOVERY_IPS
from music_assistant.providers.airplay import get_config_entries
from music_assistant.providers.airplay.constants import (
    AIRPLAY_DISCOVERY_TYPE,
    CONF_STORED_VOLUME,
    FALLBACK_VOLUME,
    RAOP_DISCOVERY_TYPE,
)
from music_assistant.providers.airplay.player import AirPlayPlayer
from music_assistant.providers.airplay.provider import (
    AirPlayProvider,
    ManualAirPlayDiscovery,
    _airplay_info_to_txt_properties,
    _device_id_from_airplay_info,
    _normalize_airplay_device_id,
    _normalize_manual_airplay_host,
    _parse_airplay_info_response,
)


def _packed_ip(address: str) -> bytes:
    """Return packed IP bytes for an AsyncServiceInfo."""
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return socket.inet_pton(family, address)


def _service_info(
    service_type: str = AIRPLAY_DISCOVERY_TYPE,
    address: str = "192.0.2.10",
    port: int = 7000,
    name: str = "Manual AirPlay",
    device_id: str = "AA:BB:CC:DD:EE:FF",
) -> AsyncServiceInfo:
    """Create a test AirPlay service info."""
    if service_type == RAOP_DISCOVERY_TYPE:
        service_name = f"{device_id.replace(':', '')}@{name}.{service_type}"
    else:
        service_name = f"{name}.{service_type}"
    return AsyncServiceInfo(
        service_type,
        name=service_name,
        addresses=[_packed_ip(address)],
        port=port,
        properties={
            "deviceid": device_id,
            "model": "AppleTV6,2",
            "manufacturer": "Apple",
            "sf": "0x0",
        },
        server="Manual-AirPlay.local.",
    )


def _provider(manual_hosts: list[str] | None = None) -> AirPlayProvider:
    """Create an AirPlay provider with mocked Mass dependencies."""
    mass = MagicMock()
    mass.players = MagicMock()
    mass.cache = MagicMock()
    mass.loop = asyncio.get_running_loop()
    mass.server_id = "0123456789abcdef0123456789abcdef"
    mass.streams.publish_ip = "192.0.2.1"
    mass.get_provider_instances.return_value = []
    mass.discovery.async_find_mdns_service = AsyncMock(return_value=None)
    mass.players.get_player.return_value = None
    mass.players.register = AsyncMock()
    mass.config.create_default_player_config = MagicMock()
    mass.config.get_base_player_config.return_value = MagicMock()

    def get_raw_player_config_value(
        player_id: str, key: str, default: object | None = None
    ) -> object | None:
        del player_id
        if key == CONF_ENABLED:
            return True
        if key == CONF_STORED_VOLUME:
            return FALLBACK_VOLUME
        return default

    mass.config.get_raw_player_config_value.side_effect = get_raw_player_config_value

    config = MagicMock()
    config.instance_id = "airplay"

    def get_config_value(key: str, default: object | None = None) -> object | None:
        if key == CONF_ENTRY_MANUAL_DISCOVERY_IPS.key:
            return manual_hosts if manual_hosts is not None else default
        return "GLOBAL"

    config.get_value.side_effect = get_config_value

    manifest = SimpleNamespace(domain="airplay", name="AirPlay")
    provider = AirPlayProvider(mass, cast("ProviderManifest", manifest), config, set())
    provider._bridge_manager = MagicMock()
    provider._bridge_manager.setup_bridge = AsyncMock()
    provider._bridge_manager.remove_bridge = AsyncMock()
    provider._manual_ip_config = tuple(manual_hosts or [])
    return provider


@pytest.mark.asyncio
async def test_airplay_config_entries_include_manual_discovery_ips() -> None:
    """AirPlay should expose the shared manual discovery setting."""
    entries = await get_config_entries(MagicMock())

    assert entries == (CONF_ENTRY_MANUAL_DISCOVERY_IPS,)


@pytest.mark.parametrize(
    ("raw_address", "host"),
    [
        ("192.0.2.10", "192.0.2.10"),
        ("example.local", "example.local"),
        ("fd00::1", "fd00::1"),
        ("[fd00::1]", "fd00::1"),
    ],
)
def test_normalize_manual_airplay_host(raw_address: str, host: str) -> None:
    """Manual AirPlay address parsing should support host/IP values only."""
    assert _normalize_manual_airplay_host(raw_address) == host


@pytest.mark.parametrize(
    "raw_address",
    [
        "",
        "example.local:7001",
        "[fd00::1]:7000",
        "http://example.local",
        "example.local/path",
    ],
)
def test_normalize_manual_airplay_host_invalid(raw_address: str) -> None:
    """Invalid manual AirPlay addresses should be rejected."""
    with pytest.raises(
        ValueError,
        match=(
            r"Address is empty|Custom ports are not supported|"
            r"Only IP addresses or hostnames are supported"
        ),
    ):
        _normalize_manual_airplay_host(raw_address)


def test_airplay_info_to_txt_properties_maps_expected_keys() -> None:
    """AirPlay /info metadata should become mDNS-style TXT properties."""
    properties = _airplay_info_to_txt_properties(
        {
            "deviceID": "aa:bb:cc:dd:ee:ff",
            "model": "AppleTV6,2",
            "manufacturer": "Apple",
            "sourceVersion": "740.1",
            "statusFlags": 0x8,
        }
    )

    assert properties["deviceid"] == "AA:BB:CC:DD:EE:FF"
    assert properties["srcvers"] == "740.1"
    assert properties["sf"] == "0x8"
    assert properties["flags"] == "0x8"
    assert properties["am"] == "AppleTV6,2"


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("aa:bb:cc:dd:ee:ff", "AABBCCDDEEFF"),
        ("AA-BB-CC-DD-EE-FF", "AABBCCDDEEFF"),
        ("not-a-mac", None),
        ("00:00:00:00:00:00", None),
    ],
)
def test_normalize_airplay_device_id(raw_value: str, expected: str | None) -> None:
    """Manual discovery should only accept usable MAC-like device IDs."""
    assert _normalize_airplay_device_id(raw_value) == expected


def test_device_id_from_airplay_info_uses_known_keys() -> None:
    """Manual discovery should extract a stable device ID from /info metadata."""
    assert _device_id_from_airplay_info({"macAddress": "aa:bb:cc:dd:ee:ff"}) == "AABBCCDDEEFF"
    assert _device_id_from_airplay_info({"deviceID": "not-a-mac"}) is None


def test_parse_airplay_info_response() -> None:
    """HTTP/RTSP AirPlay /info responses should parse plist bodies."""
    body = plistlib.dumps({"deviceID": "AA:BB:CC:DD:EE:FF", "name": "Manual"})
    response = (
        f"RTSP/1.0 200 OK\r\nContent-Length: {len(body)}\r\n\r\n".encode()
        + body
        + b"ignored trailing data"
    )

    assert _parse_airplay_info_response(response) == {
        "deviceID": "AA:BB:CC:DD:EE:FF",
        "name": "Manual",
    }


@pytest.mark.parametrize(
    "response",
    [
        b"RTSP/1.0 404 Not Found\r\n\r\n",
        b"RTSP/1.0 200 OK\r\n\r\nnot a plist",
        b"missing header separator",
    ],
)
def test_parse_airplay_info_response_invalid(response: bytes) -> None:
    """Malformed /info responses should not create discovery metadata."""
    assert _parse_airplay_info_response(response) is None


@pytest.mark.asyncio
async def test_probe_manual_airplay_device_builds_synthetic_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful /info probe should produce synthetic service info."""
    provider = _provider()
    resolve_mock = AsyncMock(return_value=["192.0.2.10"])
    request_mock = AsyncMock(
        side_effect=[
            {
                "deviceID": "AA:BB:CC:DD:EE:FF",
                "name": "Manual AirPlay",
                "model": "AppleTV6,2",
                "manufacturer": "Apple",
            },
            None,
        ]
    )
    monkeypatch.setattr(provider, "_resolve_manual_airplay_addresses", resolve_mock)
    monkeypatch.setattr(provider, "_request_airplay_info", request_mock)

    result = await provider._probe_manual_airplay_device("manual.local")

    assert result is not None
    assert result.display_name == "Manual AirPlay"
    assert result.device_id == "AABBCCDDEEFF"
    assert len(result.service_infos) == 1
    assert result.service_infos[0].type == AIRPLAY_DISCOVERY_TYPE
    assert result.service_infos[0].decoded_properties["deviceid"] == "AA:BB:CC:DD:EE:FF"


@pytest.mark.asyncio
async def test_probe_manual_airplay_device_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unreachable manual hosts should not create discovery info."""
    provider = _provider()
    resolve_mock = AsyncMock(return_value=["192.0.2.10"])
    request_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(provider, "_resolve_manual_airplay_addresses", resolve_mock)
    monkeypatch.setattr(provider, "_request_airplay_info", request_mock)

    assert await provider._probe_manual_airplay_device("192.0.2.10") is None


@pytest.mark.asyncio
async def test_probe_manual_airplay_device_requires_device_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual probes need a stable device ID to avoid duplicate players."""
    provider = _provider()
    resolve_mock = AsyncMock(return_value=["192.0.2.10"])
    request_mock = AsyncMock(side_effect=[{"name": "Manual"}, None])
    monkeypatch.setattr(provider, "_resolve_manual_airplay_addresses", resolve_mock)
    monkeypatch.setattr(provider, "_request_airplay_info", request_mock)

    assert await provider._probe_manual_airplay_device("192.0.2.10") is None


@pytest.mark.asyncio
async def test_setup_manual_players_registers_probe_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful manual probe should register an AirPlay player."""
    provider = _provider(["192.0.2.10"])
    airplay_info = _service_info()
    probe_mock = AsyncMock(
        return_value=ManualAirPlayDiscovery(
            display_name="Manual AirPlay",
            device_id="AABBCCDDEEFF",
            service_infos=(airplay_info,),
        )
    )
    monkeypatch.setattr(provider, "_probe_manual_airplay_device", probe_mock)

    await provider._setup_manual_players()

    cast("AsyncMock", provider.mass.players.register).assert_awaited_once()
    player = cast("AsyncMock", provider.mass.players.register).call_args.args[0]
    assert isinstance(player, AirPlayPlayer)
    assert player.player_id == "apaabbccddeeff"
    assert player.address == "192.0.2.10"
    assert player.airplay_discovery_info is airplay_info
    cast("AsyncMock", provider._bridge_manager.setup_bridge).assert_awaited_once_with(player)


@pytest.mark.asyncio
async def test_setup_manual_players_updates_existing_player(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A manual probe for an existing player should update discovery info only."""
    provider = _provider(["192.0.2.10"])
    existing_player = MagicMock(spec=AirPlayPlayer)
    get_player_mock = MagicMock(return_value=existing_player)
    airplay_info = _service_info()
    probe_mock = AsyncMock(
        return_value=ManualAirPlayDiscovery(
            display_name="Manual AirPlay",
            device_id="AABBCCDDEEFF",
            service_infos=(airplay_info,),
        )
    )
    monkeypatch.setattr(provider.mass.players, "get_player", get_player_mock)
    monkeypatch.setattr(provider, "_probe_manual_airplay_device", probe_mock)

    await provider._setup_manual_players()

    existing_player.set_discovery_info.assert_called_once_with(airplay_info, "Manual AirPlay")
    cast("AsyncMock", provider.mass.players.register).assert_not_awaited()
    cast("AsyncMock", provider._bridge_manager.setup_bridge).assert_not_awaited()


@pytest.mark.asyncio
async def test_setup_manual_players_skips_disabled_player(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabled manual AirPlay players should not be registered."""
    provider = _provider(["192.0.2.10"])

    def get_raw_player_config_value(
        player_id: str, key: str, default: object | None = None
    ) -> object | None:
        del player_id
        return False if key == CONF_ENABLED else default

    config_mock = MagicMock(side_effect=get_raw_player_config_value)
    probe_mock = AsyncMock(
        return_value=ManualAirPlayDiscovery(
            display_name="Manual AirPlay",
            device_id="AABBCCDDEEFF",
            service_infos=(_service_info(),),
        )
    )
    monkeypatch.setattr(
        provider.mass.config,
        "get_raw_player_config_value",
        config_mock,
    )
    monkeypatch.setattr(provider, "_probe_manual_airplay_device", probe_mock)

    await provider._setup_manual_players()

    cast("AsyncMock", provider.mass.players.register).assert_not_awaited()
    cast("AsyncMock", provider._bridge_manager.setup_bridge).assert_not_awaited()


@pytest.mark.asyncio
async def test_mdns_update_refreshes_existing_manual_player(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later mDNS discovery event should update the already registered player."""
    provider = _provider()
    existing_player = MagicMock(spec=AirPlayPlayer)
    get_player_mock = MagicMock(return_value=existing_player)
    monkeypatch.setattr(provider.mass.players, "get_player", get_player_mock)
    info = _service_info(name="Living Room")

    await provider.on_mdns_service_state_change(info.name, ServiceStateChange.Added, info)

    existing_player.set_discovery_info.assert_called_once_with(info, "Living Room")
