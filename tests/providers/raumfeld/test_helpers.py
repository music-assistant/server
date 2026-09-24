"""Tests for the Teufel Raumfeld helper functions."""

from __future__ import annotations

from music_assistant.providers.raumfeld.helpers import (
    parse_didl_metadata,
    parse_duration,
    parse_line_in,
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
    """The legacy player id is derived from the room UDN (kept for config migration)."""
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
    }
    assert parse_didl_metadata("NOT_IMPLEMENTED")["title"] is None
