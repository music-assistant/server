"""Tests for a generic LinkPlay device on a running server: its control protocol and its outputs."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import (
    IdentifierType,
    PlaybackState,
    PlayerFeature,
    ProtocolRole,
)
from music_assistant_models.player import PlayerSource
from pywiim import WiiMError

from music_assistant.controllers.players.protocol_linking import PLAYER_ID_PREFIX
from music_assistant.providers.wiim.constants import PLAYER_ID_PREFIX as WIIM_PREFIX
from music_assistant.providers.wiim.grouping import NativeGroupCoordinator
from music_assistant.providers.wiim.linkplay_player import LinkPlayPlayer

if TYPE_CHECKING:
    from music_assistant.models.player import Player
    from tests.scenarios.harness import FakePlayerProvider, FakeProtocol, Scenario

DEN_UDN = "uuid:FF31F09E-2640-A6A5-F8D3-1316FF31F09E"
DEN_MAC = "AA:BB:CC:DD:EE:01"
DEN_IP = "192.168.1.50"
# a peer whose 24-char HTTP slave uuid maps to its UDN
KITCHEN_UDN = "uuid:A1B2C3D4-E5F6-A7B8-C9D0-E1F2A1B2C3D4"
KITCHEN_HTTP_UUID = "A1B2C3D4E5F6A7B8C9D0E1F2"
KITCHEN_MAC = "AA:BB:CC:DD:EE:02"
KITCHEN_IP = "192.168.1.51"


async def _providers(scenario: Scenario) -> tuple[FakePlayerProvider, FakePlayerProvider]:
    """Load the wiim provider with its grouping coordinator, and a DLNA provider."""
    wiim = await scenario.add_provider("wiim")
    # the real coordinator, over the protocols the fake wiim provider holds
    wiim.native_groups = NativeGroupCoordinator(wiim)  # type: ignore[attr-defined,arg-type]
    dlna = await scenario.add_provider("dlna")
    return wiim, dlna


def _coordinator(wiim: FakePlayerProvider) -> NativeGroupCoordinator:
    """Return the grouping coordinator of the wiim provider."""
    coordinator: NativeGroupCoordinator = wiim.native_groups  # type: ignore[attr-defined]
    return coordinator


def _active_output(player: Player) -> str | None:
    """Return the active output protocol of a player."""
    return player.active_output_protocol


def _client(ip_address: str) -> MagicMock:
    """Return a LinkPlay HTTP client of a reachable, standalone, router-based device."""
    client = MagicMock(host=ip_address)
    client.get_device_info_model = AsyncMock(
        return_value=SimpleNamespace(wmrm_version="4.2", needs_wifi_direct_multiroom=False)
    )
    client.get_device_group_info = AsyncMock(return_value=SimpleNamespace(role="solo"))
    client.get_slaves_info = AsyncMock(return_value=[])
    client.capabilities = {}
    return client


async def _linkplay_device(
    scenario: Scenario,
    wiim: FakePlayerProvider,
    dlna: FakePlayerProvider,
    *,
    udn: str = DEN_UDN,
    mac: str = DEN_MAC,
    ip_address: str = DEN_IP,
    name: str = "Den",
) -> tuple[LinkPlayPlayer, FakeProtocol]:
    """Attach the LinkPlay control protocol and the DLNA renderer of one device."""
    client = _client(ip_address)
    upnp_device = MagicMock(friendly_name=name, model_name="Up2Stream", manufacturer="Arylic")
    linkplay = LinkPlayPlayer(
        wiim,  # type: ignore[arg-type]
        f"{WIIM_PREFIX}{udn}",
        client,
        upnp_device,
        f"http://{ip_address}:49152/description.xml",
        mac_address=mac,
        device_info=client.get_device_info_model.return_value,
    )
    await scenario.mass.players.register(linkplay)
    await scenario.settle()
    renderer = dlna.protocol(
        udn,
        name,
        role="audio",
        identifiers={IdentifierType.MAC_ADDRESS: mac, IdentifierType.IP_ADDRESS: ip_address},
        volume_level=30,
    )
    await scenario.register(renderer)
    await scenario.idle()
    return linkplay, renderer


def _player(linkplay: LinkPlayPlayer) -> Player:
    """Return the player the LinkPlay protocol is attached to."""
    assert linkplay.player is not None
    return linkplay.player


async def test_the_device_is_one_player_led_by_its_control_protocol(scenario: Scenario) -> None:
    """LinkPlay and DLNA of one device are one player, with a minted id, LinkPlay in control."""
    wiim, dlna = await _providers(scenario)
    linkplay, renderer = await _linkplay_device(scenario, wiim, dlna)

    player = _player(linkplay)
    assert renderer.player is player
    assert player.player_id.startswith(PLAYER_ID_PREFIX)
    assert player.get_role(linkplay.protocol_id) == ProtocolRole.CONTROL
    assert player.own_protocol is linkplay
    # volume comes from the renderer, LinkPlay reports none of its own
    assert player.state.volume_level == 30
    assert [visible.provider for visible in scenario.visible_players()] == ["wiim"]


async def test_playback_goes_to_dlna_before_a_higher_priority_output(scenario: Scenario) -> None:
    """Without a stored preference the device plays through its DLNA output, not AirPlay."""
    wiim, dlna = await _providers(scenario)
    linkplay, renderer = await _linkplay_device(scenario, wiim, dlna)
    airplay = (await scenario.add_provider("airplay")).protocol(
        "AP01",
        "Den",
        role="audio",
        identifiers={IdentifierType.MAC_ADDRESS: DEN_MAC, IdentifierType.IP_ADDRESS: DEN_IP},
    )
    await scenario.register(airplay)
    player = _player(linkplay)
    assert airplay.player is player

    await scenario.play(player.player_id)

    assert player.active_output_protocol == renderer.protocol_id
    assert [command[0] for command in renderer.commands] == ["play_media"]
    assert airplay.commands == []


async def test_a_native_follower_drops_its_active_output(scenario: Scenario) -> None:
    """A device that joins a native LinkPlay group stops playing through its own DLNA output."""
    wiim, dlna = await _providers(scenario)
    leader, _ = await _linkplay_device(scenario, wiim, dlna)
    follower, follower_renderer = await _linkplay_device(
        scenario,
        wiim,
        dlna,
        udn=KITCHEN_UDN,
        mac=KITCHEN_MAC,
        ip_address=KITCHEN_IP,
        name="Kitchen",
    )
    follower_player = _player(follower)
    await scenario.play(follower_player.player_id)
    assert _active_output(follower_player) == follower_renderer.protocol_id

    _coordinator(wiim).set_leader_slaves(leader.protocol_id, [KITCHEN_HTTP_UUID])
    await _coordinator(wiim).reconcile()
    await scenario.idle()

    assert follower.synced_to == leader.protocol_id
    assert _active_output(follower_player) is None
    assert scenario.player(follower_player.player_id).synced_to == _player(leader).player_id  # type: ignore[union-attr]

    # leaving the group does not bring the output back: the next playback selects it again
    _coordinator(wiim).set_leader_slaves(leader.protocol_id, [])
    await _coordinator(wiim).reconcile()
    await scenario.idle()

    assert scenario.player(follower_player.player_id).synced_to is None  # type: ignore[union-attr]
    assert _active_output(follower_player) is None


async def test_a_standalone_topology_update_keeps_the_active_output(scenario: Scenario) -> None:
    """A topology update that leaves the device standalone does not touch its output."""
    wiim, dlna = await _providers(scenario)
    linkplay, renderer = await _linkplay_device(scenario, wiim, dlna)
    player = _player(linkplay)
    await scenario.play(player.player_id)

    linkplay.on_native_group_update()
    await scenario.idle()

    assert player.active_output_protocol == renderer.protocol_id


async def test_dlna_keeps_the_device_usable_while_the_linkplay_api_is_down(
    scenario: Scenario,
) -> None:
    """The player stays available through DLNA, which also keeps offering grouping."""
    wiim, dlna = await _providers(scenario)
    linkplay, renderer = await _linkplay_device(scenario, wiim, dlna)
    renderer._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
    linkplay._client.get_device_info_model = AsyncMock(  # type: ignore[method-assign]
        side_effect=WiiMError("unreachable")
    )

    await linkplay.poll()
    await scenario.idle()

    player = _player(linkplay)
    assert linkplay.available is False
    assert PlayerFeature.SET_MEMBERS not in linkplay.supported_features
    assert player.state.available is True
    assert player.available_for_playback is True
    assert PlayerFeature.SET_MEMBERS in player.state.supported_features


async def test_a_follower_of_an_unknown_leader_withdraws_all_grouping(
    scenario: Scenario,
) -> None:
    """A device in a group whose leader the server does not know offers no grouping at all."""
    wiim, dlna = await _providers(scenario)
    linkplay, renderer = await _linkplay_device(scenario, wiim, dlna)
    renderer._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
    player = _player(linkplay)
    assert PlayerFeature.SET_MEMBERS in player.state.supported_features

    _coordinator(wiim).set_self_role(linkplay.protocol_id, True)
    await _coordinator(wiim).reconcile()
    await scenario.idle()

    assert linkplay.grouping_locked is True
    assert PlayerFeature.SET_MEMBERS not in player.state.supported_features


async def test_transport_commands_reach_an_app_playing_on_the_renderer(
    scenario: Scenario,
) -> None:
    """Pause, play and next of the player control a Spotify Connect session on the renderer."""
    wiim, dlna = await _providers(scenario)
    linkplay, renderer = await _linkplay_device(scenario, wiim, dlna)
    renderer._attr_supported_features.add(PlayerFeature.NEXT_PREVIOUS)
    spotify = PlayerSource(
        id="spotify", name="Spotify", passive=True, can_play_pause=True, can_next_previous=True
    )
    await scenario.report(
        renderer,
        active_source="spotify",
        source_list=[spotify],
        playback_state=PlaybackState.PLAYING,
    )
    player = _player(linkplay)
    assert player.state.active_source == "spotify"
    assert PlayerFeature.NEXT_PREVIOUS in player.state.supported_features

    await scenario.mass.players.cmd_pause(player.player_id)
    await scenario.idle()
    assert player.state.playback_state == PlaybackState.PAUSED
    await scenario.mass.players.cmd_play(player.player_id)
    await scenario.mass.players.cmd_next_track(player.player_id)
    await scenario.idle()

    assert [command[0] for command in renderer.commands] == ["pause", "play", "next_track"]
