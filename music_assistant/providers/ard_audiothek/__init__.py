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
We strongly recommend developing on either macOS or Linux and start your development
environment by running the setup.sh script in the scripts folder of the repository.
This will create a virtual environment and install all dependencies needed for development.
See also our general DEVELOPMENT.md guide in the repository for more information.

"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from typing import TYPE_CHECKING

from gql import Client
from gql.transport.requests import RequestsHTTPTransport
from music_assistant_models.enums import (
    ContentType,
    ImageType,
    LinkType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemLink,
    MediaItemType,
    Playlist,
    Podcast,
    ProviderMapping,
    Radio,
    RecommendationFolder,
    SearchResults,
    Track,
)
from music_assistant_models.streamdetails import StreamDetails
from music_assistant_models.unique_list import UniqueList

from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CACHE_CATEGORY_OTHER = 1
CACHE_KEY_TIMESTAMP = (
    "timestamp"  # tuple of two ints, timestamp_subscriptions and timestamp_actions
)
CACHE_KEY_RADIO = "radio"  # list[str] : all available rss feed urls


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    # setup is called when the user wants to setup a new provider instance.
    # you are free to do any preflight checks here and but you must return
    #  an instance of the provider.
    return ARDAudiothek(mass, manifest, config)


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


class ARDAudiothek(MusicProvider):
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
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        # MANDATORY
        # you should return a tuple of provider-level features
        # here that your player provider supports or an empty tuple if none.
        # for example 'ProviderFeature.SYNC_PLAYERS' if you can sync players.
        return {
            ProviderFeature.BROWSE,
            ProviderFeature.SEARCH,
            ProviderFeature.RECOMMENDATIONS,
            ProviderFeature.LIBRARY_RADIOS,
            ProviderFeature.LIBRARY_PODCASTS,
            ProviderFeature.LIBRARY_PODCASTS_EDIT,
            # ProviderFeature.SIMILAR_TRACKS,
            # see the ProviderFeature enum for all available features
        }

    async def handle_async_init(self) -> None:
        """Pass config values to client and initialize."""
        transport = RequestsHTTPTransport(
            url="https://api.ardaudiothek.de/graphql",
            verify=True,
            retries=3,
        )

        # Create a client
        self._client = Client(
            transport=transport,
            fetch_schema_from_transport=True,
        )

        timestamps = await self.mass.cache.get(
            key=CACHE_KEY_TIMESTAMP,
            base_key=self.lookup_key,
            category=CACHE_CATEGORY_OTHER,
            default=None,
        )
        if timestamps is None:
            self.timestamp_subscriptions: int = 0
            self.timestamp_actions: int = 0
        else:
            self.timestamp_subscriptions, self.timestamp_actions = timestamps

        self.logger.debug(
            "Our timestamps are (subscriptions, actions)  (%s, %s)",
            self.timestamp_subscriptions,
            self.timestamp_actions,
        )

        # self.radio_feeds = self._load_radio_feeds()

        # we are syncing the playlog, but not event based. A simple check in on_played,
        # should be sufficient
        self.progress_guard_timestamp = 0.0

    # async def _load_radio_feeds(self):
    #     radio_feeds = await self.mass.cache.get(
    #         key=CACHE_KEY_RADIO,
    #         base_key=self.lookup_key,
    #         category=CACHE_CATEGORY_OTHER,
    #         default=None,
    #     )
    #     if radio_feeds is None:
    #         radio_feeds: set[str] = set(self._load_radio_feeds())

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        # OPTIONAL
        # this is an optional method that you can implement if
        # relevant or leave out completely if not needed.
        # In most cases this can be omitted for music providers.

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        is_removed will be set to True when the provider is removed from the configuration.
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
        return SearchResults()

    async def get_library_radios(self) -> AsyncGenerator[Radio, None]:
        """Retrieve library/subscribed radio stations from the provider."""
        # OPTIONAL
        # Will only be called if you reported the LIBRARY_RADIOS feature
        # in the supported_features and you did not override the default sync method.
        # It allows retrieving the library/favorite radio stations from your provider.
        # Warning: Async generator:
        # You should yield Radio objects for each radio station in the library.
        yield  # type: ignore[misc]

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id."""
        # Get full details of a single Radio station.
        # Mandatory only if you reported LIBRARY_RADIOS in the supported_features.
        radio = Radio(
            item_id="WyJwZXJtYW5lbnRfbGl2ZXN0cmVhbXMiLCI1ODc5MjkwMCJd",
            provider=self.domain,
            name="PULS",  # codespell:ignore
            provider_mappings={
                ProviderMapping(
                    item_id="WyJwZXJtYW5lbnRfbGl2ZXN0cmVhbXMiLCI1ODc5MjkwMCJd",
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        radio.metadata.links = {
            MediaItemLink(
                type=LinkType.WEBSITE,
                url="http://www.br.de/on3/index.html",
            ),
            MediaItemLink(
                type=LinkType.TIKTOK,
                url="https://www.tiktok.com/@deinpuls",
            ),
            MediaItemLink(
                type=LinkType.INSTAGRAM,
                url="https://www.instagram.com/dein_puls",
            ),
        }

        radio.metadata.description = "PULS ist das junge Programm des Bayerischen Rundfunks. Und zwar in echt. Das Neueste aus Politik und Popkultur, aus Bayern - und der Welt."  # noqa: E501 # codespell:ignore
        radio.metadata.genres = set("Pop und Szene")  # codespell:ignore

        radio.metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path="https://api.ardmediathek.de/image-service/images/urn:ard:image:3a2e73b206fe0a53?w=200&ch=b8c5a3f57ecad023",
                    provider=self.lookup_key,
                    remotely_accessible=True,
                )
            ]
        )

        return radio

    # async def get_library_podcasts(self) -> AsyncGenerator[Podcast, None]:
    #     """Retrieve library/subscribed podcasts from the provider.

    #     Minified podcast information is enough.
    #     """

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
        # This is only called if the provider supports the PLAYLIST_TRACKS_EDIT feature.

    async def create_playlist(self, name: str) -> Playlist:  # type: ignore[empty-body]
        """Create a new playlist on provider with given name."""
        # Create a new playlist on the provider.
        # This is only called if the provider supports the PLAYLIST_CREATE feature.

    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:  # type: ignore[empty-body]
        """Retrieve a dynamic list of similar tracks based on the provided track."""
        # Get a list of similar tracks based on the provided track.
        # This is only called if the provider supports the SIMILAR_TRACKS feature.

    async def get_resume_position(self, item_id: str, media_type: MediaType) -> tuple[bool, int]:  # type: ignore[empty-body]
        """
        Get progress (resume point) details for the given Audiobook or Podcast episode.

        This is a separate call from the regular get_item call to ensure the resume position
        is always up-to-date and because a lot providers have this info present on a dedicated
        endpoint.

        Will be called right before playback starts to ensure the resume position is correct.

        Returns a boolean with the fully_played status
        and an integer with the resume position in ms.
        """
        # optional function to get the resume position of a audiobook or podcast episode
        # only implement this if your provider supports providing this information

    async def get_audio_stream(
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
        yield  # type: ignore[misc]

    async def on_streamed(
        self,
        streamdetails: StreamDetails,
    ) -> None:
        """
        Handle callback when given streamdetails completed streaming.

        To get the number of seconds streamed, see streamdetails.seconds_streamed.
        To get the number of seconds seeked/skipped, see streamdetails.seek_position.
        Note that seconds_streamed is the total streamed seconds, so without seeked time.

        NOTE: Due to internal and player buffering,
        this may be called in advance of the actual completion.
        """
        # This is an OPTIONAL callback that is called when an item has been streamed.
        # You can use this e.g. for playback reporting or statistics.

    async def on_played(
        self,
        media_type: MediaType,
        prov_item_id: str,
        fully_played: bool,
        position: int,
        media_item: MediaItemType,
        is_playing: bool = False,
    ) -> None:
        """
        Handle callback when a (playable) media item has been played.

        This is called by the Queue controller when;
            - a track has been fully played
            - a track has been stopped (or skipped) after being played
            - every 30s when a track is playing

        Fully played is True when the track has been played to the end.

        Position is the last known position of the track in seconds, to sync resume state.
        When fully_played is set to false and position is 0,
        the user marked the item as unplayed in the UI.

        is_playing is True when the track is currently playing.

        media_item is the full media item details of the played/playing track.
        """
        # This is an OPTIONAL callback that is called when an item has been streamed.
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

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse this provider's items.

        :param path: The path to browse, (e.g. provider_id://artists).
        """
        # Browse your provider's recommendations/media items.
        # This is only called if you reported the BROWSE feature in the supported_features.
        # You should return a list of MediaItems or ItemMappings for the given path.
        # Note that you can return nested levels with BrowseFolder items.

        # The MusicProvider base model has a default implementation of this method
        # that will call the get_library_* methods if you did not override it.

        radio = Radio(
            item_id="WyJwZXJtYW5lbnRfbGl2ZXN0cmVhbXMiLCI1ODc5MjkwMCJd",
            provider=self.domain,
            name="PULS",  # codespell:ignore
            provider_mappings={
                ProviderMapping(
                    item_id="WyJwZXJtYW5lbnRfbGl2ZXN0cmVhbXMiLCI1ODc5MjkwMCJd",
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        radio.metadata.links = {
            MediaItemLink(
                type=LinkType.WEBSITE,
                url="https://www.ardaudiothek.de/sender/br/puls/?livestream=urn:ard:permanent-livestream:55aefaed64870aa5",  # codespell:ignore  # noqa: E501
            )
        }

        radio.metadata.description = "PULS ist das junge Programm des Bayerischen Rundfunks. Und zwar in echt. Das Neueste aus Politik und Popkultur, aus Bayern - und der Welt."  # noqa: E501  # codespell:ignore
        radio.metadata.genres = set("Pop und Szene")  # codespell:ignore

        radio.metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path="https://api.ardmediathek.de/image-service/images/urn:ard:image:3a2e73b206fe0a53?w=200&ch=b8c5a3f57ecad023",
                    provider=self.lookup_key,
                    remotely_accessible=True,
                )
            ]
        )

        return [
            BrowseFolder(
                item_id="popular",
                provider=self.domain,
                path=path + "popular",
                name="",
                translation_key="radiobrowser_by_popularity",
            ),
            BrowseFolder(
                item_id="country",
                provider=self.domain,
                path=path + "country",
                name="",
                translation_key="radiobrowser_by_country",
            ),
            BrowseFolder(
                item_id="tag",
                provider=self.domain,
                path=path + "tag",
                name="",
                translation_key="radiobrowser_by_tag",
            ),
            radio,
        ]

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a radio station."""
        return StreamDetails(
            provider=self.domain,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.try_parse("EAC3JOC"),
            ),
            media_type=MediaType.RADIO,
            stream_type=StreamType.HTTP,
            path="https://mcdn.hf.br.de/br/hf/puls/master.m3u8",  # codespell:ignore
            can_seek=False,
            allow_seek=False,
        )

    async def recommendations(self) -> list[RecommendationFolder]:
        """
        Get this provider's recommendations.

        Returns an actual (and often personalised) list of recommendations
        from this provider for the user/account.
        """
        # Get this provider's recommendations.
        # This is only called if you reported the RECOMMENDATIONS feature in the supported_features.
        return [
            RecommendationFolder(
                item_id="ardaudiothek-recommendations",
                name="ARD Audiothek: Recommendations",
                icon="mdi-trending-up",
                # translation_key=shelf.id_,
                items=UniqueList(
                    [
                        Podcast(
                            name="Test",
                            item_id="0",
                            publisher="Jan",
                            provider=self.lookup_key,
                            provider_mappings={
                                ProviderMapping(
                                    item_id="none",
                                    provider_domain=self.domain,
                                    provider_instance=self.instance_id,
                                )
                            },
                        )
                    ]
                ),
                provider=self.lookup_key,
            )
        ]

    async def sync_library(self, media_type: MediaType) -> None:
        """Run library sync for this provider."""
        # Run a full sync of the library for the given media type.
        # This is called by the music controller to sync items from your provider to the library.
        # As a generic rule of thumb the default implementation within the MusicProvider
        # base model should be sufficient for most (streaming) providers.
        # If you need to do some custom sync logic, you can override this method.
        # For example the filesystem provider in MA, overrides this method to scan the filesystem.
