"""MusicAssistantHarness: test wrapper for a live MusicAssistant instance."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from music_assistant_models.enums import EventType, MediaType

from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.event import MassEvent

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.player import Player


class MusicAssistantHarness:
    """Wraps a live MusicAssistant instance with test convenience methods.

    Usage::

        async def test_something(mass):
            harness = MusicAssistantHarness(mass)
            await harness.add_provider(my_provider)
            await harness.add_player(my_player)
    """

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize the harness with a running MusicAssistant instance."""
        self.mass = mass

    async def add_provider(self, provider: MusicProvider) -> None:
        """Register a music provider with the MA instance.

        Directly injects the provider into MA's internal registry without
        triggering config-dependent hooks like schedule_provider_sync.
        Use sync_library() explicitly when you need a sync.

        :param provider: Configured provider instance to register.
        """
        provider.available = True
        self.mass._providers[provider.instance_id] = provider
        # Update the global cache so provider_mappings.available returns True for
        # tracks/albums/artists belonging to this provider.
        await self.mass._update_available_providers_cache()

    async def add_player(self, player: Player) -> Player:
        """Register a player with the players controller.

        Creates the default player config in the real MA config controller before
        registering, since MockPlayer is constructed with a MagicMock mass and
        therefore cannot call create_default_player_config on the real config.

        :param player: Configured player instance to register.
        :return: The registered player.
        """
        self.mass.config.create_default_player_config(
            player.player_id,
            player.provider_id,
            player.type,
            player.name,
        )
        await self.mass.players.register_or_update(player)
        return player

    async def sync_library(self, provider_instance_id: str, timeout: float = 30.0) -> None:
        """Trigger a library sync for a provider and wait for completion.

        Directly invokes the provider's sync_library method for each supported media
        type, bypassing the config-dependent start_sync path.  This is necessary for
        mock providers that are injected without a full provider config entry.

        :param provider_instance_id: The instance_id of the provider to sync.
        :param timeout: Maximum seconds to wait for sync completion.
        """
        provider = self.mass.get_provider(provider_instance_id)
        if provider is None:
            msg = f"Provider {provider_instance_id} not found"
            raise ValueError(msg)
        if not isinstance(provider, MusicProvider):
            return
        for media_type in MediaType.ALL:
            if provider.library_supported(media_type):
                await asyncio.wait_for(
                    provider.sync_library(media_type),
                    timeout=timeout,
                )
        # fire the sync-completed event so callers that await wait_for_event() unblock
        self.mass.signal_event(EventType.MUSIC_SYNC_COMPLETED)

    async def wait_for_event(self, event_type: EventType, timeout: float = 5.0) -> MassEvent:
        """Wait for a specific event type and return the event payload.

        :param event_type: The event type to wait for.
        :param timeout: Maximum seconds to wait.
        :return: The received MassEvent.
        """
        done: asyncio.Future[MassEvent] = asyncio.get_running_loop().create_future()

        def _handler(event: MassEvent) -> None:
            if not done.done():
                done.set_result(event)

        release = self.mass.subscribe(_handler, event_type)
        try:
            return await asyncio.wait_for(done, timeout=timeout)
        finally:
            release()
