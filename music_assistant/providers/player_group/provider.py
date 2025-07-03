"""Player Group Provider implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import EventType, ProviderFeature

from music_assistant.models.player_provider import PlayerProvider

from .ugp_stream import UGPStream

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant_models.event import MassEvent


class PlayerGroupProvider(PlayerProvider):
    """Base/builtin provider for creating (permanent) player groups."""

    def __init__(self, *args, **kwargs) -> None:
        """Initialize PlayerGroupProvider."""
        super().__init__(*args, **kwargs)
        self.ugp_streams: dict[str, UGPStream] = {}
        self._on_unload: list[Callable[[], None]] = [
            self.mass.register_api_command("player_group/create", self.create_group),
        ]

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return {ProviderFeature.REMOVE_PLAYER}

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await super().loaded_in_mass()
        # register all existing group players
        await self._register_all_players()
        # listen for player added events so we can catch late joiners
        # (because a group depends on its childs to be available)
        self._on_unload.append(
            self.mass.subscribe(self._on_mass_player_added_event, EventType.PLAYER_ADDED)
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        # power off all group players at unload
        for group_player in self.players:
            if group_player.powered:
                await group_player.power(False)
        for unload_cb in self._on_unload:
            unload_cb()

    async def create_group(
        self, group_type: str, name: str, members: list[str], dynamic: bool = False
    ):
        """Create new Group Player."""
        # Import here to avoid circular import
        from .player import GroupPlayer

        return await GroupPlayer.create_group(self, group_type, name, members, dynamic)

    async def remove_player(self, player_id: str) -> None:
        """Remove a group player."""
        if group_player := self.players.get(player_id):
            await group_player.remove()

    async def _register_all_players(self) -> None:
        """Register all (virtual/fake) group players in the Player controller."""
        # Import here to avoid circular import
        from .player import GroupPlayer

        await GroupPlayer.register_all_players(self)

    async def _on_mass_player_added_event(self, event: MassEvent) -> None:
        """Handle player added event from player controller."""
        await self._register_all_players()

    async def poll_player(self, player_id: str) -> None:
        """Poll player for state updates."""
        if player := self.players.get(player_id):
            await player.poll()
