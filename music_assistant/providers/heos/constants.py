"""Constants for HEOS Player Provider."""

from music_assistant_models.enums import MediaType, PlaybackState
from pyheos import MediaType as HeosMediaType
from pyheos import PlayState as HeosPlayState

HEOS_MEDIA_TYPE_TO_MEDIA_TYPE: dict[HeosMediaType | None, MediaType] = {
    HeosMediaType.ALBUM: MediaType.ALBUM,
    HeosMediaType.ARTIST: MediaType.ARTIST,
    HeosMediaType.CONTAINER: MediaType.FOLDER,
    HeosMediaType.GENRE: MediaType.GENRE,
    HeosMediaType.HEOS_SERVER: MediaType.FOLDER,
    HeosMediaType.HEOS_SERVICE: MediaType.FOLDER,
    HeosMediaType.MUSIC_SERVICE: MediaType.FOLDER,
    HeosMediaType.PLAYLIST: MediaType.PLAYLIST,
    HeosMediaType.SONG: MediaType.TRACK,
    HeosMediaType.STATION: MediaType.TRACK,
    None: MediaType.UNKNOWN,
}

HEOS_PLAY_STATE_TO_PLAYBACK_STATE: dict[HeosPlayState | None, PlaybackState] = {
    HeosPlayState.PLAY: PlaybackState.PLAYING,
    HeosPlayState.PAUSE: PlaybackState.PAUSED,
    HeosPlayState.STOP: PlaybackState.IDLE,
    HeosPlayState.UNKNOWN: PlaybackState.UNKNOWN,
    None: PlaybackState.UNKNOWN,
}
