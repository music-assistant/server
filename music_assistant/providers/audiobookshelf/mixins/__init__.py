"""Mixins for Audiobookshelf."""

from .artist_mixin import ArtistsMixin
from .audiobook_mixin import AudiobooksMixin
from .browse_mixin import BrowseMixin
from .playlist_mixin import PlaylistMixin
from .podcast_mixin import PodcastsMixin
from .recommendations_mixin import RecommendationsMixin
from .socket_mixin import SocketMixin
from .streams_mixin import StreamsMixin

__all__ = [
    "ArtistsMixin",
    "AudiobooksMixin",
    "BrowseMixin",
    "PlaylistMixin",
    "PodcastsMixin",
    "RecommendationsMixin",
    "SocketMixin",
    "StreamsMixin",
]
