"""All enums used by the Music Assistant models."""

from enum import Enum, IntEnum


class MediaType(Enum):
    """Enum for MediaType."""

    ARTIST = "artist"
    ALBUM = "album"
    TRACK = "track"
    PLAYLIST = "playlist"
    RADIO = "radio"
    UNKNOWN = "unknown"


class MediaQuality(IntEnum):
    """Enum for Media Quality."""

    UNKNOWN = 0
    LOSSY_MP3 = 1
    LOSSY_OGG = 2
    LOSSY_AAC = 3
    FLAC_LOSSLESS = 10  # 44.1/48khz 16 bits
    FLAC_LOSSLESS_HI_RES_1 = 20  # 44.1/48khz 24 bits HI-RES
    FLAC_LOSSLESS_HI_RES_2 = 21  # 88.2/96khz 24 bits HI-RES
    FLAC_LOSSLESS_HI_RES_3 = 22  # 176/192khz 24 bits HI-RES
    FLAC_LOSSLESS_HI_RES_4 = 23  # above 192khz 24 bits HI-RES


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
    UNKNOWN = "unknown"


class StreamType(Enum):
    """Enum with stream types."""

    EXECUTABLE = "executable"
    URL = "url"
    FILE = "file"
    CACHE = "cache"


class ContentType(Enum):
    """Enum with audio content/container types supported by ffmpeg."""

    OGG = "ogg"
    FLAC = "flac"
    MP3 = "mp3"
    AAC = "aac"
    MPEG = "mpeg"
    ALAC = "alac"
    WAVE = "wave"
    AIFF = "aiff"
    WMA = "wma"
    M4A = "m4a"
    PCM_S16LE = "s16le"  # PCM signed 16-bit little-endian
    PCM_S24LE = "s24le"  # PCM signed 24-bit little-endian
    PCM_S32LE = "s32le"  # PCM signed 32-bit little-endian
    PCM_F32LE = "f32le"  # PCM 32-bit floating-point little-endian
    PCM_F64LE = "f64le"  # PCM 64-bit floating-point little-endian

    @classmethod
    def try_parse(
        cls: "ContentType", string: str, fallback: str = "mp3"
    ) -> "ContentType":
        """Try to parse ContentType from (url)string."""
        tempstr = string.lower()
        if "." in tempstr:
            tempstr = tempstr.split(".")[-1]
        tempstr = tempstr.split("?")[0]
        tempstr = tempstr.split("&")[0]
        try:
            return cls(tempstr)
        except ValueError:
            return cls(fallback)

    def is_pcm(self):
        """Return if contentype is PCM."""
        return self.name.startswith("PCM")

    def sox_supported(self):
        """Return if ContentType is supported by SoX."""
        return self.is_pcm() or self in [
            ContentType.OGG,
            ContentType.FLAC,
            ContentType.MP3,
            ContentType.WAVE,
            ContentType.AIFF,
        ]

    def sox_format(self):
        """Convert the ContentType to SoX compatible format."""
        if not self.sox_supported():
            raise NotImplementedError
        return self.value.replace("le", "")

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
    """Enum representation of the queue (play) options."""

    PLAY = "play"
    REPLACE = "replace"
    NEXT = "next"
    ADD = "add"


class CrossFadeMode(Enum):
    """Enum with crossfade modes."""

    DISABLED = "disabled"  # no crossfading at all
    STRICT = "strict"  # do not crossfade tracks of same album
    SMART = "smart"  # crossfade if possible (do not crossfade different sample rates)
    ALWAYS = "always"  # all tracks - resample to fixed sample rate


class RepeatMode(Enum):
    """Enum with repeat modes."""

    OFF = "off"  # no repeat at all
    ONE = "one"  # repeat one/single track
    ALL = "all"  # repeat entire queue


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
    STREAM_STARTED = "streaming_started"
    STREAM_ENDED = "streaming_ended"
    QUEUE_ADDED = "queue_added"
    QUEUE_UPDATED = "queue_updated"
    QUEUE_ITEMS_UPDATED = "queue_items_updated"
    QUEUE_TIME_UPDATED = "queue_time_updated"
    SHUTDOWN = "application_shutdown"
    MEDIA_ITEM_ADDED = "media_item_added"
    MEDIA_ITEM_UPDATED = "media_item_updated"
    BACKGROUND_JOB_UPDATED = "background_job_updated"


class JobStatus(Enum):
    """Enum with Job status."""

    PENDING = "pending"
    RUNNING = "running"
    CANCELLED = "cancelled"
    FINISHED = "success"
    ERROR = "error"


class ProviderType(Enum):
    """Enum with supported music providers."""

    FILESYSTEM_LOCAL = "file"
    FILESYSTEM_SMB = "smb"
    FILESYSTEM_GOOGLE_DRIVE = "gdrive"
    FILESYSTEM_ONEDRIVE = "onedrive"
    SPOTIFY = "spotify"
    QOBUZ = "qobuz"
    TUNEIN = "tunein"
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
