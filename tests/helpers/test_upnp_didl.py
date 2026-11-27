"""Tests for the DIDL-Lite metadata handed to UPnP players."""

import pytest

from music_assistant.helpers.upnp import create_didl_metadata
from music_assistant.models.player import PlayerMedia

_BASE_URL = "http://192.168.1.124:8097"


def _seeked_track() -> PlayerMedia:
    """Return a track started 4 s before its end."""
    return PlayerMedia(
        uri="library://track/1",
        title="Buena",
        duration=198,
        stream_duration=4,
        queue_item_id="q2",
    )


@pytest.mark.parametrize(
    ("url", "duration"),
    [
        (f"{_BASE_URL}/flow/session1/queue1/q2/player1.flac", "00:03:18"),
        (f"{_BASE_URL}/single/session1/queue1/q2/player1.flac", "00:00:04"),
    ],
    ids=["flow", "single_item"],
)
def test_only_a_flow_gets_the_whole_track_after_a_seek(url: str, duration: str) -> None:
    """A stream of one item ends after the seconds left; a flow carries on into the next track."""
    didl = create_didl_metadata(_seeked_track(), url)

    assert f'<res duration="{duration}"' in didl


def test_a_track_played_from_its_start_gets_its_duration() -> None:
    """Without a seek, the duration is the track's own."""
    media = PlayerMedia(uri="library://track/1", title="Buena", duration=198, queue_item_id="q2")

    didl = create_didl_metadata(media, f"{_BASE_URL}/single/session1/queue1/q2/player1.flac")

    assert '<res duration="00:03:18"' in didl


@pytest.mark.parametrize(("served_as_file", "flag"), [(True, "01"), (False, "00")])
@pytest.mark.parametrize(("codec", "mime_type"), [("wav", "audio/wav"), ("mp3", "audio/mpeg")])
def test_only_a_track_served_as_a_file_offers_byte_seek(
    served_as_file: bool, flag: str, codec: str, mime_type: str
) -> None:
    """The metadata says what the stream itself answers: parts only of a file."""
    media = PlayerMedia(uri="library://track/1", title="Buena", duration=198, queue_item_id="q2")
    url = f"{_BASE_URL}/single/session1/queue1/q2/player1.{codec}"

    didl = create_didl_metadata(media, url, served_as_file)

    assert f'protocolInfo="http-get:*:{mime_type}:DLNA.ORG_OP={flag};' in didl


def test_a_stream_without_a_duration_offers_no_byte_seek() -> None:
    """A flow stream or a radio station answers no request for a part."""
    media = PlayerMedia(uri="library://radio/1", title="Station")

    didl = create_didl_metadata(media, f"{_BASE_URL}/flow/session1/queue1/q2/player1.flac")

    assert "DLNA.ORG_OP=00;" in didl
