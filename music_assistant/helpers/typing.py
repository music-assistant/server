"""Typing helper."""
from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

# pylint: disable=invalid-name
if TYPE_CHECKING:
    from music_assistant.mass import (
        EventCallBackType,
        EventSubscriptionType,
        MusicAssistant,
    )
    from music_assistant.models.media_items import MediaType
    from music_assistant.models.player import Player, PlayerQueue, QueueItem

else:
    MusicAssistant = "MusicAssistant"
    QueueItem = "QueueItem"
    PlayerQueue = "PlayerQueue"
    StreamDetails = "StreamDetails"
    Player = "Player"
    MediaType = "MediaType"
    EventCallBackType = "EventCallBackType"
    EventSubscriptionType = "EventSubscriptionType"


QueueItems = List[QueueItem]
Players = List[Player]

OptionalInt = Optional[int]
OptionalStr = Optional[str]
