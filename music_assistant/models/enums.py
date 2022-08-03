"""All enums used by the Music Assistant models."""

from enum import Enum, IntEnum
from typing import List


class MediaType(Enum):
    """Enum for MediaType."""

    ARTIST = "artist"
    ALBUM = "album"
    TRACK = "track"
    PLAYLIST = "playlist"
    RADIO = "radio"
    FOLDER = "folder"
    UNKNOWN = "unknown"

    @classmethod
    @property
    def ALL(cls) -> List["MediaType"]:  # pylint: disable=invalid-name
        """Return all (default) MediaTypes as list."""
        return [
            MediaType.ARTIST,
            MediaType.ALBUM,
            MediaType.TRACK,
            MediaType.PLAYLIST,
            MediaType.RADIO,
        ]


class MediaQuality(IntEnum):
    """Enum for Media Quality."""

    UNKNOWN = 0
    LOSSY_MP3 = 1
    LOSSY_OGG = 2
    LOSSY_AAC = 3
    LOSSY_M4A = 4
    LOSSLESS = 10  # 44.1/48khz 16 bits
    LOSSLESS_HI_RES_1 = 20  # 44.1/48khz 24 bits HI-RES
    LOSSLESS_HI_RES_2 = 21  # 88.2/96khz 24 bits HI-RES
    LOSSLESS_HI_RES_3 = 22  # 176/192khz 24 bits HI-RES
    LOSSLESS_HI_RES_4 = 23  # above 192khz 24 bits HI-RES

    @classmethod
    def from_file_type(cls, file_type: str) -> "MediaQuality":
        """Try to parse MediaQuality from file type/extension."""
        if "mp3" in file_type:
            return MediaQuality.LOSSY_MP3
        if "ogg" in file_type:
            return MediaQuality.LOSSY_OGG
        if "aac" in file_type:
            return MediaQuality.LOSSY_AAC
        if "m4a" in file_type:
            return MediaQuality.LOSSY_M4A
        if "flac" in file_type:
            return MediaQuality.LOSSLESS
        if "wav" in file_type:
            return MediaQuality.LOSSLESS
        return MediaQuality.UNKNOWN


class LinkType(Enum):
    """Enum wth link types."""

    WEBSITE = "website"
    FACEBOOK = "facebook"
    TWITTER = "twitter"
    LASTFM = "lastfm"
    YOUTUBE = "youtube"
    INSTAGRAM = "instagram"
    SNAPCHAT = "snapchat"
    TIKTOK = "tiktok"
    DISCOGS = "discogs"
    WIKIPEDIA = "wikipedia"
    ALLMUSIC = "allmusic"


class ImageType(Enum):
    """Enum wth image types."""

    THUMB = "thumb"
    LANDSCAPE = "landscape"
    FANART = "fanart"
    LOGO = "logo"
    CLEARART = "clearart"
    BANNER = "banner"
    CUTOUT = "cutout"
    BACK = "back"
    DISCART = "discart"
    OTHER = "other"


class AlbumType(Enum):
    """Enum for Album type."""

    ALBUM = "album"
    SINGLE = "single"
    COMPILATION = "compilation"
    EP = "ep"
    UNKNOWN = "unknown"


class ContentType(Enum):
    """Enum with audio content/container types supported by ffmpeg."""

    OGG = "ogg"
    FLAC = "flac"
    MP3 = "mp3"
    AAC = "aac"
    MPEG = "mpeg"
    ALAC = "alac"
    WAV = "wav"
    AIFF = "aiff"
    WMA = "wma"
    M4A = "m4a"
    DSF = "dsf"
    WAVPACK = "wv"
    PCM_S16LE = "s16le"  # PCM signed 16-bit little-endian
    PCM_S24LE = "s24le"  # PCM signed 24-bit little-endian
    PCM_S32LE = "s32le"  # PCM signed 32-bit little-endian
    PCM_F32LE = "f32le"  # PCM 32-bit floating-point little-endian
    PCM_F64LE = "f64le"  # PCM 64-bit floating-point little-endian
    UNKNOWN = "?"

    @classmethod
    def try_parse(cls: "ContentType", string: str) -> "ContentType":
        """Try to parse ContentType from (url)string/extension."""
        tempstr = string.lower()
        if "audio/" in tempstr:
            tempstr = tempstr.split("/")[1]
        for splitter in (".", ","):
            if splitter in tempstr:
                for val in tempstr.split(splitter):
                    try:
                        return cls(val.strip())
                    except ValueError:
                        pass

        tempstr = tempstr.split("?")[0]
        tempstr = tempstr.split("&")[0]
        tempstr = tempstr.split(";")[0]
        tempstr = tempstr.replace("mp4", "m4a")
        try:
            return cls(tempstr)
        except ValueError:
            return cls.UNKNOWN

    def is_pcm(self) -> bool:
        """Return if contentype is PCM."""
        return self.name.startswith("PCM")

    def is_lossless(self) -> bool:
        """Return if format is lossless."""
        return self.is_pcm() or self in (
            ContentType.DSF,
            ContentType.FLAC,
            ContentType.AIFF,
            ContentType.WAV,
        )

    @classmethod
    def from_bit_depth(
        cls, bit_depth: int, floating_point: bool = False
    ) -> "ContentType":
        """Return (PCM) Contenttype from PCM bit depth."""
        if floating_point and bit_depth > 32:
            return cls.PCM_F64LE
        if floating_point:
            return cls.PCM_F32LE
        if bit_depth == 16:
            return cls.PCM_S16LE
        if bit_depth == 24:
            return cls.PCM_S24LE
        return cls.PCM_S32LE


class QueueOption(Enum):
    """
    Enum representation of the queue (play) options.

    - PLAY -> Insert new item(s) in queue at the current position and start playing.
    - REPLACE -> Replace entire queue contents with the new items and start playing from index 0.
    - NEXT -> Insert item(s) after current playing/buffered item.
    - REPLACE_NEXT -> Replace item(s) after current playing/buffered item.
    - ADD -> Add new item(s) to the queue (at the end if shuffle is not enabled).
    """

    PLAY = "play"
    REPLACE = "replace"
    NEXT = "next"
    REPLACE_NEXT = "replace_next"
    ADD = "add"


class CrossFadeMode(Enum):
    """
    Enum with crossfade modes.

    - DISABLED: no crossfading at all
    - STRICT: do not crossfade tracks of same album
    - SMART: crossfade if possible (do not crossfade different sample rates)
    - ALWAYS: all tracks - resample to fixed sample rate
    """

    DISABLED = "disabled"
    STRICT = "strict"
    SMART = "smart"
    ALWAYS = "always"


class RepeatMode(Enum):
    """Enum with repeat modes."""

    OFF = "off"  # no repeat at all
    ONE = "one"  # repeat one/single track
    ALL = "all"  # repeat entire queue


class MetadataMode(Enum):
    """Enum with stream metadata modes."""

    DISABLED = "disabled"  # do not notify icy support
    DEFAULT = "default"  # enable icy if player requests it, default chunksize
    LEGACY = "legacy"  # enable icy but with legacy 8kb chunksize, requires mp3


class PlayerState(Enum):
    """Enum for the (playback)state of a player."""

    IDLE = "idle"
    PAUSED = "paused"
    PLAYING = "playing"
    OFF = "off"


class EventType(Enum):
    """Enum with possible Events."""

    PLAYER_ADDED = "player_added"
    PLAYER_UPDATED = "player_updated"
    QUEUE_ADDED = "queue_added"
    QUEUE_UPDATED = "queue_updated"
    QUEUE_ITEMS_UPDATED = "queue_items_updated"
    QUEUE_TIME_UPDATED = "queue_time_updated"
    SHUTDOWN = "application_shutdown"
    BACKGROUND_JOB_UPDATED = "background_job_updated"
    BACKGROUND_JOB_FINISHED = "background_job_finished"
    MEDIA_ITEM_ADDED = "media_item_added"
    MEDIA_ITEM_UPDATED = "media_item_updated"
    MEDIA_ITEM_DELETED = "media_item_deleted"


class JobStatus(Enum):
    """Enum with Job status."""

    PENDING = "pending"
    RUNNING = "running"
    CANCELLED = "cancelled"
    FINISHED = "success"
    ERROR = "error"


class MusicProviderFeature(Enum):
    """Enum with features for a MusicProvider."""

    # browse/explore/recommendations
    BROWSE = "browse"
    SEARCH = "search"
    RECOMMENDATIONS = "recommendations"
    # library feature per mediatype
    LIBRARY_ARTISTS = "library_artists"
    LIBRARY_ALBUMS = "library_albums"
    LIBRARY_TRACKS = "library_tracks"
    LIBRARY_PLAYLISTS = "library_playlists"
    LIBRARY_RADIOS = "library_radios"
    # additional library features
    ARTIST_ALBUMS = "artist_albums"
    ARTIST_TOPTRACKS = "artist_toptracks"
    # library edit (=add/remove) feature per mediatype
    LIBRARY_ARTISTS_EDIT = "library_artists_edit"
    LIBRARY_ALBUMS_EDIT = "library_albums_edit"
    LIBRARY_TRACKS_EDIT = "library_tracks_edit"
    LIBRARY_PLAYLISTS_EDIT = "library_playlists_edit"
    LIBRARY_RADIOS_EDIT = "library_radios_edit"
    # if we can grab 'similar tracks' from the music provider
    # used to generate dynamic playlists
    SIMILAR_TRACKS = "similar_tracks"
    # playlist-specific features
    PLAYLIST_TRACKS_EDIT = "playlist_tracks_edit"
    PLAYLIST_CREATE = "playlist_create"


class ProviderType(Enum):
    """Enum with supported music providers."""

    FILESYSTEM_LOCAL = "file"
    FILESYSTEM_SMB = "smb"
    FILESYSTEM_GOOGLE_DRIVE = "gdrive"
    FILESYSTEM_ONEDRIVE = "onedrive"
    SPOTIFY = "spotify"
    QOBUZ = "qobuz"
    TUNEIN = "tunein"
    YTMUSIC = "ytmusic"
    DATABASE = "database"  # internal only
    URL = "url"  # internal only

    def is_file(self) -> bool:
        """Return if type is one of the filesystem providers."""
        return self in (
            self.FILESYSTEM_LOCAL,
            self.FILESYSTEM_SMB,
            self.FILESYSTEM_GOOGLE_DRIVE,
            self.FILESYSTEM_ONEDRIVE,
        )

    @classmethod
    def parse(cls: "ProviderType", val: str) -> "ProviderType":
        """Try to parse ContentType from provider id."""
        if isinstance(val, ProviderType):
            return val
        for mem in ProviderType:
            if val.startswith(mem.value):
                return mem
        raise ValueError(f"Unable to parse ProviderType from {val}")
