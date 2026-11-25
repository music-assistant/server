"""Tests for what a DLNA renderer is told about a track that is served as a file."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.models.player import PlayerMedia
from music_assistant.providers.dlna.player import DLNAPlayer
from tests.common import MockProvider

STREAM_URL = "http://192.168.1.2:8097/single/session1/queue1/q2/renderer.wav"


def _player(serves_as_file: MagicMock) -> tuple[DLNAPlayer, MagicMock]:
    """
    Return a renderer and its mocked device.

    :param serves_as_file: Tells whether the stream the renderer is handed is answered as a file.
    """
    provider = MockProvider("dlna", instance_id="dlna_test")
    provider.mass.streams.resolve_stream_url = AsyncMock(return_value=STREAM_URL)
    provider.mass.streams.stream_url_serves_as_file = serves_as_file
    device = MagicMock()
    device.profile_device.available = True
    device.can_stop = False
    for command in ("async_set_transport_uri", "async_set_next_transport_uri", "async_play"):
        setattr(device, command, AsyncMock())
    device.async_wait_for_can_play = AsyncMock()
    player = DLNAPlayer(
        provider,  # type: ignore[arg-type]
        "renderer",
        "http://192.168.1.50:8080/description.xml",
        device,
    )
    player._attr_available = True
    return player, device


@pytest.mark.parametrize(("served_as_file", "flag"), [(True, "01"), (False, "00")])
async def test_the_metadata_of_a_track_tells_whether_its_stream_answers_parts(
    served_as_file: bool, flag: str
) -> None:
    """A renderer gets the byte seek flag for a track it can fetch as a file, and only then."""
    serves_as_file = MagicMock(return_value=served_as_file)
    player, device = _player(serves_as_file)
    media = PlayerMedia(uri="library://track/1", title="Buena", duration=198, queue_item_id="q2")

    with patch.object(DLNAPlayer, "update_state"):
        await player.play_media(media)
        await player.enqueue_next_media(media)

    for command in (device.async_set_transport_uri, device.async_set_next_transport_uri):
        url, _title, didl = command.await_args.args
        assert url == STREAM_URL
        assert f"DLNA.ORG_OP={flag};" in didl
    serves_as_file.assert_called_with(STREAM_URL)
