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

FAKE_ARTIST_PREFIX = "_fake://"

# sentinel token value for local (unauthenticated) connections, not via plex.tv
AUTH_TOKEN_UNAUTH = "local_auth"

# item_id prefix used for Plex collections imported as playlists
COLLECTION_ID_PREFIX = "collection:"

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
