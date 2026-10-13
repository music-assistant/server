"""Tests for a queue requiring flow mode on behalf of an owner (e.g. a plugin)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
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
    controller._boundary_mix_owners = {}
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


@pytest.mark.parametrize(
    ("supports_gapless", "expected_route"),
    [(True, "single"), (False, "flow")],
    ids=["gapless player mixes per item", "other player goes to flow mode"],
)
async def test_a_boundary_mix_requirement_puts_only_a_player_without_gapless_in_flow_mode(
    supports_gapless: bool, expected_route: str
) -> None:
    """Mixed boundaries need flow mode only on a player that cannot play gapless."""
    controller = _controller()
    controller.mass.players.get_player.return_value.supports_gapless = supports_gapless
    controller.set_boundary_mix_required("queue-1", "ai_radio", True)

    url = await controller.resolve_stream_url("player-1", _media())

    assert url == f"http://mass:8097/{expected_route}/session-1/queue-1/item-1/player-1.flac"


def test_a_boundary_mix_requirement_gets_the_flow_stream_without_gapless() -> None:
    """The raw PCM helper streams a queue with mixed boundaries as a flow on such a player."""
    controller = _controller()
    controller.audio = MagicMock()
    controller._get_flow_start_item = MagicMock()
    controller._update_audio_processing_context = MagicMock()
    controller._count_as_output_stream = lambda stream: stream
    controller.mass.players.get_player.return_value.supports_gapless = False
    controller.set_boundary_mix_required("queue-1", "ai_radio", True)

    controller.get_stream(_media(), PCM_FORMAT, player_id="player-1")

    controller.audio.get_queue_flow_stream.assert_called_once()
    controller.audio.get_queue_item_stream.assert_not_called()


def test_boundary_mix_stays_required_while_any_owner_requires_it() -> None:
    """Releasing one owner's claim leaves the boundaries required while another holds one."""
    controller = _controller()

    controller.set_boundary_mix_required("queue-1", "ai_radio", True)
    controller.set_boundary_mix_required("queue-1", "other_plugin", True)
    controller.set_boundary_mix_required("queue-1", "ai_radio", False)
    still_required = controller.boundary_mix_required("queue-1")
    controller.set_boundary_mix_required("queue-1", "other_plugin", False)

    assert still_required
    assert not controller.boundary_mix_required("queue-1")
    assert not controller.flow_mode_required("queue-1")


@pytest.mark.parametrize(
    ("flow_mode", "supports_gapless", "expected"),
    [(False, True, True), (True, False, True), (False, False, False)],
    ids=["gapless", "flow mode", "neither"],
)
def test_item_boundaries_are_mixed_for_a_gapless_or_flow_mode_player(
    flow_mode: bool, supports_gapless: bool, expected: bool
) -> None:
    """Only a player that streams in flow mode or plays gapless gets its boundaries mixed."""
    controller = _controller()
    player = controller.mass.players.get_player.return_value
    player.flow_mode = flow_mode
    player.supports_gapless = supports_gapless

    assert controller.mixes_item_boundaries("queue-1") is expected
    controller.mass.players.get_player.assert_called_once_with("queue-1")


def test_item_boundaries_are_not_mixed_for_an_unknown_player() -> None:
    """A queue without its player has nothing to mix for."""
    controller = _controller()
    controller.mass.players.get_player.return_value = None

    assert controller.mixes_item_boundaries("queue-1") is False
