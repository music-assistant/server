"""Typed data models and error types for the VRT MAX provider."""

from __future__ import annotations

from dataclasses import dataclass

from mashumaro.mixins.dict import DataClassDictMixin
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ResourceTemporarilyUnavailable,
)


@dataclass(frozen=True, slots=True)
class VrtStation:
    """A single VRT MAX live radio station."""

    id: str
    name: str
    stream_url: str
    aac_url: str | None = None
    logo_url: str | None = None
    tagline: str | None = None


@dataclass(frozen=True, slots=True)
class VrtStreamInfo:
    """The playable stream reference for an on-demand episode."""

    stream_id: str
    duration: int = 0


@dataclass(frozen=True, slots=True)
class VrtChapter:
    """A tracklist entry (played song) mapped to an episode chapter."""

    position: int
    name: str
    start: float  # seconds from the episode start
    end: float | None = None


@dataclass(frozen=True, slots=True)
class VrtResumeTarget(DataClassDictMixin):
    """The resume-point write target for an on-demand episode."""

    media_id: str
    media_name: str
    duration: int = 0


@dataclass(frozen=True, slots=True)
class VrtProgress:
    """The user's playback progress for an on-demand episode."""

    completed: bool
    position: int  # seconds


@dataclass(frozen=True, slots=True)
class VrtRow:
    """A single tile row on a landing (ThemePage) page."""

    title: str
    component_id: str
    tile_type: str | None


@dataclass(frozen=True, slots=True)
class VrtProgramTile:
    """A program/podcast tile (a folder of episodes) parsed from a tile list."""

    page_id: str
    title: str
    description: str | None = None
    image_url: str | None = None


@dataclass(frozen=True, slots=True)
class VrtSeason(DataClassDictMixin):
    """A paginable episode list (a season / listen-back tab) within a program page."""

    title: str | None
    component_id: str


@dataclass(frozen=True, slots=True)
class VrtProgram(DataClassDictMixin):
    """A radio program archive or podcast (maps to an MA Podcast)."""

    page_id: str
    title: str
    description: str | None = None
    image_url: str | None = None
    publisher: str | None = None
    presenters: tuple[str, ...] = ()
    seasons: tuple[VrtSeason, ...] = ()


@dataclass(frozen=True, slots=True)
class VrtEpisode:
    """A single on-demand episode (maps to an MA PodcastEpisode)."""

    page_id: str
    title: str
    description: str | None = None
    image_url: str | None = None
    duration: int = 0
    date_label: str | None = None
    fully_played: bool = False
    resume_position: int = 0  # seconds


class VrtApiError(ResourceTemporarilyUnavailable):
    """Raised on a VRT API transport/protocol error (network, HTTP, bad payload)."""


class VrtNotFoundError(MediaNotFoundError):
    """Raised when requested VRT content genuinely does not exist (empty page)."""


class VrtAuthError(LoginFailed):
    """Raised when VRT authentication (SSO login / token exchange) fails."""
