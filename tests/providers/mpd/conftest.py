"""Fixtures for testing the MPD player provider."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from music_assistant.providers.mpd.player import MPDPlayer
from tests.common import MockProvider, create_mock_config


@pytest.fixture
def make_mpd_player() -> Callable[..., MPDPlayer]:
    """Return a factory that builds an MPDPlayer with a decoupled (mocked) provider/mass."""

    def _make_player(player_id: str = "mpd_test") -> MPDPlayer:
        provider = MockProvider("mpd", instance_id="mpd--1")
        provider.mass.config.get_base_player_config.return_value = create_mock_config("MPD Test")
        return MPDPlayer(provider=provider, player_id=player_id, host="10.0.0.6")  # type: ignore[arg-type]

    return _make_player
