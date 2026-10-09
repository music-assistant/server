"""Constants for the Filesystem Local provider."""

from __future__ import annotations

from dataclasses import replace
from typing import Final

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, ImageType

from music_assistant.helpers.rating import (
    POPM_SCALE_ITUNES,
    POPM_SCALE_WINDOWS,
    TAG_SCALE_PERCENT,
    TAG_SCALE_STARS,
)

CONF_MISSING_ALBUM_ARTIST_ACTION = "missing_album_artist_action"
CONF_CONTENT_TYPE = "content_type"

# Import of the rating embedded in the file's tags, see music_assistant.helpers.rating
CONF_RATING_IMPORT_ENABLED = "rating_import_enabled"
CONF_RATING_FAVORITE_THRESHOLD = "rating_favorite_threshold"
CONF_RATING_DISLIKE_THRESHOLD = "rating_dislike_threshold"
CONF_RATING_POPM_SCALE = "rating_popm_scale"
CONF_RATING_TAG_SCALE = "rating_tag_scale"

# Hidden conf: has the one-time re-read after enabling rating import completed?
CONF_RATING_IMPORT_BACKFILL_DONE = "rating_import_backfill_done"

# Hidden conf: Do we still need to promote authors/ narrators to full artists?
CONF_AUTHOR_NARRATOR_REPARSE_DONE = "author_narrator_reparse_done"

# Use a prefix: Authors/ narrators cannot be distinguished by their file path, like music artists.
AUTHOR_ID_PREFIX: Final[str] = "author:"
NARRATOR_ID_PREFIX: Final[str] = "narrator:"

CONF_ENTRY_MISSING_ALBUM_ARTIST = ConfigEntry(
    key=CONF_MISSING_ALBUM_ARTIST_ACTION,
    type=ConfigEntryType.STRING,
    default_value="various_artists",
    help_link="https://music-assistant.io/music-providers/local-files/#tagging-files",
    required=False,
    options=[
        ConfigValueOption("track_artist"),
        ConfigValueOption("various_artists"),
        ConfigValueOption("folder_name"),
    ],
    depends_on=CONF_CONTENT_TYPE,
    depends_on_value="music",
)


CONF_ENTRY_RATING_IMPORT_ENABLED = ConfigEntry(
    key=CONF_RATING_IMPORT_ENABLED,
    type=ConfigEntryType.BOOLEAN,
    default_value=False,
    help_link="https://music-assistant.io/music-providers/local-files/#tagging-files",
    required=False,
    depends_on=CONF_CONTENT_TYPE,
    depends_on_value="music",
)

# thresholds are on the normalized 0-10 scale, matching the Plex provider
CONF_ENTRY_RATING_FAVORITE_THRESHOLD = ConfigEntry(
    key=CONF_RATING_FAVORITE_THRESHOLD,
    type=ConfigEntryType.FLOAT,
    default_value=8.0,
    range=(0, 10),
    required=False,
    depends_on=CONF_CONTENT_TYPE,
    depends_on_value="music",
)

CONF_ENTRY_RATING_DISLIKE_THRESHOLD = ConfigEntry(
    key=CONF_RATING_DISLIKE_THRESHOLD,
    type=ConfigEntryType.FLOAT,
    default_value=2.0,
    range=(0, 10),
    required=False,
    depends_on=CONF_CONTENT_TYPE,
    depends_on_value="music",
)

# taggers disagree about what the values mean, so the scale is chosen per library
CONF_ENTRY_RATING_POPM_SCALE = ConfigEntry(
    key=CONF_RATING_POPM_SCALE,
    type=ConfigEntryType.STRING,
    default_value=POPM_SCALE_WINDOWS,
    required=False,
    options=[
        ConfigValueOption(POPM_SCALE_WINDOWS),
        ConfigValueOption(POPM_SCALE_ITUNES),
    ],
    depends_on=CONF_CONTENT_TYPE,
    depends_on_value="music",
)

CONF_ENTRY_RATING_TAG_SCALE = ConfigEntry(
    key=CONF_RATING_TAG_SCALE,
    type=ConfigEntryType.STRING,
    default_value=TAG_SCALE_PERCENT,
    required=False,
    options=[
        ConfigValueOption(TAG_SCALE_PERCENT),
        ConfigValueOption(TAG_SCALE_STARS),
    ],
    depends_on=CONF_CONTENT_TYPE,
    depends_on_value="music",
)


# the folder a new source is offered by default, where the caller may use it
DEFAULT_MEDIA_FOLDER: Final[str] = "/media"

CONF_ENTRY_PATH = ConfigEntry(
    key="path",
    type=ConfigEntryType.FOLDER,
)

CONF_ENTRY_CONTENT_TYPE = ConfigEntry(
    key=CONF_CONTENT_TYPE,
    type=ConfigEntryType.STRING,
    default_value="music",
    required=False,
    options=[
        ConfigValueOption("music"),
        ConfigValueOption("audiobooks"),
        ConfigValueOption("podcasts"),
        ConfigValueOption("sound_effects"),
    ],
)


def content_type_config_entry(content_type: str) -> ConfigEntry:
    """
    Return the read-only mirror of the (setup flow owned) content type for the options page.

    :param content_type: The content type resolved from the provider's setup data.
    """
    # mirrored as the entry default so the other entries resolve their depends_on chain
    # against it without it ever being persisted back into the stored values
    return replace(CONF_ENTRY_CONTENT_TYPE, read_only=True, default_value=content_type)


def folder_config_entry(path: str) -> ConfigEntry:
    """
    Return the line on the options page that shows which folder a source reads from.

    :param path: The folder of the source.
    """
    return ConfigEntry(key="folder", type=ConfigEntryType.LABEL, translation_params=[path])


CONF_ENTRY_LIBRARY_SYNC_TRACKS = ConfigEntry(
    key="library_sync_tracks",
    type=ConfigEntryType.BOOLEAN,
    default_value=True,
    category="sync_options",
    depends_on=CONF_CONTENT_TYPE,
    depends_on_value="music",
)
CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS = ConfigEntry(
    key="library_sync_playlists",
    type=ConfigEntryType.BOOLEAN,
    default_value=True,
    category="sync_options",
    depends_on=CONF_CONTENT_TYPE,
    depends_on_value="music",
)
CONF_ENTRY_LIBRARY_SYNC_PODCASTS = ConfigEntry(
    key="library_sync_podcasts",
    type=ConfigEntryType.BOOLEAN,
    default_value=True,
    category="sync_options",
    depends_on=CONF_CONTENT_TYPE,
    depends_on_value="podcasts",
)
CONF_ENTRY_LIBRARY_SYNC_AUDIOBOOKS = ConfigEntry(
    key="library_sync_audiobooks",
    type=ConfigEntryType.BOOLEAN,
    default_value=True,
    category="sync_options",
    depends_on=CONF_CONTENT_TYPE,
    depends_on_value="audiobooks",
)

CONF_ENTRY_PROPAGATE_GENRES = ConfigEntry(
    key="propagate_track_genres",
    type=ConfigEntryType.BOOLEAN,
    default_value=False,
    required=False,
    category="sync_options",
    depends_on=CONF_CONTENT_TYPE,
    depends_on_value="music",
)

CONF_ENTRY_IGNORE_ALBUM_PLAYLISTS = ConfigEntry(
    key="ignore_album_playlists",
    type=ConfigEntryType.BOOLEAN,
    default_value=True,
    required=False,
    depends_on=CONF_CONTENT_TYPE,
    depends_on_value="music",
)

TRACK_EXTENSIONS = {
    "aac",
    "mp3",
    "m4a",
    "mp4",
    "flac",
    "wav",
    "ogg",
    "aiff",
    "wma",
    "dsf",
    "opus",
    "wv",
    "amr",
    "awb",
    "spx",
    "tak",
    "ape",
    "mpc",
    "mp2",
    "m2a",
    "mp1",
    "dra",
    "mpeg",
    "mpg",
    "ac3",
    "ec3",
    "aif",
    "oga",
    "dff",
    "ts",
    "m2ts",
    "mp+",
}
PLAYLIST_EXTENSIONS = {"m3u", "pls", "m3u8"}
CUE_EXTENSIONS = {"cue"}
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif"}
AUDIOBOOK_EXTENSIONS = {"aa", "aax", "m4b", "m4a", "mp3", "mp4", "flac", "ogg", "opus"}
PODCAST_EPISODE_EXTENSIONS = {"aa", "aax", "m4b", "m4a", "mp3", "mp4", "flac", "ogg", "opus"}
SOUND_EFFECT_EXTENSIONS = TRACK_EXTENSIONS
SUPPORTED_EXTENSIONS = {
    *TRACK_EXTENSIONS,
    *AUDIOBOOK_EXTENSIONS,
    *PODCAST_EPISODE_EXTENSIONS,
    *PLAYLIST_EXTENSIONS,
    *CUE_EXTENSIONS,
}

# local metadata files (Kodi-style NFO and recognized folder images) are never imported as
# media: they carry no provider mapping of their own and only feed the lightweight change
# detection that reparses their representative track when one of them changes on disk
NFO_FILENAMES = {"album.nfo", "artist.nfo"}
METADATA_IMAGE_STEMS = {image_type.value for image_type in ImageType} | {
    "folder",
    "cover",
    "album",
    "artist",
}
METADATA_FILE_EXTENSIONS = {"nfo", *IMAGE_EXTENSIONS}
# the walk collects both imported media and local metadata files in a single pass
WALK_EXTENSIONS = SUPPORTED_EXTENSIONS | METADATA_FILE_EXTENSIONS


class IsChapterFile(Exception):
    """Exception to indicate that a file is part of a multi-part media (e.g. audiobook chapter)."""


CACHE_CATEGORY_ARTIST_INFO: Final[int] = 1
CACHE_CATEGORY_ALBUM_INFO: Final[int] = 2
CACHE_CATEGORY_FOLDER_IMAGES: Final[int] = 3
CACHE_CATEGORY_AUDIOBOOK_CHAPTERS: Final[int] = 4
CACHE_CATEGORY_PODCAST_METADATA: Final[int] = 5
CACHE_CATEGORY_CUE_SHEETS: Final[int] = 6
CACHE_CATEGORY_SOUND_EFFECTS: Final[int] = 7
CACHE_CATEGORY_PODCAST_EPISODES: Final[int] = 8
# tracks the current change token + representative track of a local metadata file (NFO or
# folder image); derivative and non-authoritative, so a cache miss is simply ignored
CACHE_CATEGORY_METADATA_FILE: Final[int] = 9

# a registration is only ever refreshed by actually reading the file again, never on a timer,
# so it must not expire under normal operation: an infrequently-touched item (an unchanged NFO
# for months) would otherwise silently fall back to "untracked" once the entry expired
METADATA_FILE_CACHE_EXPIRATION: Final[int] = 86400 * 365 * 10  # ~permanent for the provider's life

# how long a podcast episode listing that lost a file to a parse failure is cached for:
# the missing episode cannot reappear any sooner than this
PARTIAL_LISTING_CACHE_EXPIRATION: Final[int] = 300

# how often storage that went away during a scan is re-checked, so the provider comes
# back within minutes instead of waiting for the next scheduled sync
AVAILABILITY_PROBE_INTERVAL: Final[int] = 300
