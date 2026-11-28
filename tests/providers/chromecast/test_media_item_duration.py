"""Tests for the duration and stream type a Cast player is told for the media it loads."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

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


@pytest.mark.parametrize("stream_path", ["flow", "single"])
@pytest.mark.parametrize(
    ("stream_duration", "start"), [(None, 0), (4, -194)], ids=["from_the_start", "after_a_seek"]
)
def test_a_track_tells_where_it_starts_in_the_stream(
    stream_path: str, stream_duration: int | None, start: int
) -> None:
    """A receiver gets the progress of the track, which a seek starts partway."""
    media = PlayerMedia(
        uri="library://track/1",
        media_type=MediaType.TRACK,
        duration=198,
        stream_duration=stream_duration,
    )

    metadata = _media_item(media, stream_path)["metadata"]
    assert metadata["sectionStartTimeInMedia"] == start
    assert metadata["sectionDuration"] == 198


@pytest.mark.parametrize(
    ("media_type", "duration", "speed"),
    [
        (MediaType.TRACK, None, 1),
        (MediaType.RADIO, 198, 1),
        (MediaType.PODCAST_EPISODE, 198, 1.5),
    ],
)
def test_media_without_progress(media_type: MediaType, duration: int | None, speed: float) -> None:
    """A receiver gets no progress when its position does not run along with the media."""
    media = PlayerMedia(
        uri="library://item/1",
        media_type=media_type,
        duration=duration,
        source_id="q1",
        queue_item_id="q2",
    )

    metadata = _media_item(media, "single", speed)["metadata"]
    assert "sectionStartTimeInMedia" not in metadata
    assert "sectionDuration" not in metadata


@pytest.mark.parametrize(("stream_path", "has_progress"), [("flow", False), ("single", True)])
def test_the_default_receiver_gets_no_progress_for_a_flow_stream(
    stream_path: str, has_progress: bool
) -> None:
    """The default receiver can't follow the tracks of a flow stream, so it gets no progress."""
    media = PlayerMedia(uri="library://track/1", media_type=MediaType.TRACK, duration=198)

    metadata = _media_item(media, stream_path, use_mass_app=False)["metadata"]
    assert ("sectionDuration" in metadata) is has_progress


def _media_item(
    media: PlayerMedia, stream_path: str, speed: float = 1, use_mass_app: bool = True
) -> dict[str, Any]:
    # __init__ is skipped: it needs a provider, cast info and a live Chromecast connection
    player = cast("Any", ChromecastPlayer.__new__(ChromecastPlayer))
    player.mass = MagicMock()
    player._config = MagicMock()
    player._config.get_value.return_value = use_mass_app
    player.mass.player_queues.get_item.return_value.extra_attributes = {"playback_speed": speed}
    item: dict[str, Any] = player._create_cc_media_item(
        media, f"{_BASE_URL}/{stream_path}/s1/q1/q2/p1.flac"
    )
    return item
