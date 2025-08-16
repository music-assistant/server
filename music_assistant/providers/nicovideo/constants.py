"""Constants for the nicovideo provider in Music Assistant."""

from __future__ import annotations

from enum import Enum

from music_assistant_models.enums import ContentType


class ApiPriority(Enum):
    """Priority levels for nicovideo API calls."""

    HIGH = "high"
    LOW = "low"


# Configuration keys for nicovideo provider settings
CONF_MAIL = "mail"
CONF_MFA = "mfa"
CONF_USER_SESSION = "user_session"
CONF_AUTO_LIKE_ON_LIBRARY_ADD = "auto_like_on_library_add"
CONF_USE_FOLLOW_UNFOLLOW_ARTISTS = "use_follow_unfollow_artists"
CONF_INCLUDE_FOLLOWED_MYLISTS = "include_followed_mylists"
CONF_INCLUDE_FOLLOWED_MYLISTS_TRACKS = "include_followed_mylists_tracks"
CONF_INCLUDE_OWN_SERIES_ALBUMS = "include_own_series_albums"
CONF_INCLUDE_OWN_VIDEOS_TRACKS = "include_own_videos_tracks"
CONF_INCLUDE_OWN_MYLISTS_TRACKS = "include_own_mylists_tracks"
CONF_INCLUDE_LIBRARY_TRACK_ARTISTS = "include_library_track_artists"
CONF_RECOMMENDATION_COUNT = "recommendation_count"
CONF_RECOMMENDATION_FILTER_TAGS = "recommendation_filter_tags"
CONF_TAG_RECOMMENDATION_TAGS = "tag_recommendation_tags"
CONF_TAG_RECOMMENDATION_NEW_TRACKS_TAGS = "tag_recommendation_new_tracks_tags"
CONF_HISTORY_COUNT = "history_count"
CONF_FOLLOWING_ACTIVITIES_COUNT = "following_activities_count"

# Network constants
NICOVIDEO_USER_AGENT = "Music Assistant/1.0"

# Audio format constants based on niconico official specifications
# Sources:
# - https://qa.nicovideo.jp/faq/show/21908
# - https://qa.nicovideo.jp/faq/show/5685
NICOVIDEO_CONTENT_TYPE = ContentType.MP4
NICOVIDEO_CODEC_TYPE = ContentType.AAC
NICOVIDEO_AUDIO_CHANNELS = 2  # Stereo (2ch)
NICOVIDEO_AUDIO_BIT_DEPTH = 16  # 16-bit (confirmed from downloaded video analysis)
