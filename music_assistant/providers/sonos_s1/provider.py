"""Sonos S1 Player Provider implementation."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from music_assistant_models.enums import ProviderFeature
from soco import SoCo
from soco import config as soco_config
from soco.discovery import discover, scan_network

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.models.player_provider import PlayerProvider

from .player import SonosPlayer


@dataclass
class DiscoveredPlayer:
    """Discovered Sonos player info."""

    soco: SoCo
    sonos_player: SonosPlayer | None = None


class SonosPlayerProvider(PlayerProvider):
    """Sonos S1 Player Provider for legacy Sonos speakers."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the provider."""
        super().__init__(*args, **kwargs)
        self.sonosplayers: dict[str, SonosPlayer] = {}
        self._discovered_players: dict[str, DiscoveredPlayer] = {}

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return {
            ProviderFeature.SYNC_PLAYERS,
            # support sync groups by reporting create/remove player group support
            ProviderFeature.CREATE_GROUP_PLAYER,
            ProviderFeature.REMOVE_GROUP_PLAYER,
        }

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # Set up SoCo logging
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            logging.getLogger("soco").setLevel(logging.DEBUG)
        else:
            logging.getLogger("soco").setLevel(self.logger.level + 10)

        # Disable SoCo cache to prevent stale data
        soco_config.CACHE_ENABLED = False

        # Start discovery
        await self.discover_players()

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        # Clean up subscriptions and connections
        for sonos_player in self.sonosplayers.values():
            if hasattr(sonos_player, "subscriptions"):
                for subscription in sonos_player.subscriptions:
                    with suppress(Exception):
                        subscription.unsubscribe()

    async def discover_players(self) -> None:
        """Discover Sonos players on the network."""
        try:
            # Discover players using SoCo
            discovered = await asyncio.to_thread(discover)
            if not discovered:
                # Try manual discovery
                discovered = await asyncio.to_thread(scan_network)

            for soco in discovered:
                await self._setup_player(soco)

        except Exception as err:
            self.logger.error("Error discovering Sonos players: %s", err)

    async def _setup_player(self, soco: SoCo) -> None:
        """Set up a discovered Sonos player."""
        player_id = soco.uid

        if player_id in self.sonosplayers:
            return

        try:
            # Create SonosPlayer instance
            sonos_player = SonosPlayer(self, soco)
            self.sonosplayers[player_id] = sonos_player

            # Create discovery info
            discovered_player = DiscoveredPlayer(
                soco=soco,
                sonos_player=sonos_player,
            )
            self._discovered_players[player_id] = discovered_player

            # Register with Music Assistant
            await sonos_player.setup()

            # Set up event subscriptions
            await self._setup_subscriptions(sonos_player)

        except Exception as err:
            self.logger.error("Error setting up Sonos player %s: %s", player_id, err)

    async def _setup_subscriptions(self, sonos_player: SonosPlayer) -> None:
        """Set up event subscriptions for a Sonos player."""
        try:
            # Set up event subscriptions
            # This would involve subscribing to SoCo events for state changes
            pass
        except Exception as err:
            self.logger.debug(
                "Error setting up subscriptions for %s: %s", sonos_player.player_id, err
            )

    async def poll_player(self, player_id: str) -> None:
        """Poll player for state updates."""
        if sonos_player := self.sonosplayers.get(player_id):
            await sonos_player.poll()
