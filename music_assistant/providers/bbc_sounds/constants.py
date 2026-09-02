"""Constants for BBC Sounds provider."""

from typing import Literal


class _Constants:
    # This is the image id that is shown when there's no track image
    BLANK_IMAGE_NAME: str = "p0bqcdzf"
    DEFAULT_IMAGE_SIZE = 1280
    TRACK_DURATION_THRESHOLD: int = 300  # 5 minutes
    NOW_PLAYING_REFRESH_TIME: int = 5
    HLS: Literal["hls"] = "hls"
    DASH: Literal["dash"] = "dash"
    CONF_SHOW_LOCAL: str = "show_local"
    CONF_INTRO: str = "intro"
    CONF_STREAM_FORMAT: str = "stream_format"
    CONF_STREAM_FORMAT_HLS: str = HLS
    CONF_STREAM_FORMAT_DASH: str = DASH
    DEFAULT_EXPIRATION = 60 * 60 * 24 * 30  # 30 days
    SHORT_EXPIRATION = 60 * 60 * 3  # 3 hours
    DYNAMIC_EXPIRATION = 60  # 1 minute
    ARTWORK_TIMEOUT = 15
