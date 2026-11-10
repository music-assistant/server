"""Tests for the player helpers."""

import pytest
from music_assistant_models.player import PlayerMedia

from music_assistant.helpers.player import served_duration

_BASE_URL = "http://192.168.1.124:8097"
_FLOW_URL = f"{_BASE_URL}/flow/session1/queue1/q2/player1.flac"
_SINGLE_URL = f"{_BASE_URL}/single/session1/queue1/q2/player1.flac"


@pytest.mark.parametrize(
    ("url", "duration"), [(_FLOW_URL, 198), (_SINGLE_URL, 4)], ids=["flow", "single_item"]
)
def test_only_a_flow_gets_the_whole_track_after_a_seek(url: str, duration: int) -> None:
    """A stream of one item ends after the seconds left; a flow carries on into the next track."""
    media = PlayerMedia(uri="library://track/1", duration=198, stream_duration=4)

    assert served_duration(media, url) == duration


@pytest.mark.parametrize("url", [_FLOW_URL, _SINGLE_URL], ids=["flow", "single_item"])
def test_a_track_played_from_its_start_gets_its_duration(url: str) -> None:
    """Without a seek, the duration is the track's own."""
    media = PlayerMedia(uri="library://track/1", duration=198)

    assert served_duration(media, url) == 198


def test_media_without_a_duration_has_none() -> None:
    """A radio station or other endless stream has no duration to tell."""
    assert served_duration(PlayerMedia(uri="library://radio/1"), _FLOW_URL) is None
