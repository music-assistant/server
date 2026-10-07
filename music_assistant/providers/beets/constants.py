"""Constants for the beets music provider."""

from __future__ import annotations

from typing import Final

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import AlbumType, ConfigEntryType

CONF_LIBRARY_DB: Final = "library_db"
CONF_MUSIC_DIRECTORY: Final = "music_directory"
CONF_BEETS_DIRECTORY: Final = "beets_directory"
CONF_REPLAYGAIN_TARGET_LEVEL: Final = "replaygain_target_level"
CONF_R128_TARGET_LEVEL: Final = "r128_target_level"

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

# beets' replaygain targetlevel and r128_targetlevel, in its dB scale; the stored gains are
# relative to these, so they must match the beets config the library was analyzed with
CONF_ENTRY_REPLAYGAIN_TARGET_LEVEL = ConfigEntry(
    key=CONF_REPLAYGAIN_TARGET_LEVEL,
    type=ConfigEntryType.INTEGER,
    default_value=89,
    range=(60, 110),
    advanced=True,
    requires_reload=True,
)
CONF_ENTRY_R128_TARGET_LEVEL = ConfigEntry(
    key=CONF_R128_TARGET_LEVEL,
    type=ConfigEntryType.INTEGER,
    default_value=84,
    range=(60, 110),
    advanced=True,
    requires_reload=True,
)
# beets converts its dB target levels to LUFS by subtracting this
BEETS_DB_TO_LUFS_OFFSET: Final = 107

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
# artists are keyed by their MusicBrainz id, so same-named artists stay apart, or by name
# when beets has no valid id for them
ARTIST_MBID_ID_PREFIX: Final = "artist-mbid-"
ARTIST_NAME_ID_PREFIX: Final = "artist-name-"
SQLITE_BUSY_TIMEOUT: Final = 30.0
ITEM_BATCH_SIZE: Final = 500
SYNC_CONCURRENCY: Final = 8
# part of every item checksum: raise it whenever parsing changes, so the next sync re-imports
# the items that were parsed by the previous version
PARSER_VERSION: Final = 2
