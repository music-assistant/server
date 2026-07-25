"""Tests for control-capable players in the AirPlay provider."""

from __future__ import annotations

import logging
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType, PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.errors import PlayerCommandFailed
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
    CONF_COMPANION_CREDENTIALS,
    CONF_MRP_CREDENTIALS,
    CONF_NATIVE_MRP_CREDENTIALS,
    MRP_DISCOVERY_TYPE,
)
from music_assistant.providers.airplay.control_player import AirPlayControlPlayer
from music_assistant.providers.airplay.player import AirPlayPlayer, GenericAirPlayPlayer
from music_assistant.providers.airplay.provider import AirPlayProvider

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
    setup_data: dict[str, object] | None = None,
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
    provider.mass.config.save_player_config = AsyncMock()
    # Credentials live in the player's (encrypted) setup_data; serve them through the
    # mass.config get/set surface that Player.get_setup_value / _update_setup_data use.
    # The same dict object is returned so tests can observe cleared/updated values.
    stored_setup_data = setup_data if setup_data is not None else {}

    def _config_get(key: str, default: object = None) -> object:
        if key == f"players/{PLAYER_ID}/setup_data":
            return stored_setup_data
        if key == f"players/{PLAYER_ID}":
            return {"player_id": PLAYER_ID}
        return default

    def _config_set(key: str, value: object, **_kwargs: object) -> None:
        prefix = f"players/{PLAYER_ID}/setup_data/"
        if key.startswith(prefix):
            stored_setup_data[key[len(prefix) :]] = value

    provider.mass.config.get.side_effect = _config_get
    provider.mass.config.set.side_effect = _config_set
    provider.mass.config.decrypt_string.side_effect = lambda value: value
    provider.mass.config.encrypt_string.side_effect = lambda value: value
    # Real Companion services advertise the flags under the mixed-case key
    # "rpFl"; zeroconf preserves TXT key casing as sent on the wire.
    companion_info = (
        _service_info(
            COMPANION_DISCOVERY_TYPE,
            properties={"rpFl": companion_flags},
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
    # Pairability alone never advertises POWER: without stored credentials or a
    # connected control channel the command could not be served.
    assert PlayerFeature.POWER not in player.supported_features
    assert PlayerFeature.NEXT_PREVIOUS not in player.supported_features


def test_playback_state_churn_does_not_force_control_reconnect() -> None:
    """A receiver toggling volatile TXT state must not change the reconnect signature."""
    idle = _service_info(
        AIRPLAY_DISCOVERY_TYPE,
        properties={"deviceid": DEVICE_ID, "pk": "device-key", "flags": "0x4"},
    )
    # Same device while receiving a stream: only the session `flags` bit differs.
    streaming = _service_info(
        AIRPLAY_DISCOVERY_TYPE,
        properties={"deviceid": DEVICE_ID, "pk": "device-key", "flags": "0x404"},
    )
    assert AirPlayControlPlayer._service_signature(idle) == AirPlayControlPlayer._service_signature(
        streaming
    )

    # TXT keys are case-insensitive (RFC 6763): re-casing keys is not a change.
    recased = _service_info(
        AIRPLAY_DISCOVERY_TYPE,
        properties={"DeviceID": DEVICE_ID, "PK": "device-key", "Flags": "0x4"},
    )
    assert AirPlayControlPlayer._service_signature(idle) == AirPlayControlPlayer._service_signature(
        recased
    )

    # A change to a connection-relevant field still forces a reconnect.
    rekeyed = _service_info(
        AIRPLAY_DISCOVERY_TYPE,
        properties={"deviceid": DEVICE_ID, "pk": "rotated-key", "flags": "0x4"},
    )
    assert AirPlayControlPlayer._service_signature(idle) != AirPlayControlPlayer._service_signature(
        rekeyed
    )


def test_power_feature_requires_credentials_or_connection() -> None:
    """POWER is advertised for stored Companion credentials or a live channel."""
    paired = _make_control_player(setup_data={CONF_COMPANION_CREDENTIALS: "companion-creds"})
    assert PlayerFeature.POWER in paired.supported_features

    connected = _make_control_player()
    device = MagicMock(spec=AppleTV)
    device.features.in_state.side_effect = lambda _state, feature: feature == FeatureName.TurnOn
    connected._companion_device = device
    assert PlayerFeature.POWER in connected.supported_features


def test_power_feature_skips_transient_mrp_tunnels() -> None:
    """A transient MRP tunnel does not count as real power control."""
    # pyatv reports TurnOn/TurnOff as available on every MRP connection, but a HomePod
    # reached over the transient tunnel cannot act on them and derives its power state
    # from logicalDeviceCount, which does not count AirPlay audio sessions.
    homepod = _make_control_player(model="HomePod Mini", companion_flags="0x62792")
    assert homepod._uses_transient_mrp is True

    device = MagicMock(spec=AppleTV)
    device.features.in_state.side_effect = lambda _state, feature: feature == FeatureName.TurnOn
    homepod._mrp_device = device

    assert PlayerFeature.POWER not in homepod.supported_features


def test_native_transport_features_follow_live_capabilities() -> None:
    """External next/previous controls are advertised only while available."""
    player = _make_control_player()
    device = MagicMock(spec=AppleTV)
    device.features.in_state.side_effect = lambda _state, feature: feature == FeatureName.Next

    player._companion_device = device

    assert PlayerFeature.NEXT_PREVIOUS in player.supported_features


async def test_pairing_moved_out_of_config_entries() -> None:
    """Pairing and credentials are handled by the setup flow, not by config entries."""
    # even with stored credentials, no pairing action/credential entry is emitted:
    # the interactive setup flow owns pairing and stores creds in setup_data.
    player = _make_control_player(
        setup_data={
            CONF_COMPANION_CREDENTIALS: "companion-creds",
            CONF_MRP_CREDENTIALS: "mrp-creds",
            CONF_NATIVE_MRP_CREDENTIALS: "native-creds",
        }
    )

    entries = await player.get_config_entries()
    keys = {entry.key for entry in entries}

    assert CONF_COMPANION_CREDENTIALS not in keys
    assert CONF_MRP_CREDENTIALS not in keys
    assert CONF_NATIVE_MRP_CREDENTIALS not in keys
    assert not any("pairing" in key for key in keys)
    # the run_setup_flow override is what drives the pairing now
    assert type(player).run_setup_flow is not AirPlayPlayer.run_setup_flow


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


def test_native_volume_update_ignored_while_streaming() -> None:
    """Native volume reports never overwrite the volume of a running stream."""
    # volume_set routes to the stream while streaming and leaves the native device
    # volume alone, so a report about that separate knob must not clobber (or persist)
    # the level the user just set.
    player = _make_control_player(model="HomePod Mini", companion_flags="0x62792")
    player.stream = MagicMock(running=True)
    player._attr_volume_level = 62
    player._attr_volume_muted = False

    with patch.object(player, "update_state") as update_state:
        player._handle_volume_update("mrp", 10)

    assert player.volume_level == 62
    assert player.volume_muted is False
    cast("MagicMock", player.mass.config).set_raw_player_config_value.assert_not_called()
    update_state.assert_not_called()


def test_native_volume_update_applies_when_idle() -> None:
    """Native volume reports still drive player state while no stream is running."""
    player = _make_control_player()
    player.stream = MagicMock(running=False)
    player._attr_volume_level = 62
    player._attr_volume_muted = False

    with patch.object(player, "update_state") as update_state:
        player._handle_volume_update("companion", 10)

    assert player.volume_level == 10
    update_state.assert_called_once()


def test_control_pairing_is_optional_and_never_requires_setup() -> None:
    """Optional control pairing never marks the player as requiring setup."""
    # needs_setup makes a player unavailable for playback, so it must only
    # reflect the streaming requirements: an Apple TV streams fine without
    # Companion/MRP credentials and pairing is offered in its settings instead.
    player = _make_control_player()
    assert player.companion_pairing_supported is True
    assert player.mrp_pairing_supported is True
    assert player.needs_setup is False

    homepod = _make_control_player(model="HomePod Mini", companion_flags="0x62792")
    assert homepod.companion_pairing_supported is False
    assert homepod.mrp_pairing_supported is False
    assert homepod.needs_setup is False


async def test_unpaired_apple_tv_never_attempts_control_connection() -> None:
    """An Apple TV without stored credentials is never connected (or paired)."""
    # An unsolicited connect would run pyatv's transient pair-setup, which makes
    # an Apple TV display the AirPlay pairing dialog. Regression test for the
    # spontaneous pairing popups: no credentials means no connection attempt,
    # also while the Companion record is still undiscovered.
    for companion_flags in ("0x367A2", None):
        player = _make_control_player(companion_flags=companion_flags)
        with patch(
            "music_assistant.providers.airplay.control_player.pyatv.connect",
        ) as connect:
            assert await player._connect_companion() is False
            assert await player._connect_mrp() is False
        connect.assert_not_awaited()

    # Apple TV detection reads the TXT model key case-insensitively as well.
    player = _make_control_player(companion_flags=None)
    assert player.airplay_discovery_info is not None
    properties = player.airplay_discovery_info.decoded_properties
    properties["Model"] = properties.pop("model")
    with patch(
        "music_assistant.providers.airplay.control_player.pyatv.connect",
    ) as connect:
        assert await player._connect_mrp() is False
    connect.assert_not_awaited()


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


def test_power_off_update_ignored_while_streaming() -> None:
    """A device streaming from Music Assistant is never marked powered off."""
    # MRP derives power from logicalDeviceCount, which does not count AirPlay audio
    # sessions, so a playing HomePod reports PowerState.Off. Acting on that would
    # strand the player as "off" and trip the auto-ungroup in the players controller.
    player = _make_control_player(model="HomePod Mini", companion_flags="0x62792")
    player.stream = MagicMock(running=True)
    player._attr_powered = True
    player._power_on_event.set()
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_active_source = "airplay"

    with patch.object(player, "update_state") as update_state:
        player._handle_power_update("mrp", PowerState.Off)

    assert player.powered is True
    assert player._power_on_event.is_set()
    assert player.playback_state == PlaybackState.PLAYING
    assert player.active_source == "airplay"
    update_state.assert_not_called()


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
    player = _make_control_player(setup_data={CONF_COMPANION_CREDENTIALS: "companion-creds"})
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
    player = _make_control_player(setup_data={CONF_MRP_CREDENTIALS: credentials})
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
    setup_data: dict[str, object] = {CONF_MRP_CREDENTIALS: "invalid-creds"}
    player = _make_control_player(setup_data=setup_data)

    with patch(
        "music_assistant.providers.airplay.control_player.pyatv.connect",
        side_effect=pyatv_exceptions.InvalidCredentialsError(),
    ):
        assert await player._connect_mrp() is False

    # the rejected credential is cleared from the player's setup_data
    assert setup_data[CONF_MRP_CREDENTIALS] is None


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
        setup_data={
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
    ("manufacturer", "model", "expected_type"),
    [
        ("Apple", "Apple TV 4K", AirPlayControlPlayer),
        ("Apple", "HomePod Mini", AirPlayControlPlayer),
        # Macs are multi-purpose devices, not standalone players.
        ("Apple", "MacBook Pro", GenericAirPlayPlayer),
        # Advertised control capabilities never promote a third-party receiver.
        ("Receiver", "Generic", GenericAirPlayPlayer),
    ],
)
async def test_provider_selects_player_model_from_device_identity(
    manufacturer: str,
    model: str,
    expected_type: type[AirPlayPlayer],
) -> None:
    """The player model follows the device identity, not discovered capabilities."""
    provider = AirPlayProvider.__new__(AirPlayProvider)
    provider.mass = MagicMock()
    provider.logger = logging.getLogger("test.airplay.provider")
    provider.config = MagicMock()
    provider.config.instance_id = "airplay"
    provider._bridge_manager = MagicMock()
    provider._bridge_manager.evaluate_bridge = AsyncMock()
    provider.dashboards = MagicMock()
    provider._companion_info_by_address = {
        "192.168.1.10": _service_info(
            COMPANION_DISCOVERY_TYPE,
            properties={"rpFl": "0x367A2"},
        )
    }
    provider._mrp_info_by_address = {}
    provider.mass.discovery.async_find_mdns_service = AsyncMock(return_value=None)
    provider.mass.players.register = AsyncMock()
    provider.mass.players.get_player.return_value = None
    provider.mass.config.get_raw_player_config_value.side_effect = (
        lambda _player_id, key, default=None: True if key == "enabled" else default
    )
    info = _service_info(
        AIRPLAY_DISCOVERY_TYPE,
        properties={"features": AP2_FEATURES},
    )

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


async def test_apple_device_setup_attaches_discovered_companion_service() -> None:
    """An Apple device picks up its Companion service discovered by name."""
    provider = AirPlayProvider.__new__(AirPlayProvider)
    provider.mass = MagicMock()
    provider.logger = logging.getLogger("test.airplay.provider")
    provider.config = MagicMock()
    provider.config.instance_id = "airplay"
    provider._bridge_manager = MagicMock()
    provider._bridge_manager.evaluate_bridge = AsyncMock()
    provider.dashboards = MagicMock()
    provider._companion_info_by_address = {}
    provider._mrp_info_by_address = {}
    companion_info = _service_info(
        COMPANION_DISCOVERY_TYPE,
        properties={"rpFl": "0x367A2"},
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
            return_value=("Apple", "Apple TV 4K"),
        ),
    ):
        await provider._setup_player("apple", "Apple TV", info)

    player = provider.mass.players.register.await_args.args[0]
    assert isinstance(player, AirPlayControlPlayer)
    assert player.companion_discovery_info is companion_info


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


async def test_control_service_cache_prunes_addresses_no_longer_advertised() -> None:
    """A control service that moves to a new address leaves no stale cache entry."""
    provider = AirPlayProvider.__new__(AirPlayProvider)
    provider.mass = MagicMock()
    provider._companion_info_by_address = {}
    provider.get_players = MagicMock(return_value=[])  # type: ignore[method-assign]
    info = _service_info(COMPANION_DISCOVERY_TYPE)
    other_info = _service_info(COMPANION_DISCOVERY_TYPE, address="192.168.1.60")
    other_info.name = f"other.{COMPANION_DISCOVERY_TYPE}"
    moved_info = _service_info(COMPANION_DISCOVERY_TYPE, address="192.168.1.40")

    for service_info in (info, other_info, moved_info):
        await provider._handle_companion_service_state_change(
            service_info.name,
            ServiceStateChange.Updated,
            service_info,
        )

    assert "192.168.1.10" not in provider._companion_info_by_address
    assert provider._companion_info_by_address["192.168.1.40"] is moved_info
    assert provider._companion_info_by_address["192.168.1.60"] is other_info


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


async def test_late_companion_discovery_never_changes_player_model() -> None:
    """A control service appearing for a generic endpoint never changes its model."""
    provider = AirPlayProvider.__new__(AirPlayProvider)
    provider.mass = MagicMock()
    provider.logger = logging.getLogger("test.airplay.provider")
    provider.config = MagicMock()
    provider.config.instance_id = "airplay"
    provider._companion_info_by_address = {}
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
    provider.get_players = MagicMock(return_value=[generic_player])  # type: ignore[method-assign]
    provider.mass.players.unregister = AsyncMock()
    provider.mass.players.register = AsyncMock()
    info = _service_info(
        COMPANION_DISCOVERY_TYPE,
        properties={"rpFl": "0x367A2"},
    )

    await provider._handle_companion_service_state_change(
        info.name,
        ServiceStateChange.Added,
        info,
    )

    provider.mass.players.unregister.assert_not_awaited()
    provider.mass.players.register.assert_not_awaited()


async def test_generic_device_setup_skips_control_discovery() -> None:
    """A non-Apple receiver registers without Companion/MRP service lookups."""
    provider = AirPlayProvider.__new__(AirPlayProvider)
    provider.mass = MagicMock()
    provider.logger = logging.getLogger("test.airplay.provider")
    provider.config = MagicMock()
    provider.config.instance_id = "airplay"
    provider._bridge_manager = MagicMock()
    provider._bridge_manager.evaluate_bridge = AsyncMock()
    provider.dashboards = MagicMock()
    provider._companion_info_by_address = {}
    provider._mrp_info_by_address = {}
    provider.mass.discovery.async_find_mdns_service = AsyncMock(return_value=None)
    provider.mass.players.register = AsyncMock()
    provider.mass.players.get_player.return_value = None
    provider.mass.config.get_raw_player_config_value.side_effect = (
        lambda _player_id, key, default=None: True if key == "enabled" else default
    )
    info = _service_info(
        AIRPLAY_DISCOVERY_TYPE,
        properties={"model": "ThirdPartyReceiver1,1", "features": "0x0"},
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
    assert isinstance(player, GenericAirPlayPlayer)
    control_lookups = [
        lookup_call
        for lookup_call in provider.mass.discovery.async_find_mdns_service.await_args_list
        if lookup_call.args
        and lookup_call.args[0] in (COMPANION_DISCOVERY_TYPE, MRP_DISCOVERY_TYPE)
    ]
    assert not control_lookups


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


async def test_list_installed_app_ids_none_when_feature_unavailable() -> None:
    """No AppList feature available on the device returns None."""
    player = _make_control_player()
    with patch.object(player, "_device_for_feature", return_value=None):
        assert await player.async_list_installed_app_ids() is None


async def test_list_installed_app_ids_none_on_command_error() -> None:
    """A control-channel error while listing apps returns None."""
    player = _make_control_player()
    device = MagicMock()
    device.apps.app_list = AsyncMock(side_effect=pyatv_exceptions.CommandError("boom"))
    with patch.object(player, "_device_for_feature", return_value=device):
        assert await player.async_list_installed_app_ids() is None


async def test_list_installed_app_ids_returns_bundle_ids() -> None:
    """A successful app list returns the set of installed bundle ids."""
    player = _make_control_player()
    device = MagicMock()
    device.apps.app_list = AsyncMock(return_value=[App("App A", "com.a"), App("App B", "com.b")])
    with patch.object(player, "_device_for_feature", return_value=device):
        assert await player.async_list_installed_app_ids() == {"com.a", "com.b"}


async def test_launch_app_raises_when_feature_unavailable() -> None:
    """Launching with no LaunchApp feature raises PlayerCommandFailed."""
    player = _make_control_player()
    with (
        patch.object(player, "_device_for_feature", return_value=None),
        pytest.raises(PlayerCommandFailed),
    ):
        await player.async_launch_app("musicassistant://dashboard/show")


async def test_launch_app_wraps_command_error() -> None:
    """A control-channel error while launching raises PlayerCommandFailed."""
    player = _make_control_player()
    device = MagicMock()
    device.apps.launch_app = AsyncMock(side_effect=pyatv_exceptions.CommandError("nope"))
    with (
        patch.object(player, "_device_for_feature", return_value=device),
        pytest.raises(PlayerCommandFailed),
    ):
        await player.async_launch_app("musicassistant://dashboard/show")


async def test_launch_app_dispatches_to_device() -> None:
    """Launching on an available device dispatches the value over Companion."""
    player = _make_control_player()
    device = MagicMock()
    device.apps.launch_app = AsyncMock()
    with patch.object(player, "_device_for_feature", return_value=device):
        await player.async_launch_app("musicassistant://dashboard/show")
    device.apps.launch_app.assert_awaited_once_with("musicassistant://dashboard/show")
