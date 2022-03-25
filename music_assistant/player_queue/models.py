"""Models and helpers for a player queue."""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping

import ujson
from mashumaro import DataClassDictMixin

from music_assistant.music.models import MediaType, Radio, Track
from music_assistant.players.models import PlayerState

LOGGER = logging.getLogger("player_queue")


class QueueOption(Enum):
    """Enum representation of the queue (play) options."""

    PLAY = "play"
    REPLACE = "replace"
    NEXT = "next"
    ADD = "add"


@dataclass
class QueueItem(DataClassDictMixin):
    """Representation of a queue item."""

    uri: str
    name: str = ""
    duration: int | None = None
    item_id: str = ""
    sort_index: int = 0
    streamdetails: StreamDetails | None = None

    def __post_init__(self):
        """Set default values."""
        if not self.item_id:
            self.item_id = str(uuid.uuid4())
        if not self.name:
            self.name = self.uri

    @classmethod
    def from_media_item(cls, media_item: Track | Radio):
        """Construct QueueItem from track/radio item."""
        return cls(uri=media_item.uri, duration=media_item.duration)


@dataclass
class PlayerQueue(DataClassDictMixin):
    """Model for a PlayerQueue."""

    queue_id: str = str(uuid.uuid4())
    shuffle_enabled: bool = False
    repeat_enabled: bool = False
    crossfade_duration: int = 0
    name: str = ""
    players: List[str] = field(default=list)
    current_index: int | None = None
    current_item_time: int = 0
    last_item: int | None = None
    queue_stream_start_index: int = 0
    queue_stream_next_index: int = 0
    queue_stream_active: bool = False
    state: PlayerState = PlayerState.IDLE
    last_state = PlayerState.IDLE
    items: List[QueueItem] = field(default=list)
    streamdetails: StreamDetails = None

    @property
    def current_item(self) -> QueueItem | None:
        """
        Return the current item in the queue.

        Returns None if queue is empty.
        """
        if self.current_index is None or self.current_index > len(self.items):
            return None
        return self.items[self.current_index]

    @property
    def next_index(self) -> int | None:
        """
        Return the next index for this PlayerQueue.

        Return None if queue is empty or no more items.
        """
        if not self.items:
            # queue is empty
            return None
        if self.current_index is None:
            # playback started
            return 0
        # player already playing (or paused) so return the next item
        if len(self.items) > (self.current_index + 1):
            return self.current_index + 1
        if self.repeat_enabled:
            # repeat enabled, start queue at beginning
            return 0
        return None

    @property
    def next_item(self) -> QueueItem | None:
        """
        Return the next item in the queue.

        Returns None if queue is empty or no more items.
        """
        if self.next_index is not None:
            return self.items[self.next_index]
        return None

    def get_item(self, index: int) -> QueueItem | None:
        """Get queue item by index."""
        if index is not None and len(self.items) > index:
            return self.items[index]
        return None

    def item_by_id(self, queue_item_id: str) -> QueueItem | None:
        """Get item by queue_item_id from queue."""
        if not queue_item_id:
            return None
        return next((x for x in self.items if x.item_id == queue_item_id), None)

    def index_by_id(self, queue_item_id: str) -> int | None:
        """Get index by queue_item_id."""
        for index, item in enumerate(self.items):
            if item.item_id == queue_item_id:
                return index
        return None

    @classmethod
    def from_db_row(cls, db_row: Mapping):
        """Create PlayerQueue object from database row."""
        db_row = dict(db_row)
        for key in ["players", "items"]:
            if key in db_row:
                db_row[key] = ujson.loads(db_row[key])
        return cls.from_dict(db_row)


class StreamType(Enum):
    """Enum with stream types."""

    EXECUTABLE = "executable"
    URL = "url"
    FILE = "file"
    CACHE = "cache"


class ContentType(Enum):
    """Enum with audio content types supported by ffmpeg."""

    OGG = "ogg"
    FLAC = "flac"
    MP3 = "mp3"
    AAC = "aac"
    MPEG = "mpeg"
    PCM_S16LE = "s16le"  # PCM signed 16-bit little-endian
    PCM_S24LE = "s24le"  # PCM signed 24-bit little-endian
    PCM_S32LE = "s32le"  # PCM signed 32-bit little-endian
    PCM_F32LE = "f32le"  # PCM 32-bit floating-point little-endian
    PCM_F64LE = "f64le"  # PCM 64-bit floating-point little-endian

    def is_pcm(self):
        """Return if contentype is PCM."""
        return self.name.startswith("PCM")

    def sox_supported(self):
        """Return if ContentType is supported by SoX."""
        return self not in [ContentType.AAC, ContentType.MPEG]

    def sox_format(self):
        """Convert the ContentType to SoX compatible format."""
        if not self.sox_supported():
            raise NotImplementedError
        return self.value.replace("le", "")


@dataclass
class StreamDetails(DataClassDictMixin):
    """Model for streamdetails."""

    type: StreamType
    provider: str
    item_id: str
    path: str
    content_type: ContentType
    player_id: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    seconds_played: int = 0
    gain_correct: float = 0
    loudness: float | None = None
    sample_rate: int | None = None
    bit_depth: int | None = None
    media_type: MediaType = MediaType.TRACK

    def __post_serialize__(self, d: Dict[Any, Any]) -> Dict[Any, Any]:
        """Exclude internal fields from dict."""
        d.pop("path")
        d.pop("details")
        return d

    def __str__(self):
        """Return pretty printable string of object."""
        return f"{self.type.value}/{self.content_type.value} - {self.provider}/{self.item_id}"
