"""All constants for Music Assistant."""

from enum import Enum


class EventType(Enum):
    """Enum with possible Events."""

    PLAYER_ADDED = "player added"
    PLAYER_REMOVED = "player removed"
    PLAYER_CHANGED = "player changed"
    STREAM_STARTED = "streaming started"
    STREAM_ENDED = "streaming ended"
    CONFIG_CHANGED = "config changed"
    MUSIC_SYNC_STATUS = "music sync status"
    QUEUE_ADDED = "queue_added"
    QUEUE_UPDATED = "queue updated"
    QUEUE_ITEMS_UPDATED = "queue items updated"
    SHUTDOWN = "application shutdown"
    ARTIST_ADDED = "artist added"
    ALBUM_ADDED = "album added"
    TRACK_ADDED = "track added"
    PLAYLIST_ADDED = "playlist added"
    RADIO_ADDED = "radio added"
    TASK_UPDATED = "task updated"
    PROVIDER_REGISTERED = "PROVIDER_REGISTERED"


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
