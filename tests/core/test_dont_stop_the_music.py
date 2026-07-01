"""Tests for the Don't stop the music availability check."""

from typing import cast
from unittest.mock import MagicMock

import pytest
from music_assistant_models.errors import UnsupportedFeaturedException

from music_assistant.controllers.player_queues import PlayerQueuesController


def _fake_controller(similar_tracks_providers: list[object]) -> MagicMock:
    fake = MagicMock()
    fake.mass.get_providers_supporting_feature.return_value = similar_tracks_providers
    # keep the trailing radio-fill branch out of the way
    queue = MagicMock()
    queue.enqueued_media_items = []
    fake._queues = {"q1": queue}
    return fake


def test_dont_stop_the_music_rejected_without_similar_tracks_provider() -> None:
    """Enabling the feature raises when no provider can supply similar tracks."""
    fake = _fake_controller([])
    with pytest.raises(UnsupportedFeaturedException):
        PlayerQueuesController.set_dont_stop_the_music(
            cast("PlayerQueuesController", fake), "q1", True
        )


def test_dont_stop_the_music_enabled_by_plugin_provider() -> None:
    """A plugin/metadata similar-tracks provider (e.g. sonic_similarity) enables the feature."""
    fake = _fake_controller([MagicMock()])
    PlayerQueuesController.set_dont_stop_the_music(cast("PlayerQueuesController", fake), "q1", True)
    assert fake._queues["q1"].dont_stop_the_music_enabled is True
