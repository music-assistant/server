"""Typing helper."""

from typing import TYPE_CHECKING, Any, Callable, Optional, List, Tuple

from music_assistant.constants import EventType

# pylint: disable=invalid-name
if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant, EventDetails, EventCallBackType, EventSubscriptionType
    from music_assistant.models.media_items import MediaType
    from music_assistant.models.player import (
        PlayerQueue,
        QueueItem,
    )
    from music_assistant.models.player import Player

else:
    MusicAssistant = "MusicAssistant"
    QueueItem = "QueueItem"
    PlayerQueue = "PlayerQueue"
    StreamDetails = "StreamDetails"
    Player = "Player"
    MediaType = "MediaType"
    EventDetails = Any | None
    EventCallBackType = "EventCallBackType"
    EventSubscriptionType = "EventSubscriptionType"


QueueItems = List[QueueItem]
Players = List[Player]

OptionalInt = Optional[int]
OptionalStr = Optional[str]

