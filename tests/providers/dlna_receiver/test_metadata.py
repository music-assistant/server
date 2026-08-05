"""Tests for DLNA metadata parsing and playback-time helpers."""

from __future__ import annotations

from typing import Any, cast

from music_assistant_models.streamdetails import StreamMetadata

from music_assistant.providers.dlna_receiver.metadata import (
    clear_playback,
    freeze_elapsed,
    parse_didl_metadata,
    parse_duration,
    position_for,
)
from music_assistant.providers.dlna_receiver.models import RendererInstance
from music_assistant.providers.dlna_receiver.renderer import UPnPRenderer
from music_assistant.providers.dlna_receiver.ssdp import SSDPAdvertiser


def _instance() -> RendererInstance:
    """Create a renderer instance without network resources."""
    return RendererInstance(
        player_id="player_kitchen",
        player_name="Kitchen",
        renderer=cast("UPnPRenderer", cast("Any", object())),
        ssdp=cast("SSDPAdvertiser", cast("Any", object())),
    )


def test_parse_didl_metadata_extracts_supported_fields() -> None:
    """A valid DIDL item exposes sender metadata to Music Assistant."""
    metadata = """\
<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"
 xmlns:dc="http://purl.org/dc/elements/1.1/"
 xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">
  <item>
    <dc:title>Example Track</dc:title>
    <upnp:artist>Example Artist</upnp:artist>
    <upnp:album>Example Album</upnp:album>
    <upnp:albumArtURI>http://media.local/cover.jpg</upnp:albumArtURI>
    <res duration="01:02:03.500">http://media.local/track.flac</res>
  </item>
</DIDL-Lite>
"""

    assert parse_didl_metadata(metadata) == {
        "title": "Example Track",
        "artist": "Example Artist",
        "album": "Example Album",
        "image_url": "http://media.local/cover.jpg",
        "duration": "01:02:03.500",
    }


def test_parse_didl_metadata_rejects_oversized_document() -> None:
    """Oversized DIDL is rejected whole instead of parsing a truncated document."""
    metadata = "<title>must-not-survive</title>" + (" " * (64 * 1024))

    assert parse_didl_metadata(metadata) == {
        "title": None,
        "artist": None,
        "album": None,
        "image_url": None,
        "duration": None,
    }


def test_parse_didl_metadata_rejects_malformed_xml() -> None:
    """Malformed sender metadata does not break transport setup."""
    assert parse_didl_metadata("<DIDL-Lite><item>") == {
        "title": None,
        "artist": None,
        "album": None,
        "image_url": None,
        "duration": None,
    }


def test_parse_duration_accepts_upnp_formats() -> None:
    """UPnP duration formats are converted to whole seconds."""
    assert parse_duration("01:02:03.500") == 3723
    assert parse_duration("02:03") == 123
    assert parse_duration("45.9") == 45
    assert parse_duration("invalid") is None


def test_position_for_clamps_elapsed_to_duration() -> None:
    """Reported position advances from its offset but never exceeds duration."""
    instance = _instance()
    instance.elapsed_offset = 60
    instance.play_start_time = 100.0
    instance.current_metadata = {"duration": "00:01:30"}

    assert position_for(instance, now=140.0) == (90, 90)


def test_freeze_elapsed_and_clear_playback() -> None:
    """Pause freezes progress and stop clears only transient playback state."""
    instance = _instance()
    instance.play_start_time = 100.0
    instance.elapsed_offset = 10
    instance.stream_metadata = StreamMetadata(title="Track", duration=180)
    instance.metadata_dirty = True

    freeze_elapsed(instance, now=125.9)

    state = cast("Any", instance)
    assert state.elapsed_offset == 35
    assert state.play_start_time is None

    clear_playback(instance)

    assert state.elapsed_offset == 0
    assert state.play_start_time is None
    assert state.stream_metadata is None
    assert state.metadata_dirty is False
