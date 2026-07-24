"""Mixins for Audiobookshelf."""

from .artist_mixin import AbsArtistsMixin
from .audiobook_mixin import AbsAudiobooksMixin
from .browse_mixin import AbsBrowseMixin
from .playlist_mixin import AbsPlaylistMixin
from .podcast_mixin import AbsPodcastsMixin
from .recommendations_mixin import AbsRecommendationsMixin
from .socket_mixin import AbsSocketMixin
from .streams_mixin import AbsStreamsMixin

__all__ = [
    "AbsArtistsMixin",
    "AbsAudiobooksMixin",
    "AbsBrowseMixin",
    "AbsPlaylistMixin",
    "AbsPodcastsMixin",
    "AbsRecommendationsMixin",
    "AbsSocketMixin",
    "AbsStreamsMixin",
]
