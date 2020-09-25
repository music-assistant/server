"""Typing helper."""

from typing import TYPE_CHECKING, List, Optional

# pylint: disable=invalid-name
if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant as MusicAssistantType
    from music_assistant.models.player_queue import (
        QueueItem as QueueItemType,
        PlayerQueue as PlayerQueueType,
    )
    from music_assistant.models.streamdetails import StreamDetails as StreamDetailsType
    from music_assistant.models.player import Player as PlayerType

else:
    MusicAssistantType = "MusicAssistant"
    QueueItemType = "QueueItem"
    PlayerQueueType = "PlayerQueue"
    StreamDetailsType = "StreamDetailsType"
    PlayerType = "PlayerType"


QueueItems = List[QueueItemType]
Players = List[PlayerType]

OptionalInt = Optional[int]
OptionalStr = Optional[str]
