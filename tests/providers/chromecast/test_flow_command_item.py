"""
Tests for the flow-mode 'command' queue item a Cast player inserts.

In flow mode the whole queue plays as one continuous stream, so the player gets a
special command item appended to its cast queue: fetching it triggers queue-next on
the server (the on-player next button, and the recovery path when the flow stream
dies). Vendor cast stacks reject the item when its declared contentType does not
match the silence file the url actually serves, so that coupling is asserted here.
"""

from __future__ import annotations

import mimetypes
from typing import Any, cast
from unittest.mock import MagicMock

from music_assistant_models.enums import PlaybackState
from music_assistant_models.player import PlayerMedia

from music_assistant.constants import SILENCE_FILE
from music_assistant.providers.chromecast.player import ChromecastPlayer

COMMAND_URL = "http://mass:8097/command/session-1/queue-1/next.mp3"


def _fake_flow_player(*, cast_queue_items: list[Any] | None = None) -> Any:
    """
    Build a Cast player playing a flow stream, on mocked collaborators.

    The player is seeded mid-flow-playback so on_player_media_updated runs its
    metadata update for real, with the messages it sends captured on the mocked
    media controller.

    :param cast_queue_items: Items the receiver reports in its cast queue.
    """
    # __init__ is skipped: it needs a provider, cast info and a live Chromecast connection.
    # Typed as Any because the collaborators below are read back as the mocks they are.
    fake = cast("Any", ChromecastPlayer.__new__(ChromecastPlayer))
    fake.mass = MagicMock()
    fake.mass.streams.get_command_url.return_value = COMMAND_URL
    fake.logger = MagicMock()
    fake._player_id = "cast-child-1"
    fake.cc = MagicMock()
    fake.cc.media_controller.status.player_is_playing = True
    fake.cc.media_controller.status.media_session_id = 7
    fake.cc.media_controller.status.items = cast_queue_items or []
    fake.active_cast_group = None
    fake.flow_meta_checksum = None
    fake._attr_powered = True
    fake._attr_playback_state = PlaybackState.PLAYING
    fake._attr_current_media = PlayerMedia(uri="http://mass:8097/flow/s1/q1/i1/p1.flac")
    fake._state = MagicMock(current_media=PlayerMedia(uri="http://mass:8097/flow/s1/q1/i1/p1.flac"))
    return fake


async def _run_flow_metadata_update(fake: Any) -> list[dict[str, Any]]:
    """Run the player's flow metadata update and return the cast messages it sent."""
    fake.on_player_media_updated()
    await fake.mass.create_task.call_args[0][0]
    return [call.kwargs["data"] for call in fake.cc.media_controller.send_message.call_args_list]


async def test_command_item_declares_the_type_of_the_served_silence_file() -> None:
    """The inserted item's contentType matches the file its url serves, not the flow codec."""
    fake = _fake_flow_player()
    messages = await _run_flow_metadata_update(fake)
    insert = next(msg for msg in messages if msg["type"] == "QUEUE_INSERT")
    media = insert["items"][0]["media"]
    assert media["contentId"] == COMMAND_URL
    assert media["contentType"] == mimetypes.guess_type(SILENCE_FILE)[0]
    fake.mass.streams.get_command_url.assert_called_once_with("cast-child-1", "next")


async def test_command_item_is_not_inserted_without_a_command_url() -> None:
    """No command item is queued when there is no active session to build a url for."""
    fake = _fake_flow_player()
    fake.mass.streams.get_command_url.return_value = None
    messages = await _run_flow_metadata_update(fake)
    assert not [msg for msg in messages if msg["type"] == "QUEUE_INSERT"]


async def test_command_item_is_not_inserted_twice() -> None:
    """A cast queue already holding the command item does not get another one."""
    fake = _fake_flow_player(cast_queue_items=[MagicMock(), MagicMock()])
    messages = await _run_flow_metadata_update(fake)
    assert not [msg for msg in messages if msg["type"] == "QUEUE_INSERT"]
