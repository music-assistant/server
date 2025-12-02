"""Constants for the Pocket Casts provider."""

from __future__ import annotations

# Pocket Casts API endpoints
POCKETCASTS_API_BASE = "https://api.pocketcasts.com"
POCKETCASTS_LOGIN_URL = f"{POCKETCASTS_API_BASE}/user/login"
POCKETCASTS_SUBSCRIPTION_STATUS_URL = f"{POCKETCASTS_API_BASE}/subscription/status"
POCKETCASTS_PODCAST_LIST_URL = f"{POCKETCASTS_API_BASE}/user/podcast/list"

# Podcast episodes API (separate subdomain, uses redirect)
POCKETCASTS_PODCAST_FULL_URL = "https://podcast-api.pocketcasts.com/podcast/full/{uuid}"

# Episode progress API endpoints
POCKETCASTS_PODCAST_EPISODES_URL = f"{POCKETCASTS_API_BASE}/user/podcast/episodes"
POCKETCASTS_SYNC_UPDATE_EPISODE_URL = f"{POCKETCASTS_API_BASE}/sync/update_episode"
POCKETCASTS_IN_PROGRESS_URL = f"{POCKETCASTS_API_BASE}/user/in_progress"
POCKETCASTS_STARRED_URL = f"{POCKETCASTS_API_BASE}/user/starred"
POCKETCASTS_NEW_RELEASES_URL = f"{POCKETCASTS_API_BASE}/user/new_releases"
POCKETCASTS_HISTORY_URL = f"{POCKETCASTS_API_BASE}/user/history"
POCKETCASTS_BOOKMARKS_URL = f"{POCKETCASTS_API_BASE}/user/bookmark/list"
POCKETCASTS_UP_NEXT_URL = f"{POCKETCASTS_API_BASE}/up_next/list"

# Artwork URL pattern
POCKETCASTS_ARTWORK_URL = "https://static.pocketcasts.com/discover/images/webp/200/{uuid}.webp"

# Browse path constants
BROWSE_UP_NEXT = "up_next"
BROWSE_IN_PROGRESS = "in_progress"
BROWSE_STARRED = "starred"
BROWSE_NEW_RELEASES = "new_releases"
BROWSE_HISTORY = "history"
BROWSE_BOOKMARKS = "bookmarks"

# Episode playing status constants (from Pocket Casts API)
STATUS_NOT_PLAYED = 1
STATUS_IN_PROGRESS = 2
STATUS_COMPLETED = 3
