"""Tests for control-capable players in the AirPlay provider."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType, PlaybackState, PlayerFeature, PlayerType
from pyatv import exceptions as pyatv_exceptions
from pyatv.const import (
    DeviceState,
    FeatureName,
    PairingRequirement,
    PowerState,
    Protocol,
)
from pyatv.const import MediaType as PyatvMediaType
from pyatv.interface import App, AppleTV, Playing
from pyatv.settings import MrpTunnel
from zeroconf import ServiceStateChange

from music_assistant.models.player import PlayerMedia
from music_assistant.providers.airplay.constants import (
    AIRPLAY_DISCOVERY_TYPE,
    COMPANION_DISCOVERY_TYPE,
    CONF_ACTION_START_COMPANION_PAIRING,
    CONF_ACTION_START_MRP_PAIRING,
    CONF_COMPANION_CREDENTIALS,
    CONF_COMPANION_PAIRING_PIN,
    CONF_MRP_CREDENTIALS,
    CONF_MRP_PAIRING_PIN,
    CONF_NATIVE_MRP_CREDENTIALS,
    MRP_DISCOVERY_TYPE,
)
from music_assistant.providers.airplay.control_player import AirPlayControlPlayer
from music_assistant.providers.airplay.player import AirPlayPlayer, GenericAirPlayPlayer
from music_assistant.providers.airplay.provider import AirPlayProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

PLAYER_ID = "apaabbccddeeff"
DEVICE_ID = "AA:BB:CC:DD:EE:FF"
DACP_ID = "0123456789ABCDEF"
AP2_FEATURES = "0x4A7FDFD5,0x3C177FDE"


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


def _make_control_player(
    *,
    model: str = "Apple TV 4K",
    companion_flags: str | None = "0x367A2",
    config_values: dict[str, object] | None = None,
) -> AirPlayControlPlayer:
    """Create a control-capable AirPlay player with mocked provider state."""
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
    return AirPlayControlPlayer(
        provider=provider,
        player_id=PLAYER_ID,
        raop_discovery_info=None,
        airplay_discovery_info=_service_info(
            AIRPLAY_DISCOVERY_TYPE,
            properties={
                "deviceid": DEVICE_ID,
                "features": AP2_FEATURES,
                "model": "AppleTV11,1" if "TV" in model else "AudioAccessory5,1",
                "osvers": "26.0",
            },
        ),
        companion_discovery_info=companion_info,
        mrp_discovery_info=None,
        address="192.168.1.10",
        display_name="Test Apple Device",
        manufacturer="Apple",
        model=model,
        initial_volume=25,
    )


def test_player_models_have_distinct_types() -> None:
    """Generic and control-capable AirPlay endpoints use distinct player types."""
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
    controlled = _make_control_player()

    assert generic.type == PlayerType.PROTOCOL
    assert controlled.type == PlayerType.PLAYER


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
    player = _make_control_player(companion_flags=flags)

    assert player.companion_pairing_supported is supported
    assert (PlayerFeature.POWER in player.supported_features) is supported
    assert PlayerFeature.NEXT_PREVIOUS not in player.supported_features


def test_native_transport_features_follow_live_capabilities() -> None:
    """External next/previous controls are advertised only while available."""
    player = _make_control_player()
    device = MagicMock(spec=AppleTV)
    device.features.in_state.side_effect = lambda _state, feature: feature == FeatureName.Next

    player._companion_device = device

    assert PlayerFeature.NEXT_PREVIOUS in player.supported_features


async def test_config_entries_keep_pairing_sections_separate() -> None:
    """Companion and MRP pairing entries are composed independently."""
    player = _make_control_player()

    entries = await player.get_config_entries()
    keys = {entry.key for entry in entries}

    assert CONF_ACTION_START_COMPANION_PAIRING in keys
    assert CONF_ACTION_START_MRP_PAIRING in keys
    assert CONF_COMPANION_CREDENTIALS in keys
    assert CONF_MRP_CREDENTIALS in keys
    assert CONF_NATIVE_MRP_CREDENTIALS in keys


def test_mute_feature_follows_available_control_path() -> None:
    """Mute is available only for an active stream or native absolute volume."""
    player = _make_control_player()
    assert PlayerFeature.VOLUME_MUTE not in player.supported_features

    player.stream = MagicMock(running=True)
    assert PlayerFeature.VOLUME_MUTE in player.supported_features

    player.stream = None
    device = MagicMock(spec=AppleTV)
    device.features.in_state.side_effect = lambda _state, feature: feature == FeatureName.SetVolume
    player._mrp_device = device
    assert PlayerFeature.VOLUME_MUTE in player.supported_features


async def test_native_mute_zeros_and_restores_volume() -> None:
    """Native mute emulation restores the previous absolute volume."""
    player = _make_control_player()
    player._attr_volume_level = 42
    device = MagicMock(spec=AppleTV)
    device.features.in_state.side_effect = lambda _state, feature: feature == FeatureName.SetVolume
    device.audio.set_volume = AsyncMock()
    player._mrp_device = device

    await player.volume_mute(True)
    mute_state_after_mute = player.volume_muted
    assert mute_state_after_mute is True
    assert player.volume_level == 42
    device.audio.set_volume.assert_awaited_once_with(0)

    device.audio.set_volume.reset_mock()
    await player.volume_mute(False)
    mute_state_after_unmute = player.volume_muted
    assert mute_state_after_unmute is False
    device.audio.set_volume.assert_awaited_once_with(42)


def test_duplicate_native_volume_update_is_ignored() -> None:
    """Repeated native volume events do not rewrite config or player state."""
    player = _make_control_player()
    player._attr_volume_level = 42
    player._attr_volume_muted = False

    with patch.object(player, "update_state") as update_state:
        player._handle_volume_update("companion", 42)

    cast("MagicMock", player.mass.config).set_raw_player_config_value.assert_not_called()
    update_state.assert_not_called()


def test_pairable_companion_service_requires_setup_until_paired() -> None:
    """Companion and MRP pairing contribute to the player's setup state."""
    player = _make_control_player()
    assert player.needs_setup is True

    paired_player = _make_control_player(
        config_values={CONF_COMPANION_CREDENTIALS: "companion-creds"}
    )
    assert paired_player.needs_setup is True

    fully_paired_player = _make_control_player(
        config_values={
            CONF_COMPANION_CREDENTIALS: "companion-creds",
            CONF_MRP_CREDENTIALS: "mrp-creds",
        }
    )
    assert fully_paired_player.needs_setup is False

    homepod = _make_control_player(model="HomePod Mini", companion_flags="0x62792")
    assert homepod.companion_pairing_supported is False
    assert homepod.mrp_pairing_supported is False
    assert homepod.needs_setup is False


async def test_play_media_wakes_device_before_starting_stream() -> None:
    """A sleeping controlled device receives wake before the AirPlay stream starts."""
    player = _make_control_player()
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
    player = _make_control_player()
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
    player = _make_control_player()
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
    player = _make_control_player()

    with patch.object(player, "update_state") as update_state:
        player._handle_power_update("companion", PowerState.On)

    assert player.powered is True
    assert player._power_on_event.is_set()
    update_state.assert_called_once()


def test_mrp_updates_external_source_and_media() -> None:
    """MRP now-playing pushes expose the active app and external media."""
    player = _make_control_player()
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
    player = _make_control_player()
    device = MagicMock(spec=AppleTV)
    device.metadata.app = None
    player._mrp_device = device

    with patch.object(player, "update_state"):
        player._handle_playing_update(
            Playing(device_state=DeviceState.Playing, title="External media")
        )

    assert player.active_source == "airplay_control"
    assert player.current_media is not None
    assert player.current_media.title == "External media"
    assert player.source_list[0].name == "AirPlay device"


def test_mrp_idle_update_clears_external_media() -> None:
    """An MRP idle update clears the previous external source and media."""
    player = _make_control_player()
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
    player = _make_control_player()
    player._companion_device = MagicMock(spec=AppleTV)

    with (
        patch.object(player, "_connect_mrp", new=AsyncMock(return_value=True)),
        patch.object(player, "_disconnect_control_services", new=AsyncMock()) as disconnect,
    ):
        assert await player._connect_control_services() is True

    disconnect.assert_not_awaited()


async def test_connection_retains_listener_references() -> None:
    """Pyatv listeners remain strongly referenced for the connection lifetime."""
    player = _make_control_player(config_values={CONF_COMPANION_CREDENTIALS: "companion-creds"})
    device = MagicMock(spec=AppleTV)
    device.features.in_state.return_value = False

    with (
        patch(
            "music_assistant.providers.airplay.control_player.pyatv.connect", return_value=device
        ),
        patch.object(player, "update_state"),
    ):
        assert await player._connect_companion() is False

    assert player._companion_listener is not None
    await player._disconnect_control_services()
    assert player._companion_listener is None


async def test_mrp_connection_uses_dedicated_pairing_credentials() -> None:
    """Playback monitoring connects with pyatv's complete AirPlay credentials."""
    credentials = "ltpk:ltsk:accessory-id:client-id"
    player = _make_control_player(config_values={CONF_MRP_CREDENTIALS: credentials})
    device = MagicMock(spec=AppleTV)
    device.features.in_state.side_effect = lambda _state, feature: (
        feature == FeatureName.PushUpdates
    )
    device.metadata.playing = AsyncMock(return_value=Playing(device_state=DeviceState.Idle))

    with (
        patch(
            "music_assistant.providers.airplay.control_player.pyatv.connect",
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


async def test_rejected_mrp_credentials_are_cleared() -> None:
    """Rejected MRP credentials return playback monitoring to setup state."""
    values: dict[str, object] = {CONF_MRP_CREDENTIALS: "invalid-creds"}
    player = _make_control_player(config_values=values)

    with patch(
        "music_assistant.providers.airplay.control_player.pyatv.connect",
        side_effect=pyatv_exceptions.InvalidCredentialsError(),
    ):
        assert await player._connect_mrp() is False

    assert values[CONF_MRP_CREDENTIALS] is None
    cast("MagicMock", player.mass.config).set_raw_player_config_value.assert_called_once_with(
        player.player_id,
        CONF_MRP_CREDENTIALS,
        None,
    )


async def test_homepod_mrp_connection_uses_transient_credentials() -> None:
    """HomePod playback monitoring connects without persisted credentials."""
    player = _make_control_player(model="HomePod Mini", companion_flags="0x62792")
    device = MagicMock(spec=AppleTV)
    device.features.in_state.side_effect = lambda _state, feature: (
        feature == FeatureName.PushUpdates
    )
    device.metadata.playing = AsyncMock(return_value=Playing(device_state=DeviceState.Idle))

    with (
        patch(
            "music_assistant.providers.airplay.control_player.pyatv.connect",
            return_value=device,
        ) as connect,
        patch.object(player, "update_state"),
    ):
        assert await player._connect_mrp() is False

    assert connect.await_args is not None
    config = connect.await_args.args[0]
    service = config.get_service(Protocol.AirPlay)
    assert service is not None
    assert service.credentials is None


async def test_third_party_mrp_tunnel_is_forced_from_capabilities() -> None:
    """A third-party MRP tunnel is not blocked by pyatv's model allowlist."""
    player = _make_control_player(companion_flags=None)
    assert player.airplay_discovery_info is not None
    player.airplay_discovery_info.decoded_properties["model"] = "ThirdPartyReceiver1,1"
    device = MagicMock(spec=AppleTV)
    device.features.in_state.side_effect = lambda _state, feature: (
        feature == FeatureName.PushUpdates
    )
    device.metadata.playing = AsyncMock(return_value=Playing(device_state=DeviceState.Idle))

    with (
        patch(
            "music_assistant.providers.airplay.control_player.pyatv.connect",
            return_value=device,
        ) as connect,
        patch.object(player, "update_state"),
    ):
        assert await player._connect_mrp() is False

    assert connect.await_args is not None
    config = connect.await_args.args[0]
    storage = connect.await_args.kwargs["storage"]
    settings = await storage.get_settings(config)
    assert settings.protocols.airplay.mrp_tunnel == MrpTunnel.Force


async def test_native_mrp_connection_uses_advertised_service() -> None:
    """Native MRP monitoring connects directly to its advertised endpoint."""
    player = _make_control_player(companion_flags=None)
    player.mrp_discovery_info = _service_info(
        MRP_DISCOVERY_TYPE,
        properties={"SystemBuildVersion": "18A123"},
        address="192.168.1.30",
    )
    device = MagicMock(spec=AppleTV)
    device.features.in_state.side_effect = lambda _state, feature: (
        feature == FeatureName.PushUpdates
    )
    device.metadata.playing = AsyncMock(return_value=Playing(device_state=DeviceState.Idle))

    with (
        patch(
            "music_assistant.providers.airplay.control_player.pyatv.connect",
            return_value=device,
        ) as connect,
        patch.object(player, "update_state"),
    ):
        assert await player._connect_mrp() is False

    assert connect.await_args is not None
    config = connect.await_args.args[0]
    service = config.get_service(Protocol.MRP)
    assert service is not None
    assert service.port == player.mrp_discovery_info.port


def test_mrp_credentials_are_scoped_to_transport() -> None:
    """Native and tunneled MRP keep independent pairing credentials."""
    player = _make_control_player(
        config_values={
            CONF_MRP_CREDENTIALS: "tunnel-creds",
            CONF_NATIVE_MRP_CREDENTIALS: "native-creds",
        }
    )
    assert player._mrp_credentials == "tunnel-creds"

    player.mrp_discovery_info = _service_info(
        MRP_DISCOVERY_TYPE,
        properties={"SystemBuildVersion": "18A123"},
    )

    assert player._mrp_credentials == "native-creds"


def test_protocol_config_uses_its_own_discovery_address() -> None:
    """MRP connects to the AirPlay endpoint rather than a Companion-only address."""
    player = _make_control_player()
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
    ("airplay_features", "companion_flags", "mrp_properties", "expected_type"),
    [
        (
            AP2_FEATURES,
            "0x367A2",
            None,
            AirPlayControlPlayer,
        ),
        (
            AP2_FEATURES,
            None,
            None,
            AirPlayControlPlayer,
        ),
        (
            "0x0",
            "0x367A2",
            None,
            AirPlayControlPlayer,
        ),
        (
            "0x0",
            None,
            {"SystemBuildVersion": "18A123", "AllowPairing": "yes"},
            AirPlayControlPlayer,
        ),
        (
            "0x0",
            None,
            {"SystemBuildVersion": "19A123"},
            GenericAirPlayPlayer,
        ),
        (
            "0x0",
            None,
            None,
            GenericAirPlayPlayer,
        ),
    ],
)
async def test_provider_selects_player_model_from_device_capabilities(
    airplay_features: str,
    companion_flags: str | None,
    mrp_properties: dict[str, str] | None,
    expected_type: type[AirPlayPlayer],
) -> None:
    """Discovery selects the controlled model from advertised protocol support."""
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
    provider._mrp_info_by_address = (
        {
            "192.168.1.10": _service_info(
                MRP_DISCOVERY_TYPE,
                properties=mrp_properties,
            )
        }
        if mrp_properties is not None
        else {}
    )
    provider.mass.discovery.async_find_mdns_service = AsyncMock(return_value=None)
    provider.mass.players.register = AsyncMock()
    provider.mass.players.get_player.return_value = None
    provider.mass.config.get_raw_player_config_value.side_effect = (
        lambda _player_id, key, default=None: True if key == "enabled" else default
    )
    info = _service_info(
        AIRPLAY_DISCOVERY_TYPE,
        properties={"model": "ThirdPartyReceiver1,1", "features": airplay_features},
    )

    with (
        patch(
            "music_assistant.providers.airplay.provider.get_primary_ip_address_from_zeroconf",
            return_value="192.168.1.10",
        ),
        patch(
            "music_assistant.providers.airplay.provider.get_model_info",
            return_value=("Receiver", "Generic"),
        ),
    ):
        await provider._setup_player("player", "Player", info)

    player = provider.mass.players.register.await_args.args[0]
    assert isinstance(player, expected_type)


async def test_provider_waits_for_companion_discovery_before_selecting_model() -> None:
    """An endpoint can use Companion discovered after its AirPlay service."""
    provider = AirPlayProvider.__new__(AirPlayProvider)
    provider.mass = MagicMock()
    provider.logger = logging.getLogger("test.airplay.provider")
    provider.config = MagicMock()
    provider.config.instance_id = "airplay"
    provider._bridge_manager = MagicMock()
    provider._bridge_manager.evaluate_bridge = AsyncMock()
    provider._companion_info_by_address = {}
    provider._mrp_info_by_address = {}
    companion_info = _service_info(
        COMPANION_DISCOVERY_TYPE,
        properties={"rpfl": "0x367A2"},
    )

    async def _find_service(
        service_type: str,
        display_name: str,
        timeout: float = 3.0,
    ) -> MagicMock | None:
        _ = display_name, timeout
        return companion_info if service_type == COMPANION_DISCOVERY_TYPE else None

    provider.mass.discovery.async_find_mdns_service = AsyncMock(side_effect=_find_service)
    provider.mass.players.register = AsyncMock()
    provider.mass.players.get_player.return_value = None
    provider.mass.config.get_raw_player_config_value.side_effect = (
        lambda _player_id, key, default=None: True if key == "enabled" else default
    )
    info = _service_info(
        AIRPLAY_DISCOVERY_TYPE,
        properties={"model": "AppleTV11,1"},
    )
    info.parsed_addresses.return_value = ["fd00::10", "192.168.1.10"]

    with (
        patch(
            "music_assistant.providers.airplay.provider.get_primary_ip_address_from_zeroconf",
            return_value="fd00::10",
        ),
        patch(
            "music_assistant.providers.airplay.provider.get_model_info",
            return_value=("Receiver", "Generic"),
        ),
    ):
        await provider._setup_player("apple", "Apple TV", info)

    player = provider.mass.players.register.await_args.args[0]
    assert isinstance(player, AirPlayControlPlayer)


async def test_companion_discovery_is_attached_and_retained_during_sleep() -> None:
    """The last Companion endpoint remains available when mDNS withdraws it."""
    provider = AirPlayProvider.__new__(AirPlayProvider)
    provider.mass = MagicMock()
    provider._companion_info_by_address = {}
    player = _make_control_player(companion_flags=None)
    player.address = "fd00::10"
    assert player.airplay_discovery_info is not None
    cast("MagicMock", player.airplay_discovery_info).parsed_addresses.return_value = [
        "fd00::10",
        "192.168.1.10",
    ]
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


async def test_mrp_discovery_is_attached_by_shared_address() -> None:
    """A native MRP service is attached to its controlled AirPlay endpoint."""
    provider = AirPlayProvider.__new__(AirPlayProvider)
    provider.mass = MagicMock()
    provider._mrp_info_by_address = {}
    player = _make_control_player(companion_flags=None)
    set_mrp_info = AsyncMock()
    player.set_mrp_discovery_info = set_mrp_info  # type: ignore[method-assign]
    provider.get_players = MagicMock(return_value=[player])  # type: ignore[method-assign]
    info = _service_info(MRP_DISCOVERY_TYPE)

    await provider._handle_mrp_service_state_change(
        info.name,
        ServiceStateChange.Added,
        info,
    )

    set_mrp_info.assert_awaited_once_with(info)
    assert provider._mrp_info_by_address["192.168.1.10"] is info


async def test_late_companion_discovery_promotes_generic_player() -> None:
    """A late control service upgrades an idle generic AirPlay endpoint."""
    provider = AirPlayProvider.__new__(AirPlayProvider)
    provider.mass = MagicMock()
    provider.logger = logging.getLogger("test.airplay.provider")
    provider.config = MagicMock()
    provider.config.instance_id = "airplay"
    provider._bridge_manager = MagicMock()
    provider._bridge_manager.remove_bridge = AsyncMock()
    provider._bridge_manager.evaluate_bridge = AsyncMock()
    provider._companion_info_by_address = {
        "192.168.1.10": _service_info(
            COMPANION_DISCOVERY_TYPE,
            properties={"rpfl": "0x367A2"},
        )
    }
    provider._mrp_info_by_address = {}
    generic_player = GenericAirPlayPlayer(
        provider=provider,
        player_id=PLAYER_ID,
        raop_discovery_info=None,
        airplay_discovery_info=_service_info(AIRPLAY_DISCOVERY_TYPE),
        address="192.168.1.10",
        display_name="Controlled receiver",
        manufacturer="Receiver",
        model="Model",
    )
    provider.mass.players.get_player.return_value = generic_player
    provider.mass.players.unregister = AsyncMock()
    provider.mass.players.register = AsyncMock()

    await provider._promote_control_player(PLAYER_ID)

    provider.mass.players.unregister.assert_awaited_once_with(PLAYER_ID)
    promoted_player = provider.mass.players.register.await_args.args[0]
    assert isinstance(promoted_player, AirPlayControlPlayer)
    provider._bridge_manager.evaluate_bridge.assert_awaited_once_with(promoted_player)


async def test_related_discovery_finds_differently_named_cached_service() -> None:
    """Control discovery can match a cached service by address instead of name."""
    provider = AirPlayProvider.__new__(AirPlayProvider)
    provider.mass = MagicMock()
    provider.mass.discovery.aiozc.zeroconf.cache.cache = {
        "_companion-link._tcp.local.": {},
        "uuid._companion-link._tcp.local.": {},
    }
    info = _service_info(COMPANION_DISCOVERY_TYPE)
    info.async_request = AsyncMock(return_value=True)

    with patch(
        "music_assistant.providers.airplay.provider.AsyncServiceInfo",
        return_value=info,
    ) as service_info:
        result = await provider._find_cached_discovery_info(
            COMPANION_DISCOVERY_TYPE,
            {"192.168.1.10"},
        )

    assert result is info
    service_info.assert_called_once_with(
        COMPANION_DISCOVERY_TYPE,
        "uuid._companion-link._tcp.local.",
    )


async def test_companion_pairing_stores_separate_credentials() -> None:
    """Companion pairing retains credentials independently from AirPlay pairing."""
    player = _make_control_player()
    pairing = MagicMock()
    pairing.begin = AsyncMock()
    pairing.finish = AsyncMock()
    pairing.close = AsyncMock()
    pairing.has_paired = True
    pairing.service.credentials = "companion-creds"
    values: dict[str, ConfigValueType] = {CONF_COMPANION_PAIRING_PIN: "1234"}

    with patch("music_assistant.providers.airplay.control_player.pyatv.pair", return_value=pairing):
        await player._start_companion_pairing()
        await player._finish_companion_pairing(values)

    pairing.pin.assert_called_once_with(1234)
    assert values[CONF_COMPANION_CREDENTIALS] == "companion-creds"


async def test_mrp_pairing_stores_complete_airplay_credentials() -> None:
    """Playback monitoring stores the complete credentials returned by pyatv."""
    player = _make_control_player()
    pairing = MagicMock()
    pairing.begin = AsyncMock()
    pairing.finish = AsyncMock()
    pairing.close = AsyncMock()
    pairing.has_paired = True
    pairing.service.credentials = "ltpk:ltsk:accessory-id:client-id"
    values: dict[str, ConfigValueType] = {CONF_MRP_PAIRING_PIN: "1234"}

    with patch("music_assistant.providers.airplay.control_player.pyatv.pair", return_value=pairing):
        await player._start_mrp_pairing()
        await player._finish_mrp_pairing(values)

    pairing.pin.assert_called_once_with(1234)
    assert values[CONF_MRP_CREDENTIALS] == "ltpk:ltsk:accessory-id:client-id"


async def test_reset_companion_pairing_reconnects_mrp() -> None:
    """Resetting Companion pairing immediately restores independent MRP monitoring."""
    player = _make_control_player(config_values={CONF_COMPANION_CREDENTIALS: "companion-creds"})
    values: dict[str, ConfigValueType] = {}

    with (
        patch.object(player, "_disconnect_control_services", new=AsyncMock()) as disconnect,
        patch.object(player, "_schedule_connection") as schedule,
    ):
        await player._reset_companion_pairing(values)

    disconnect.assert_awaited_once()
    schedule.assert_called_once_with(force=True)
    assert values[CONF_COMPANION_CREDENTIALS] is None
