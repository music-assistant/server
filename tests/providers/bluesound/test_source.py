"""Tests for external BluOS playback sources that have no input ID."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import PlaybackState
from pyblu.parse import parse_inputs, parse_presets

from music_assistant.providers.bluesound.const import PLAYER_SOURCE_MAP
from music_assistant.providers.bluesound.player import BluesoundPlayer
from tests.common import MockProvider, attach_mock, mock_player_mass

if TYPE_CHECKING:
    from pyblu import Input

# play urls of an optical and an HDMI ARC input, as BluOS reports them
OPTICAL_URL = "Capture:hw:imxspdif,0/1/25/2?id=input1"
HDMI_ARC_URL = "Capture:hw:2,0/1/25/2"


def _player(input_id: str | None = None, service: str | None = "LocalMusic") -> MagicMock:
    """Create a player status without contacting a device."""
    fake = MagicMock()
    fake.mass.streams.base_url = "http://mass.example:8097"
    fake.client.base_url = "http://player.example:11000"
    fake.status.input_id = input_id
    fake.status.service = service
    fake.status.stream_url = None
    fake.status.name = "Kashmir"
    fake.status.artist = "Led Zeppelin"
    fake.status.album = "Mothership"
    fake.status.image = None
    fake.status.total_seconds = 508
    fake.status.can_seek = True
    fake._attr_source_list = []
    fake._attr_active_source = None
    fake._input_source_ids = {}
    return fake


@pytest.mark.parametrize(
    ("input_id", "service", "expected"),
    [
        (None, "LocalMusic", "LocalMusic"),
        (None, "UnknownService", "UnknownService"),
        ("custom-input", "LocalMusic", "custom-input"),
        (None, None, "bluos_external"),
    ],
)
def test_external_source_has_stable_identifier(
    input_id: str | None, service: str | None, expected: str
) -> None:
    """Library and unknown services must not create a source with a null identifier."""
    fake = _player(input_id, service)

    BluesoundPlayer._resolve_source(cast("BluesoundPlayer", fake))

    assert fake._attr_active_source == expected
    source = fake._attr_source_list[-1]
    assert source.id == expected
    assert source.name == expected
    assert source.passive is True


@pytest.mark.parametrize("service", list(PLAYER_SOURCE_MAP))
def test_known_service_keeps_its_source(service: str) -> None:
    """Known services retain their existing source definitions."""
    fake = _player(service=service)

    BluesoundPlayer._resolve_source(cast("BluesoundPlayer", fake))

    assert fake._attr_active_source == service
    assert fake._attr_source_list == [PLAYER_SOURCE_MAP[service]]


def test_library_track_change_keeps_source_and_updates_metadata() -> None:
    """A new track changes the media without changing the library source identifier."""
    fake = _player()
    player = cast("BluesoundPlayer", fake)
    BluesoundPlayer._resolve_source(player)
    BluesoundPlayer._resolve_media(player)
    assert fake._attr_current_media.title == "Kashmir"
    assert fake._attr_current_media.artist == "Led Zeppelin"
    assert fake._attr_current_media.album == "Mothership"

    fake.status.name = "Teardrop"
    fake.status.artist = "Massive Attack"
    fake.status.album = "Collected"
    fake._attr_source_list = []
    BluesoundPlayer._resolve_source(player)
    BluesoundPlayer._resolve_media(player)

    assert fake._attr_active_source == "LocalMusic"
    assert fake._attr_source_list[-1].id == "LocalMusic"
    assert fake._attr_current_media.title == "Teardrop"
    assert fake._attr_current_media.artist == "Massive Attack"
    assert fake._attr_current_media.album == "Collected"


def test_ma_stream_is_not_classified_as_external() -> None:
    """An MA stream remains managed by the MA queue even if a service is reported."""
    fake = _player()
    fake.status.stream_url = "http://mass.example:8097/flow/test.flac"

    BluesoundPlayer._resolve_source(cast("BluesoundPlayer", fake))

    assert fake._attr_active_source is None
    assert fake._attr_source_list == []


@pytest.mark.parametrize("own_stream", [False, True], ids=["library", "ma_stream"])
def test_published_source_after_ma_to_library_switch(own_stream: bool) -> None:
    """Publish library metadata despite the remembered MA queue, but trust no MA stream."""
    mass = mock_player_mass()
    mass.streams.base_url = "http://mass.example:8097"
    player, mock_protocol = attach_mock(
        MockProvider("bluesound", mass=mass), "bluesound_player", "BluOS"
    )
    protocol = cast("BluesoundPlayer", mock_protocol)
    protocol._attr_trusts_reported_source = BluesoundPlayer._attr_trusts_reported_source
    protocol._attr_playback_state = PlaybackState.PLAYING
    player.set_active_mass_source(player.player_id)
    player.update_state(signal_event=False)
    assert player.active_source == player.player_id

    status = _player().status
    if own_stream:
        status.stream_url = "http://mass.example:8097/flow/test.flac"
    protocol.status = status
    protocol.client = MagicMock(base_url="http://player.example:11000")
    protocol._input_source_ids = {}
    BluesoundPlayer._resolve_source(protocol)
    BluesoundPlayer._resolve_media(protocol)
    player.update_state(signal_event=False)

    assert player.active_source == (player.player_id if own_stream else "LocalMusic")
    if not own_stream:
        assert player.current_media is not None
        assert player.current_media.title == "Kashmir"
        assert player.current_media.artist == "Led Zeppelin"
        assert player.current_media.album == "Mothership"


async def test_a_grouped_zone_reports_the_player_of_its_leader_as_source() -> None:
    """A zone that follows a leader names the leader's player, not its protocol, as source."""
    mass = mock_player_mass()
    leader_player, _ = attach_mock(MockProvider("bluesound", mass=mass), "leader_zone", "BluOS")
    fake = MagicMock()
    fake.mass = mass
    fake.dynamic_poll_count = 0
    fake.provider.player_map = {("192.168.1.62", 11000): "leader_zone"}
    fake.client.status = AsyncMock(return_value=MagicMock(input_id=None, mute=False))
    leader = MagicMock(ip="192.168.1.62", port=11000)
    fake.client.sync_status = AsyncMock(return_value=MagicMock(leader=leader, volume=20))
    fake._get_bluesound_sources = AsyncMock(return_value=[])

    await BluesoundPlayer.update_attributes(cast("BluesoundPlayer", fake))

    assert leader_player.player_id != "leader_zone"
    assert fake._attr_active_source == leader_player.player_id


async def test_only_the_captured_inputs_of_the_player_are_marked_as_inputs() -> None:
    """The input list also holds Bluetooth and services, which are no inputs, like presets."""
    fake = MagicMock()
    fake.client.presets = AsyncMock(return_value=[SimpleNamespace(id=1, name="Radio")])
    fake.client.inputs = AsyncMock(return_value=_inputs())

    sources = await BluesoundPlayer._get_bluesound_sources(cast("BluesoundPlayer", fake))

    assert {x.name: x.is_input for x in sources} == {
        "Preset 01: Radio": False,
        "Input: Bluetooth": False,
        "Input: Analog Input": True,
        "Input: Turntable": True,
        "Input: HDMI ARC": True,
        "Input: Spotify": False,
        "Input: Spotify Connect": False,
    }


@pytest.mark.parametrize(
    ("input_id", "name", "url"),
    [
        ("input1", "Turntable", OPTICAL_URL),
        ("input4", "HDMI ARC", HDMI_ARC_URL),
    ],
)
async def test_a_playing_input_keeps_its_entry(input_id: str, name: str, url: str) -> None:
    """An input of the list, also a renamed one, is the active source with its own entry."""
    fake = _player(input_id=input_id, service="Capture")
    fake.status.name = name
    fake.client.presets = AsyncMock(return_value=[])
    fake.client.inputs = AsyncMock(return_value=_inputs())
    player = cast("BluesoundPlayer", fake)
    fake._attr_source_list = await BluesoundPlayer._get_bluesound_sources(player)

    BluesoundPlayer._resolve_source(player)

    assert fake._attr_active_source == f"input-{url}"
    inputs = [x for x in fake._attr_source_list if x.is_input]
    assert [x.id for x in inputs].count(f"input-{url}") == 1
    assert len(inputs) == 3


@pytest.mark.parametrize("input_id", ["analog", None], ids=["unknown_id", "no_id"])
async def test_a_playing_input_is_found_by_its_url(input_id: str | None) -> None:
    """An input the status names by no known id is matched by the url the player streams."""
    fake = _player(input_id=input_id, service="Capture")
    fake.status.name = "Analog Input"
    fake.status.stream_url = "Capture:plughw:0,0"
    fake.client.presets = AsyncMock(return_value=[])
    fake.client.inputs = AsyncMock(
        return_value=parse_inputs(
            b'<radiotime service="Capture"><item text="Analog Input" URL="Capture%3Aplughw%3A0%2C0"'
            b' image="/images/capture/ic_analoginput.png" type="audio"/></radiotime>'
        )
    )
    player = cast("BluesoundPlayer", fake)
    fake._attr_source_list = await BluesoundPlayer._get_bluesound_sources(player)

    BluesoundPlayer._resolve_source(player)

    assert fake._attr_active_source == "input-Capture:plughw:0,0"
    assert [x.id for x in fake._attr_source_list] == ["input-Capture:plughw:0,0"]


def _inputs() -> list[Input]:
    """Return the inputs of a player in the forms BluOS reports them."""
    return parse_inputs(
        b'<radiotime service="Capture">'
        b'<item text="Bluetooth" inputType="bluetooth" id="input2" URL="Capture%3Abluez%3Abluetooth"'
        b' image="/images/BluetoothIcon.png" type="audio"/>'
        b'<item text="Analog Input" inputType="analog" id="input0"'
        b' URL="Capture%3Aplughw%3Aimxnadadc%2C0%2F48000%2F24%2F2%3Fid%3Dinput0"'
        b' image="/images/capture/ic_analoginput.png" type="audio"/>'
        # an optical input the user renamed
        b'<item text="Turntable" inputType="spdif" id="input1"'
        b' URL="Capture%3Ahw%3Aimxspdif%2C0%2F1%2F25%2F2%3Fid%3Dinput1"'
        b' image="/images/capture/ic_opticalinput.png" type="audio"/>'
        b'<item text="HDMI ARC" inputType="arc" id="input4" URL="Capture%3Ahw%3A2%2C0%2F1%2F25%2F2"'
        b' image="/images/capture/ic_tv.png" type="audio"/>'
        b'<item text="Spotify" id="Spotify" URL="Spotify%3Aplay"'
        b' image="/Sources/images/SpotifyIcon.png" serviceType="CloudService" type="audio"/>'
        # the form older BluOS versions report Spotify in
        b'<item text="Spotify Connect" id="spotify" URL="Capture%3Aspotify%3Aplay"'
        b' image="/Sources/images/SpotifyIcon.png" type="audio"/>'
        b"</radiotime>"
    )


@pytest.mark.parametrize(
    ("name", "is_input"),
    [("Analog Input", True), ("Digital Input 1", True), ("HDMI ARC", True), ("Bluetooth", False)],
)
def test_a_playing_input_is_marked_as_input(name: str, is_input: bool) -> None:
    """The entry of an input the player plays is an input, Bluetooth is not."""
    fake = _player(service=None)
    fake.status.name = name

    BluesoundPlayer._resolve_source(cast("BluesoundPlayer", fake))

    assert fake._attr_source_list[-1].is_input is is_input


async def test_an_empty_preset_list_leaves_the_inputs_as_sources() -> None:
    """The secondary of a stereo pair answers its presets with an empty body, which is no error."""
    fake = MagicMock()
    fake.client.presets = AsyncMock(side_effect=lambda **_: parse_presets(b""))
    fake.client.inputs = AsyncMock(return_value=[MagicMock(url="Capture:hw:1", text="Optical")])

    sources = await BluesoundPlayer._get_bluesound_sources(cast("BluesoundPlayer", fake))

    assert [source.id for source in sources] == ["input-Capture:hw:1"]
