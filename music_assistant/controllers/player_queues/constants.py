"""Constants for the player queues controller."""

from __future__ import annotations

CONF_DEFAULT_ENQUEUE_SELECT_ARTIST = "default_enqueue_select_artist"
CONF_DEFAULT_ENQUEUE_SELECT_ALBUM = "default_enqueue_select_album"

ENQUEUE_SELECT_ARTIST_DEFAULT_VALUE = "all_tracks"
ENQUEUE_SELECT_ALBUM_DEFAULT_VALUE = "all_tracks"

CONF_DEFAULT_ENQUEUE_OPTION_ARTIST = "default_enqueue_option_artist"
CONF_DEFAULT_ENQUEUE_OPTION_ALBUM = "default_enqueue_option_album"
CONF_DEFAULT_ENQUEUE_OPTION_TRACK = "default_enqueue_option_track"
CONF_DEFAULT_ENQUEUE_OPTION_GENRE = "default_enqueue_option_genre"
CONF_DEFAULT_ENQUEUE_OPTION_LIVE_SOURCES = "default_enqueue_option_live_sources"
CONF_DEFAULT_ENQUEUE_OPTION_PLAYLIST = "default_enqueue_option_playlist"
CONF_DEFAULT_ENQUEUE_OPTION_AUDIOBOOK = "default_enqueue_option_audiobook"
CONF_DEFAULT_ENQUEUE_OPTION_PODCAST = "default_enqueue_option_podcast"
CONF_DEFAULT_ENQUEUE_OPTION_PODCAST_EPISODE = "default_enqueue_option_podcast_episode"
CONF_DEFAULT_ENQUEUE_OPTION_FOLDER = "default_enqueue_option_folder"
CONF_DEFAULT_ENQUEUE_OPTION_UNKNOWN = "default_enqueue_option_unknown"

CONF_AUTOPLAY_LABEL = "autoplay_label"
CONF_AUTOPLAY_MODE = "autoplay_mode"
CONF_AUTOPLAY_PLAYLIST = "autoplay_playlist"
CONF_CROSSFADE_LABEL = "crossfade_label"

CACHE_CATEGORY_PLAYER_QUEUE_STATE = 0
CACHE_CATEGORY_PLAYER_QUEUE_ITEMS = 1
