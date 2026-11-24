"""Tests for the metadata updates a Cast player sends while it plays a flow stream."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import MediaType, PlaybackState
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.chromecast.player import ChromecastPlayer

FLOW_URL = "http://mass:8097/flow/s1/q1/i1/p1.flac"


def _fake_flow_player() -> Any:
    """Build a Cast player playing a flow stream, on mocked collaborators."""
    # __init__ is skipped: it needs a provider, cast info and a live Chromecast connection.
    # Typed as Any because the collaborators below are read back as the mocks they are.
    fake = cast("Any", ChromecastPlayer.__new__(ChromecastPlayer))
    fake.mass = MagicMock()
    fake.mass.streams.base_url = "http://mass:8097"
    fake.mass.player_queues.queue_data_or_none.return_value.session_id = "s1"
    fake.mass.player_queues.get_item.return_value.extra_attributes = {}
    queue = fake.mass.players.get_active_queue.return_value
    queue.current_item.queue_item_id = "i2"
    queue.corrected_elapsed_time = 1.5
    fake.logger = MagicMock()
    fake._protocol_id = "cast-child-1"
    fake.cc = MagicMock()
    fake.cc.media_controller.status.player_is_playing = True
    fake.cc.media_controller.status.media_session_id = 7
    fake.cc.media_controller.status.adjusted_current_time = 201.5
    fake.cc.media_controller.status.media_metadata = {}
    fake.active_cast_group = None
    fake.flow_meta_checksum = None
    fake._attr_powered = True
    fake._attr_playback_state = PlaybackState.PLAYING
    fake._attr_current_media = PlayerMedia(uri=FLOW_URL)
    # the player the Cast is attached to, first seen after the player split
    fake._player = MagicMock(
        player_id="pl0123456789ab",
        current_media=PlayerMedia(
            uri="library://track/2",
            media_type=MediaType.TRACK,
            title="Track 2",
            duration=180,
            source_id="q1",
            queue_item_id="i2",
        ),
    )
    return fake


async def _sent_metadata(fake: Any) -> list[dict[str, Any]]:
    """Return the metadata of every message the player sends for an update of its media."""
    fake.mass.create_task.reset_mock()
    fake.cc.media_controller.send_message.reset_mock()
    fake.on_player_media_updated()
    if fake.mass.create_task.called:
        await fake.mass.create_task.call_args[0][0]
    return [
        call.kwargs["data"]["customData"]["metadata"]
        for call in fake.cc.media_controller.send_message.call_args_list
    ]


async def test_flow_metadata_update_queues_nothing_behind_the_flow_stream() -> None:
    """
    A flow stream only gets its metadata refreshed, nothing is queued behind it.

    The receiver plays or preloads whatever follows the flow stream once that stream ends.
    """
    fake = _fake_flow_player()
    fake.on_player_media_updated()
    await fake.mass.create_task.call_args[0][0]
    messages = [
        call.kwargs["data"] for call in fake.cc.media_controller.send_message.call_args_list
    ]
    assert [msg["type"] for msg in messages] == ["PLAY"]
    assert messages[0]["customData"]["metadata"]["title"] == "Track 2"


async def test_flow_metadata_places_the_track_in_the_stream() -> None:
    """A receiver gets where the track starts in the flow stream and how long it lasts."""
    (metadata,) = await _sent_metadata(_fake_flow_player())
    assert metadata["sectionStartTimeInMedia"] == 200
    assert metadata["sectionDuration"] == 180


async def test_flow_metadata_keeps_a_start_that_is_about_right() -> None:
    """A small difference in the computed start is no reason to update the receiver again."""
    fake = _fake_flow_player()
    fake.cc.media_controller.status.media_metadata = {
        "sectionStartTimeInMedia": 198.6,
        "sectionDuration": 180,
    }
    (metadata,) = await _sent_metadata(fake)
    assert metadata["sectionStartTimeInMedia"] == 198.6

    fake.cc.media_controller.status.adjusted_current_time = 202
    assert await _sent_metadata(fake) == []


async def test_flow_metadata_follows_a_track_that_starts_over() -> None:
    """A repeated track is placed anew, though it is the same media."""
    fake = _fake_flow_player()
    await _sent_metadata(fake)
    status = fake.cc.media_controller.status
    status.media_metadata = {"sectionStartTimeInMedia": 200, "sectionDuration": 180}
    status.adjusted_current_time = 381.5
    queue = fake.mass.players.get_active_queue.return_value
    queue.queue_id = "q1"
    queue.flow_mode = True

    fake._on_queue_time_updated(MagicMock(object_id="q2"))
    fake.mass.create_task.assert_called_once()
    fake._on_queue_time_updated(MagicMock(object_id="q1"))
    await fake.mass.create_task.call_args[0][0]
    data = fake.cc.media_controller.send_message.call_args.kwargs["data"]
    assert data["customData"]["metadata"]["sectionStartTimeInMedia"] == 380


@pytest.mark.parametrize(
    ("duration", "media_type", "speed"),
    [(None, MediaType.TRACK, 1), (180, MediaType.RADIO, 1), (180, MediaType.PODCAST_EPISODE, 1.5)],
    ids=["no_duration", "radio", "playback_speed"],
)
async def test_flow_metadata_without_progress(
    duration: int | None, media_type: MediaType, speed: float
) -> None:
    """Media whose position can't be read from the stream position has no progress."""
    fake = _fake_flow_player()
    fake._player.current_media.duration = duration
    fake._player.current_media.media_type = media_type
    fake.mass.player_queues.get_item.return_value.extra_attributes = {"playback_speed": speed}
    (metadata,) = await _sent_metadata(fake)
    assert metadata["title"] == "Track 2"
    assert "sectionDuration" not in metadata


async def test_flow_metadata_waits_for_the_queue_and_the_receiver() -> None:
    """Nothing is sent while the receiver or the player has yet to follow the queue."""
    fake = _fake_flow_player()
    queue_data = fake.mass.player_queues.queue_data_or_none.return_value
    queue_data.session_id = "s2"
    assert await _sent_metadata(fake) == []
    queue_data.session_id = "s1"
    fake.mass.players.get_active_queue.return_value.current_item.queue_item_id = "i3"
    assert await _sent_metadata(fake) == []


async def test_metadata_of_another_servers_stream_is_left_alone() -> None:
    """The stream of another Music Assistant server on the device is not updated."""
    fake = _fake_flow_player()
    fake._attr_current_media = PlayerMedia(uri="http://other:8097/flow/s1/q1/i1/p1.flac")
    assert await _sent_metadata(fake) == []
