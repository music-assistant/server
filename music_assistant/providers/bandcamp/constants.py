"""Constants for the Bandcamp music provider."""

from bandcamp_async_api.models import CollectionType
from music_assistant_models.enums import ProviderFeature

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.BROWSE,
    ProviderFeature.RECOMMENDATIONS,
}

# Config keys
CONF_IDENTITY = "identity"
CONF_TOP_TRACKS_LIMIT = "top_tracks_limit"
CONF_GET_LYRICS = "get_lyrics"
DEFAULT_TOP_TRACKS_LIMIT = 50

# Cache TTLs
CACHE_METADATA = 3600 * 24 * 30  # 30 days - artist/album/track metadata rarely changes
CACHE_USER_LISTS = 3600 * 4  # 4 hours - wishlists/following change with user activity
CACHE_EMPTY_RESULTS = 300  # 5 minutes - avoid hammering API for genuinely empty lists

# Browse path slugs
BROWSE_COLLECTION = "collection"
BROWSE_FEED = "feed"
BROWSE_WISHLIST = "wishlist"
BROWSE_FOLLOWING = "following"
BROWSE_FANS = "fans"
BROWSE_FOLLOWERS = "followers"

# Maps person sub-route names to (method_suffix, CollectionType).
# "content" → _browse_person_content, "following" → _browse_person_following,
# "people" → _browse_person_people.
PERSON_SUB_ROUTES: dict[str, tuple[str, CollectionType]] = {
    BROWSE_COLLECTION: ("content", CollectionType.COLLECTION),
    BROWSE_WISHLIST: ("content", CollectionType.WISHLIST),
    BROWSE_FOLLOWING: ("following", CollectionType.FOLLOWING),
    BROWSE_FANS: ("people", CollectionType.FOLLOWING_FANS),
    BROWSE_FOLLOWERS: ("people", CollectionType.FOLLOWERS),
}

# Sub-folders shown for each person's profile
PERSON_SUB_FOLDERS = [
    (BROWSE_COLLECTION, "Collection"),
    (BROWSE_WISHLIST, "Wishlist"),
    (BROWSE_FOLLOWING, "Following"),
    (BROWSE_FANS, "Fans"),
    (BROWSE_FOLLOWERS, "Followers"),
]
