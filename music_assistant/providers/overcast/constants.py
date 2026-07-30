"""Constants for the Overcast provider."""

from __future__ import annotations

from music_assistant_models.enums import ProviderFeature

BASE_URL = "https://overcast.fm"
LOGIN_URL = f"{BASE_URL}/login"
PODCASTS_URL = f"{BASE_URL}/podcasts"
OPML_EXPORT_URL = f"{BASE_URL}/account/export_opml/extended"

SESSION_COOKIE_NAME = "o"

CONF_SESSION_COOKIE = "session_cookie"
CONF_MAX_NUM_EPISODES = "max_num_episodes"

CACHE_CATEGORY_OPML = 1
CACHE_KEY_LAST_APPLIED = "last_applied_playback_ts"  # ISO datetime of newest applied progress

# The OPML export endpoint is rate limited by Overcast (roughly 10 requests/day),
# so the export is cached to match the default 12h library sync interval and is
# served stale while a background refresh runs.
OPML_CACHE_EXPIRATION = 12 * 3600
RATE_LIMIT_FALLBACK_BACKOFF = 3600

# Overcast answers a request carrying a rejected session cookie with a redirect to
# the login page, so any redirect away from the export means the session is gone.
AUTH_REJECT_STATUSES = (301, 302, 303, 307, 308)

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_PODCASTS,
    ProviderFeature.BROWSE,
}
