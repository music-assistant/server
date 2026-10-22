"""Tests for the Teufel Raumfeld helper functions."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import RepeatMode

from music_assistant.providers.raumfeld.constants import RENDERER_QUERY_TIMEOUT
from music_assistant.providers.raumfeld.helpers import (
    container_object_id,
    external_source,
    library_uri,
    parse_control_url,
    parse_didl_metadata,
    parse_duration,
    parse_last_change,
    parse_last_change_response,
    parse_library_items,
    parse_line_in,
    parse_model_name,
    parse_play_mode,
    parse_serial_number,
    play_mode_for,
    query_last_change,
    room_udn_to_player_id,
    serial_to_player_id,
)

_LINE_IN_DIDL = (
    '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/">'
    '<item id="0/Line In/uuid%3Acced8852-1234" parentID="0/Line In">'
    "<dc:title>Connector Fitnessruimte</dc:title>"
    '<res protocolInfo="http-get:*:audio/x-flac:*">http://192.168.1.43:8888/stream.flac</res>'
    "</item>"
    '<item id="0/Line In/uuid%3Aed4ccbb0-5678" parentID="0/Line In">'
    "<dc:title>Speaker TV</dc:title>"
    '<res protocolInfo="http-get:*:audio/x-flac:*">http://192.168.1.41:8888/stream.flac</res>'
    "</item>"
    "</DIDL-Lite>"
)


def test_room_udn_to_player_id() -> None:
    """The fallback player id is derived from the room UDN, for a room with a shared serial."""
    assert room_udn_to_player_id("uuid:1234-abcd") == "raumfeld_1234-abcd"
    # the same UDN always yields the same id (unlike a mutable room name)
    assert room_udn_to_player_id("uuid:1234-abcd") == room_udn_to_player_id("uuid:1234-abcd")


def test_serial_to_player_id() -> None:
    """The player id comes from the hardware serial, normalised and punctuation-free."""
    assert serial_to_player_id("04:a3:16:f3:b6:c4") == "raumfeld_04a316f3b6c4"
    # the same device always yields the same id, however the serial is punctuated or cased
    assert serial_to_player_id("04-A3-16-F3-B6-C4") == serial_to_player_id("04:a3:16:f3:b6:c4")


def test_parse_duration() -> None:
    """Duration parsing is tolerant of empty / NOT_IMPLEMENTED values."""
    assert parse_duration("0:03:20") == 200
    assert parse_duration("3:20") == 200
    assert parse_duration(None) is None
    assert parse_duration("") is None
    assert parse_duration("NOT_IMPLEMENTED") is None
    assert parse_duration("garbage") is None


def test_parse_line_in() -> None:
    """Line-In listings map each input to its room by renderer UUID."""
    result = parse_line_in(_LINE_IN_DIDL)
    assert result == {
        "cced8852-1234": ("http://192.168.1.43:8888/stream.flac", "Connector Fitnessruimte"),
        "ed4ccbb0-5678": ("http://192.168.1.41:8888/stream.flac", "Speaker TV"),
    }


def test_parse_line_in_invalid() -> None:
    """Malformed or empty Line-In XML yields an empty mapping (never raises)."""
    assert parse_line_in(None) == {}
    assert parse_line_in("") == {}
    assert parse_line_in("<not-xml") == {}


def test_parse_didl_metadata_empty() -> None:
    """Empty / NOT_IMPLEMENTED DIDL metadata yields all-None (never raises)."""
    assert parse_didl_metadata(None) == {
        "title": None,
        "artist": None,
        "album": None,
        "image_url": None,
        "section": None,
        "item_id": None,
    }
    assert parse_didl_metadata("NOT_IMPLEMENTED")["title"] is None


_DEVICE_DESCRIPTION = (
    '<?xml version="1.0"?>'
    '<root xmlns="urn:schemas-upnp-org:device-1-0">'
    "<device><deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>"
    "<friendlyName>Speaker Bar</friendlyName>"
    "<modelName>Raumfeld One S</modelName>"
    "<serialNumber> 04:a3:16:f3:b6:c4 </serialNumber>"
    "<deviceList><device><serialNumber>ff:ff:ff:ff:ff:ff</serialNumber></device></deviceList>"
    "</device></root>"
)


def test_parse_serial_number() -> None:
    """The root device's serial is read from the namespaced description, trimmed."""
    assert parse_serial_number(_DEVICE_DESCRIPTION) == "04:a3:16:f3:b6:c4"


def test_parse_serial_number_missing_or_malformed() -> None:
    """A description without a serial, malformed XML or nothing at all yields None."""
    assert (
        parse_serial_number("<root><device><friendlyName>x</friendlyName></device></root>") is None
    )
    assert parse_serial_number("<root><serialNumber>  </serialNumber></root>") is None
    assert parse_serial_number("<root><unclosed>") is None
    assert parse_serial_number("") is None
    assert parse_serial_number(None) is None


def test_parse_model_name() -> None:
    """The root device's model name is read from the description, or None without one."""
    assert parse_model_name(_DEVICE_DESCRIPTION) == "Raumfeld One S"
    assert parse_model_name("<root><device><friendlyName>x</friendlyName></device></root>") is None
    assert parse_model_name("<root><unclosed>") is None
    assert parse_model_name(None) is None


_SPOTIFY_DIDL = (
    '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
    'xmlns:raumfeld="urn:schemas-raumfeld-com:meta-data/raumfeld" '
    'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/">'
    '<item id="0/Spotify/Track" parentID="0/Spotify" restricted="0">'
    "<raumfeld:section>Spotify</raumfeld:section>"
    "<dc:title>Who's That</dc:title>"
    "<upnp:artist>The War On Drugs</upnp:artist>"
    "</item></DIDL-Lite>"
)


def test_parse_didl_metadata_section_and_item() -> None:
    """The section and item id of a playing item are read along with its title."""
    meta = parse_didl_metadata(_SPOTIFY_DIDL)
    assert meta["title"] == "Who's That"
    assert meta["section"] == "Spotify"
    assert meta["item_id"] == "0/Spotify/Track"


def test_external_source() -> None:
    """Known sections map to the source Music Assistant knows; others are used as they are."""
    assert external_source("Spotify") == ("spotify", "Spotify")
    assert external_source("RadioTime") == ("tunein", "TuneIn")
    assert external_source("Tidal") == ("tidal", "Tidal")
    assert external_source("My Music") == ("my_music", "My Music")
    assert external_source(None) == ("raumfeld", "Raumfeld")
    assert external_source("  ") == ("raumfeld", "Raumfeld")


def test_parse_last_change() -> None:
    """Every state variable of a LastChange event is returned with its value."""
    event = (
        '<Event xmlns="urn:schemas-upnp-org:metadata-1-0/AVT/"><InstanceID val="0">'
        '<TransportState val="PLAYING"/>'
        '<CurrentTransportActions val="Pause,Previous,Next,Seek"/>'
        '<AVTransportURIMetaData val="&lt;DIDL-Lite/&gt;"/>'
        "</InstanceID></Event>"
    )
    state = parse_last_change(event)
    assert state["TransportState"] == "PLAYING"
    assert state["CurrentTransportActions"] == "Pause,Previous,Next,Seek"
    assert state["AVTransportURIMetaData"] == "<DIDL-Lite/>"
    assert parse_last_change("<Event><unclosed>") == {}
    assert parse_last_change(None) == {}


def test_parse_control_url() -> None:
    """The control URL of the requested service is read from the description."""
    description = (
        '<root xmlns="urn:schemas-upnp-org:device-1-0"><device><serviceList>'
        "<service><serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType>"
        "<controlURL>/RenderingControl/ctrl</controlURL></service>"
        "<service><serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>"
        "<controlURL>/AVTransport/ctrl</controlURL></service>"
        "</serviceList></device></root>"
    )
    assert parse_control_url(description, "AVTransport") == "/AVTransport/ctrl"
    assert parse_control_url(description, "ContentDirectory") is None
    assert parse_control_url("<root><unclosed>", "AVTransport") is None
    assert parse_control_url(None, "AVTransport") is None


def test_parse_last_change_response() -> None:
    """The value is read from a standard <return> element and from Raumfeld's own naming."""
    event = '&lt;Event&gt;&lt;InstanceID val="0"&gt;&lt;TransportState val="PLAYING"/&gt;'
    event += "&lt;/InstanceID&gt;&lt;/Event&gt;"
    envelope = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
        '<u:QueryStateVariableResponse xmlns:u="urn:schemas-upnp-org:control-1-0">'
        "<{tag}>{event}</{tag}>"
        "</u:QueryStateVariableResponse></s:Body></s:Envelope>"
    )
    for tag in ("return", "LastChange"):
        state = parse_last_change_response(envelope.format(tag=tag, event=event))
        assert state["TransportState"] == "PLAYING"
    assert parse_last_change_response("<s:Envelope><unclosed>") == {}
    assert parse_last_change_response(None) == {}


async def test_last_change_query_has_a_short_timeout() -> None:
    """A renderer that stops answering cannot hold up polling for long."""
    response = MagicMock(status=200)
    response.text = AsyncMock(return_value="")
    request = MagicMock()
    request.__aenter__ = AsyncMock(return_value=response)
    request.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    session.post.return_value = request
    assert await query_last_change(session, "http://192.168.1.42:54730/AVTransport/ctrl") == {}
    assert session.post.call_args.kwargs["timeout"].total == RENDERER_QUERY_TIMEOUT


_LIBRARY_DIDL = (
    '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"'
    ' xmlns:dc="http://purl.org/dc/elements/1.1/">'
    '<item id="0/RadioTime/Search/s-s25777"><dc:title>NPO Radio 2</dc:title></item>'
    '<container id="0/My Music/Albums/42"><dc:title>Shine On</dc:title></container>'
    '<item id="0/Untitled"></item>'
    "</DIDL-Lite>"
)


def test_parse_library_items() -> None:
    """Items and containers of a library listing, titled by their id when they have none."""
    assert parse_library_items(_LIBRARY_DIDL) == [
        ("0/RadioTime/Search/s-s25777", "NPO Radio 2", False),
        ("0/My Music/Albums/42", "Shine On", True),
        ("0/Untitled", "0/Untitled", False),
    ]
    assert parse_library_items("") == []
    assert parse_library_items("<not xml") == []


def test_library_uri() -> None:
    """An item plays as a single, a container as a playlist from its start."""
    single = library_uri("uuid:media-server", "0/RadioTime/Search/s-s25777", container=False)
    assert single == (
        "dlna-playsingle://uuid%3Amedia-server?sid=urn%3Aupnp-org%3AserviceId%3A"
        "ContentDirectory&iid=0%2FRadioTime%2FSearch%2Fs-s25777"
    )
    container = library_uri("uuid:media-server", "0/My Music/Albums/42", container=True)
    assert container.startswith("dlna-playcontainer://uuid%3Amedia-server?")
    assert container.endswith("&cid=0%2FMy%20Music%2FAlbums%2F42&md=0")


def test_container_object_id() -> None:
    """The album or playlist a container URI plays; anything else plays none."""
    uri = library_uri("uuid:media-server", "0/My Music/Albums/42", container=True)
    assert container_object_id(uri) == "0/My Music/Albums/42"
    single = library_uri("uuid:media-server", "0/RadioTime/Search/s-s25777", container=False)
    assert container_object_id(single) is None
    assert container_object_id(None) is None


@pytest.mark.parametrize("play_mode", ["NORMAL", "SHUFFLE", "REPEAT_ALL", "RANDOM", "REPEAT_ONE"])
def test_a_play_mode_comes_back_the_same(play_mode: str) -> None:
    """Every play mode the renderer reports is the one its own state sets again."""
    shuffle, repeat = parse_play_mode(play_mode)
    assert shuffle is not None
    assert play_mode_for(shuffle, repeat) == play_mode


def test_an_unknown_play_mode_is_not_guessed() -> None:
    """A play mode the renderer did not report, or one not seen before, stays unknown."""
    assert parse_play_mode(None) == (None, RepeatMode.UNKNOWN)
    assert parse_play_mode("INTRO") == (None, RepeatMode.UNKNOWN)
