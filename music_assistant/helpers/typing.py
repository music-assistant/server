"""Typing helper."""

from typing import TYPE_CHECKING, Optional, Set

# pylint: disable=invalid-name
if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant
    from music_assistant.models.player_queue import (
        QueueItem,
        PlayerQueue,
    )
    from music_assistant.models.streamdetails import StreamDetails
    from music_assistant.models.player import Player
    from music_assistant.managers.config import ConfigSubItem

else:
    MusicAssistant = "MusicAssistant"
    QueueItem = "QueueItem"
    PlayerQueue = "PlayerQueue"
    StreamDetails = "StreamDetails"
    Player = "Player"
    ConfigSubItem = "ConfigSubItem"


QueueItems = Set[QueueItem]
Players = Set[Player]

OptionalInt = Optional[int]
OptionalStr = Optional[str]
