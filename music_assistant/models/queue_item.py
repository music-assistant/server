"""Model a QueueItem."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Union
from uuid import uuid4

from mashumaro import DataClassDictMixin

from music_assistant.models.enums import MediaType
from music_assistant.models.media_items import (
    ItemMapping,
    MediaItemImage,
    Radio,
    StreamDetails,
    Track,
)


@dataclass
class QueueItem(DataClassDictMixin):
    """Representation of a queue item."""

    name: str = ""
    duration: Optional[int] = None
    item_id: str = ""
    sort_index: int = 0
    streamdetails: Optional[StreamDetails] = None
    media_item: Union[Track, Radio, None] = None
    image: Optional[MediaItemImage] = None

    def __post_init__(self):
        """Set default values."""
        if not self.item_id:
            self.item_id = str(uuid4())
        if self.streamdetails and self.streamdetails.stream_title:
            self.name = self.streamdetails.stream_title
        if not self.name:
            self.name = self.uri

    @classmethod
    def __pre_deserialize__(cls, d: Dict[Any, Any]) -> Dict[Any, Any]:
        """Run actions before deserialization."""
        d.pop("streamdetails", None)
        return d

    @property
    def uri(self) -> str:
        """Return uri for this QueueItem (for logging purposes)."""
        if self.media_item:
            return self.media_item.uri
        return self.item_id

    @property
    def media_type(self) -> MediaType:
        """Return MediaType for this QueueItem (for convenience purposes)."""
        if self.media_item:
            return self.media_item.media_type
        return MediaType.UNKNOWN

    @classmethod
    def from_media_item(cls, media_item: Track | Radio):
        """Construct QueueItem from track/radio item."""
        if media_item.media_type == MediaType.TRACK:
            artists = "/".join((x.name for x in media_item.artists))
            name = f"{artists} - {media_item.name}"
            # save a lot of data/bandwidth by simplifying nested objects
            media_item.artists = [ItemMapping.from_item(x) for x in media_item.artists]
            if media_item.album:
                media_item.album = ItemMapping.from_item(media_item.album)
            media_item.albums = []
        else:
            name = media_item.name
        return cls(
            name=name,
            duration=media_item.duration,
            media_item=media_item,
            image=media_item.image,
        )
