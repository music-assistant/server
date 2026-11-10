"""Models of the DR LYD provider: the documents of the radio API of DR and the live channels."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from mashumaro import DataClassDictMixin, field_options
from mashumaro.config import BaseConfig


class Document(DataClassDictMixin):
    """A document of the radio API of DR, of which the fields that the provider reads are listed."""

    class Config(BaseConfig):
        """Serialize the fields under the names DR uses, so a cached document reads back."""

        serialize_by_alias = True


@dataclass(frozen=True, slots=True, kw_only=True)
class DRImageAsset(Document):
    """A picture of an item of DR."""

    id: str
    target: str
    ratio: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DRAudioAsset(Document):
    """A way to listen to an item of DR."""

    format: str
    target: str
    url: str
    bitrate: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class DRChannel(Document):
    """A channel of DR, which lists the regional channels it has."""

    id: str
    slug: str
    title: str
    is_flow_channel: bool = field(metadata=field_options(alias="isFlowChannel"))
    audio_assets: list[DRAudioAsset] = field(
        metadata=field_options(alias="audioAssets"), default_factory=list
    )
    channel_logos: list[DRImageAsset] = field(
        metadata=field_options(alias="channelLogos"), default_factory=list
    )
    image_assets: list[DRImageAsset] = field(
        metadata=field_options(alias="imageAssets"), default_factory=list
    )
    districts: list[DRChannel] = field(default_factory=list)


@dataclass(frozen=True, slots=True, kw_only=True)
class DRChannelRef(Document):
    """The channel that a programme is on."""

    slug: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DRProgramme(Document):
    """A programme on air on a channel, until its end."""

    title: str
    end_time: datetime = field(metadata=field_options(alias="endTime"))
    channel: DRChannelRef
    image_assets: list[DRImageAsset] = field(metadata=field_options(alias="imageAssets"))


@dataclass(frozen=True, slots=True, kw_only=True)
class DRRole(Document):
    """A credit on a track."""

    role: str
    name: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DRTrack(Document):
    """A track that DR has played on a channel."""

    title: str
    description: str
    played_time: datetime = field(metadata=field_options(alias="playedTime"))
    duration_milliseconds: int = field(metadata=field_options(alias="durationMilliseconds"))
    classical: bool
    roles: list[DRRole]

    @property
    def end_time(self) -> datetime:
        """Return the time the track is over."""
        return self.played_time + timedelta(milliseconds=self.duration_milliseconds)


@dataclass(frozen=True, slots=True, kw_only=True)
class DRSeries(Document):
    """A series of DR, which is a podcast."""

    production_number: str = field(metadata=field_options(alias="productionNumber"))
    title: str
    description: str
    categories: list[str]
    latest_episode_start_time: datetime = field(
        metadata=field_options(alias="latestEpisodeStartTime")
    )
    image_assets: list[DRImageAsset] = field(metadata=field_options(alias="imageAssets"))
    explicit_content: bool = field(metadata=field_options(alias="explicitContent"))
    presentation_url: str = field(metadata=field_options(alias="presentationUrl"))
    is_available_on_demand: bool = field(metadata=field_options(alias="isAvailableOnDemand"))


@dataclass(frozen=True, slots=True, kw_only=True)
class DRSeriesRef(Document):
    """The series that an episode is part of."""

    production_number: str = field(metadata=field_options(alias="productionNumber"))


@dataclass(frozen=True, slots=True, kw_only=True)
class DREpisode(Document):
    """An episode of a series of DR."""

    production_number: str = field(metadata=field_options(alias="productionNumber"))
    title: str
    description: str
    duration_milliseconds: int = field(metadata=field_options(alias="durationMilliseconds"))
    start_time: datetime = field(metadata=field_options(alias="startTime"))
    image_assets: list[DRImageAsset] = field(metadata=field_options(alias="imageAssets"))
    audio_assets: list[DRAudioAsset] = field(metadata=field_options(alias="audioAssets"))
    explicit_content: bool = field(metadata=field_options(alias="explicitContent"))
    presentation_url: str = field(metadata=field_options(alias="presentationUrl"))
    is_available_on_demand: bool = field(metadata=field_options(alias="isAvailableOnDemand"))
    series: DRSeriesRef


@dataclass(frozen=True, slots=True, kw_only=True)
class Page[ItemT](Document):
    """A page of a list of DR, which links to the next page if there is one."""

    items: list[ItemT]
    next: str | None = None


class SeriesPage(Page[DRSeries]):
    """A page of series."""


class EpisodePage(Page[DREpisode]):
    """A page of episodes."""


class TrackPage(Page[DRTrack]):
    """A page of tracks."""


@dataclass(frozen=True, slots=True)
class Channel(DataClassDictMixin):
    """A live radio channel of DR, with the stream to play."""

    slug: str
    name: str
    urn: str
    stream_url: str
    hls: bool
    image_url: str | None
