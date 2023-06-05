"""All enums used by the Music Assistant models."""
from __future__ import annotations

from enum import Enum
from typing import Any, TypeVar

# pylint:disable=ungrouped-imports
try:
    from enum import StrEnum
except (AttributeError, ImportError):
    # Python 3.10 compatibility for strenum
    _StrEnumSelfT = TypeVar("_StrEnumSelfT", bound="StrEnum")

    class StrEnum(str, Enum):
        """Partial backport of Python 3.11's StrEnum for our basic use cases."""

        def __new__(
            cls: type[_StrEnumSelfT], value: str, *args: Any, **kwargs: Any
        ) -> _StrEnumSelfT:
            """Create a new StrEnum instance."""
            if not isinstance(value, str):
                raise TypeError(f"{value!r} is not a string")
            return super().__new__(cls, value, *args, **kwargs)

        def __str__(self) -> str:
            """Return self."""
            return str(self)

        @staticmethod
        def _generate_next_value_(
            name: str, start: int, count: int, last_values: list[Any]  # noqa
        ) -> Any:
            """Make `auto()` explicitly unsupported.

            We may revisit this when it's very clear that Python 3.11's
            `StrEnum.auto()` behavior will no longer change.
            """
            raise TypeError("auto() is not supported by this implementation")


class MediaType(StrEnum):
    """StrEnum for MediaType."""

    ARTIST = "artist"
    ALBUM = "album"
    TRACK = "track"
    PLAYLIST = "playlist"
    RADIO = "radio"
    FOLDER = "folder"
    UNKNOWN = "unknown"

    @classmethod
    @property
    def ALL(cls) -> tuple[MediaType, ...]:  # noqa: N802
        """Return all (default) MediaTypes as tuple."""
        return (
            MediaType.ARTIST,
            MediaType.ALBUM,
            MediaType.TRACK,
            MediaType.PLAYLIST,
            MediaType.RADIO,
        )


class LinkType(StrEnum):
    """StrEnum with link types."""

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


class ImageType(StrEnum):
    """StrEnum with image types."""

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


class AlbumType(StrEnum):
    """StrEnum for Album type."""

    ALBUM = "album"
    SINGLE = "single"
    COMPILATION = "compilation"
    EP = "ep"
    PODCAST = "podcast"
    AUDIOBOOK = "audiobook"
    UNKNOWN = "unknown"


class ContentType(StrEnum):
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
    M4B = "m4b"
    DSF = "dsf"
    WAVPACK = "wv"
    PCM_S16LE = "s16le"  # PCM signed 16-bit little-endian
    PCM_S24LE = "s24le"  # PCM signed 24-bit little-endian
    PCM_S32LE = "s32le"  # PCM signed 32-bit little-endian
    PCM_F32LE = "f32le"  # PCM 32-bit floating-point little-endian
    PCM_F64LE = "f64le"  # PCM 64-bit floating-point little-endian
    PCM = "pcm"  # PCM generic (details determined later)
    MPEG_DASH = "dash"
    UNKNOWN = "?"

    @classmethod
    def try_parse(cls: ContentType, string: str) -> ContentType:
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
        tempstr = tempstr.replace("mpd", "dash")
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
    def from_bit_depth(cls, bit_depth: int, floating_point: bool = False) -> ContentType:
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


class QueueOption(StrEnum):
    """StrEnum representation of the queue (play) options.

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


class RepeatMode(StrEnum):
    """Enum with repeat modes."""

    OFF = "off"  # no repeat at all
    ONE = "one"  # repeat one/single track
    ALL = "all"  # repeat entire queue


class PlayerState(StrEnum):
    """StrEnum for the (playback)state of a player."""

    IDLE = "idle"
    PAUSED = "paused"
    PLAYING = "playing"
    OFF = "off"


class PlayerType(StrEnum):
    """Enum with possible Player Types.

    player: A regular player.
    group: A (dedicated) group player or playergroup.
    stereo_pair: Two speakers playing as one stereo pair.
    """

    PLAYER = "player"
    GROUP = "group"
    STEREO_PAIR = "stereo_pair"


class PlayerFeature(StrEnum):
    """Enum with possible Player features.

    power: The player has a dedicated power control.
    volume: The player supports adjusting the volume.
    mute: The player supports muting the volume.
    sync: The player supports syncing with other players (of the same platform).
    accurate_time: The player provides millisecond accurate timing information.
    seek: The player supports seeking to a specific.
    set_members: The PlayerGroup supports adding/removing members.
    queue: The player supports (en)queuing of media items.
    """

    POWER = "power"
    VOLUME_SET = "volume_set"
    VOLUME_MUTE = "volume_mute"
    PAUSE = "pause"
    SYNC = "sync"
    ACCURATE_TIME = "accurate_time"
    SEEK = "seek"
    SET_MEMBERS = "set_members"
    QUEUE = "queue"
    CROSSFADE = "crossfade"


class EventType(StrEnum):
    """Enum with possible Events."""

    PLAYER_ADDED = "player_added"
    PLAYER_UPDATED = "player_updated"
    PLAYER_REMOVED = "player_removed"
    PLAYER_SETTINGS_UPDATED = "player_settings_updated"
    QUEUE_ADDED = "queue_added"
    QUEUE_UPDATED = "queue_updated"
    QUEUE_ITEMS_UPDATED = "queue_items_updated"
    QUEUE_TIME_UPDATED = "queue_time_updated"
    QUEUE_SETTINGS_UPDATED = "queue_settings_updated"
    SHUTDOWN = "application_shutdown"
    MEDIA_ITEM_ADDED = "media_item_added"
    MEDIA_ITEM_UPDATED = "media_item_updated"
    MEDIA_ITEM_DELETED = "media_item_deleted"
    PROVIDERS_UPDATED = "providers_updated"
    PLAYER_CONFIG_UPDATED = "player_config_updated"
    SYNC_TASKS_UPDATED = "sync_tasks_updated"
    AUTH_SESSION = "auth_session"


class ProviderFeature(StrEnum):
    """Enum with features for a Provider."""

    #
    # MUSICPROVIDER FEATURES
    #

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

    #
    # PLAYERPROVIDER FEATURES
    #
    # we currently have none ;-)

    #
    # METADATAPROVIDER FEATURES
    #
    ARTIST_METADATA = "artist_metadata"
    ALBUM_METADATA = "album_metadata"
    TRACK_METADATA = "track_metadata"
    GET_ARTIST_MBID = "get_artist_mbid"

    #
    # PLUGIN FEATURES
    #


class ProviderType(StrEnum):
    """Enum with supported provider types."""

    MUSIC = "music"
    PLAYER = "player"
    METADATA = "metadata"
    PLUGIN = "plugin"


class ConfigEntryType(StrEnum):
    """Enum for the type of a config entry."""

    BOOLEAN = "boolean"
    STRING = "string"
    SECURE_STRING = "secure_string"
    INTEGER = "integer"
    FLOAT = "float"
    LABEL = "label"
    DIVIDER = "divider"
    ACTION = "action"
