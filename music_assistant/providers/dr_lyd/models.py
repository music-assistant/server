"""Typed data models of the DR LYD provider."""

from __future__ import annotations

from dataclasses import dataclass

from mashumaro.mixins.dict import DataClassDictMixin


@dataclass(frozen=True, slots=True)
class Channel(DataClassDictMixin):
    """A live radio channel of DR."""

    slug: str
    name: str
    urn: str
    stream_url: str
    hls: bool
    image_url: str | None
    district: bool


@dataclass(frozen=True, slots=True)
class Series(DataClassDictMixin):
    """A podcast of DR."""

    id: str
    title: str
    description: str | None
    episodes: int
    # epoch seconds of the latest episode
    latest: float | None
    image_url: str | None
    genres: tuple[str, ...]
    explicit: bool
    page_url: str | None


@dataclass(frozen=True, slots=True)
class Episode(DataClassDictMixin):
    """An episode of a podcast of DR, with the audio to play."""

    id: str
    title: str
    description: str | None
    # seconds
    duration: int
    # epoch seconds
    published: float | None
    audio_url: str
    hls: bool
    image_url: str | None
    explicit: bool
    page_url: str | None
    series_id: str | None
    series_title: str | None


@dataclass(frozen=True, slots=True)
class Track(DataClassDictMixin):
    """A track that DR has played on a channel, in epoch seconds."""

    title: str
    artist: str | None
    start: float
    end: float


@dataclass(frozen=True, slots=True)
class Programme(DataClassDictMixin):
    """A programme on air on a channel, until the epoch second end."""

    channel_slug: str
    title: str
    image_url: str | None
    end: float


@dataclass(frozen=True, slots=True)
class NowPlaying:
    """What a channel plays: a track or a programme, with the picture of the programme."""

    title: str
    artist: str | None = None
    image_url: str | None = None
