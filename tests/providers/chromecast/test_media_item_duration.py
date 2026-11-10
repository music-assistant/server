"""Tests for the duration a Cast player is told for the media it loads."""

from __future__ import annotations

from typing import Any, cast

import pytest
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.chromecast.player import ChromecastPlayer

_BASE_URL = "http://192.168.1.124:8097"


@pytest.mark.parametrize(
    ("stream_path", "duration"),
    [("flow", 198), ("single", 4)],
    ids=["flow", "single_item"],
)
def test_only_a_flow_gets_the_whole_track_after_a_seek(stream_path: str, duration: int) -> None:
    """After a seek, a flow stream carries on into the next track, so it shows the whole track."""
    # __init__ is skipped: it needs a provider, cast info and a live Chromecast connection
    player = cast("Any", ChromecastPlayer.__new__(ChromecastPlayer))
    media = PlayerMedia(uri="library://track/1", title="Buena", duration=198, stream_duration=4)

    item = player._create_cc_media_item(media, f"{_BASE_URL}/{stream_path}/s1/q1/q2/p1.flac")

    assert item["duration"] == duration
