"""Constants for the Jellyfin integration."""

from typing import Final

DOMAIN: Final = "jellyfin"

CLIENT_VERSION: Final = "0.1"

COLLECTION_TYPE_MOVIES: Final = "movies"
COLLECTION_TYPE_MUSIC: Final = "music"
COLLECTION_TYPE_TVSHOWS: Final = "tvshows"

CONF_CLIENT_DEVICE_ID: Final = "client_device_id"

DEFAULT_NAME: Final = "Jellyfin"

ITEM_KEY_COLLECTION_TYPE: Final = "CollectionType"
ITEM_KEY_ID: Final = "Id"
ITEM_KEY_IMAGE_TAGS: Final = "ImageTags"
ITEM_KEY_INDEX_NUMBER: Final = "IndexNumber"
ITEM_KEY_MEDIA_SOURCES: Final = "MediaSources"
ITEM_KEY_MEDIA_TYPE: Final = "MediaType"
ITEM_KEY_MEDIA_STREAMS: Final = "MediaStreams"
ITEM_KEY_MEDIA_CHANNELS: Final = "Channels"
ITEM_KEY_MEDIA_CODEC: Final = "Codec"
ITEM_KEY_NAME: Final = "Name"
ITEM_KEY_PROVIDER_IDS: Final = "ProviderIds"
ITEM_KEY_PRODUCTION_YEAR: Final = "ProductionYear"
ITEM_KEY_OVERVIEW: Final = "Overview"
ITEM_KEY_MUSICBRAINZ_RELEASE_GROUP: Final = "MusicBrainzReleaseGroup"
ITEM_KEY_MUSICBRAINZ_ARTIST: Final = "MusicBrainzArtist"
ITEM_KEY_MUSICBRAINZ_TRACK: Final = "MusicBrainzTrack"
ITEM_KEY_SORT_NAME: Final = "SortName"
ITEM_KEY_ALBUM_ARTIST: Final = "AlbumArtist"
ITEM_KEY_ALBUM: Final = "Album"
ITEM_KEY_PARENT_ID: Final = "ParentId"
ITEM_KEY_ARTIST_ITEMS: Final = "ArtistItems"
ITEM_KEY_CAN_DOWNLOAD: Final = "CanDownload"
ITEM_KEY_PARENT_INDEX_NUM: Final = "ParentIndexNumber"
ITEM_KEY_RUNTIME_TICKS: Final = "RunTimeTicks"

ITEM_TYPE_ALBUM: Final = "MusicAlbum"
ITEM_TYPE_ARTIST: Final = "MusicArtist"
ITEM_TYPE_AUDIO: Final = "Audio"
ITEM_TYPE_LIBRARY: Final = "CollectionFolder"

MAX_IMAGE_WIDTH: Final = 500
MAX_STREAMING_BITRATE: Final = "140000000"

MEDIA_SOURCE_KEY_PATH: Final = "Path"

MEDIA_TYPE_AUDIO: Final = "Audio"
MEDIA_TYPE_NONE: Final = ""

SUPPORTED_COLLECTION_TYPES: Final = [COLLECTION_TYPE_MUSIC]

PLAYABLE_ITEM_TYPES: Final = [ITEM_TYPE_AUDIO]


USER_APP_NAME: Final = "Music Assistant"
USER_AGENT: Final = "Music-Assistant-1.0"
