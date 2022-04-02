"""Logic to play music from MusicProviders to supported players."""
from __future__ import annotations

from typing import Dict, Tuple

from music_assistant.constants import EventType
from music_assistant.helpers.errors import AlreadyRegisteredError
from music_assistant.helpers.typing import MusicAssistant

from .models import Player, PlayerGroup, PlayerQueue
from .stream import StreamController

PlayerType = Player | PlayerGroup

DB_TABLE = "queue_settings"


class PlayerController:
    """Controller holding all logic to play music from MusicProviders to supported players."""

    def __init__(self, mass: MusicAssistant, stream_port: int) -> None:
        """Initialize class."""
        self.mass = mass
        self.logger = mass.logger.getChild("players")
        self._players: Dict[str, PlayerType] = {}
        self._player_queues: Dict[str, PlayerQueue] = {}
        self.streams = StreamController(mass, stream_port)

    async def setup(self) -> None:
        """Async initialize of module."""
        await self.mass.database.execute(
            """CREATE TABLE IF NOT EXISTS queue_settings(
                queue_id TEXT UNIQUE,
                crossfade_duration INTEGER,
                shuffle_enabled BOOLEAN,
                repeat_enabled BOOLEAN,
                volume_normalization_enabled BOOLEAN,
                volume_normalization_target INTEGER)"""
        )
        await self.streams.setup()

    @property
    def players(self) -> Tuple[PlayerType]:
        """Return all available players."""
        return tuple(x for x in self._players.values() if x.available)

    @property
    def player_queues(self) -> Tuple[PlayerQueue]:
        """Return all available PlayerQueue's."""
        return tuple(x for x in self._player_queues.values() if x.available)

    def __iter__(self):
        """Iterate over (available) players."""
        return iter(x for x in self._players.values() if x.available)

    def get_player(
        self, player_id: str, include_unavailable: bool = False
    ) -> PlayerType | None:
        """Return Player by player_id or None if not found/unavailable."""
        if player := self._players.get(player_id):
            if player.available or include_unavailable:
                return player
        return None

    def get_player_queue(
        self, queue_id: str, include_unavailable: bool = False
    ) -> PlayerQueue | None:
        """Return PlayerQueue by id or None if not found/unavailable."""
        if player_queue := self._player_queues.get(queue_id):
            if player_queue.available or include_unavailable:
                return player_queue
        return None

    def get_player_by_name(self, name: str) -> PlayerType | None:
        """Return Player by name or None if no match is found."""
        return next((x for x in self._players.values() if x.name == name), None)

    async def register_player(self, player: PlayerType) -> None:
        """Register a new player on the controller."""
        player_id = player.player_id

        if player_id in self._players:
            raise AlreadyRegisteredError(f"Player {player_id} is already registered")

        # make sure that the mass instance is set on the player
        player.mass = self.mass
        self._players[player_id] = player

        # create playerqueue for this player
        self._player_queues[player.player_id] = player_queue = PlayerQueue(self.mass, player_id)
        await player_queue.setup()
        self.logger.info(
            "Player registered: %s/%s",
            player_id,
            player.name,
        )
        self.mass.signal_event(EventType.PLAYER_ADDED, player)
