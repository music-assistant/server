"""Models and helpers for a player provider."""

from music_assistant.helpers.typing import Players
from music_assistant.models.provider import Provider, ProviderType


class PlayerProvider(Provider):
    """
    Base class for a Playerprovider.

    Should be overridden/subclassed by provider specific implementation.
    """

    @property
    def type(self) -> ProviderType:
        """Return ProviderType."""
        return ProviderType.PLAYER_PROVIDER

    @property
    def players(self, calculated_state=False) -> Players:
        """Return all players belonging to this provider."""
        # pylint: disable=no-member
        return [
            player
            for player in self.mass.player_manager.players
            if player.provider_id == self.id
        ]
