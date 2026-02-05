"""Constants for Emby provider."""

# Emby API item keys
ITEM_KEY_ID = "Id"
ITEM_KEY_RUNTIME_TICKS = "RunTimeTicks"
ITEM_KEY_MEDIA_STREAMS = "MediaStreams"
ITEM_KEY_COLLECTION_TYPE = "CollectionType"

# Field lists for API requests
TRACK_FIELDS = [
    "Name",
    "Artists",
    "Album",
    "AlbumId",
    "Duration",
    "RunTimeTicks",
    "MediaStreams",
    "ImageTags",
    "DateCreated",
]

ALBUM_FIELDS = [
    "Name",
    "Artists",
    "ArtistItems",
    "Overview",
    "ImageTags",
    "DateCreated",
    "ProductionYear",
]

ARTIST_FIELDS = [
    "Name",
    "Overview",
    "ImageTags",
    "DateCreated",
]

# Supported audio containers for streaming
SUPPORTED_CONTAINER_FORMATS = ["mp3", "flac", "aac", "opus", "wav", "m4a"]

# App/user agent name
USER_APP_NAME = "MusicAssistant"
