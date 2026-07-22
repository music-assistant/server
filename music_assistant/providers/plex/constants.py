"""Constants for the Plex Music Provider."""

from __future__ import annotations

CONF_ACTION_AUTH_MYPLEX = "auth_myplex"
CONF_ACTION_AUTH_LOCAL = "auth_local"
CONF_ACTION_CLEAR_AUTH = "auth"
CONF_ACTION_LIBRARY = "library"
CONF_ACTION_GDM = "gdm"

CONF_AUTH_TOKEN = "token"
CONF_LIBRARY_ID = "library_id"
CONF_LOCAL_SERVER_IP = "local_server_ip"
CONF_LOCAL_SERVER_PORT = "local_server_port"
CONF_LOCAL_SERVER_SSL = "local_server_ssl"
CONF_LOCAL_SERVER_VERIFY_CERT = "local_server_verify_cert"
CONF_IMPORT_COLLECTIONS = "import_collections"
CONF_COLLECTION_PREFIX = "collection_prefix"
CONF_PLEX_LIKE_RATING = "plex_like_rating"
CONF_PLEX_FAVORITE_THRESHOLD = "plex_favorite_threshold"
CONF_PLEX_UNLIKE_RATING = "plex_unlike_rating"
CONF_HUB_ITEMS_LIMIT = "hub_items_limit"
CONF_EXTENDED_RECOMMENDATIONS = "extended_recommendations"

FAKE_ARTIST_PREFIX = "_fake://"

# sentinel token value for local (unauthenticated) connections, not via plex.tv
AUTH_TOKEN_UNAUTH = "local_auth"

# item_id prefix used for Plex collections imported as playlists
COLLECTION_ID_PREFIX = "collection:"

# item_id prefix used for Plex "Mixes For You" playlists
MIX_ITEM_PREFIX = "mix:"

# Mix title/artwork are cached so replay from recently-played survives Plex
# rotating the mix out of its hub. 90 days is chosen to outlive MA's
# playlog retention (see controllers/music.py: _cleanup_database).
MIX_CACHE_EXPIRATION = 86400 * 90

# Query parameters passed to /hubs/sections when loading recommendations.
# Some of these are not in public Plex docs but are required to surface
# the "Mixes For You" hub and library playlists.
RECOMMENDATIONS_HUB_PARAMS = (
    "includeMyMixes=1"
    "&includeStations=1"
    "&includeLibraryPlaylists=1"
    "&includeExternalMetadata=1"
    "&includeAnniversaryReleases=1"
    "&excludeElements=Similar,Mood"
    "&excludeFields=summary"
)

# error messages (templates use str.format)
ERR_INVALID_CREDENTIALS = "Invalid login credentials"
ERR_AUTH_FAILED = "Authentication failed"
ERR_MYPLEX_AUTH_FAILED = "Authentication to MyPlex failed"
ERR_MYPLEX_TOKEN_NOT_RECEIVED = "Authentication to MyPlex failed: token not received"
ERR_NO_LIBRARIES = "Unable to retrieve Servers and/or Music Libraries"
ERR_ITEM_NOT_FOUND = "Item {item_id} not found"
ERR_ARTIST_NOT_FOUND = "Artist not found: {item_id}"
ERR_TRACK_NOT_FOUND = "Track {item_id} not found"
ERR_ARTIST_INVALID_ID = "Artist does not have a valid ID"
ERR_NO_ARTIST_FOR_TRACK = "No artist was found for track"
