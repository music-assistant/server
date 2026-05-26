"""Trimmed response dataclasses used in tool replies.

Tools that need to return a Music Assistant entity use these light-weight shapes
to keep payloads small for LLM context windows. Resources, by contrast, return
the full ``music_assistant_models`` types directly because clients usually
expect a complete object when they fetch a URI.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TrackBrief:
    """A track summary for tool responses."""

    uri: str
    name: str
    artists: list[str] = field(default_factory=list)
    album: str | None = None
    duration: int | None = None


@dataclass
class AlbumBrief:
    """An album summary for tool responses."""

    uri: str
    name: str
    artist: str | None = None
    year: int | None = None


@dataclass
class ArtistBrief:
    """An artist summary for tool responses."""

    uri: str
    name: str


@dataclass
class PlaylistBrief:
    """A playlist summary for tool responses."""

    uri: str
    name: str
    track_count: int | None = None
    owner: str | None = None


@dataclass
class RadioBrief:
    """A radio summary for tool responses."""

    uri: str
    name: str
    description: str | None = None


@dataclass
class PlayerBrief:
    """A player summary for tool responses."""

    player_id: str
    name: str
    state: str
    volume_level: int | None = None
    powered: bool = True
    current_item: str | None = None


@dataclass
class QueueItemBrief:
    """A queue item summary."""

    item_id: str
    name: str
    duration: int | None = None
    artists: list[str] = field(default_factory=list)


@dataclass
class QueueBrief:
    """A queue summary for tool responses."""

    queue_id: str
    current_index: int | None
    item_count: int
    shuffle: bool
    repeat: str
    items: list[QueueItemBrief] = field(default_factory=list)


@dataclass
class RecommendationFolderBrief:
    """One curated recommendation folder (e.g. "Mood: Focus") with its track URIs."""

    name: str
    item_uris: list[str] = field(default_factory=list)
