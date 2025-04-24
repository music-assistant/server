"""Constants for YT Music provider."""

from enum import StrEnum


class YTMRecommendationIcons(StrEnum):
    """Icons for YTM recommendation types."""

    LISTEN_AGAIN = "mdi-book-refresh-outline"
    CONTINUE_WATCHING = "mdi-clock-outline"
    DISCOVER = "mdi-magnify"
    YOUR_MIX = "mdi-music-circle-outline"
    NEW_RELEASES = "mdi-new-box"
    RECOMMENDED = "mdi-star-circle-outline"
    DEFAULT = "mdi-music-note-outline"
