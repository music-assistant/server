"""Constants for the Filesystem Local provider."""

from __future__ import annotations

from dataclasses import replace
from typing import Final

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, ImageType

CONF_MISSING_ALBUM_ARTIST_ACTION = "missing_album_artist_action"
CONF_CONTENT_TYPE = "content_type"

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


CONF_ENTRY_PATH = ConfigEntry(
    key="path",
    type=ConfigEntryType.STRING,
    default_value="/media",
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
# folders holding any of these are treated as album track folders (for disc-artwork detection)
ALBUM_CONTENT_EXTENSIONS = TRACK_EXTENSIONS | CUE_EXTENSIONS
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

# Music metadata sidecars. NFO files are Kodi-style and only meaningful in their item's
# mapping directory (album folder / artist folder); disc-subfolder album.nfo is ignored.
NFO_SIDECAR_NAMES: Final[frozenset[str]] = frozenset({"album.nfo", "artist.nfo"})
# Image stems the folder-image parser recognizes: the typed image names plus the generic
# thumbnail fallbacks. Kept in sync with LocalFileSystemProvider._get_local_images.
RECOGNIZED_IMAGE_STEMS: Final[frozenset[str]] = frozenset(
    {*(image_type.value for image_type in ImageType), "folder", "cover", "album", "artist"}
)
# Extensions the music sync walk additionally surfaces (on top of SUPPORTED_EXTENSIONS) so
# sidecar changes are detectable from the listings the walk already produces.
SIDECAR_SCAN_EXTENSIONS: Final[set[str]] = {*IMAGE_EXTENSIONS, "nfo"}


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

# how long a podcast episode listing that lost a file to a parse failure is cached for:
# the missing episode cannot reappear any sooner than this
PARTIAL_LISTING_CACHE_EXPIRATION: Final[int] = 300

DEFAULT_AUDIOBOOK_PODCAST_GENRE: Final[str] = "Spoken Word"

# how often storage that went away during a scan is re-checked, so the provider comes
# back within minutes instead of waiting for the next scheduled sync
AVAILABILITY_PROBE_INTERVAL: Final[int] = 300
