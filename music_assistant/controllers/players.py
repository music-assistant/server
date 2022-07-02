"""Logic to play music from MusicProviders to supported players."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Dict, Tuple

from music_assistant.models.enums import EventType, PlayerState
from music_assistant.models.errors import AlreadyRegisteredError
from music_assistant.models.event import MassEvent
from music_assistant.models.player import Player
from music_assistant.models.player_queue import PlayerQueue

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


class PlayerController:
    """Controller holding all logic to play music from MusicProviders to supported players."""

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize class."""
        self.mass = mass
        self.logger = mass.logger.getChild("players")
        self._players: Dict[str, Player] = {}
        self._player_queues: Dict[str, PlayerQueue] = {}

    async def setup(self) -> None:
        """Async initialize of module."""
        self.mass.create_task(self._poll_players())

    async def cleanup(self) -> None:
        """Cleanup on exit."""
        for player_id in set(self._players.keys()):
            player = self._players.pop(player_id)
            player.on_remove()
        for queue_id in set(self._player_queues.keys()):
            self._player_queues.pop(queue_id)

    @property
    def players(self) -> Tuple[Player]:
        """Return all registered players."""
        return tuple(self._players.values())

    @property
    def player_queues(self) -> Tuple[PlayerQueue]:
        """Return all available PlayerQueue's."""
        return tuple(self._player_queues.values())

    def __iter__(self):
        """Iterate over (available) players."""
        return iter(self._players.values())

    def get_player(self, player_id: str) -> Player | None:
        """Return Player by player_id or None if not found."""
        return self._players.get(player_id)

    def get_player_queue(self, queue_id: str) -> PlayerQueue | None:
        """Return PlayerQueue by id or None if not found."""
        return self._player_queues.get(queue_id)

    def get_player_by_name(self, name: str) -> Player | None:
        """Return Player by name or None if no match is found."""
        return next((x for x in self._players.values() if x.name == name), None)

    async def register_player(self, player: Player) -> None:
        """Register a new player on the controller."""
        if self.mass.closed:
            return
        player_id = player.player_id

        if player_id in self._players:
            raise AlreadyRegisteredError(f"Player {player_id} is already registered")

        # make sure that the mass instance is set on the player
        player.mass = self.mass
        self._players[player_id] = player

        # create playerqueue for this player
        self._player_queues[player.player_id] = player_queue = PlayerQueue(
            self.mass, player_id
        )
        await player_queue.setup()

        self.logger.info(
            "Player registered: %s/%s",
            player_id,
            player.name,
        )
        self.mass.signal_event(
            MassEvent(EventType.PLAYER_ADDED, object_id=player.player_id, data=player)
        )

    async def _poll_players(self) -> None:
        """Poll players every X interval."""
        interval = 30
        cur_tick = 0
        while True:
            for player in self.players:
                if not player.available:
                    continue
                if cur_tick == interval or (
                    player.active_queue.active
                    and player.state
                    in (
                        PlayerState.PLAYING,
                        PlayerState.PAUSED,
                    )
                ):
                    player.update_state()
            if cur_tick == interval:
                cur_tick = 0
            else:
                cur_tick += 1
            await asyncio.sleep(1)
