"""Tests for the rate at which a single queue item is handed to a player."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import ContentType, MediaType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.streams.constants import (
    SINGLE_ITEM_READRATE,
    SINGLE_ITEM_READRATE_INITIAL_BURST,
)
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant.controllers.streams.controller import StreamsController

EXPECTED_ARGS = [
    "-readrate",
    SINGLE_ITEM_READRATE,
    "-readrate_initial_burst",
    SINGLE_ITEM_READRATE_INITIAL_BURST,
]


def _queue_item(with_streamdetails: bool = True) -> QueueItem:
    """Return a queue item, optionally without resolved streamdetails."""
    streamdetails = (
        StreamDetails(
            provider="test--1",
            item_id="track-1",
            audio_format=AudioFormat(content_type=ContentType.FLAC),
            media_type=MediaType.TRACK,
        )
        if with_streamdetails
        else None
    )
    return QueueItem(
        queue_id="player-1",
        queue_item_id="item-1",
        name="Test Track",
        duration=180,
        streamdetails=streamdetails,
    )


def _patch_provider(
    controller: StreamsController, monkeypatch: pytest.MonkeyPatch, provider: MagicMock | None
) -> None:
    """Make provider lookups on the controller return the given provider."""
    monkeypatch.setattr(controller.mass, "get_provider", MagicMock(return_value=provider))


def _provider(is_streaming: bool) -> MagicMock:
    """Return a music provider that is or is not a streaming service."""
    provider = MagicMock(spec=MusicProvider)
    provider.is_streaming_provider = is_streaming
    return provider


def test_streaming_provider_audio_is_paced(
    streams_controller: StreamsController, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audio from a music service is handed over at a listening pace, after a burst."""
    _patch_provider(streams_controller, monkeypatch, _provider(is_streaming=True))
    assert streams_controller._output_pacing_args(_queue_item()) == EXPECTED_ARGS


def test_own_media_is_not_paced(
    streams_controller: StreamsController, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user's own files and self-hosted sources are served as fast as they read."""
    _patch_provider(streams_controller, monkeypatch, _provider(is_streaming=False))
    assert streams_controller._output_pacing_args(_queue_item()) == []


def test_unresolved_item_is_not_paced(
    streams_controller: StreamsController, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without streamdetails there is no provider to judge, so nothing is imposed."""
    _patch_provider(streams_controller, monkeypatch, _provider(is_streaming=True))
    assert streams_controller._output_pacing_args(_queue_item(with_streamdetails=False)) == []


@pytest.mark.parametrize("provider", [None, MagicMock()])
def test_non_music_provider_is_not_paced(
    streams_controller: StreamsController,
    provider: MagicMock | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing provider, or one that is not a music provider, is left alone."""
    _patch_provider(streams_controller, monkeypatch, provider)
    assert streams_controller._output_pacing_args(_queue_item()) == []


def test_pacing_is_faster_than_playback() -> None:
    """The rate has to stay ahead of playback, or every player would underrun."""
    assert float(SINGLE_ITEM_READRATE) > 1.0
    assert float(SINGLE_ITEM_READRATE_INITIAL_BURST) > 0
