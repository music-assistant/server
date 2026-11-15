"""Models for the CBC Listen API and provider items."""

from __future__ import annotations

from dataclasses import dataclass, field

from mashumaro import DataClassDictMixin, field_options
from mashumaro.config import BaseConfig


class Document(DataClassDictMixin):
    """Base class for CBC Listen API JSON documents."""

    class Config(BaseConfig):
        """Serialize fields under their original CBC alias for cache round-trips."""

        serialize_by_alias = True


@dataclass(frozen=True, slots=True, kw_only=True)
class CBCStream(Document):
    """A regional live radio stream on a CBC network."""

    id: int
    full_title: str = field(metadata=field_options(alias="fullTitle"))
    province: str = ""
    stream_url: str = field(metadata=field_options(alias="streamURL"), default="")
    call_sign: str = field(metadata=field_options(alias="callSign"), default="")
    location_key: str = field(metadata=field_options(alias="programGuideLocationKey"), default="")


@dataclass(frozen=True, slots=True, kw_only=True)
class CBCNetwork(Document):
    """A CBC live radio network (Radio One or CBC Music) and its regional streams."""

    network_id: int = field(metadata=field_options(alias="networkID"))
    streams: list[CBCStream] = field(default_factory=list)


@dataclass(frozen=True, slots=True, kw_only=True)
class CBCProgram(Document):
    """A scheduled radio program in a station's program queue."""

    # Syndicated and special programs send a string slug for showID rather than an integer.
    show_id: int | str | None = field(metadata=field_options(alias="showID"), default=None)
    program_title: str = field(metadata=field_options(alias="programTitle"), default="")
    host_name: str = field(metadata=field_options(alias="hostName"), default="")
    program_image: str = field(metadata=field_options(alias="programImage"), default="")
    epoch_start: int = field(metadata=field_options(alias="epochStart"), default=0)
    epoch_end: int = field(metadata=field_options(alias="epochEnd"), default=0)


@dataclass(frozen=True, slots=True, kw_only=True)
class CBCTrack(Document):
    """A track entry in a show's daily playlog."""

    title: str
    artists: str = ""
    composers: str = ""
    album: str = ""
    broadcasted_time: int = field(metadata=field_options(alias="broadcastedTime"), default=0)


@dataclass(frozen=True, slots=True, kw_only=True)
class CBCShow(Document):
    """A CBC podcast or on-demand radio show."""

    id: int
    title: str
    description: str = ""
    hosts: str = ""
    image: str = ""
    web_url: str = field(metadata=field_options(alias="webURL"), default="")
    original_podcast: bool = field(metadata=field_options(alias="originalPodcast"), default=False)
    episode_count: int = field(metadata=field_options(alias="episodeCount"), default=0)
    segment_count: int = field(metadata=field_options(alias="segmentCount"), default=0)
    category: str | None = field(
        metadata=field_options(alias="itunesCategoryDescription"), default=None
    )

    @property
    def has_audio(self) -> bool:
        """Return whether the show has any episodes or segments."""
        return (self.episode_count + self.segment_count) > 0


@dataclass(frozen=True, slots=True, kw_only=True)
class CBCClip(Document):
    """An episode or segment audio clip belonging to a CBC podcast or radio show."""

    clip_id: int = field(metadata=field_options(alias="clipID"))
    show_id: int = field(metadata=field_options(alias="showID"))
    title: str
    description: str = ""
    src: str = ""
    duration: int = 0
    released_at: int = field(metadata=field_options(alias="releasedAt"), default=0)
    image_url: str = field(metadata=field_options(alias="imageURL"), default="")
    web_url: str = field(metadata=field_options(alias="webURL"), default="")
    content_tier: str = field(metadata=field_options(alias="contentTier"), default="")


@dataclass(frozen=True, slots=True, kw_only=True)
class CBCClipPage(Document):
    """A page of clips for a CBC show."""

    clip_count: int = field(metadata=field_options(alias="clipCount"), default=0)
    clips: list[CBCClip] = field(default_factory=list)


@dataclass(frozen=True, slots=True, kw_only=True)
class CBCPlaylogEpisode(Document):
    """Summary of one broadcast playlog episode for a show."""

    episode_date_id: int = field(metadata=field_options(alias="episodeDateId"))


@dataclass(frozen=True, slots=True, kw_only=True)
class CBCPlaylogPage(Document):
    """A page of playlog episode summaries for a show."""

    episodes: list[CBCPlaylogEpisode] = field(default_factory=list)


@dataclass(frozen=True, slots=True, kw_only=True)
class Channel(DataClassDictMixin):
    """A playable live radio station of CBC Listen."""

    station_id: str
    name: str
    network_id: int
    location_key: str
    province: str
    call_sign: str
    stream_url: str
