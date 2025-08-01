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
CONF_INCLUDE_FOLLOWING_MYLISTS = "include_following_mylists"
CONF_INCLUDE_FOLLOWING_MYLISTS_TRACKS = "include_following_mylists_tracks"
CONF_INCLUDE_OWN_MYLISTS_TRACKS = "include_own_mylists_tracks"
CONF_REQUIRED_TAGS_FOR_RECOMMENDATIONS = "required_tags_for_recommendations"
CONF_RECOMMENDATION_COUNT = "recommendation_count"
CONF_HISTORY_COUNT = "history_count"
CONF_FOLLOWING_ACTIVITIES_COUNT = "following_activities_count"

NICONICO_COOKIE_DOMAIN = ".nicovideo.jp"
