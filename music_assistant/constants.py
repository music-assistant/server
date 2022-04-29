"""All constants for Music Assistant."""

from dataclasses import dataclass
from enum import Enum
from time import time
from typing import Any, Coroutine, Optional


class EventType(Enum):
    """Enum with possible Events."""

    PLAYER_ADDED = "player added"
    PLAYER_REMOVED = "player removed"
    PLAYER_UPDATED = "player updated"
    STREAM_STARTED = "streaming started"
    STREAM_ENDED = "streaming ended"
    MUSIC_SYNC_STATUS = "music sync status"
    QUEUE_ADDED = "queue_added"
    QUEUE_UPDATED = "queue updated"
    QUEUE_ITEMS_UPDATED = "queue items updated"
    QUEUE_TIME_UPDATED = "queue time updated"
    SHUTDOWN = "application shutdown"
    ARTIST_ADDED = "artist added"
    ALBUM_ADDED = "album added"
    TRACK_ADDED = "track added"
    PLAYLIST_ADDED = "playlist added"
    PLAYLIST_UPDATED = "playlist updated"
    RADIO_ADDED = "radio added"
    TASK_UPDATED = "task updated"
    PROVIDER_REGISTERED = "provider registered"
    BACKGROUND_JOB_UPDATED = "background_job_updated"


@dataclass
class MassEvent:
    """Representation of an Event emitted in/by Music Assistant."""

    type: EventType
    object_id: Optional[str] = None  # player_id, queue_id or uri
    data: Optional[Any] = None  # optional data (such as the object)


class JobStatus(Enum):
    """Enum with Job status."""

    PENDING = "pending"
    RUNNING = "running"
    CANCELLED = "cancelled"
    FINISHED = "success"
    ERROR = "error"


@dataclass
class BackgroundJob:
    """Description of a background job/task."""

    id: str
    coro: Coroutine
    name: str
    timestamp: float = time()
    status: JobStatus = JobStatus.PENDING

    def to_dict(self):
        """Return serializable dict from object."""
        return {
            "id": self.id,
            "name": self.name,
            "timestamp": self.status.value,
            "status": self.status.value,
        }


# player attributes
ATTR_PLAYER_ID = "player_id"
ATTR_PROVIDER_ID = "provider_id"
ATTR_NAME = "name"
ATTR_POWERED = "powered"
ATTR_ELAPSED_TIME = "elapsed_time"
ATTR_STATE = "state"
ATTR_AVAILABLE = "available"
ATTR_CURRENT_URI = "current_uri"
ATTR_VOLUME_LEVEL = "volume_level"
ATTR_MUTED = "muted"
ATTR_IS_GROUP_PLAYER = "is_group_player"
ATTR_GROUP_CHILDS = "group_childs"
ATTR_DEVICE_INFO = "device_info"
ATTR_SHOULD_POLL = "should_poll"
ATTR_FEATURES = "features"
ATTR_CONFIG_ENTRIES = "config_entries"
ATTR_UPDATED_AT = "updated_at"
ATTR_ACTIVE_QUEUE = "active_queue"
ATTR_GROUP_PARENTS = "group_parents"
