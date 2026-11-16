"""Tests for the metadata updates a Cast player sends while it plays a flow stream."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

from music_assistant_models.enums import PlaybackState
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.chromecast.player import ChromecastPlayer

FLOW_URL = "http://mass:8097/flow/s1/q1/i1/p1.flac"


def _fake_flow_player() -> Any:
    """Build a Cast player playing a flow stream, on mocked collaborators."""
    # __init__ is skipped: it needs a provider, cast info and a live Chromecast connection.
    # Typed as Any because the collaborators below are read back as the mocks they are.
    fake = cast("Any", ChromecastPlayer.__new__(ChromecastPlayer))
    fake.mass = MagicMock()
    fake.logger = MagicMock()
    fake._protocol_id = "cast-child-1"
    fake.cc = MagicMock()
    fake.cc.media_controller.status.player_is_playing = True
    fake.cc.media_controller.status.media_session_id = 7
    fake.active_cast_group = None
    fake.flow_meta_checksum = None
    fake._attr_powered = True
    fake._attr_playback_state = PlaybackState.PLAYING
    fake._attr_current_media = PlayerMedia(uri=FLOW_URL)
    # the player the Cast is attached to, first seen after the player split
    fake._player = MagicMock(
        player_id="pl0123456789ab",
        current_media=PlayerMedia(uri=FLOW_URL, title="Track 2"),
    )
    return fake


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
