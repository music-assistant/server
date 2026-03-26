"""Extended mock player and provider with playback state tracking for E2E tests."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from music_assistant_models.enums import PlaybackState

from tests.common import MockPlayer, MockProvider

if TYPE_CHECKING:
    from music_assistant_models.player import PlayerMedia


class MockPlayerProvider(MockProvider):
    """Mock player provider that can register players with MA."""

    def __init__(self, domain: str = "mock_player", mass: MagicMock | None = None) -> None:
        """Initialize the mock player provider."""
        super().__init__(domain=domain, mass=mass or MagicMock())


class TrackingMockPlayer(MockPlayer):
    """MockPlayer extended with playback state tracking.

    Adds simulate_play(), simulate_stop(), and simulate_pause() helpers so
    E2E tests can assert on current_item_id and playback_state without
    needing a real stream.
    """

    def __init__(
        self,
        provider: MockPlayerProvider,
        player_id: str,
        name: str,
    ) -> None:
        """Initialize the tracking mock player."""
        super().__init__(provider=provider, player_id=player_id, name=name)
        self._attr_playback_state = PlaybackState.IDLE
        self._current_item_id: str | None = None
        self._cache.clear()

    @property
    def current_item_id(self) -> str | None:
        """Return the item_id of the currently playing item."""
        return self._current_item_id

    def simulate_play(self, item_id: str) -> None:
        """Simulate the player starting playback of an item.

        :param item_id: The item_id of the track being played.
        """
        self._attr_playback_state = PlaybackState.PLAYING
        self._current_item_id = item_id
        self._cache.clear()

    def simulate_stop(self) -> None:
        """Simulate the player stopping playback."""
        self._attr_playback_state = PlaybackState.IDLE
        self._current_item_id = None
        self._cache.clear()

    def simulate_pause(self) -> None:
        """Simulate the player pausing playback."""
        self._attr_playback_state = PlaybackState.PAUSED
        self._cache.clear()

    async def stop(self) -> None:
        """Handle stop command from MA by transitioning to idle state."""
        self.simulate_stop()

    async def play_media(self, media: PlayerMedia) -> None:
        """Accept a play_media command without starting real audio playback."""
