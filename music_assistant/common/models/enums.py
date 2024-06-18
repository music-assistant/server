"""All enums used by the Music Assistant models."""

from __future__ import annotations

import contextlib
from enum import StrEnum
from typing import Self

from music_assistant.common.helpers.util import classproperty


class MediaType(StrEnum):
    """Enum for MediaType."""

    ARTIST = "artist"
    ALBUM = "album"
    TRACK = "track"
    PLAYLIST = "playlist"
    RADIO = "radio"
    FOLDER = "folder"
    ANNOUNCEMENT = "announcement"
    FLOW_STREAM = "flow_stream"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls: Self, value: object) -> Self:  # noqa: ARG003
        """Set default enum member if an unknown value is provided."""
        return cls.UNKNOWN

    @classproperty
    def ALL(self) -> tuple[MediaType, ...]:  # noqa: N802
        """Return all (default) MediaTypes as tuple."""
        return (
            MediaType.ARTIST,
            MediaType.ALBUM,
            MediaType.TRACK,
            MediaType.PLAYLIST,
            MediaType.RADIO,
        )


class ExternalID(StrEnum):
    """Enum with External ID types."""

    # musicbrainz:
    # for tracks this is the RecordingID
    # for albums this is the ReleaseGroupID (NOT the release ID!)
    # for artists this is the ArtistID
    MUSICBRAINZ = "musicbrainz"
    ISRC = "isrc"  # used to identify unique recordings
    BARCODE = "barcode"  # EAN-13 barcode for identifying albums
    ACOUSTID = "acoustid"  # unique fingerprint (id) for a recording
    ASIN = "asin"  # amazon unique number to identify albums
    DISCOGS = "discogs"  # id for media item on discogs
    TADB = "tadb"  # the audio db id
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls: Self, value: object) -> Self:  # noqa: ARG003
        """Set default enum member if an unknown value is provided."""
        return cls.UNKNOWN


class LinkType(StrEnum):
    """Enum with link types."""

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
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls: Self, value: object) -> Self:  # noqa: ARG003
        """Set default enum member if an unknown value is provided."""
        return cls.UNKNOWN


class ImageType(StrEnum):
    """Enum with image types."""

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

    @classmethod
    def _missing_(cls: Self, value: object) -> Self:  # noqa: ARG003
        """Set default enum member if an unknown value is provided."""
        return cls.OTHER


class AlbumType(StrEnum):
    """Enum for Album type."""

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
    MP4 = "mp4"
    M4B = "m4b"
    DSF = "dsf"
    OPUS = "opus"
    WAVPACK = "wv"
    PCM_S16LE = "s16le"  # PCM signed 16-bit little-endian
    PCM_S24LE = "s24le"  # PCM signed 24-bit little-endian
    PCM_S32LE = "s32le"  # PCM signed 32-bit little-endian
    PCM_F32LE = "f32le"  # PCM 32-bit floating-point little-endian
    PCM_F64LE = "f64le"  # PCM 64-bit floating-point little-endian
    PCM = "pcm"  # PCM generic (details determined later)
    UNKNOWN = "?"

    @classmethod
    def _missing_(cls, value: object) -> Self:  # noqa: ARG003
        """Set default enum member if an unknown value is provided."""
        return cls.UNKNOWN

    @classmethod
    def try_parse(cls, string: str) -> Self:
        """Try to parse ContentType from (url)string/extension."""
        tempstr = string.lower()
        if "audio/" in tempstr:
            tempstr = tempstr.split("/")[1]
        for splitter in (".", ","):
            if splitter in tempstr:
                for val in tempstr.split(splitter):
                    with contextlib.suppress(ValueError):
                        parsed = cls(val.strip())
                    if parsed != ContentType.UNKNOWN:
                        return parsed
        tempstr = tempstr.split("?")[0]
        tempstr = tempstr.split("&")[0]
        tempstr = tempstr.split(";")[0]
        tempstr = tempstr.replace("mp4", "m4a")
        tempstr = tempstr.replace("mp4a", "m4a")
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
            ContentType.ALAC,
            ContentType.WAVPACK,
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
    """Enum representation of the queue (play) options.

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
    """Enum for the (playback)state of a player."""

    IDLE = "idle"
    PAUSED = "paused"
    PLAYING = "playing"


class PlayerType(StrEnum):
    """Enum with possible Player Types.

    player: A regular player.
    stereo_pair: Same as player but a dedicated stereo pair of 2 speakers.
    group: A (dedicated) group player or (universal) playergroup.
    sync_group: A group/preset of players that can be synced together.
    """

    PLAYER = "player"
    STEREO_PAIR = "stereo_pair"
    GROUP = "group"
    SYNC_GROUP = "sync_group"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls: Self, value: object) -> Self:  # noqa: ARG003
        """Set default enum member if an unknown value is provided."""
        return cls.UNKNOWN


class PlayerFeature(StrEnum):
    """Enum with possible Player features.

    power: The player has a dedicated power control.
    volume: The player supports adjusting the volume.
    mute: The player supports muting the volume.
    sync: The player supports syncing with other players (of the same platform).
    accurate_time: The player provides millisecond accurate timing information.
    seek: The player supports seeking to a specific.
    queue: The player supports (en)queuing of media items natively.
    """

    POWER = "power"
    VOLUME_SET = "volume_set"
    VOLUME_MUTE = "volume_mute"
    PAUSE = "pause"
    SYNC = "sync"
    SEEK = "seek"
    ENQUEUE_NEXT = "enqueue_next"
    PLAY_ANNOUNCEMENT = "play_announcement"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls: Self, value: object) -> Self:  # noqa: ARG003
        """Set default enum member if an unknown value is provided."""
        return cls.UNKNOWN


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
    SHUTDOWN = "application_shutdown"
    MEDIA_ITEM_ADDED = "media_item_added"
    MEDIA_ITEM_UPDATED = "media_item_updated"
    MEDIA_ITEM_DELETED = "media_item_deleted"
    PROVIDERS_UPDATED = "providers_updated"
    PLAYER_CONFIG_UPDATED = "player_config_updated"
    SYNC_TASKS_UPDATED = "sync_tasks_updated"
    AUTH_SESSION = "auth_session"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls: Self, value: object) -> Self:  # noqa: ARG003
        """Set default enum member if an unknown value is provided."""
        return cls.UNKNOWN


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
    PLAYER_GROUP_CREATE = "player_group_create"
    SYNC_PLAYERS = "sync_players"

    #
    # METADATAPROVIDER FEATURES
    #
    ARTIST_METADATA = "artist_metadata"
    ALBUM_METADATA = "album_metadata"
    TRACK_METADATA = "track_metadata"

    #
    # PLUGIN FEATURES
    #
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls: Self, value: object) -> Self:  # noqa: ARG003
        """Set default enum member if an unknown value is provided."""
        return cls.UNKNOWN


class ProviderType(StrEnum):
    """Enum with supported provider types."""

    MUSIC = "music"
    PLAYER = "player"
    METADATA = "metadata"
    PLUGIN = "plugin"
    CORE = "core"


class ConfigEntryType(StrEnum):
    """Enum for the type of a config entry."""

    BOOLEAN = "boolean"
    STRING = "string"
    SECURE_STRING = "secure_string"
    INTEGER = "integer"
    FLOAT = "float"
    LABEL = "label"
    INTEGER_TUPLE = "integer_tuple"
    DIVIDER = "divider"
    ACTION = "action"
    ICON = "icon"
    ALERT = "alert"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls: Self, value: object) -> Self:  # noqa: ARG003
        """Set default enum member if an unknown value is provided."""
        return cls.UNKNOWN


class StreamType(StrEnum):
    """Enum for the type of streamdetails."""

    HTTP = "http"  # regular http stream
    ENCRYPTED_HTTP = "encrypted_http"  # encrypted http stream
    HLS = "hls"  # http HLS stream
    ICY = "icy"  # http stream with icy metadata
    LOCAL_FILE = "local_file"
    CUSTOM = "custom"
