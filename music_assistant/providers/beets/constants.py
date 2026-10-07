"""Constants for the beets music provider."""

from __future__ import annotations

from typing import Final

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import AlbumType, ConfigEntryType

CONF_LIBRARY_DB: Final = "library_db"
CONF_MUSIC_DIRECTORY: Final = "music_directory"
CONF_BEETS_DIRECTORY: Final = "beets_directory"

CONF_ENTRY_LIBRARY_DB = ConfigEntry(
    key=CONF_LIBRARY_DB,
    type=ConfigEntryType.STRING,
    default_value="/media/beets/library.db",
)
CONF_ENTRY_MUSIC_DIRECTORY = ConfigEntry(
    key=CONF_MUSIC_DIRECTORY,
    type=ConfigEntryType.STRING,
    default_value="/media/music",
)
CONF_ENTRY_BEETS_DIRECTORY = ConfigEntry(
    key=CONF_BEETS_DIRECTORY,
    type=ConfigEntryType.STRING,
    default_value="",
    required=False,
)

# beets joins multi-valued fields (artists, genres, ...) with this in the database
BEETS_MULTI_VALUE_DELIMITER: Final = "\\␀"
# albumtypes, and multi-valued fields written before the delimiter above existed
BEETS_LIST_DELIMITER: Final = "; "

# the first beets album type found in this order decides the MA album type
ALBUM_TYPE_PRIORITY: Final[tuple[tuple[str, AlbumType], ...]] = (
    ("compilation", AlbumType.COMPILATION),
    ("soundtrack", AlbumType.SOUNDTRACK),
    ("live", AlbumType.LIVE),
    ("ep", AlbumType.EP),
    ("single", AlbumType.SINGLE),
    ("album", AlbumType.ALBUM),
)

IMAGE_PATH_PREFIX: Final = "album/"
# beets numbers items and albums separately and every library from 1, so provider ids carry
# the media type and the instance id
TRACK_ID_PREFIX: Final = "track-"
ALBUM_ID_PREFIX: Final = "album-"
SQLITE_BUSY_TIMEOUT: Final = 30.0
ITEM_BATCH_SIZE: Final = 500
SYNC_CONCURRENCY: Final = 8
# part of every item checksum: raise it whenever parsing changes, so the next sync re-imports
# the items that were parsed by the previous version
PARSER_VERSION: Final = 1
