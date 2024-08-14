"""
DEMO/TEMPLATE Music Provider for Music Assistant.

This is an empty music provider with no actual implementation.
Its meant to get started developing a new music provider for Music Assistant.

Use it as a reference to discover what methods exists and what they should return.
Also it is good to look at existing music providers to get a better understanding,
due to the fact that providers may be flexible and support different features.

If you are relying on a third-party library to interact with the music source,
you can then reference your library in the manifest in the requirements section,
which is a list of (versioned!) python modules (pip syntax) that should be installed
when the provider is selected by the user.

Please keep in mind that Music Assistant is a fully async application and all
methods should be implemented as async methods. If you are not familiar with
async programming in Python, we recommend you to read up on it first.
If you are using a third-party library that is not async, you can need to use the several
helper methods such as asyncio.to_thread or the create_task in the mass object to wrap
the calls to the library in a thread.

To add a new provider to Music Assistant, you need to create a new folder
in the providers folder with the name of your provider (e.g. 'my_music_provider').
In that folder you should create (at least) a __init__.py file and a manifest.json file.

Optional is an icon.svg file that will be used as the icon for the provider in the UI,
but we also support that you specify a material design icon in the manifest.json file.

IMPORTANT NOTE:
We strongly recommend developing on either MacOS or Linux and start your development
environment by running the setup.sh script in the scripts folder of the repository.
This will create a virtual environment and install all dependencies needed for development.
See also our general DEVELOPMENT.md guide in the repository for more information.

"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from typing import TYPE_CHECKING

from music_assistant.common.models.config_entries import ConfigEntry, ConfigValueType
from music_assistant.common.models.enums import ContentType, MediaType, ProviderFeature, StreamType
from music_assistant.common.models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItemType,
    Playlist,
    ProviderMapping,
    Radio,
    SearchResults,
    Track,
)
from music_assistant.common.models.streamdetails import StreamDetails
from music_assistant.server.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    # setup is called when the user wants to setup a new provider instance.
    # you are free to do any preflight checks here and but you must return
    #  an instance of the provider.
    return MyDemoMusicprovider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # ruff: noqa: ARG001
    # Config Entries are used to configure the Music Provider if needed.
    # See the models of ConfigEntry and ConfigValueType for more information what is supported.
    # The ConfigEntry is a dataclass that represents a single configuration entry.
    # The ConfigValueType is an Enum that represents the type of value that
    # can be stored in a ConfigEntry.
    # If your provider does not need any configuration, you can return an empty tuple.

    # We support flow-like configuration where you can have multiple steps of configuration
    # using the 'action' parameter to distinguish between the different steps.
    # The 'values' parameter contains the raw values of the config entries that were filled in
    # by the user in the UI. This is a dictionary with the key being the config entry id
    # and the value being the actual value filled in by the user.

    # For authentication flows where the user needs to be redirected to a login page
    # or some other external service, we have a simple helper that can help you with those steps
    # and a callback url that you can use to redirect the user back to the Music Assistant UI.
    # See for example the Deezer provider for an example of how to use this.
    return ()


class MyDemoMusicprovider(MusicProvider):
    """
    Example/demo Music provider.

    Note that this is always subclassed from MusicProvider,
    which in turn is a subclass of the generic Provider model.

    The base implementation already takes care of some convenience methods,
    such as the mass object and the logger. Take a look at the base class
    for more information on what is available.

    Just like with any other subclass, make sure that if you override
    any of the default methods (such as __init__), you call the super() method.
    In most cases its not needed to override any of the builtin methods and you only
    implement the abc methods with your actual implementation.
    """

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        # MANDATORY
        # you should return a tuple of provider-level features
        # here that your player provider supports or an empty tuple if none.
        # for example 'ProviderFeature.SYNC_PLAYERS' if you can sync players.
        return (
            ProviderFeature.BROWSE,
            ProviderFeature.SEARCH,
            ProviderFeature.RECOMMENDATIONS,
            ProviderFeature.LIBRARY_ARTISTS,
            ProviderFeature.LIBRARY_ALBUMS,
            ProviderFeature.LIBRARY_TRACKS,
            ProviderFeature.LIBRARY_PLAYLISTS,
            ProviderFeature.ARTIST_ALBUMS,
            ProviderFeature.ARTIST_TOPTRACKS,
            ProviderFeature.LIBRARY_ARTISTS_EDIT,
            ProviderFeature.LIBRARY_ALBUMS_EDIT,
            ProviderFeature.LIBRARY_TRACKS_EDIT,
            ProviderFeature.LIBRARY_PLAYLISTS_EDIT,
            ProviderFeature.SIMILAR_TRACKS,
            # see the ProviderFeature enum for all available features
        )

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        # OPTIONAL
        # this is an optional method that you can implement if
        # relevant or leave out completely if not needed.
        # In most cases this can be omitted for music providers.

    async def unload(self) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        """
        # OPTIONAL
        # This is an optional method that you can implement if
        # relevant or leave out completely if not needed.
        # It will be called when the provider is unloaded from Music Assistant.
        # for example to disconnect from a service or clean up resources.

    @property
    def is_streaming_provider(self) -> bool:
        """
        Return True if the provider is a streaming provider.

        This literally means that the catalog is not the same as the library contents.
        For local based providers (files, plex), the catalog is the same as the library content.
        It also means that data is if this provider is NOT a streaming provider,
        data cross instances is unique, the catalog and library differs per instance.

        Setting this to True will only query one instance of the provider for search and lookups.
        Setting this to False will query all instances of this provider for search and lookups.
        """
        # For streaming providers return True here but for local file based providers return False.
        return True

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """Perform search on musicprovider.

        :param search_query: Search query.
        :param media_types: A list of media_types to include.
        :param limit: Number of items to return in the search (per type).
        """
        # OPTIONAL
        # Will only be called if you reported the SEARCH feature in the supported_features.
        # It allows searching your provider for media items.
        # See the model for SearchResults for more information on what to return, but
        # in general you should return a list of MediaItems for each media type.

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve library artists from the provider."""
        # OPTIONAL
        # Will only be called if you reported the LIBRARY_ARTISTS feature
        # in the supported_features and you did not override the default sync method.
        # It allows retrieving the library/favorite artists from your provider.
        # Warning: Async generator:
        # You should yield Artist objects for each artist in the library.
        yield Artist(
            # A simple example of an artist object,
            # you should replace this with actual data from your provider.
            # Explore the Artist model for all options and descriptions.
            item_id="123",
            provider=self.instance_id,
            name="Artist Name",
            provider_mappings={
                ProviderMapping(
                    # A provider mapping is used to provide details about this item on this provider
                    # Music Assistant differentiates between domain and instance id to account for
                    # multiple instances of the same provider.
                    # The instance_id is auto generated by MA.
                    item_id="123",
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    # set 'available' to false if the item is (temporary) unavailable
                    available=True,
                    audio_format=AudioFormat(
                        # provide details here about sample rate etc. if known
                        content_type=ContentType.FLAC,
                    ),
                )
            },
        )

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve library albums from the provider."""
        # OPTIONAL
        # Will only be called if you reported the LIBRARY_ALBUMS feature
        # in the supported_features and you did not override the default sync method.
        # It allows retrieving the library/favorite albums from your provider.
        # Warning: Async generator:
        # You should yield Album objects for each album in the library.
        yield  # type: ignore

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from the provider."""
        # OPTIONAL
        # Will only be called if you reported the LIBRARY_TRACKS feature
        # in the supported_features and you did not override the default sync method.
        # It allows retrieving the library/favorite tracks from your provider.
        # Warning: Async generator:
        # You should yield Track objects for each track in the library.
        yield  # type: ignore

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve library/subscribed playlists from the provider."""
        # OPTIONAL
        # Will only be called if you reported the LIBRARY_PLAYLISTS feature
        # in the supported_features and you did not override the default sync method.
        # It allows retrieving the library/favorite playlists from your provider.
        # Warning: Async generator:
        # You should yield Playlist objects for each playlist in the library.
        yield  # type: ignore

    async def get_library_radios(self) -> AsyncGenerator[Radio, None]:
        """Retrieve library/subscribed radio stations from the provider."""
        # OPTIONAL
        # Will only be called if you reported the LIBRARY_RADIOS feature
        # in the supported_features and you did not override the default sync method.
        # It allows retrieving the library/favorite radio stations from your provider.
        # Warning: Async generator:
        # You should yield Radio objects for each radio station in the library.
        yield

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        # Get full details of a single Artist.
        # Mandatory only if you reported LIBRARY_ARTISTS in the supported_features.

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get a list of all albums for the given artist."""
        # Get a list of all albums for the given artist.
        # Mandatory only if you reported ARTIST_ALBUMS in the supported_features.

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get a list of most popular tracks for the given artist."""
        # Get a list of most popular tracks for the given artist.
        # Mandatory only if you reported ARTIST_TOPTRACKS in the supported_features.
        # Note that (local) file based providers will simply return all artist tracks here.

    async def get_album(self, prov_album_id: str) -> Album:  # type: ignore[return]
        """Get full album details by id."""
        # Get full details of a single Album.
        # Mandatory only if you reported LIBRARY_ALBUMS in the supported_features.

    async def get_track(self, prov_track_id: str) -> Track:  # type: ignore[return]
        """Get full track details by id."""
        # Get full details of a single Track.
        # Mandatory only if you reported LIBRARY_TRACKS in the supported_features.

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:  # type: ignore[return]
        """Get full playlist details by id."""
        # Get full details of a single Playlist.
        # Mandatory only if you reported LIBRARY_PLAYLISTS in the supported

    async def get_radio(self, prov_radio_id: str) -> Radio:  # type: ignore[return]
        """Get full radio details by id."""
        # Get full details of a single Radio station.
        # Mandatory only if you reported LIBRARY_RADIOS in the supported_features.

    async def get_album_tracks(
        self,
        prov_album_id: str,  # type: ignore[return]
    ) -> list[Track]:
        """Get album tracks for given album id."""
        # Get all tracks for a given album.
        # Mandatory only if you reported ARTIST_ALBUMS in the supported_features.

    async def get_playlist_tracks(
        self,
        prov_playlist_id: str,
        page: int = 0,
    ) -> list[Track]:
        """Get all playlist tracks for given playlist id."""
        # Get all tracks for a given playlist.
        # Mandatory only if you reported LIBRARY_PLAYLISTS in the supported_features.

    async def library_add(self, item: MediaItemType) -> bool:
        """Add item to provider's library. Return true on success."""
        # Add an item to your provider's library.
        # This is only called if the provider supports the EDIT feature for the media type.
        return True

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove item from provider's library. Return true on success."""
        # Remove an item from your provider's library.
        # This is only called if the provider supports the EDIT feature for the media type.
        return True

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add track(s) to playlist."""
        # Add track(s) to a playlist.
        # This is only called if the provider supports the PLAYLIST_TRACKS_EDIT feature.

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist."""
        # Remove track(s) from a playlist.
        # This is only called if the provider supports the EDPLAYLIST_TRACKS_EDITIT feature.

    async def create_playlist(self, name: str) -> Playlist:  # type: ignore[return]
        """Create a new playlist on provider with given name."""
        # Create a new playlist on the provider.
        # This is only called if the provider supports the PLAYLIST_CREATE feature.

    async def get_similar_tracks(  # type: ignore[return]
        self, prov_track_id: str, limit: int = 25
    ) -> list[Track]:
        """Retrieve a dynamic list of similar tracks based on the provided track."""
        # Get a list of similar tracks based on the provided track.
        # This is only called if the provider supports the SIMILAR_TRACKS feature.

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Get streamdetails for a track/radio."""
        # Get stream details for a track or radio.
        # Implementing this method is MANDATORY to allow playback.
        # The StreamDetails contain info how Music Assistant can play the track.
        # item_id will always be a track or radio id. Later, when/if MA supports
        # podcasts or audiobooks, this may as well be an episode or chapter id.
        # You should return a StreamDetails object here with the info as accurate as possible
        # to allow Music Assistant to process the audio using ffmpeg.
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                # provide details here about sample rate etc. if known
                # set content type to unknown to let ffmpeg guess the codec/container
                content_type=ContentType.UNKNOWN,
            ),
            media_type=MediaType.TRACK,
            # streamtype defines how the stream is provided
            # for most providers this will be HTTP but you can also use CUSTOM
            # to provide a custom stream generator in get_audio_stream.
            stream_type=StreamType.HTTP,
            # explore the StreamDetails model and StreamType enum for more options
            # but the above should be the mandatory fields to set.
        )

    async def get_audio_stream(  # type: ignore[return]
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """
        Return the (custom) audio stream for the provider item.

        Will only be called when the stream_type is set to CUSTOM.
        """
        # this is an async generator that should yield raw audio bytes
        # for the given streamdetails. You can use this to provide a custom
        # stream generator for the audio stream. This is only called when the
        # stream_type is set to CUSTOM in the get_stream_details method.
        yield  # type: ignore

    async def on_streamed(self, streamdetails: StreamDetails, seconds_streamed: int) -> None:
        """Handle callback when an item completed streaming."""
        # This is OPTIONAL callback that is called when an item has been streamed.
        # You can use this e.g. for playback reporting or statistics.

    async def resolve_image(self, path: str) -> str | bytes:
        """
        Resolve an image from an image path.

        This either returns (a generator to get) raw bytes of the image or
        a string with an http(s) URL or local path that is accessible from the server.
        """
        # This is an OPTIONAL method that you can implement to resolve image paths.
        # This is used to resolve image paths that are returned in the MediaItems.
        # You can return a URL to an image or a generator that yields the raw bytes of the image.
        # This will only be called when you set 'remotely_accessible'
        # to false in a MediaItemImage object.
        return path

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping]:
        """Browse this provider's items.

        :param path: The path to browse, (e.g. provider_id://artists).
        """
        # Browse your provider's recommendations/media items.
        # This is only called if you reported the BROWSE feature in the supported_features.
        # You should return a list of MediaItems or ItemMappings for the given path.
        # Note that you can return nested levels with BrowseFolder items.

        # The MusicProvider base model has a default implementation of this method
        # that will call the get_library_* methods if you did not override it.
        return []

    async def recommendations(self) -> list[MediaItemType]:
        """Get this provider's recommendations.

        Returns a actual and personalised list of Media items with recommendations
        form this provider for the user/account. It may return nested levels with
        BrowseFolder items.
        """
        # Get this provider's recommendations.
        # This is only called if you reported the RECOMMENDATIONS feature in the supported_features.
        return []

    async def sync_library(self, media_types: tuple[MediaType, ...]) -> None:
        """Run library sync for this provider."""
        # Run a full sync of the library for the given media types.
        # This is called by the music controller to sync items from your provider to the library.
        # As a generic rule of thumb the default implementation within the MusicProvider
        # base model should be sufficient for most (streaming) providers.
        # If you need to do some custom sync logic, you can override this method.
        # For example the filesystem provider in MA, overrides this method to scan the filesystem.
