"""Constants for Emby provider."""

# Emby API item keys
AUTH_ACCESS_TOKEN = "AccessToken"
AUTH_USER = "User"

ITEMS = "Items"
ITEM_LIMIT = 500
ITEM_KEY_ID = "Id"
ITEM_KEY_RUNTIME_TICKS = "RunTimeTicks"
ITEM_KEY_MEDIA_STREAMS = "MediaStreams"
ITEM_KEY_COLLECTION_TYPE = "CollectionType"
ITEM_KEY_NAME = "Name"
ITEM_KEY_ALBUM_ID = "AlbumId"
ITEM_KEY_ALBUM_NAME = "Album"
ITEM_KEY_ARTIST_ITEMS = "ArtistItems"
ITEM_KEY_IMAGE_TAGS = "ImageTags"
ITEM_KEY_DATE_CREATED = "DateCreated"
ITEM_KEY_PRODUCTION_YEAR = "ProductionYear"
ITEM_KEY_OVERVIEW = "Overview"
ITEM_KEY_DURATION = "Duration"
ITEM_KEY_ARTISTS = "Artists"
ITEM_KEY_PLAYLIST_ITEMS = "PlaylistItems"
ITEM_KEY_TYPE = "Type"
ITEM_KEY_CONTAINER = "Container"

AUDIO_STREAM_CODEC = "Codec"
AUDIO_STREAM_SAMPLE_RATE = "SampleRate"
AUDIO_STREAM_BIT_DEPTH = "BitDepth"
AUDIO_STREAM_CHANNELS = "Channels"

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
