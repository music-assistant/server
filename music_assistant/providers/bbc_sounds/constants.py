"""Constants for BBC Sounds provider."""

from enum import StrEnum, auto


class Constants:
    """Constants used across the BBC Sounds provider."""

    # This is the image id that is shown when there's no track image
    BLANK_IMAGE_NAME: str = "p0bqcdzf"
    DEFAULT_IMAGE_SIZE = 1280
    TRACK_DURATION_THRESHOLD: int = 300  # 5 minutes
    NOW_PLAYING_REFRESH_TIME: int = 10
    CONF_INTRO: str = "intro"
    DEFAULT_EXPIRATION = 60 * 60 * 24 * 30  # 30 days
    SHORT_EXPIRATION = 60 * 60 * 3  # 3 hours
    DYNAMIC_EXPIRATION = 60  # 1 minute
    ARTWORK_TIMEOUT = 15

    # The latest news playlist has a varying suffix on it
    LATEST_NEWS_PLAYLIST_SUFFIX = "latest_playables_for_curation"

    # Schedule dates are UK broadcast days
    SCHEDULE_TIMEZONE = "Europe/London"


class ValidMenuIDs(StrEnum):
    """Expected main menu IDs from the API / library."""

    LIVE = auto()  # The constructed listen live menu
    LISTEN_LIVE = auto()  # The built-in listen live menu
    CATCH_UP = auto()
    SCHEDULES = auto()
    MY_SOUNDS = auto()
    CONTINUE_LISTENING = auto()
    EDITORIAL_COLLECTION = auto()
    LOCAL_RAIL = auto()
    SINGLE_ITEM_PROMO = auto()
    COLLECTIONS = auto()
    CATEGORIES = auto()
    RECOMMENDATIONS = auto()
    UNMISSABLE_SPEECH = auto()
    PODCASTS = auto()
    UNMISSABLE_MUSIC = auto()
    MUSIC = auto()
    EXPLORE = auto()
    STATIONS = auto()
    NEWS = auto()
