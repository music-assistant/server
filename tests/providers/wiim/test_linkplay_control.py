"""Tests for a generic LinkPlay device on a running server: its control protocol and its outputs."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
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


def _client(ip_address: str, volume: int) -> MagicMock:
    """
    Return a LinkPlay HTTP client of a reachable, standalone, router-based device.

    The level and mute it is sent show in the status it reports, as they do on the device.

    :param ip_address: The IP address of the device.
    :param volume: The level the device reports at first.
    """
    status: dict[str, Any] = {"volume": volume, "mute": False}

    def send_command(command: str) -> dict[str, Any]:
        # the level is the only command sent as a raw LinkPlay command
        status["volume"] = int(command.removeprefix("setPlayerCmd:vol:"))
        return {}

    client = MagicMock(host=ip_address)
    client.get_device_info_model = AsyncMock(
        return_value=SimpleNamespace(wmrm_version="4.2", needs_wifi_direct_multiroom=False)
    )
    client.get_device_group_info = AsyncMock(return_value=SimpleNamespace(role="solo"))
    client.get_slaves_info = AsyncMock(return_value=[])
    client.get_player_status = AsyncMock(return_value=status)
    client.send_command = AsyncMock(side_effect=send_command)
    client.set_mute = AsyncMock(side_effect=lambda muted: status.update(mute=muted))
    client.capabilities = {}
    return client


def _client_of(linkplay: LinkPlayPlayer) -> MagicMock:
    """Return the LinkPlay HTTP client of a device."""
    return cast("MagicMock", linkplay._client)


async def _linkplay_device(
    scenario: Scenario,
    wiim: FakePlayerProvider,
    dlna: FakePlayerProvider,
    *,
    udn: str = DEN_UDN,
    mac: str = DEN_MAC,
    ip_address: str = DEN_IP,
    name: str = "Den",
    volume: int = 20,
) -> tuple[LinkPlayPlayer, FakeProtocol]:
    """Attach the LinkPlay control protocol and the DLNA renderer of one device."""
    client = _client(ip_address, volume)
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
    await linkplay.poll()
    await scenario.idle()

    player = _player(linkplay)
    assert renderer.player is player
    assert player.player_id.startswith(PLAYER_ID_PREFIX)
    assert player.get_role(linkplay.protocol_id) == ProtocolRole.CONTROL
    assert player.own_protocol is linkplay
    # the level is the one LinkPlay reports, not the one of the renderer (30)
    assert player.volume_level == 20
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
    assert player.available is True
    assert player.available_for_playback is True
    assert PlayerFeature.SET_MEMBERS in player.supported_features


async def test_a_follower_of_an_unknown_leader_withdraws_all_grouping(
    scenario: Scenario,
) -> None:
    """A device in a group whose leader the server does not know offers no grouping at all."""
    wiim, dlna = await _providers(scenario)
    linkplay, renderer = await _linkplay_device(scenario, wiim, dlna)
    renderer._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
    player = _player(linkplay)
    assert PlayerFeature.SET_MEMBERS in player.supported_features

    _coordinator(wiim).set_self_role(linkplay.protocol_id, True)
    await _coordinator(wiim).reconcile()
    await scenario.idle()

    assert linkplay.grouping_locked is True
    assert PlayerFeature.SET_MEMBERS not in player.supported_features


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
    assert player.active_source == "spotify"
    assert [info.protocol_id for info in player.protocols if info.active] == [renderer.protocol_id]
    assert PlayerFeature.NEXT_PREVIOUS in player.supported_features

    await scenario.mass.players.cmd_pause(player.player_id)
    await scenario.idle()
    assert player.playback_state == PlaybackState.PAUSED
    await scenario.mass.players.cmd_play(player.player_id)
    await scenario.mass.players.cmd_next_track(player.player_id)
    await scenario.idle()

    assert [command[0] for command in renderer.commands] == ["pause", "play", "next_track"]


async def test_volume_and_mute_go_to_the_linkplay_control_while_the_renderer_plays(
    scenario: Scenario,
) -> None:
    """Without a selection volume and mute go to LinkPlay, and only it reports back."""
    wiim, dlna = await _providers(scenario)
    linkplay, renderer = await _linkplay_device(scenario, wiim, dlna)
    player = _player(linkplay)
    await scenario.play(player.player_id)
    assert player.active_output_protocol == renderer.protocol_id

    await scenario.mass.players.cmd_volume_set(player.player_id, 29)
    await scenario.mass.players.cmd_volume_mute(player.player_id, True)
    await scenario.idle()

    _client_of(linkplay).send_command.assert_awaited_once_with("setPlayerCmd:vol:29")
    _client_of(linkplay).set_mute.assert_awaited_once_with(True)
    assert not {"volume_set", "volume_mute"} & {command[0] for command in renderer.commands}
    assert (player.volume_control, player.mute_control) == ("native", "native")
    assert (player.volume_level, player.volume_muted) == (29, True)

    # a level the renderer reports does not move the level of the player
    await scenario.report(renderer, volume_level=80)
    await scenario.idle()
    assert player.volume_level == 29


async def test_group_volume_reaches_each_device_of_a_native_linkplay_group(
    scenario: Scenario,
) -> None:
    """
    A group volume command reaches each device of a native group on its own LinkPlay control.

    The leader counts its own level, and the follower, which plays what its leader plays,
    gets its level on its own control as well.
    """
    wiim, dlna = await _providers(scenario)
    leader, _ = await _linkplay_device(scenario, wiim, dlna, volume=40)
    follower, _ = await _linkplay_device(
        scenario,
        wiim,
        dlna,
        udn=KITCHEN_UDN,
        mac=KITCHEN_MAC,
        ip_address=KITCHEN_IP,
        name="Kitchen",
        volume=20,
    )
    # the devices report the group: the leader lists the follower, the follower follows
    _client_of(leader).get_device_group_info.return_value = SimpleNamespace(role="master")
    _client_of(leader).get_slaves_info.return_value = [
        {"uuid": KITCHEN_HTTP_UUID, "ip": KITCHEN_IP}
    ]
    _client_of(follower).get_device_group_info.return_value = SimpleNamespace(role="slave")
    await leader.poll()
    await follower.poll()
    await scenario.idle()
    leader_player, follower_player = _player(leader), _player(follower)
    assert follower_player.synced_to == leader_player.player_id
    assert leader_player.group_volume == 40

    await scenario.mass.players.cmd_group_volume(leader_player.player_id, 70)
    await scenario.idle()

    # each turned up half way from its own level to 100
    _client_of(leader).send_command.assert_awaited_once_with("setPlayerCmd:vol:70")
    _client_of(follower).send_command.assert_awaited_once_with("setPlayerCmd:vol:60")
    assert (leader_player.volume_level, follower_player.volume_level) == (70, 60)
