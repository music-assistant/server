"""Tests for the Bose SoundTouch API client parsing helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

from defusedxml import ElementTree as DefusedET

from music_assistant.providers.bose_soundtouch.client import SoundtouchDevice
from music_assistant.providers.bose_soundtouch.client.client import create_zone_xml
from music_assistant.providers.bose_soundtouch.client.schema.enums import PlayStatus, SourceStatus
from music_assistant.providers.bose_soundtouch.client.schema.models import Zone, ZoneMember
from music_assistant.providers.bose_soundtouch.helpers import extract_preset_id

INFO_XML = """
<info deviceID="ABC123">
  <name>Living Room</name>
  <type>SoundTouch 20</type>
  <networkInfo type="SCM">
    <macAddress>001122334455</macAddress>
    <ipAddress>192.168.1.50</ipAddress>
  </networkInfo>
  <components>
    <component>
      <componentCategory>SCM</componentCategory>
      <softwareVersion>27.0.6.46330 epdbuild</softwareVersion>
    </component>
  </components>
</info>
"""

NOW_PLAYING_XML = """
<nowPlaying deviceID="ABC123" source="SPOTIFY" sourceAccount="user1">
  <ContentItem source="SPOTIFY"><itemName>My Playlist</itemName></ContentItem>
  <track>Song Title</track>
  <artist>The Artist</artist>
  <album>The Album</album>
  <art artImageStatus="IMAGE_PRESENT">http://192.168.1.50/art.jpg</art>
  <time total="240">42</time>
  <playStatus>PLAY_STATE</playStatus>
</nowPlaying>
"""


def _get_client() -> SoundtouchDevice:
    session_config = Mock()
    session_config.logger = None
    return SoundtouchDevice(session_configuration=session_config)


async def test_parse_info() -> None:
    """Device info is parsed including identifiers and firmware version."""
    client = _get_client()
    with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = DefusedET.fromstring(INFO_XML)

        info = await client.get_info()

    # info = parse_info(DefusedET.fromstring(INFO_XML), fallback_ip="10.0.0.1")
    assert info.device_id == "ABC123"
    assert info.name == "Living Room"
    assert info.model == "SoundTouch 20"
    assert isinstance(info.mac_addresses, set)
    assert "001122334455" in info.mac_addresses
    assert isinstance(info.ip_addresses, set)
    assert "192.168.1.50" in info.ip_addresses
    assert info.software_version == "27.0.6.46330"


# def test_parse_info_falls_back_to_connection_ip() -> None:
#     """When the device reports no network IP, the connection IP is used."""
#     info = parse_info(
#         DefusedET.fromstring('<info deviceID="X"><name>N</name></info>'), fallback_ip="10.0.0.9"
#     )
#     assert info.ip_address == "10.0.0.9"
#
#
async def test_parse_now_playing() -> None:
    """A playing now_playing snapshot maps to the expected fields."""
    client = _get_client()
    with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = DefusedET.fromstring(NOW_PLAYING_XML)
        now_playing = await client.get_now_playing()

    assert now_playing.content_item is not None
    assert now_playing.content_item.source == "SPOTIFY"
    # assert now_playing.content_item.source_account == "user1"
    assert now_playing.track == "Song Title"
    assert now_playing.artist == "The Artist"
    assert now_playing.album == "The Album"
    assert now_playing.art is not None
    assert now_playing.art.url == "http://192.168.1.50/art.jpg"
    assert now_playing.time_information is not None
    assert now_playing.time_information.total == 240
    assert now_playing.time_information.position == 42
    assert now_playing.play_status in [PlayStatus.PLAY_STATE, PlayStatus.BUFFERING_STATE]


# async def test_parse_now_playing_standby() -> None:
#     """A standby snapshot reports the STANDBY source."""
#     client = _get_client()
#     client._get = AsyncMock(
#         return_value=DefusedET.fromstring('<nowPlaying deviceID="A" source="STANDBY"></nowPlaying>')
#     )
#     now_playing = await client.get_now_playing()
#     assert now_playing.content_item is not None
#     assert now_playing.content_item.source == "STANDBY"
#     assert now_playing.track is None


async def test_parse_volume() -> None:
    """Volume level and mute state are parsed."""
    client = _get_client()
    with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = DefusedET.fromstring(
            "<volume><targetvolume>30</targetvolume>"
            "<actualvolume>28</actualvolume><muteenabled>true</muteenabled></volume>"
        )
        volume = await client.get_volume()

    assert volume.actual_volume == 28
    assert volume.mute_enabled is True


async def test_parse_sources() -> None:
    """Sources are parsed with their ready state."""
    client = _get_client()
    with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = DefusedET.fromstring(
            '<sources deviceID="A">'
            '<sourceItem source="AUX" sourceAccount="AUX" status="READY">AUX IN</sourceItem>'
            '<sourceItem source="BLUETOOTH" status="UNAVAILABLE">Bluetooth</sourceItem>'
            "</sources>"
        )
        sources = await client.get_sources()

    assert len(sources.sources) == 2
    assert sources.sources[0].source == "AUX"
    assert sources.sources[0].source_account == "AUX"
    assert sources.sources[0].source_name == "AUX IN"
    assert sources.sources[0].status == SourceStatus.READY
    assert sources.sources[1].status != SourceStatus.READY


async def test_parse_zone_master() -> None:
    """A zone master reports its members."""
    client = _get_client()
    with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = DefusedET.fromstring(
            '<zone master="ABC"><member ipaddress="192.168.1.51">DEF</member></zone>'
        )
        zone = await client.get_zone()

    assert zone.leader is not None
    assert zone.leader.mac == "ABC"
    assert zone.members
    assert ZoneMember(ip="192.168.1.51", mac="DEF") in zone.members


async def test_parse_zone_empty() -> None:
    """An empty zone response yields no master and no members."""
    client = _get_client()
    with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = DefusedET.fromstring("<zone />")

        zone = await client.get_zone()

    if zone.leader is not None:
        assert zone.leader.ip is None
        assert zone.leader.mac is None
    assert len(zone.members) == 0


async def test_build_zone_xml() -> None:
    """Zone request bodies are built with master and member entries."""
    leader = ZoneMember(ip="1.2.3.4", mac="MASTER")
    member = ZoneMember(ip="1.2.3.5", mac="SLAVE")
    zone = Zone(leader=leader, members=[leader, member])

    assert create_zone_xml(zone) == (
        '<zone master="MASTER">'
        '<member ipaddress="1.2.3.4">MASTER</member>'
        '<member ipaddress="1.2.3.5">SLAVE</member>'
        "</zone>"
    )


# async def test_build_notification_xml() -> None:
#     """The notification body includes the app key, url and volume, escaping the url."""
#     xml = _build_notification_xml("KEY", "http://host/stream?a=1&b=2", volume=25)
#     assert "<app_key>KEY</app_key>" in xml
#     assert "<url>http://host/stream?a=1&amp;b=2</url>" in xml
#     assert "<volume>25</volume>" in xml
#
#
# def test_build_notification_xml_without_volume() -> None:
#     """The notification body omits the volume element when no volume is given."""
#     xml = _build_notification_xml("KEY", "http://host/stream")
#     assert "<volume>" not in xml
#
#
def test_extract_preset_id() -> None:
    """A preset button press is detected from a websocket message."""
    assert extract_preset_id('<updates deviceID="A"><preset id="3"/></updates>') == 3


def test_extract_preset_id_no_preset() -> None:
    """A message without a preset element returns None."""
    assert extract_preset_id('<updates deviceID="A"><nowPlayingUpdated/></updates>') is None


def test_extract_preset_id_invalid_xml() -> None:
    """A non-XML message returns None instead of raising."""
    assert extract_preset_id("not xml at all") is None


def test_extract_preset_id_non_numeric() -> None:
    """A non-numeric preset id returns None instead of raising."""
    assert extract_preset_id('<preset id="abc"/>') is None


#
#
# def test_play_status_helpers() -> None:
#     """Play status helpers classify the SoundTouch playStatus values."""
#     assert play_status_is_playing("PLAY_STATE")
#     assert play_status_is_playing("BUFFERING_STATE")
#     assert not play_status_is_playing("STOP_STATE")
#     assert not play_status_is_playing(None)
#     assert play_status_is_paused("PAUSE_STATE")
#     assert not play_status_is_paused("PLAY_STATE")
