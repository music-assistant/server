"""Models for providers/plugins."""

from abc import abstractmethod
from enum import Enum
from typing import Dict, List, Optional

from music_assistant.helpers.typing import MusicAssistant, Players
from music_assistant.models.config_entry import ConfigEntry
from music_assistant.models.media_types import (
    Album,
    Artist,
    MediaType,
    Playlist,
    Radio,
    SearchResult,
    Track,
)
from music_assistant.models.streamdetails import StreamDetails


class ProviderType(Enum):
    """Enum with plugin types."""

    MUSIC_PROVIDER = "music_provider"
    PLAYER_PROVIDER = "player_provider"
    METADATA_PROVIDER = "metadata_provider"
    PLUGIN = "plugin"


class Provider:
    """Base model for a provider/plugin."""

    mass: MusicAssistant = None  # will be set automagically while loading the provider
    available: bool = False  # will be set automagically while loading the provider

    @property
    @abstractmethod
    def type(self) -> ProviderType:
        """Return ProviderType."""

    @property
    @abstractmethod
    def id(self) -> str:
        """Return provider ID for this provider."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Return provider Name for this provider."""

    @property
    @abstractmethod
    def config_entries(self) -> List[ConfigEntry]:
        """Return Config Entries for this provider."""

    @abstractmethod
    async def on_start(self) -> bool:
        """
        Handle initialization of the provider based on config.

        Return bool if start was succesfull. Called on startup.
        """
        raise NotImplementedError

    @abstractmethod
    async def on_stop(self) -> None:
        """Handle correct close/cleanup of the provider on exit. Called on shutdown/reload."""


class Plugin(Provider):
    """
    Base class for a Plugin.

    Should be overridden/subclassed by provider specific implementation.
    """

    @property
    def type(self) -> ProviderType:
        """Return ProviderType."""
        return ProviderType.PLUGIN


class PlayerProvider(Provider):
    """
    Base class for a Playerprovider.

    Should be overridden/subclassed by provider specific implementation.
    """

    @property
    def type(self) -> ProviderType:
        """Return ProviderType."""
        return ProviderType.PLAYER_PROVIDER

    @property
    def players(self) -> Players:
        """Return all players belonging to this provider."""
        # pylint: disable=no-member
        return [player for player in self.mass.players if player.provider_id == self.id]


class MetadataProvider(Provider):
    """
    Base class for a MetadataProvider.

    Should be overridden/subclassed by provider specific implementation.
    """

    @property
    def type(self) -> ProviderType:
        """Return ProviderType."""
        return ProviderType.METADATA_PROVIDER

    async def get_artist_images(self, mb_artist_id: str) -> Dict:
        """Retrieve artist metadata as dict by musicbrainz artist id."""
        raise NotImplementedError

    async def get_album_images(self, mb_album_id: str) -> Dict:
        """Retrieve album metadata as dict by musicbrainz album id."""
        raise NotImplementedError


class MusicProvider(Provider):
    """
    Base class for a Musicprovider.

    Should be overriden in the provider specific implementation.
    """

    @property
    def type(self) -> ProviderType:
        """Return ProviderType."""
        return ProviderType.MUSIC_PROVIDER

    @property
    def supported_mediatypes(self) -> List[MediaType]:
        """Return MediaTypes the provider supports."""
        return [
            MediaType.Album,
            MediaType.Artist,
            MediaType.Playlist,
            MediaType.Radio,
            MediaType.Track,
        ]

    async def search(
        self, search_query: str, media_types=Optional[List[MediaType]], limit: int = 5
    ) -> SearchResult:
        """
        Perform search on musicprovider.

            :param search_query: Search query.
            :param media_types: A list of media_types to include. All types if None.
            :param limit: Number of items to return in the search (per type).
        """
        raise NotImplementedError

    async def get_library_artists(self) -> List[Artist]:
        """Retrieve library artists from the provider."""
        if MediaType.Artist in self.supported_mediatypes:
            raise NotImplementedError

    async def get_library_albums(self) -> List[Album]:
        """Retrieve library albums from the provider."""
        if MediaType.Album in self.supported_mediatypes:
            raise NotImplementedError

    async def get_library_tracks(self) -> List[Track]:
        """Retrieve library tracks from the provider."""
        if MediaType.Track in self.supported_mediatypes:
            raise NotImplementedError

    async def get_library_playlists(self) -> List[Playlist]:
        """Retrieve library/subscribed playlists from the provider."""
        if MediaType.Playlist in self.supported_mediatypes:
            raise NotImplementedError

    async def get_radios(self) -> List[Radio]:
        """Retrieve library/subscribed radio stations from the provider."""
        if MediaType.Radio in self.supported_mediatypes:
            raise NotImplementedError

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        if MediaType.Artist in self.supported_mediatypes:
            raise NotImplementedError

    async def get_artist_albums(self, prov_artist_id: str) -> List[Album]:
        """Get a list of all albums for the given artist."""
        if MediaType.Album in self.supported_mediatypes:
            raise NotImplementedError

    async def get_artist_toptracks(self, prov_artist_id: str) -> List[Track]:
        """Get a list of most popular tracks for the given artist."""
        if MediaType.Track in self.supported_mediatypes:
            raise NotImplementedError

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        if MediaType.Album in self.supported_mediatypes:
            raise NotImplementedError

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        if MediaType.Track in self.supported_mediatypes:
            raise NotImplementedError

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        if MediaType.Playlist in self.supported_mediatypes:
            raise NotImplementedError

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id."""
        if MediaType.Radio in self.supported_mediatypes:
            raise NotImplementedError

    async def get_album_tracks(self, prov_album_id: str) -> List[Track]:
        """Get album tracks for given album id."""
        if MediaType.Album in self.supported_mediatypes:
            raise NotImplementedError

    async def get_playlist_tracks(self, prov_playlist_id: str) -> List[Track]:
        """Get all playlist tracks for given playlist id."""
        if MediaType.Playlist in self.supported_mediatypes:
            raise NotImplementedError

    async def library_add(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Add item to provider's library. Return true on succes."""
        raise NotImplementedError

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove item from provider's library. Return true on succes."""
        raise NotImplementedError

    async def add_playlist_tracks(
        self, prov_playlist_id: str, prov_track_ids: List[str]
    ) -> bool:
        """Add track(s) to playlist. Return true on succes."""
        if MediaType.Playlist in self.supported_mediatypes:
            raise NotImplementedError

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, prov_track_ids: List[str]
    ) -> bool:
        """Remove track(s) from playlist. Return true on succes."""
        if MediaType.Playlist in self.supported_mediatypes:
            raise NotImplementedError

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Get streamdetails for a track/radio."""
        raise NotImplementedError
