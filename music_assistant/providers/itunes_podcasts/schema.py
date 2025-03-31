"""Schema for iTunes Podcast Search.

Only what is needed.
"""

from dataclasses import dataclass, field

from mashumaro import field_options
from mashumaro.config import BaseConfig
from mashumaro.mixins.json import DataClassJSONMixin


class _BaseModel(DataClassJSONMixin):
    """Model shared between schema definitions."""

    class Config(BaseConfig):
        """Base configuration."""

        forbid_extra_keys = False
        serialize_by_alias = True


@dataclass(kw_only=True)
class PodcastSearchResult(_BaseModel):
    """PodcastSearchResult."""

    collection_id: int | None = field(metadata=field_options(alias="collectionId"), default=None)
    kind: str | None = None
    artist_name: str | None = field(metadata=field_options(alias="artistName"), default=None)
    collection_name: str | None = field(
        metadata=field_options(alias="collectionName"), default=None
    )
    collection_censored_name: str | None = field(
        metadata=field_options(alias="collectionCensoredName"), default=None
    )
    track_name: str | None = field(metadata=field_options(alias="trackName"), default=None)
    track_censored_name: str | None = field(
        metadata=field_options(alias="trackCensoredName"), default=None
    )
    feed_url: str | None = field(metadata=field_options(alias="feedUrl"), default=None)
    artwork_url_30: str | None = field(metadata=field_options(alias="artworkUrl30"), default=None)
    artwork_url_60: str | None = field(metadata=field_options(alias="artworkUrl60"), default=None)
    artwork_url_100: str | None = field(metadata=field_options(alias="artworkUrl100"), default=None)
    artwork_url_600: str | None = field(metadata=field_options(alias="artworkUrl600"), default=None)
    release_date: str | None = field(metadata=field_options(alias="releaseDate"), default=None)
    track_count: int = field(metadata=field_options(alias="trackCount"), default=0)
    primary_genre_name: str | None = field(
        metadata=field_options(alias="primaryGenreName"), default=None
    )
    genres: list[str] = field(default_factory=list)


@dataclass(kw_only=True)
class ITunesSearchResults(_BaseModel):
    """SearchResults."""

    result_count: int = field(metadata=field_options(alias="resultCount"), default=0)
    results: list[PodcastSearchResult] = field(default_factory=list)


# below is only what we need


@dataclass(kw_only=True)
class TopPodcastsGenres(_BaseModel):
    """TopPodcastsGenres."""

    genre_id: str | int = field(metadata=field_options(alias="genreId"), default="")
    name: str


@dataclass(kw_only=True)
class TopPodcastsResult(_BaseModel):
    """TopPodcastsResult."""

    artist_name: str = field(metadata=field_options(alias="artistName"), default="")
    id_: str | int = field(metadata=field_options(alias="id"), default="")
    name: str = ""
    genres: list[TopPodcastsGenres] = field(default_factory=list)
    artwork_url_30: str | None = field(metadata=field_options(alias="artworkUrl30"), default=None)
    artwork_url_60: str | None = field(metadata=field_options(alias="artworkUrl60"), default=None)
    artwork_url_100: str | None = field(metadata=field_options(alias="artworkUrl100"), default=None)
    artwork_url_600: str | None = field(metadata=field_options(alias="artworkUrl600"), default=None)
    content_advisory_rating: str | None = field(
        metadata=field_options(alias="contentAdvisoryRating"), default=None
    )


@dataclass(kw_only=True)
class TopPodcastsResults(_BaseModel):
    """TopPodcastsResults."""

    country: str
    results: list[TopPodcastsResult] = field(default_factory=list)


@dataclass(kw_only=True)
class TopPodcastsResponse(_BaseModel):
    """TopPodcastsResponse."""

    feed: TopPodcastsResults | None = None


# HELPER
@dataclass(kw_only=True)
class TopPodcastsHelper(_BaseModel):
    """TopPodcastsHelper.

    This is used to cache the recommendations.
    """

    top_podcasts: list[PodcastSearchResult] = field(default_factory=list)
