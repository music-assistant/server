"""Constants for the Filesystem Local provider."""

from __future__ import annotations

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, ProviderFeature

CONF_MISSING_ALBUM_ARTIST_ACTION = "missing_album_artist_action"
CONF_CONTENT_TYPE = "content_type"

CONF_ENTRY_MISSING_ALBUM_ARTIST = ConfigEntry(
    key=CONF_MISSING_ALBUM_ARTIST_ACTION,
    type=ConfigEntryType.STRING,
    label="Action when a track is missing the Albumartist ID3 tag",
    default_value="various_artists",
    help_link="https://music-assistant.io/music-providers/filesystem/#tagging-files",
    required=False,
    options=[
        ConfigValueOption("Use Track artist(s)", "track_artist"),
        ConfigValueOption("Use Various Artists", "various_artists"),
        ConfigValueOption("Use Folder name (if possible)", "folder_name"),
    ],
    depends_on=CONF_CONTENT_TYPE,
    depends_on_value="music",
)


CONF_ENTRY_PATH = ConfigEntry(
    key="path",
    type=ConfigEntryType.STRING,
    label="Path",
    default_value="/media",
)

CONF_ENTRY_CONTENT_TYPE = ConfigEntry(
    key=CONF_CONTENT_TYPE,
    type=ConfigEntryType.STRING,
    label="Content type in media folder(s)",
    default_value="music",
    description="The type of content to expect in the media folder(s)",
    required=False,
    options=[
        ConfigValueOption("Music", "music"),
        ConfigValueOption("Audiobooks", "audiobooks"),
        ConfigValueOption("Podcasts", "podcasts"),
    ],
)
CONF_ENTRY_CONTENT_TYPE_READ_ONLY = ConfigEntry.from_dict(
    {
        **CONF_ENTRY_CONTENT_TYPE.to_dict(),
        "depends_on": CONF_ENTRY_PATH.key,
        "depends_on_value": "thisdoesnotexist",
    }
)

CONF_ENTRY_IGNORE_ALBUM_PLAYLISTS = ConfigEntry(
    key="ignore_album_playlists",
    type=ConfigEntryType.BOOLEAN,
    label="Ignore playlists with album tracks within album folders",
    description="A digital album often comes with a playlist file (.m3u) "
    "that contains the tracks of the album. Adding all these playlists to the library, "
    "is not very practical so it's better to just ignore them.\n\n"
    "If this option is enabled, all playlists will be ignored which are more than "
    "1 level deep anywhere in the folder structure. E.g. /music/artistname/albumname/playlist.m3u",
    default_value=True,
    required=False,
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
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif"}
AUDIOBOOK_EXTENSIONS = {"aa", "aax", "m4b", "m4a", "mp3", "mp4", "flac", "ogg"}
PODCAST_EPISODE_EXTENSIONS = {"aa", "aax", "m4b", "m4a", "mp3", "mp4", "flac", "ogg"}
PLAYLIST_EXTENSIONS = {"m3u", "pls", "m3u8"}
SUPPORTED_EXTENSIONS = {
    *TRACK_EXTENSIONS,
    *AUDIOBOOK_EXTENSIONS,
    *PODCAST_EPISODE_EXTENSIONS,
    *PLAYLIST_EXTENSIONS,
}


SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
}


class IsChapterFile(Exception):
    """Exception to indicate that a file is part of a multi-part media (e.g. audiobook chapter)."""
