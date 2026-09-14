"""Constants for YT Music provider."""

from enum import StrEnum

YTM_DOMAIN = "https://music.youtube.com"
YTM_COOKIE_DOMAIN = ".youtube.com"
# The cookie field YouTube derives the SAPISIDHASH authorization from: without it a cookie
# was copied from a request that was not signed in.
COOKIE_AUTH_FIELD = "__Secure-3PAPISID"
TRANSLATION_OWNER = "provider.ytmusic"


class YTMRecommendationIcons(StrEnum):
    """Icons for YTM recommendation types."""

    LISTEN_AGAIN = "mdi-book-refresh-outline"
    CONTINUE_WATCHING = "mdi-clock-outline"
    DISCOVER = "mdi-magnify"
    YOUR_MIX = "mdi-music-circle-outline"
    NEW_RELEASES = "mdi-new-box"
    RECOMMENDED = "mdi-star-circle-outline"
    DEFAULT = "mdi-music-note-outline"
