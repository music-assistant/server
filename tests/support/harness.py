"""MusicAssistantHarness: test wrapper for a live MusicAssistant instance."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from music_assistant_models.enums import EventType

if TYPE_CHECKING:
    from music_assistant_models.event import MassEvent

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.music_provider import MusicProvider
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

    async def add_player(self, player: Player) -> Player:
        """Register a player with the players controller.

        :param player: Configured player instance to register.
        :return: The registered player.
        """
        await self.mass.players.register_or_update(player)
        return player

    async def sync_library(self, provider_instance_id: str, timeout: float = 30.0) -> None:
        """Trigger a library sync for a provider and wait for completion.

        :param provider_instance_id: The instance_id of the provider to sync.
        :param timeout: Maximum seconds to wait for sync completion.
        """
        done = asyncio.Event()

        def _on_sync_complete(_event: MassEvent) -> None:
            done.set()

        release = self.mass.subscribe(_on_sync_complete, EventType.MUSIC_SYNC_COMPLETED)
        try:
            await self.mass.music.start_sync(providers=[provider_instance_id])
            await asyncio.wait_for(done.wait(), timeout=timeout)
        finally:
            release()

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
