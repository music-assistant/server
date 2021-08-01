"""Typing helper."""

from typing import TYPE_CHECKING, Optional, Set

# pylint: disable=invalid-name
if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant
    from music_assistant.models.player_queue import (
        QueueItem,
        PlayerQueue,
    )
    from music_assistant.models.streamdetails import StreamDetails, StreamType
    from music_assistant.models.player import Player
    from music_assistant.managers.config import ConfigSubItem
    from music_assistant.models.media_types import MediaType

else:
    MusicAssistant = "MusicAssistant"
    QueueItem = "QueueItem"
    PlayerQueue = "PlayerQueue"
    StreamDetails = "StreamDetails"
    Player = "Player"
    ConfigSubItem = "ConfigSubItem"
    MediaType = "MediaType"
    StreamType = "StreamType"


QueueItems = Set[QueueItem]
Players = Set[Player]

OptionalInt = Optional[int]
OptionalStr = Optional[str]
