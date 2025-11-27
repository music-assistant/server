"""Constants for Built-in/generic provider."""

from __future__ import annotations

from typing import NotRequired, TypedDict

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, ImageType
from music_assistant_models.media_items import MediaItemImage

from music_assistant.constants import (
    CONF_ENTRY_LIBRARY_SYNC_BACK,
    CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS,
    CONF_ENTRY_LIBRARY_SYNC_RADIOS,
    CONF_ENTRY_LIBRARY_SYNC_TRACKS,
    CONF_ENTRY_PROVIDER_SYNC_INTERVAL_PLAYLISTS,
    CONF_ENTRY_PROVIDER_SYNC_INTERVAL_RADIOS,
    CONF_ENTRY_PROVIDER_SYNC_INTERVAL_TRACKS,
)


class StoredItem(TypedDict):
    """Definition of an media item (for the builtin provider) stored in persistent storage."""

    item_id: str  # url or (locally accessible) file path (or id in case of playlist)
    name: str
    image_url: NotRequired[str]
    last_updated: NotRequired[int]


CONF_KEY_RADIOS = "stored_radios"
CONF_KEY_TRACKS = "stored_tracks"
CONF_KEY_PLAYLISTS = "stored_playlists"


ALL_FAVORITE_TRACKS = "all_favorite_tracks"
RANDOM_ARTIST = "random_artist"
RANDOM_ALBUM = "random_album"
RANDOM_TRACKS = "random_tracks"
RECENTLY_PLAYED = "recently_played"
RECENTLY_ADDED_TRACKS = "recently_added_tracks"

BUILTIN_PLAYLISTS = {
    ALL_FAVORITE_TRACKS: "All favorited tracks",
    RANDOM_ARTIST: "Random Artist (from library)",
    RANDOM_ALBUM: "Random Album (from library)",
    RANDOM_TRACKS: "500 Random tracks (from library)",
    RECENTLY_PLAYED: "Recently played tracks",
    RECENTLY_ADDED_TRACKS: "Recently added tracks",
}
BUILTIN_PLAYLISTS_ENTRIES = [
    ConfigEntry(
        key=key,
        type=ConfigEntryType.BOOLEAN,
        label=name,
        default_value=True,
        category="generic",
    )
    for key, name in BUILTIN_PLAYLISTS.items()
]

COLLAGE_IMAGE_PLAYLISTS = (ALL_FAVORITE_TRACKS, RANDOM_TRACKS)

DEFAULT_THUMB = MediaItemImage(
    type=ImageType.THUMB,
    path="logo.png",
    provider="builtin",
    remotely_accessible=False,
)

DEFAULT_FANART = MediaItemImage(
    type=ImageType.FANART,
    path="fanart.jpg",
    provider="builtin",
    remotely_accessible=False,
)

CONF_ENTRY_LIBRARY_SYNC_TRACKS_HIDDEN = ConfigEntry.from_dict(
    {
        **CONF_ENTRY_LIBRARY_SYNC_TRACKS.to_dict(),
        "hidden": True,
        "default_value": True,
    }
)
CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS_HIDDEN = ConfigEntry.from_dict(
    {
        **CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS.to_dict(),
        "hidden": True,
        "default_value": True,
    }
)
CONF_ENTRY_LIBRARY_SYNC_TRACKS_HIDDEN = ConfigEntry.from_dict(
    {
        **CONF_ENTRY_LIBRARY_SYNC_TRACKS.to_dict(),
        "hidden": True,
        "default_value": True,
    }
)
CONF_ENTRY_LIBRARY_SYNC_RADIOS_HIDDEN = ConfigEntry.from_dict(
    {
        **CONF_ENTRY_LIBRARY_SYNC_RADIOS.to_dict(),
        "hidden": True,
        "default_value": True,
    }
)
CONF_ENTRY_PROVIDER_SYNC_INTERVAL_PLAYLISTS_MOD = ConfigEntry.from_dict(
    {
        **CONF_ENTRY_PROVIDER_SYNC_INTERVAL_PLAYLISTS.to_dict(),
        "default_value": 180,
        "label": "Playlists refresh interval",
        "description": "The interval at which the builtin generated playlists are refreshed.",
    }
)


CONF_ENTRY_PROVIDER_SYNC_INTERVAL_TRACKS_HIDDEN = ConfigEntry.from_dict(
    {
        **CONF_ENTRY_PROVIDER_SYNC_INTERVAL_TRACKS.to_dict(),
        "hidden": True,
        "default_value": 180,
    }
)
CONF_ENTRY_PROVIDER_SYNC_INTERVAL_RADIOS_HIDDEN = ConfigEntry.from_dict(
    {
        **CONF_ENTRY_PROVIDER_SYNC_INTERVAL_RADIOS.to_dict(),
        "hidden": True,
        "default_value": 180,
    }
)
CONF_ENTRY_LIBRARY_SYNC_BACK_HIDDEN = ConfigEntry.from_dict(
    {
        **CONF_ENTRY_LIBRARY_SYNC_BACK.to_dict(),
        "hidden": True,
        "default_value": True,
    }
)
