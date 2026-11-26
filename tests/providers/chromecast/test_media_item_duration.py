"""Tests for the duration and stream type a Cast player is told for the media it loads."""

from __future__ import annotations

from typing import Any, cast

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.chromecast.player import ChromecastPlayer

_BASE_URL = "http://192.168.1.124:8097"


@pytest.mark.parametrize(
    ("stream_path", "duration"),
    [("flow", None), ("single", 4)],
    ids=["flow", "single_item"],
)
def test_only_a_single_item_has_a_duration(stream_path: str, duration: int | None) -> None:
    """After a seek, a single item gets what is left; a flow stream has no length of its own."""
    media = PlayerMedia(uri="library://track/1", title="Buena", duration=198, stream_duration=4)

    assert _media_item(media, stream_path)["duration"] == duration


@pytest.mark.parametrize(
    ("media_type", "duration", "stream_path", "stream_type"),
    [
        (MediaType.TRACK, 198, "flow", "BUFFERED"),
        (MediaType.TRACK, None, "flow", "BUFFERED"),
        (MediaType.TRACK, 198, "single", "BUFFERED"),
        (MediaType.PODCAST_EPISODE, 198, "single", "BUFFERED"),
        (MediaType.TRACK, None, "single", "LIVE"),
        (MediaType.RADIO, None, "single", "LIVE"),
        (MediaType.RADIO, 198, "single", "LIVE"),
        (MediaType.AUDIO_SOURCE, 198, "source", "LIVE"),
        (MediaType.ANNOUNCEMENT, None, "announcement", "LIVE"),
    ],
)
def test_only_media_without_an_end_is_live(
    media_type: MediaType, duration: int | None, stream_path: str, stream_type: str
) -> None:
    """A track is loaded as a regular item, a radio station or live source as a live stream."""
    media = PlayerMedia(uri="library://item/1", media_type=media_type, duration=duration)

    assert _media_item(media, stream_path)["streamType"] == stream_type


def _media_item(media: PlayerMedia, stream_path: str) -> dict[str, Any]:
    # __init__ is skipped: it needs a provider, cast info and a live Chromecast connection
    player = cast("Any", ChromecastPlayer.__new__(ChromecastPlayer))
    item: dict[str, Any] = player._create_cc_media_item(
        media, f"{_BASE_URL}/{stream_path}/s1/q1/q2/p1.flac"
    )
    return item
