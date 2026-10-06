"""Tests for controlling a device-native Spotify Connect session on a DLNA-backed LinkPlay shell."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from async_upnp_client.profiles.dlna import TransportState
from music_assistant_models.enums import PlaybackState, PlayerFeature

from music_assistant.controllers.players import PlayerController
from music_assistant.models.player import LinkedOutputProtocol
from music_assistant.providers.dlna.player import DLNAPlayer
from music_assistant.providers.wiim.grouping import NativeGroupRole
from music_assistant.providers.wiim.linkplay_player import LinkPlayPlayer
from tests.common import MockProvider, create_mock_config

SHELL_ID = "wiim_uuid:FF31F09E-2640-A6A5-F8D3-1316FF31F09E"
DLNA_ID = "uuid:FF31F09E-2640-A6A5-F8D3-1316FF31F09E"


def _mock_mass() -> MagicMock:
    """Return a mocked MA instance with a real player controller."""
    mass = MagicMock()
    mass.closing = False
    mass.loop = None
    mass.config.get = MagicMock(return_value=[])
    mass.config.get_raw_player_config_value = MagicMock(
        side_effect=lambda _pid, _key, default=None: default
    )
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    mass.config.get_base_player_config.return_value = create_mock_config("Bedroom Speaker")
    mass.get_providers = MagicMock(return_value=[])
    mass.get_providers_supporting_feature = MagicMock(return_value=[])
    mass.player_queues.get = MagicMock(return_value=None)
    mass.streams.base_url = "http://192.168.1.2:8097"
    mass.players = PlayerController(mass)
    return mass


def _mock_device(transport_state: TransportState) -> MagicMock:
    """Return a mocked DLNA device that renders its own Spotify Connect session."""
    device = MagicMock()
    device.profile_device.available = True
    device.name = "Bedroom Speaker"
    device.volume_level = 0.3
    device.is_volume_muted = False
    device.transport_state = transport_state
    device.current_track_uri = "spotify:track:4uLU6hMCjMI75M1A2tKUQC"
    device.media_title = "Some Song"
    device.media_artist = "Some Artist"
    device.media_album_name = "Some Album"
    device.media_image_url = None
    device.media_duration = 200
    device.media_position = 30
    device.media_position_updated_at = None
    device.has_play_media = True
    device.has_pause = True
    device.can_pause = True
    device.has_next = True
    device.has_previous = True
    device.has_seek_rel_time = True
    device.async_pause = AsyncMock()
    device.async_play = AsyncMock()
    device.async_stop = AsyncMock()
    device.async_next = AsyncMock()
    return device


async def _setup(transport_state: TransportState) -> tuple[PlayerController, MagicMock]:
    """Register a LinkPlay shell backed by a DLNA player that plays Spotify Connect."""
    mass = _mock_mass()
    device = _mock_device(transport_state)
    dlna = DLNAPlayer(
        MockProvider("dlna", instance_id="dlna", mass=mass),  # type: ignore[arg-type]
        DLNA_ID,
        "http://192.168.178.144/description.xml",
        device=device,
    )
    dlna.set_static_attributes()
    await dlna.set_dynamic_attributes()
    dlna.get_config_value = MagicMock(return_value=False)  # type: ignore[method-assign]

    wiim_prov = MagicMock(instance_id="wiim", domain="wiim", mass=mass)
    wiim_prov.native_groups.role_of.return_value = NativeGroupRole.STANDALONE
    wiim_prov.native_groups.members_of.return_value = []
    wiim_prov.native_groups.can_group_with.return_value = set()
    wiim_prov.native_groups.is_unknown_leader_follower.return_value = False
    upnp = MagicMock(friendly_name="Bedroom Speaker", model_name="Up2Stream AMP2.0 V4")
    shell = LinkPlayPlayer(
        wiim_prov,
        SHELL_ID,
        MagicMock(host="192.168.178.144"),
        upnp,
        "http://192.168.178.144/description.xml",
        device_info=MagicMock(),
    )
    shell.set_linked_output_protocols([LinkedOutputProtocol(DLNA_ID, "dlna", priority=50)])

    controller = mass.players
    controller._players = {SHELL_ID: shell, DLNA_ID: dlna}
    dlna.set_protocol_parent_id(SHELL_ID)
    for player in (dlna, shell):
        player.set_initialized()
        player.update_state(signal_event=False)
    shell.refresh_state(signal_event=False)
    return controller, device


async def test_pause_pauses_the_session() -> None:
    """Pausing the shell pauses the Spotify Connect session instead of stopping it."""
    controller, device = await _setup(TransportState.PLAYING)
    controller._handle_cmd_stop = AsyncMock()  # type: ignore[method-assign]

    await controller._handle_cmd_pause(SHELL_ID)

    controller._handle_cmd_stop.assert_not_awaited()
    device.async_pause.assert_awaited_once()


async def test_play_resumes_the_paused_session() -> None:
    """Playing the paused shell resumes the Spotify Connect session on the device."""
    controller, device = await _setup(TransportState.PAUSED_PLAYBACK)
    assert controller.get_player(SHELL_ID).state.playback_state == PlaybackState.PAUSED  # type: ignore[union-attr]

    await controller._handle_cmd_play(SHELL_ID)

    device.async_play.assert_awaited_once()


async def test_next_track_skips_within_the_session() -> None:
    """Skipping on the shell sends Next to the device rendering the Spotify Connect session."""
    controller, device = await _setup(TransportState.PLAYING)
    shell = controller.get_player(SHELL_ID)
    assert shell is not None
    assert PlayerFeature.NEXT_PREVIOUS in shell.state.supported_features

    await controller.cmd_next_track(SHELL_ID)

    device.async_next.assert_awaited_once()
