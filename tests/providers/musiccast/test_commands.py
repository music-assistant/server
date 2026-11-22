"""Tests for the translation of player commands into MusicCast and UPnP requests."""

import time
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.musiccast.player import UpnpUpdateHelper

from .conftest import FakeAVTransport, MusicCastNetwork, add_capabilities


@pytest.mark.parametrize(
    ("command", "source", "expected_upnp", "expected_device"),
    [
        pytest.param("play", "music_assistant", ["play"], [], id="play-ma"),
        pytest.param("pause", "music_assistant", ["stop"], [], id="pause-ma-stops"),
        pytest.param("stop", "music_assistant", ["stop"], [], id="stop-ma"),
        pytest.param("next_track", "music_assistant", ["next"], [], id="next-ma"),
        pytest.param("previous_track", "music_assistant", ["previous"], [], id="previous-ma"),
        pytest.param("play", "spotify", [], ["netusb_play"], id="play-net"),
        pytest.param("pause", "spotify", [], ["netusb_pause"], id="pause-net"),
        pytest.param("stop", "spotify", [], ["netusb_stop"], id="stop-net"),
        pytest.param("next_track", "spotify", [], ["netusb_next_track"], id="next-net"),
        pytest.param("previous_track", "spotify", [], ["netusb_previous_track"], id="previous-net"),
        pytest.param("next_track", "tuner", [], ["tuner_next_station"], id="next-tuner"),
        pytest.param(
            "previous_track", "tuner", [], ["tuner_previous_station"], id="previous-tuner"
        ),
        pytest.param("pause", "tuner", [], [], id="pause-tuner"),
        pytest.param("play", "audio1", [], [], id="play-input"),
        pytest.param("next_track", "audio1", [], [], id="next-input"),
    ],
)
async def test_transport_command_goes_to_what_plays(
    network: MusicCastNetwork,
    avt: FakeAVTransport,
    command: str,
    source: str,
    expected_upnp: list[str],
    expected_device: list[str],
) -> None:
    """A stream of MA is controlled over UPnP, a net source or tuner over the device API."""
    player = network.player("A___main")
    data = player.physical_device.device.data
    data.netusb_input = "spotify"
    data.zones["main"].input = "server" if source == "music_assistant" else source
    player.upnp_update_helper = UpnpUpdateHelper(
        last_poll=time.time(),
        controlled_by_mass=source == "music_assistant",
        current_uri="http://ma:8097/single/session/pl_living/item/A___main.flac",
    )

    await getattr(player, command)()

    assert avt.requests == expected_upnp
    assert player.physical_device.device.commands == expected_device


@pytest.mark.parametrize(
    ("zone2_power", "expected_zone2_input"),
    [
        pytest.param("on", "audio1", id="powered-zone-hands-over-net"),
        pytest.param("standby", "server", id="zone-in-standby-left-alone"),
    ],
)
async def test_play_media_takes_net_from_other_zone(
    network: MusicCastNetwork, avt: FakeAVTransport, zone2_power: str, expected_zone2_input: str
) -> None:
    """Playing on a zone moves a powered other zone off the net input the device has once."""
    url = "http://ma:8097/single/session/pl_living/item/A___main.flac"
    network.mass.streams.resolve_stream_url = AsyncMock(return_value=url)
    player = network.player("A___main")
    zones = player.physical_device.device.data.zones
    zones["zone2"].input = "server"
    zones["zone2"].power = zone2_power

    await player.play_media(PlayerMedia(uri="pl_living_item_1"))

    assert zones["zone2"].input == expected_zone2_input
    assert zones["main"].input == "server"
    # a stop first, so a play right after another does not wedge the renderer
    assert avt.requests == ["stop", f"set_url {url}", "play"]
    assert player.upnp_update_helper is not None
    assert player.upnp_update_helper.controlled_by_mass


@pytest.mark.parametrize(
    ("option_key", "option_value", "expected_value"),
    [
        pytest.param("zone_ENHANCER", False, False, id="switch"),
        pytest.param("zone_TONE_CONTROL_bass", 4, 4, id="number"),
        pytest.param("zone_TONE_CONTROL_bass", 8, 0, id="number-out-of-range"),
        pytest.param("zone_SLEEP", "30", 30, id="option-with-number-key"),
        pytest.param("zone_SLEEP", "45", 0, id="unknown-option"),
        pytest.param("zone_PARTY_MODE", True, False, id="read-only"),
    ],
)
async def test_set_option_checks_the_value(
    network: MusicCastNetwork,
    option_key: str,
    option_value: bool | int | str,
    expected_value: bool | int,
) -> None:
    """A valid value reaches the receiver, an invalid one or a read-only option changes nothing."""
    player = network.player("A___main")
    values = add_capabilities(player.zone_device.zone_data)

    await player.set_option(option_key, option_value)

    assert values[option_key] == expected_value
