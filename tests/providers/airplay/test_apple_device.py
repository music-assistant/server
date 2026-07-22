"""Tests for Apple device support in the AirPlay provider."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType, PlaybackState, PlayerFeature, PlayerType
from pyatv.const import (
    DeviceState,
    FeatureName,
    PairingRequirement,
    PowerState,
    Protocol,
)
from pyatv.const import MediaType as PyatvMediaType
from pyatv.interface import App, AppleTV, Playing
from zeroconf import ServiceStateChange

from music_assistant.models.player import PlayerMedia
from music_assistant.providers.airplay.apple_device import AppleDevicePlayer
from music_assistant.providers.airplay.constants import (
    AIRPLAY_DISCOVERY_TYPE,
    COMPANION_DISCOVERY_TYPE,
    CONF_COMPANION_CREDENTIALS,
    CONF_COMPANION_PAIRING_PIN,
    CONF_MRP_CREDENTIALS,
    CONF_MRP_PAIRING_PIN,
)
from music_assistant.providers.airplay.player import AirPlayPlayer, GenericAirPlayPlayer
from music_assistant.providers.airplay.provider import AirPlayProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

PLAYER_ID = "apaabbccddeeff"
DEVICE_ID = "AA:BB:CC:DD:EE:FF"
DACP_ID = "0123456789ABCDEF"


def _service_info(
    service_type: str,
    *,
    properties: dict[str, str] | None = None,
    address: str = "192.168.1.10",
) -> MagicMock:
    """Create an mDNS service-info mock."""
    info = MagicMock()
    info.type = service_type
    info.name = f"test.{service_type}"
    info.port = 7000 if service_type == AIRPLAY_DISCOVERY_TYPE else 49152
    info.decoded_properties = properties or {}
    info.properties = {
        key.encode(): value.encode() for key, value in info.decoded_properties.items()
    }
    info.addresses = [b"\xc0\xa8\x01\x0a"]
    info.parsed_addresses.return_value = [address]
    return info


def _make_apple_player(
    *,
    model: str = "Apple TV 4K",
    companion_flags: str | None = "0x367A2",
    config_values: dict[str, object] | None = None,
) -> AppleDevicePlayer:
    """Create an Apple device player with mocked provider state."""
    provider = MagicMock()
    provider.instance_id = "airplay"
    provider.dacp_id = DACP_ID
    provider.logger = logging.getLogger("test.airplay.apple")
    config = MagicMock()
    values = config_values or {}
    config.get_value.side_effect = lambda key, default=None: values.get(key, default)
    config.update.side_effect = values.update
    provider.mass.config.get_base_player_config.return_value = config
    companion_info = (
        _service_info(
            COMPANION_DISCOVERY_TYPE,
            properties={"rpfl": companion_flags},
        )
        if companion_flags is not None
        else None
    )
    return AppleDevicePlayer(
        provider=provider,
        player_id=PLAYER_ID,
        raop_discovery_info=None,
        airplay_discovery_info=_service_info(
            AIRPLAY_DISCOVERY_TYPE,
            properties={
                "deviceid": DEVICE_ID,
                "features": "0x4A7FDFD5,0x3C177FDE",
                "model": "AppleTV11,1" if "TV" in model else "AudioAccessory5,1",
                "osvers": "26.0",
            },
        ),
        companion_discovery_info=companion_info,
        address="192.168.1.10",
        display_name="Test Apple Device",
        manufacturer="Apple",
        model=model,
        initial_volume=25,
    )


def test_player_models_have_distinct_types() -> None:
    """Generic AirPlay endpoints and Apple devices use distinct player types."""
    provider = MagicMock()
    generic = GenericAirPlayPlayer(
        provider=provider,
        player_id="generic",
        raop_discovery_info=None,
        airplay_discovery_info=None,
        address="192.168.1.20",
        display_name="Generic",
        manufacturer="Receiver",
        model="Receiver",
    )
    apple = _make_apple_player()

    assert generic.type == PlayerType.PROTOCOL
    assert apple.type == PlayerType.PLAYER


@pytest.mark.parametrize(
    ("flags", "supported"),
    [
        ("0x367A2", True),
        ("0x367A6", False),
        ("0x62792", False),
        ("invalid", False),
    ],
)
def test_companion_pairing_follows_advertised_flags(flags: str, supported: bool) -> None:
    """Only a Companion service advertising PIN pairing is offered for setup."""
    player = _make_apple_player(companion_flags=flags)

    assert player.companion_pairing_supported is supported
    assert (PlayerFeature.POWER in player.supported_features) is supported
    assert PlayerFeature.NEXT_PREVIOUS not in player.supported_features


def test_native_transport_features_follow_live_capabilities() -> None:
    """External next/previous controls are advertised only while available."""
    player = _make_apple_player()
    device = MagicMock(spec=AppleTV)
    device.features.in_state.side_effect = lambda _state, feature: feature == FeatureName.Next

    player._companion_device = device

    assert PlayerFeature.NEXT_PREVIOUS in player.supported_features


def test_pairable_companion_service_requires_setup_until_paired() -> None:
    """Apple device control pairing contributes to the player's setup state."""
    player = _make_apple_player()
    assert player.needs_setup is True

    paired_player = _make_apple_player(
        config_values={CONF_COMPANION_CREDENTIALS: "companion-creds"}
    )
    assert paired_player.needs_setup is True

    fully_paired_player = _make_apple_player(
        config_values={
            CONF_COMPANION_CREDENTIALS: "companion-creds",
            CONF_MRP_CREDENTIALS: "mrp-creds",
        }
    )
    assert fully_paired_player.needs_setup is False


async def test_play_media_wakes_device_before_starting_stream() -> None:
    """A sleeping Apple device receives wake before the AirPlay stream starts."""
    player = _make_apple_player()
    player._attr_powered = False
    device = MagicMock(spec=AppleTV)
    device.features.in_state.side_effect = lambda _state, feature: feature == FeatureName.TurnOn
    events: list[str] = []

    async def _turn_on() -> None:
        events.append("wake")
        player._handle_power_update("companion", PowerState.On)

    async def _play_media(_player: AirPlayPlayer, _media: PlayerMedia) -> None:
        events.append("stream")

    device.power.turn_on = AsyncMock(side_effect=_turn_on)
    player._companion_device = device

    with patch.object(AirPlayPlayer, "play_media", side_effect=_play_media, autospec=True):
        await player.play_media(MagicMock(spec=PlayerMedia))

    assert events == ["wake", "stream"]


async def test_stop_routes_stale_stream_to_external_playback() -> None:
    """A stopped AirPlay stream does not block native playback control."""
    player = _make_apple_player()
    player.stream = MagicMock(running=False)
    device = MagicMock(spec=AppleTV)
    device.features.in_state.side_effect = lambda _state, feature: feature == FeatureName.Stop
    device.remote_control.stop = AsyncMock()
    player._mrp_device = device

    with patch.object(AirPlayPlayer, "stop", new=AsyncMock()) as stop_stream:
        await player.stop()

    stop_stream.assert_not_awaited()
    device.remote_control.stop.assert_awaited_once()


def test_power_off_update_tracks_sleep_state() -> None:
    """A Companion power-off update clears stale playback state."""
    player = _make_apple_player()
    player.stream = MagicMock(running=False)
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_active_source = "external"
    player._attr_current_media = MagicMock()

    with patch.object(player, "update_state") as update_state:
        player._handle_power_update("companion", PowerState.Off)
        assert player.powered is False
        assert player.playback_state == PlaybackState.IDLE
        assert player.active_source is None
        assert player.current_media is None

        update_state.assert_called_once()


def test_power_on_update_tracks_awake_state() -> None:
    """A Companion power-on update confirms that the device is awake."""
    player = _make_apple_player()

    with patch.object(player, "update_state") as update_state:
        player._handle_power_update("companion", PowerState.On)

    assert player.powered is True
    assert player._power_on_event.is_set()
    update_state.assert_called_once()


def test_mrp_updates_external_source_and_media() -> None:
    """MRP now-playing pushes expose the active app and external media."""
    player = _make_apple_player()
    device = MagicMock(spec=AppleTV)
    device.metadata.app = App("Music", "com.apple.Music")
    player._mrp_device = device
    playing = Playing(
        media_type=PyatvMediaType.Music,
        device_state=DeviceState.Playing,
        title="External track",
        artist="Artist",
        album="Album",
        total_time=180,
        position=12,
        content_identifier="external-track",
    )

    with patch.object(player, "update_state"):
        player._handle_playing_update(playing)

    assert player.playback_state == PlaybackState.PLAYING
    assert player.active_source == "com.apple.Music"
    assert player.current_media is not None
    assert player.current_media.media_type == MediaType.TRACK
    assert player.current_media.title == "External track"
    assert player.current_media.elapsed_time == 12
    assert player.source_list[0].name == "Music"


def test_mrp_update_without_app_uses_generic_source() -> None:
    """MRP playback without app metadata uses the generic Apple source."""
    player = _make_apple_player()
    device = MagicMock(spec=AppleTV)
    device.metadata.app = None
    player._mrp_device = device

    with patch.object(player, "update_state"):
        player._handle_playing_update(
            Playing(device_state=DeviceState.Playing, title="External media")
        )

    assert player.active_source == "apple_device"
    assert player.current_media is not None
    assert player.current_media.title == "External media"
    assert player.source_list[0].name == "Apple device"


def test_mrp_idle_update_clears_external_media() -> None:
    """An MRP idle update clears the previous external source and media."""
    player = _make_apple_player()
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_active_source = "com.apple.Music"
    player._attr_current_media = MagicMock()

    with patch.object(player, "update_state"):
        player._handle_playing_update(Playing(device_state=DeviceState.Idle))

    assert player.playback_state == PlaybackState.IDLE
    assert player.active_source is None
    assert player.current_media is None


async def test_mrp_retry_does_not_recycle_connected_companion() -> None:
    """A failed MRP monitor leaves an active Companion control channel intact."""
    player = _make_apple_player()
    player._companion_device = MagicMock(spec=AppleTV)

    with (
        patch.object(player, "_connect_mrp", new=AsyncMock(return_value=True)),
        patch.object(player, "_disconnect_apple_services", new=AsyncMock()) as disconnect,
    ):
        assert await player._connect_apple_services() is True

    disconnect.assert_not_awaited()


async def test_connection_retains_listener_references() -> None:
    """Pyatv listeners remain strongly referenced for the connection lifetime."""
    player = _make_apple_player(config_values={CONF_COMPANION_CREDENTIALS: "companion-creds"})
    device = MagicMock(spec=AppleTV)
    device.features.in_state.return_value = False

    with (
        patch("music_assistant.providers.airplay.apple_device.pyatv.connect", return_value=device),
        patch.object(player, "update_state"),
    ):
        assert await player._connect_companion() is False

    assert player._companion_listener is not None
    await player._disconnect_apple_services()
    assert player._companion_listener is None


async def test_mrp_connection_uses_dedicated_pairing_credentials() -> None:
    """Playback monitoring connects with pyatv's complete AirPlay credentials."""
    credentials = "ltpk:ltsk:accessory-id:client-id"
    player = _make_apple_player(config_values={CONF_MRP_CREDENTIALS: credentials})
    device = MagicMock(spec=AppleTV)
    device.features.in_state.side_effect = lambda _state, feature: (
        feature == FeatureName.PushUpdates
    )
    device.metadata.playing = AsyncMock(return_value=Playing(device_state=DeviceState.Idle))

    with (
        patch(
            "music_assistant.providers.airplay.apple_device.pyatv.connect",
            return_value=device,
        ) as connect,
        patch.object(player, "update_state"),
    ):
        assert await player._connect_mrp() is False

    assert connect.await_args is not None
    config = connect.await_args.args[0]
    service = config.get_service(Protocol.AirPlay)
    assert service is not None
    assert service.credentials == credentials
    assert player._mrp_state_listener is not None
    assert player._mrp_push_listener is not None


def test_protocol_config_uses_its_own_discovery_address() -> None:
    """MRP connects to the AirPlay endpoint rather than a Companion-only address."""
    player = _make_apple_player()
    player.companion_discovery_info = _service_info(
        COMPANION_DISCOVERY_TYPE,
        address="192.168.1.10",
    )
    airplay_info = _service_info(
        AIRPLAY_DISCOVERY_TYPE,
        address="192.168.1.20",
    )

    config = player._build_config(
        airplay_info,
        Protocol.AirPlay,
        None,
        PairingRequirement.NotNeeded,
    )

    assert config is not None
    assert str(config.address) == "192.168.1.20"


@pytest.mark.parametrize(
    ("manufacturer", "model", "raw_model", "companion_flags", "expected_type"),
    [
        ("Apple", "Apple TV 4K", "AppleTV11,1", "0x367A2", AppleDevicePlayer),
        ("Apple", "Apple TV 4K", "AppleTV11,1", None, GenericAirPlayPlayer),
        ("Apple", "Apple TV 4K", "AppleTV11,1", "0x62792", GenericAirPlayPlayer),
        ("Apple", "HomePod Mini", "AudioAccessory5,1", "0x62792", GenericAirPlayPlayer),
        ("Apple", "Apple TV 4K", "Unknown", "0x367A2", GenericAirPlayPlayer),
        ("Receiver", "Generic", "AppleTV11,1", "0x367A2", GenericAirPlayPlayer),
    ],
)
async def test_provider_selects_player_model_from_device_capabilities(
    manufacturer: str,
    model: str,
    raw_model: str,
    companion_flags: str | None,
    expected_type: type[AirPlayPlayer],
) -> None:
    """Discovery requires both Apple identity and pairable Companion control."""
    provider = AirPlayProvider.__new__(AirPlayProvider)
    provider.mass = MagicMock()
    provider.logger = logging.getLogger("test.airplay.provider")
    provider.config = MagicMock()
    provider.config.instance_id = "airplay"
    provider._bridge_manager = MagicMock()
    provider._bridge_manager.evaluate_bridge = AsyncMock()
    provider._companion_info_by_address = (
        {
            "192.168.1.10": _service_info(
                COMPANION_DISCOVERY_TYPE,
                properties={"rpfl": companion_flags},
            )
        }
        if companion_flags is not None
        else {}
    )
    provider.mass.discovery.async_find_mdns_service = AsyncMock(return_value=None)
    provider.mass.players.register = AsyncMock()
    provider.mass.players.get_player.return_value = None
    provider.mass.config.get_raw_player_config_value.side_effect = (
        lambda _player_id, key, default=None: True if key == "enabled" else default
    )
    info = _service_info(AIRPLAY_DISCOVERY_TYPE, properties={"model": raw_model})

    with (
        patch(
            "music_assistant.providers.airplay.provider.get_primary_ip_address_from_zeroconf",
            return_value="192.168.1.10",
        ),
        patch(
            "music_assistant.providers.airplay.provider.get_model_info",
            return_value=(manufacturer, model),
        ),
    ):
        await provider._setup_player("player", "Player", info)

    player = provider.mass.players.register.await_args.args[0]
    assert isinstance(player, expected_type)


async def test_provider_waits_for_companion_discovery_before_selecting_model() -> None:
    """An Apple endpoint can use Companion discovered after its AirPlay service."""
    provider = AirPlayProvider.__new__(AirPlayProvider)
    provider.mass = MagicMock()
    provider.logger = logging.getLogger("test.airplay.provider")
    provider.config = MagicMock()
    provider.config.instance_id = "airplay"
    provider._bridge_manager = MagicMock()
    provider._bridge_manager.evaluate_bridge = AsyncMock()
    provider._companion_info_by_address = {}
    companion_info = _service_info(
        COMPANION_DISCOVERY_TYPE,
        properties={"rpfl": "0x367A2"},
    )
    provider.mass.discovery.async_find_mdns_service = AsyncMock(side_effect=[None, companion_info])
    provider.mass.players.register = AsyncMock()
    provider.mass.players.get_player.return_value = None
    provider.mass.config.get_raw_player_config_value.side_effect = (
        lambda _player_id, key, default=None: True if key == "enabled" else default
    )
    info = _service_info(
        AIRPLAY_DISCOVERY_TYPE,
        properties={"model": "AppleTV11,1"},
    )

    with (
        patch(
            "music_assistant.providers.airplay.provider.get_primary_ip_address_from_zeroconf",
            return_value="192.168.1.10",
        ),
        patch(
            "music_assistant.providers.airplay.provider.get_model_info",
            return_value=("Apple", "Apple TV 4K"),
        ),
    ):
        await provider._setup_player("apple", "Apple TV", info)

    player = provider.mass.players.register.await_args.args[0]
    assert isinstance(player, AppleDevicePlayer)


async def test_companion_discovery_is_attached_and_retained_during_sleep() -> None:
    """The last Companion endpoint remains available when mDNS withdraws it."""
    provider = AirPlayProvider.__new__(AirPlayProvider)
    provider.mass = MagicMock()
    provider._companion_info_by_address = {}
    player = _make_apple_player(companion_flags=None)
    set_companion_info = AsyncMock()
    player.set_companion_discovery_info = set_companion_info  # type: ignore[method-assign]
    provider.get_players = MagicMock(return_value=[player])  # type: ignore[method-assign]
    provider.mass.players.get_player.return_value = player
    info = _service_info(COMPANION_DISCOVERY_TYPE)

    await provider._handle_companion_service_state_change(
        info.name,
        ServiceStateChange.Added,
        info,
    )
    set_companion_info.assert_awaited_once_with(info)

    await provider._handle_companion_service_state_change(
        info.name,
        ServiceStateChange.Removed,
        None,
    )
    set_companion_info.assert_awaited_once_with(info)
    assert provider._companion_info_by_address["192.168.1.10"] is info


async def test_companion_pairing_stores_separate_credentials() -> None:
    """Companion pairing retains credentials independently from AirPlay pairing."""
    player = _make_apple_player()
    pairing = MagicMock()
    pairing.begin = AsyncMock()
    pairing.finish = AsyncMock()
    pairing.close = AsyncMock()
    pairing.has_paired = True
    pairing.service.credentials = "companion-creds"
    values: dict[str, ConfigValueType] = {CONF_COMPANION_PAIRING_PIN: "1234"}

    with patch("music_assistant.providers.airplay.apple_device.pyatv.pair", return_value=pairing):
        await player._start_companion_pairing()
        await player._finish_companion_pairing(values)

    pairing.pin.assert_called_once_with(1234)
    assert values[CONF_COMPANION_CREDENTIALS] == "companion-creds"


async def test_mrp_pairing_stores_complete_airplay_credentials() -> None:
    """Playback monitoring stores the complete credentials returned by pyatv."""
    player = _make_apple_player()
    pairing = MagicMock()
    pairing.begin = AsyncMock()
    pairing.finish = AsyncMock()
    pairing.close = AsyncMock()
    pairing.has_paired = True
    pairing.service.credentials = "ltpk:ltsk:accessory-id:client-id"
    values: dict[str, ConfigValueType] = {CONF_MRP_PAIRING_PIN: "1234"}

    with patch("music_assistant.providers.airplay.apple_device.pyatv.pair", return_value=pairing):
        await player._start_mrp_pairing()
        await player._finish_mrp_pairing(values)

    pairing.pin.assert_called_once_with(1234)
    assert values[CONF_MRP_CREDENTIALS] == "ltpk:ltsk:accessory-id:client-id"


async def test_reset_companion_pairing_reconnects_mrp() -> None:
    """Resetting Companion pairing immediately restores independent MRP monitoring."""
    player = _make_apple_player(config_values={CONF_COMPANION_CREDENTIALS: "companion-creds"})
    values: dict[str, ConfigValueType] = {}

    with (
        patch.object(player, "_disconnect_apple_services", new=AsyncMock()) as disconnect,
        patch.object(player, "_schedule_connection") as schedule,
    ):
        await player._reset_companion_pairing(values)

    disconnect.assert_awaited_once()
    schedule.assert_called_once_with(force=True)
    assert values[CONF_COMPANION_CREDENTIALS] is None
