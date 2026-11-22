"""Tests for the MusicCast player state updates and polling."""

import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientError
from music_assistant_models.enums import PlaybackState, PlayerFeature
from music_assistant_models.player import PlayerOptionType

from music_assistant.controllers.players.power import _CONTINUES_PLAYBACK
from music_assistant.helpers.upnp import AVTransport
from music_assistant.providers.musiccast.constants import CONF_PLAYER_AUTO_ADVANCE
from music_assistant.providers.musiccast.player import (
    MusicCastPlayer,
    UpnpUpdateHelper,
    get_player_option_translation_key,
)
from tests.common import bare_player_controller

from .conftest import FakeAVTransport, MusicCastNetwork, add_capabilities, link


@pytest.mark.parametrize(
    ("min_volume", "max_volume", "current_volume", "expected_volume"),
    [
        pytest.param(0, 100, 40, 40, id="plain"),
        pytest.param(0, 161, 80, 50, id="rounded"),
        pytest.param(10, 110, 60, 50, id="min-volume-offset"),
    ],
)
async def test_update_without_upnp_media_info(
    network: MusicCastNetwork,
    monkeypatch: pytest.MonkeyPatch,
    min_volume: int,
    max_volume: int,
    current_volume: int,
    expected_volume: int,
) -> None:
    """A failing UPnP media info request still publishes the volume from the device data."""
    monkeypatch.setattr(
        AVTransport, "get_media_info", AsyncMock(side_effect=ClientError("timeout"))
    )
    player = network.player("A___main")
    zone_data = player.zone_device.zone_data
    zone_data.min_volume = min_volume
    zone_data.max_volume = max_volume
    zone_data.current_volume = current_volume
    player.upnp_update_helper = UpnpUpdateHelper(
        last_poll=time.time() - 10, controlled_by_mass=False, current_uri=None
    )
    update_state = MagicMock()
    monkeypatch.setattr(player, "update_state", update_state)

    await player.set_dynamic_attributes()

    assert player._attr_volume_level == expected_volume
    update_state.assert_called_once()


@pytest.mark.parametrize(
    ("registered_player_ids", "polling_player_id"),
    [
        pytest.param(["A___main", "A___zone2"], "A___main", id="main-polls"),
        pytest.param(["A___zone2"], "A___zone2", id="zone-polls-while-main-disabled"),
    ],
)
async def test_poll_by_first_registered_zone(
    network: MusicCastNetwork,
    monkeypatch: pytest.MonkeyPatch,
    registered_player_ids: list[str],
    polling_player_id: str,
) -> None:
    """Exactly one registered player of a device fetches the device on poll."""
    monkeypatch.setattr(MusicCastPlayer, "set_dynamic_attributes", AsyncMock())
    for player_id in ["A___main", "A___zone2"]:
        if player_id not in registered_player_ids:
            del network.registry[player_id]
    device = network.player("A___main").physical_device.device

    polled_by = []
    for player_id in registered_player_ids:
        fetch_count = device.fetch_count
        await network.player(player_id).poll()
        if device.fetch_count > fetch_count:
            polled_by.append(player_id)

    assert polled_by == [polling_player_id]


async def test_poll_restores_udp_for_reenabled_player(
    network: MusicCastNetwork, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A player re-enabled while UDP was stopped gets UDP back with the next successful poll."""
    monkeypatch.setattr(MusicCastPlayer, "set_dynamic_attributes", AsyncMock())
    player = network.player("A___main")
    device = player.physical_device.device
    # UDP stopped by a connection loss, the re-enabled player starts out available
    device.transport = None
    player._attr_available = True

    await player.poll()

    assert device.transport is not None


def _attach_to_new_player(player: MusicCastPlayer) -> None:
    """Attach a zone to a player with an id of its own, as a zone first seen now gets."""
    player.set_player(MagicMock(player_id="pl_living"))


@pytest.mark.parametrize(
    ("zone_input", "current_uri", "controlled_by_mass", "expected_source"),
    [
        pytest.param(
            "server",
            "http://ma:8097/single/session/pl_living/item/A___main.flac",
            True,
            "pl_living",
            id="stream-of-music-assistant",
        ),
        pytest.param(
            "audio1",
            "http://ma:8097/single/session/pl_living/item/A___main.flac",
            False,
            "audio1",
            id="input-after-music-assistant",
        ),
        pytest.param(
            "server", "http://192.168.1.5/song.mp3", False, "server", id="dlna-of-other-app"
        ),
        pytest.param(
            "server",
            "http://ma:8097/single/session/pl_patio/item/A___zone2.flac",
            False,
            "server",
            id="stream-for-other-zone",
        ),
    ],
)
async def test_control_by_music_assistant_is_read_from_the_upnp_uri(
    network: MusicCastNetwork,
    avt: FakeAVTransport,
    monkeypatch: pytest.MonkeyPatch,
    zone_input: str,
    current_uri: str,
    controlled_by_mass: bool,
    expected_source: str,
) -> None:
    """Only a stream of Music Assistant for the zone on its server input is played by MA."""
    player = network.player("A___main")
    _attach_to_new_player(player)
    player.zone_device.zone_data.input = zone_input
    avt.current_uri = current_uri
    monkeypatch.setattr(player, "update_state", MagicMock())

    await player.set_dynamic_attributes()

    assert player.upnp_update_helper is not None
    assert player.upnp_update_helper.controlled_by_mass is controlled_by_mass
    assert player._attr_active_source == expected_source
    # MA stops instead of pausing, a pause of the receiver is not reliable
    assert (PlayerFeature.PAUSE in player._attr_supported_features) is not controlled_by_mass


@pytest.mark.parametrize(
    ("zone_input", "power", "netusb_playback", "expected_state", "expected_elapsed_time"),
    [
        pytest.param("spotify", "on", "play", PlaybackState.PLAYING, 42, id="net-playing"),
        pytest.param("spotify", "on", "pause", PlaybackState.PAUSED, None, id="net-paused"),
        pytest.param("spotify", "on", "stop", PlaybackState.IDLE, None, id="net-stopped"),
        pytest.param("audio1", "on", "stop", PlaybackState.PLAYING, None, id="input"),
        pytest.param("spotify", "standby", "play", PlaybackState.IDLE, None, id="standby"),
    ],
)
async def test_zone_state_maps_to_playback_state(
    network: MusicCastNetwork,
    monkeypatch: pytest.MonkeyPatch,
    zone_input: str,
    power: str,
    netusb_playback: str,
    expected_state: PlaybackState,
    expected_elapsed_time: int | None,
) -> None:
    """A net source reports its playback and position, a powered input always plays."""
    player = network.player("A___main")
    data = player.physical_device.device.data
    data.netusb_input = "spotify"
    data.netusb_playback = netusb_playback
    data.netusb_play_time = 42
    data.netusb_play_time_updated = datetime(2026, 11, 22, tzinfo=UTC)
    data.zones["main"].input = zone_input
    data.zones["main"].power = power
    player.upnp_update_helper = UpnpUpdateHelper(
        last_poll=time.time(), controlled_by_mass=False, current_uri=None
    )
    monkeypatch.setattr(player, "update_state", MagicMock())

    await player.set_dynamic_attributes()

    assert player._attr_playback_state == expected_state
    assert player._attr_elapsed_time == expected_elapsed_time


@pytest.mark.parametrize(
    ("server_input", "server_controlled_by_mass", "expected_media", "expected_source"),
    [
        pytest.param(
            "server",
            True,
            ("http://ma:8097/single/session/pl_living/item/A___main.flac", None),
            None,
            id="server-plays-music-assistant",
        ),
        pytest.param(
            "spotify",
            False,
            ("A___main_spotify", "Song"),
            "mc_link",
            id="server-plays-own-source",
        ),
    ],
)
async def test_group_client_shows_what_its_server_plays(
    network: MusicCastNetwork,
    monkeypatch: pytest.MonkeyPatch,
    server_input: str,
    server_controlled_by_mass: bool,
    expected_media: tuple[str, str | None],
    expected_source: str | None,
) -> None:
    """A client of a MusicCast group shows the media of its server, not its own."""
    server = network.player("A___main")
    client = network.player("B___main")
    link(server.zone_device, client.zone_device)
    server_data = server.physical_device.device.data
    server_data.netusb_input = server_input
    server_data.netusb_track = "Song"
    server_data.zones["main"].input = server_input
    server.upnp_update_helper = UpnpUpdateHelper(
        last_poll=time.time(),
        controlled_by_mass=server_controlled_by_mass,
        current_uri="http://ma:8097/single/session/pl_living/item/A___main.flac",
    )
    client.upnp_update_helper = UpnpUpdateHelper(
        last_poll=time.time(), controlled_by_mass=False, current_uri=None
    )
    monkeypatch.setattr(client, "update_state", MagicMock())

    await client.set_dynamic_attributes()

    assert client.current_media is not None
    assert (client.current_media.uri, client.current_media.title) == expected_media
    assert client._attr_active_source == expected_source


async def test_capabilities_become_player_options(
    network: MusicCastNetwork, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each capability of the zone is an option of the matching type, sensors read only."""
    player = network.player("A___main")
    add_capabilities(player.zone_device.zone_data)
    player.upnp_update_helper = UpnpUpdateHelper(
        last_poll=time.time(), controlled_by_mass=False, current_uri=None
    )
    monkeypatch.setattr(player, "update_state", MagicMock())

    await player.set_dynamic_attributes()

    options = {x.key: x for x in player._attr_options}
    assert {
        key: (x.translation_key, x.type, x.read_only, x.value) for key, x in options.items()
    } == {
        "zone_PARTY_MODE": ("party_mode", PlayerOptionType.BOOLEAN, True, False),
        "zone_ENHANCER": ("enhancer", PlayerOptionType.BOOLEAN, False, True),
        "zone_DIALOGUE_LEVEL": ("dialogue_level", PlayerOptionType.INTEGER, True, 2),
        "zone_TONE_CONTROL_bass": ("bass", PlayerOptionType.INTEGER, False, 0),
        "zone_SOUND_FORMAT": ("sound_format", PlayerOptionType.STRING, True, "Dolby Atmos"),
        "zone_SLEEP": ("sleep", PlayerOptionType.STRING, False, "0"),
    }
    bass = options["zone_TONE_CONTROL_bass"]
    assert (bass.min_value, bass.max_value, bass.step) == (-6, 6, 1)
    assert options["zone_SLEEP"].options is not None
    assert [(x.key, x.name) for x in options["zone_SLEEP"].options] == [
        ("0", "Off"),
        ("30", "30 min"),
    ]


@pytest.mark.parametrize(
    ("capability_id", "expected_translation_key"),
    [
        pytest.param("zone_TONE_CONTROL_treble", "treble", id="treble"),
        pytest.param("zone_SURR_DECODER_TYPE", "surround_decoder_type", id="surround-decoder"),
    ],
)
def test_capability_names_match_shared_option_names(
    capability_id: str, expected_translation_key: str
) -> None:
    """Capabilities with an option name shared by other players use that name."""
    assert get_player_option_translation_key(capability_id) == expected_translation_key


async def test_end_of_track_advances_the_queue_of_the_player(network: MusicCastNetwork) -> None:
    """A receiver that went idle at the end of a track advances the queue of its player."""
    player = network.player("A___main")
    _attach_to_new_player(player)
    queue = MagicMock(queue_id="pl_living", active=True)
    queue.current_item.duration = 200
    queue.current_item.queue_item_id = "item_1"
    network.mass.player_queues.get.side_effect = {"pl_living": queue}.get
    network.mass.player_queues.next = AsyncMock()
    network.mass.config.get_protocol_value.return_value = True
    player.upnp_update_helper = UpnpUpdateHelper(
        last_poll=time.time(), controlled_by_mass=True, current_uri="http://ma:8097/x.flac"
    )
    player._last_playback_state = PlaybackState.PLAYING
    player._last_playing_elapsed_time = 198
    player._attr_playback_state = PlaybackState.IDLE

    player._maybe_advance_on_track_end()
    await player._advance_queue_after_idle("item_1")

    network.mass.config.get_protocol_value.assert_called_once_with(
        player.protocol_id, CONF_PLAYER_AUTO_ADVANCE, default=True
    )
    network.mass.call_later.assert_called_once()
    network.mass.player_queues.next.assert_awaited_once_with("pl_living")


async def test_end_of_track_advance_switches_no_member_of_a_group_on(
    network: MusicCastNetwork,
) -> None:
    """The advance after an end of track is no start of a group, so it switches nothing on."""
    player = network.player("A___main")
    _attach_to_new_player(player)
    queue = MagicMock(queue_id="pl_living", active=True)
    queue.current_item.queue_item_id = "item_1"
    network.mass.player_queues.get.side_effect = {"pl_living": queue}.get
    network.mass.players = bare_player_controller()
    seen: list[bool] = []

    async def _next(*_args: object) -> None:
        seen.append(_CONTINUES_PLAYBACK.get())

    network.mass.player_queues.next = _next
    player._attr_playback_state = PlaybackState.IDLE

    await player._advance_queue_after_idle("item_1")

    assert seen == [True]


def test_a_musiccast_player_gets_the_gapless_burst(network: MusicCastNetwork) -> None:
    """A MusicCast device only plays a track gapless once it holds a whole opening chunk of it."""
    assert network.player("A___main").needs_gapless_burst


async def test_inputs_of_the_receiver_are_marked_as_inputs(network: MusicCastNetwork) -> None:
    """The inputs of a zone are inputs; what the receiver plays itself or links in is not."""
    player = network.player("A___zone2")
    player.zone_device.zone_data.input_list = [
        "hdmi1",
        "optical",
        "usb_dac",
        "audio_cd",
        "line_cd",
        "usb",
        "bluetooth",
        "spotify",
        "tuner",
        "cd",
        "mc_link",
        "main_sync",
    ]

    await player.set_static_attributes()

    assert {x.id: x.is_input for x in player._attr_source_list} == {
        "hdmi1": True,
        "optical": True,
        "usb_dac": True,
        "audio_cd": True,
        "line_cd": True,
        "usb": False,
        "bluetooth": False,
        "spotify": False,
        "tuner": False,
        "cd": False,
        "mc_link": False,
        "main_sync": False,
    }
