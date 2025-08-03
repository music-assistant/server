"""Constants for the Niconico provider in Music Assistant."""

from __future__ import annotations

from enum import Enum


class ApiPriority(Enum):
    """Priority levels for NicoNico API calls."""

    HIGH = "high"
    LOW = "low"


CONF_MAIL = "mail"
CONF_MFA = "mfa"
CONF_USER_SESSION = "user_session"
CONF_SENSITIVE_CONTENTS = "sensitive_contents"
CONF_AUTO_LIKE_ON_LIBRARY_ADD = "auto_like_on_library_add"
CONF_USE_FOLLOW_UNFOLLOW_ARTISTS = "use_follow_unfollow_artists"
CONF_INCLUDE_FOLLOWED_MYLISTS = "include_followed_mylists"
CONF_INCLUDE_FOLLOWED_MYLISTS_TRACKS = "include_followed_mylists_tracks"
CONF_INCLUDE_OWN_SERIES_ALBUMS = "include_own_series_albums"
CONF_INCLUDE_OWN_VIDEOS_TRACKS = "include_own_videos_tracks"
CONF_INCLUDE_OWN_MYLISTS_TRACKS = "include_own_mylists_tracks"
CONF_INCLUDE_LIBRARY_TRACK_ARTISTS = "include_library_track_artists"
CONF_REQUIRED_TAGS_FOR_RECOMMENDATIONS = "required_tags_for_recommendations"
CONF_RECOMMENDATION_COUNT = "recommendation_count"
CONF_HISTORY_COUNT = "history_count"
CONF_FOLLOWING_ACTIVITIES_COUNT = "following_activities_count"

NICONICO_COOKIE_DOMAIN = ".nicovideo.jp"
