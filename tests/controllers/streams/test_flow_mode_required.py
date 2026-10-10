"""Tests for a queue requiring flow mode on behalf of an owner (e.g. a plugin)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from music_assistant_models.enums import ContentType, MediaType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.player import PlayerMedia

from music_assistant.controllers.streams import StreamsController

PCM_FORMAT = AudioFormat(content_type=ContentType.PCM_S16LE, sample_rate=44100, bit_depth=16)


def _controller() -> Any:
    """Return a streams controller with one gapless, non-flow HTTP player."""
    controller: Any = StreamsController.__new__(StreamsController)
    controller._base_url = "http://mass:8097"
    controller._flow_mode_owners = {}
    controller.mass = mass = MagicMock()
    player = MagicMock()
    player.config.get_value.side_effect = lambda _key, default=None: default
    player.flow_mode = False
    player.supports_gapless = True
    mass.players.get_player.return_value = player
    mass.player_queues.get.return_value = SimpleNamespace(
        queue_id="queue-1",
        crossfade_enabled=False,
        overlay_enabled=False,
        overlay_source=None,
    )
    return controller


def _media(media_type: MediaType = MediaType.SOUND_EFFECT) -> PlayerMedia:
    """Return queue media with the data needed to resolve its stream."""
    return PlayerMedia(
        uri="ai_radio://clip/1",
        media_type=media_type,
        source_id="queue-1",
        queue_item_id="item-1",
        queue_session_id="session-1",
    )


async def test_required_queue_resolves_to_the_flow_url() -> None:
    """A queue that requires flow mode gets the flow url, any other the single url."""
    controller = _controller()

    single_url = await controller.resolve_stream_url("player-1", _media())
    controller.set_flow_mode_required("queue-1", "ai_radio", True)
    flow_url = await controller.resolve_stream_url("player-1", _media())

    assert single_url == "http://mass:8097/single/session-1/queue-1/item-1/player-1.flac"
    assert flow_url == "http://mass:8097/flow/session-1/queue-1/item-1/player-1.flac"


async def test_required_queue_keeps_radio_out_of_flow_mode() -> None:
    """Radio stays a single long-lived stream even on a queue that requires flow mode."""
    controller = _controller()
    controller.set_flow_mode_required("queue-1", "ai_radio", True)

    url = await controller.resolve_stream_url("player-1", _media(MediaType.RADIO))

    assert url.startswith("http://mass:8097/single/")


def test_required_queue_gets_the_flow_stream() -> None:
    """The raw PCM helper streams a required queue as a flow."""
    controller = _controller()
    controller.audio = MagicMock()
    controller._get_flow_start_item = MagicMock()
    controller._update_audio_processing_context = MagicMock()
    controller._count_as_output_stream = lambda stream: stream
    controller.set_flow_mode_required("queue-1", "ai_radio", True)

    controller.get_stream(_media(), PCM_FORMAT, player_id="player-1")

    controller.audio.get_queue_flow_stream.assert_called_once()
    controller.audio.get_queue_item_stream.assert_not_called()


def test_flow_mode_stays_required_while_any_owner_requires_it() -> None:
    """Releasing one owner's claim leaves the queue required while another still holds one."""
    controller = _controller()

    controller.set_flow_mode_required("queue-1", "ai_radio", True)
    controller.set_flow_mode_required("queue-1", "other_plugin", True)
    controller.set_flow_mode_required("queue-1", "ai_radio", False)
    still_required = controller.flow_mode_required("queue-1")
    controller.set_flow_mode_required("queue-1", "other_plugin", False)

    assert still_required
    assert not controller.flow_mode_required("queue-1")
    assert not controller.flow_mode_required("queue-2")
