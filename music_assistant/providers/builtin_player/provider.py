"""Provider implementation for the Built-in Player."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, cast

import shortuuid
from music_assistant_models.builtin_player import BuiltinPlayerEvent, BuiltinPlayerState
from music_assistant_models.enums import BuiltinPlayerEventType, EventType, PlayerFeature

from music_assistant.models.player import Player
from music_assistant.models.player_provider import PlayerProvider

from .player import BuiltinPlayer


class BuiltinPlayerProvider(PlayerProvider):
    """Builtin Player Provider for playing to the Music Assistant Web Interface."""

    _unregister_cbs: list[Callable[[], None]]

    async def handle_async_init(self) -> None:
        """Handle asynchronous initialization of the provider."""
        self._unregister_cbs = [
            self.mass.register_api_command("builtin_player/register", self.register_player),
            self.mass.register_api_command("builtin_player/unregister", self.unregister_player),
            self.mass.register_api_command("builtin_player/update_state", self.update_player_state),
        ]

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        is_removed will be set to True when the provider is removed from the configuration.
        """
        for unload_cb in self._unregister_cbs:
            unload_cb()

    async def remove_player(self, player_id: str) -> None:
        """Remove a player."""
        self.mass.signal_event(
            EventType.BUILTIN_PLAYER,
            player_id,
            BuiltinPlayerEvent(type=BuiltinPlayerEventType.TIMEOUT),
        )
        await self.unregister_player(player_id)

    async def register_player(self, player_name: str, player_id: str | None) -> Player:
        """Register a player.

        Every player must first be registered through this `builtin_player/register` API command
        before any playback can occur.
        Since players queues can time out, this command either will create a new player queue,
        or restore it from the last session.

        - player_name: Human readable name of the player, will only be used in case this call
                       creates a new queue.
        - player_id: the id of the builtin player, set to None on new sessions. The returned player
                     will have a new random player_id
        """
        if player_id is None:
            player_id = f"ma_{shortuuid.random(10).lower()}"

        player_features = {
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
            PlayerFeature.PAUSE,
            PlayerFeature.POWER,
        }

        player = self.mass.players.get(player_id)

        if player is None:
            player = BuiltinPlayer(
                player_id=player_id,
                provider=self,
                name=player_name,
                features=tuple(player_features),
            )
            await self.mass.players.register_or_update(player)
        else:
            if TYPE_CHECKING:
                player = cast("BuiltinPlayer", player)
            player.register(player_name)

        return player

    async def unregister_player(self, player_id: str) -> None:
        """Manually unregister a player with `builtin_player/unregister`."""
        if player := self.mass.players.get(player_id):
            if TYPE_CHECKING:
                player = cast("BuiltinPlayer", player)
            player.unregister_routes()

    async def update_player_state(self, player_id: str, state: BuiltinPlayerState) -> bool:
        """Update current state of a player.

        A player must periodically update the state of through this `builtin_player/update_state`
        API command.

        Returns False in case the player already timed out or simply doesn't exist.
        In that case, register the player first with `builtin_player/register`.
        """
        if not (player := self.mass.players.get(player_id)):
            return False

        if TYPE_CHECKING:
            player = cast("BuiltinPlayer", player)

        player.update_builtin_player_state(state)

        return True
