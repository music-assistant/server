"""Tests for the Bose SoundTouch API client parsing helpers."""

from __future__ import annotations

from defusedxml import ElementTree as DefusedET

from music_assistant.providers.bose_soundtouch.client import (
    _build_notification_xml,
    _build_zone_xml,
    extract_preset_id,
    parse_info,
    parse_now_playing,
    parse_sources,
    parse_volume,
    parse_zone,
    play_status_is_paused,
    play_status_is_playing,
)

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


def test_parse_info() -> None:
    """Device info is parsed including identifiers and firmware version."""
    info = parse_info(DefusedET.fromstring(INFO_XML), fallback_ip="10.0.0.1")
    assert info.device_id == "ABC123"
    assert info.name == "Living Room"
    assert info.model == "SoundTouch 20"
    assert info.mac_address == "001122334455"
    assert info.ip_address == "192.168.1.50"
    assert info.software_version == "27.0.6.46330"


def test_parse_info_falls_back_to_connection_ip() -> None:
    """When the device reports no network IP, the connection IP is used."""
    info = parse_info(
        DefusedET.fromstring('<info deviceID="X"><name>N</name></info>'), fallback_ip="10.0.0.9"
    )
    assert info.ip_address == "10.0.0.9"


def test_parse_now_playing() -> None:
    """A playing now_playing snapshot maps to the expected fields."""
    now = parse_now_playing(DefusedET.fromstring(NOW_PLAYING_XML))
    assert now.source == "SPOTIFY"
    assert now.source_account == "user1"
    assert now.title == "Song Title"
    assert now.artist == "The Artist"
    assert now.album == "The Album"
    assert now.image_url == "http://192.168.1.50/art.jpg"
    assert now.duration == 240
    assert now.position == 42
    assert play_status_is_playing(now.play_status)
    assert not play_status_is_paused(now.play_status)


def test_parse_now_playing_standby() -> None:
    """A standby snapshot reports the STANDBY source."""
    now = parse_now_playing(
        DefusedET.fromstring('<nowPlaying deviceID="A" source="STANDBY"></nowPlaying>')
    )
    assert now.source == "STANDBY"
    assert now.title is None


def test_parse_volume() -> None:
    """Volume level and mute state are parsed."""
    volume = parse_volume(
        DefusedET.fromstring(
            "<volume><targetvolume>30</targetvolume>"
            "<actualvolume>28</actualvolume><muteenabled>true</muteenabled></volume>"
        )
    )
    assert volume.level == 28
    assert volume.muted is True


def test_parse_sources() -> None:
    """Sources are parsed with their ready state."""
    sources = parse_sources(
        DefusedET.fromstring(
            '<sources deviceID="A">'
            '<sourceItem source="AUX" sourceAccount="AUX" status="READY">AUX IN</sourceItem>'
            '<sourceItem source="BLUETOOTH" status="UNAVAILABLE">Bluetooth</sourceItem>'
            "</sources>"
        )
    )
    assert len(sources) == 2
    assert sources[0].source == "AUX"
    assert sources[0].source_account == "AUX"
    assert sources[0].name == "AUX IN"
    assert sources[0].ready is True
    assert sources[1].ready is False


def test_parse_zone_master() -> None:
    """A zone master reports its members."""
    zone = parse_zone(
        DefusedET.fromstring(
            '<zone master="ABC"><member ipaddress="192.168.1.51">DEF</member></zone>'
        )
    )
    assert zone.master_id == "ABC"
    assert zone.member_ids == ["DEF"]


def test_parse_zone_empty() -> None:
    """An empty zone response yields no master and no members."""
    zone = parse_zone(DefusedET.fromstring("<zone />"))
    assert zone.master_id is None
    assert zone.member_ids == []


def test_build_zone_xml() -> None:
    """Zone request bodies are built with master and member entries."""
    xml = _build_zone_xml("MASTER", [("MASTER", "1.2.3.4"), ("SLAVE", "1.2.3.5")])
    assert xml == (
        '<zone master="MASTER">'
        '<member ipaddress="1.2.3.4">MASTER</member>'
        '<member ipaddress="1.2.3.5">SLAVE</member>'
        "</zone>"
    )


def test_build_notification_xml() -> None:
    """The notification body includes the app key, url and volume, escaping the url."""
    xml = _build_notification_xml("KEY", "http://host/stream?a=1&b=2", volume=25)
    assert "<app_key>KEY</app_key>" in xml
    assert "<url>http://host/stream?a=1&amp;b=2</url>" in xml
    assert "<volume>25</volume>" in xml


def test_build_notification_xml_without_volume() -> None:
    """The notification body omits the volume element when no volume is given."""
    xml = _build_notification_xml("KEY", "http://host/stream")
    assert "<volume>" not in xml


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


def test_play_status_helpers() -> None:
    """Play status helpers classify the SoundTouch playStatus values."""
    assert play_status_is_playing("PLAY_STATE")
    assert play_status_is_playing("BUFFERING_STATE")
    assert not play_status_is_playing("STOP_STATE")
    assert not play_status_is_playing(None)
    assert play_status_is_paused("PAUSE_STATE")
    assert not play_status_is_paused("PLAY_STATE")
