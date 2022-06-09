"""Model a QueueItem."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Union
from uuid import uuid4

from mashumaro import DataClassDictMixin

from music_assistant.models.enums import ContentType, MediaType, ProviderType
from music_assistant.models.media_items import Radio, StreamDetails, Track


@dataclass
class QueueItem(DataClassDictMixin):
    """Representation of a queue item."""

    uri: str
    name: str = ""
    duration: Optional[int] = None
    item_id: str = ""
    sort_index: int = 0
    streamdetails: Optional[StreamDetails] = None
    media_type: MediaType = MediaType.UNKNOWN
    image: Optional[str] = None
    available: bool = True
    media_item: Union[Track, Radio, None] = None

    def __post_init__(self):
        """Set default values."""
        if not self.item_id:
            self.item_id = str(uuid4())
        if not self.name:
            self.name = self.uri

    @classmethod
    def __pre_deserialize__(cls, d: Dict[Any, Any]) -> Dict[Any, Any]:
        """Run actions before deserialization."""
        d.pop("streamdetails", None)
        return d

    def __post_serialize__(self, d: Dict[Any, Any]) -> Dict[Any, Any]:
        """Run actions before serialization."""
        if self.media_type == MediaType.RADIO:
            d.pop("duration")
        return d

    @classmethod
    def from_url(
        cls,
        url: str,
        name: Optional[str] = None,
        media_type: MediaType = MediaType.URL,
    ) -> QueueItem:
        """Create QueueItem from plain url (or local file)."""
        return cls(
            uri=url,
            name=name or url.split("?")[0],
            media_type=media_type,
            streamdetails=StreamDetails(
                provider=ProviderType.URL,
                item_id=url,
                content_type=ContentType.try_parse(url),
                media_type=media_type,
                data=url,
            ),
        )

    @classmethod
    def from_media_item(cls, media_item: Track | Radio):
        """Construct QueueItem from track/radio item."""
        if isinstance(media_item, Track):
            artists = "/".join((x.name for x in media_item.artists))
            name = f"{artists} - {media_item.name}"
        else:
            name = media_item.name
        return cls(
            uri=media_item.uri,
            name=name,
            duration=media_item.duration,
            media_type=media_item.media_type,
            media_item=media_item,
            image=media_item.image,
            available=media_item.available,
        )
