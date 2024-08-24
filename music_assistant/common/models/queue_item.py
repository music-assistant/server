"""Model a QueueItem."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Self
from uuid import uuid4

from mashumaro import DataClassDictMixin

from .enums import MediaType
from .media_items import ItemMapping, MediaItemImage, Radio, Track, UniqueList, is_track
from .streamdetails import StreamDetails


@dataclass
class QueueItem(DataClassDictMixin):
    """Representation of a queue item."""

    queue_id: str
    queue_item_id: str

    name: str
    duration: int | None
    sort_index: int = 0
    streamdetails: StreamDetails | None = None
    media_item: Track | Radio | None = None
    image: MediaItemImage | None = None
    index: int = 0

    def __post_init__(self) -> None:
        """Set default values."""
        if not self.name and self.streamdetails and self.streamdetails.stream_title:
            self.name = self.streamdetails.stream_title
        if not self.name:
            self.name = self.uri

    def __post_serialize__(self, d: dict[Any, Any]) -> dict[Any, Any]:
        """Execute action(s) on serialization."""
        # Exclude internal streamdetails fields from dict
        if streamdetails := d.get("streamdetails"):
            streamdetails.pop("data", None)
            streamdetails.pop("direct", None)
            streamdetails.pop("expires", None)
            streamdetails.pop("path", None)
            streamdetails.pop("decryption_key", None)
        return d

    @property
    def uri(self) -> str:
        """Return uri for this QueueItem (for logging purposes)."""
        if self.media_item and self.media_item.uri:
            return self.media_item.uri
        return self.queue_item_id

    @property
    def media_type(self) -> MediaType:
        """Return MediaType for this QueueItem (for convenience purposes)."""
        if self.media_item:
            return self.media_item.media_type
        if self.streamdetails:
            return self.streamdetails.media_type
        return MediaType.UNKNOWN

    @classmethod
    def from_media_item(cls, queue_id: str, media_item: Track | Radio) -> QueueItem:
        """Construct QueueItem from track/radio item."""
        if is_track(media_item):
            artists = "/".join(x.name for x in media_item.artists)
            name = f"{artists} - {media_item.name}"
            # save a lot of data/bandwidth by simplifying nested objects
            media_item.artists = UniqueList([ItemMapping.from_item(x) for x in media_item.artists])
            if media_item.album:
                media_item.album = ItemMapping.from_item(media_item.album)
        else:
            name = media_item.name
        return cls(
            queue_id=queue_id,
            queue_item_id=uuid4().hex,
            name=name,
            duration=media_item.duration,
            media_item=media_item,
            image=get_image(media_item),
        )

    def to_cache(self) -> dict[str, Any]:
        """Return the dict that is suitable for storing into the cache db."""
        base = self.to_dict()
        base.pop("streamdetails", None)
        return base

    @classmethod
    def from_cache(cls, d: dict[Any, Any]) -> Self:
        """Restore a QueueItem from a cache dict."""
        d.pop("streamdetails", None)
        return cls.from_dict(d)


def get_image(media_item: Track | Radio | None) -> MediaItemImage | None:
    """Find the Image for the MediaItem."""
    if not media_item:
        return None
    if media_item.image:
        return media_item.image
    if media_item.media_type == MediaType.TRACK and (album := getattr(media_item, "album", None)):
        return get_image(album)
    return None
