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
from typing import TYPE_CHECKING, Any

from gql import Client
from gql.transport.requests import RequestsHTTPTransport
from music_assistant_models.enums import (
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemType,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    Radio,
    RecommendationFolder,
    SearchResults,
    Track,
)
from music_assistant_models.streamdetails import StreamDetails
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.cache import use_cache
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.ard_audiothek.helper import (
    ard_search_query,
    livestream_query,
    organizations_query,
    publication_service_metadata_query,
    publication_services_query,
    publications_list_query,
    show_episode_query,
    show_length_query,
    show_query,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


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
        result = self._client.execute(
            ard_search_query, variable_values={"query": search_query, "limit": limit}
        )["search"]

        podcasts = []
        for element in result["shows"]["nodes"]:
            podcasts += [
                _parse_podcast(
                    self.domain,
                    self.lookup_key,
                    self.instance_id,
                    element,
                    element["coreId"],
                )
            ]

        return SearchResults(podcasts=podcasts)

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
        rad = self._client.execute(livestream_query, variable_values={"coreId": prov_radio_id})[
            "permanentLivestreamByCoreId"
        ]

        metadata_query = self._client.execute(
            publication_service_metadata_query,
            variable_values={"coreId": rad["publisherCoreId"]},
        )["publicationServiceByCoreId"]

        radio = _parse_radio(
            self.domain,
            self.lookup_key,
            self.instance_id,
            metadata_query,
            rad,
            prov_radio_id,
        )

        radio = Radio(
            item_id=prov_radio_id,
            provider=self.domain,
            name=rad["title"],
            provider_mappings={
                ProviderMapping(
                    item_id=prov_radio_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        media_links = None

        radio.metadata.links = media_links
        # {
        #     MediaItemLink(
        #         type=LinkType.WEBSITE,
        #         url="http://www.br.de/on3/index.html",
        #     ),
        #     MediaItemLink(
        #         type=LinkType.TIKTOK,
        #         url="https://www.tiktok.com/@deinpuls",
        #     ),
        #     MediaItemLink(
        #         type=LinkType.INSTAGRAM,
        #         url="https://www.instagram.com/dein_puls",
        #     ),
        # }

        radio.metadata.description = metadata_query["synopsis"]
        radio.metadata.genres = {metadata_query["genre"]}

        radio.metadata.add_image(create_media_image(self.domain, rad["imagesList"]))

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

    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:  # type: ignore[empty-body]
        """Retrieve a dynamic list of similar tracks based on the provided track."""
        # Get a list of similar tracks based on the provided track.
        # This is only called if the provider supports the SIMILAR_TRACKS feature.

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

        part_parts = path.split("://")[1].split("/")
        organization = part_parts[0] if part_parts else ""
        provider = part_parts[1] if len(part_parts) > 1 else ""
        radio_station = part_parts[2] if len(part_parts) > 2 else ""

        if not organization:
            return await self.get_organizations(path)

        if not provider:
            # list radios for specific organization
            return await self.get_publication_services(path, organization)

        if not radio_station:
            return await self.get_publications_list(provider)

        return []

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get podcast."""
        result = self._client.execute(show_query, variable_values={"showId": prov_podcast_id})[
            "show"
        ]

        return _parse_podcast(
            self.domain,
            self.lookup_key,
            self.instance_id,
            result,
            prov_podcast_id,
        )

    async def get_podcast_episodes(
        self, prov_podcast_id: str
    ) -> AsyncGenerator[PodcastEpisode, None]:
        """Get podcast episodes."""
        length = self._client.execute(
            show_length_query, variable_values={"showId": prov_podcast_id}
        )["show"]["items"]["totalCount"]
        step_size = 32
        for offset in range(0, length, step_size):
            result = self._client.execute(
                show_query,
                variable_values={"showId": prov_podcast_id, "first": step_size, "offset": offset},
            )["show"]
            for idx, episode in enumerate(result["items"]["nodes"]):
                if len(episode["audioList"]) == 0:
                    continue
                yield _parse_podcast_episode(
                    self.domain, self.lookup_key, self.instance_id, episode, prov_podcast_id, idx
                )

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get single podcast episode."""
        result = self._client.execute(
            show_episode_query, variable_values={"coreId": prov_episode_id}
        )["itemByCoreId"]
        if result is None:
            raise MediaNotFoundError("Episode not found")
        return _parse_podcast_episode(
            self.domain,
            self.lookup_key,
            self.instance_id,
            result,
            result["showId"],
            result["rowId"],
        )

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a radio station."""
        if media_type == MediaType.RADIO:
            result = self._client.execute(livestream_query, variable_values={"coreId": item_id})[
                "permanentLivestreamByCoreId"
            ]
            seek = False
        elif media_type == MediaType.PODCAST_EPISODE:
            result = self._client.execute(show_episode_query, variable_values={"coreId": item_id})[
                "itemByCoreId"
            ]
            seek = True

        streams = result["audioList"]
        selected_stream = max(streams, key=lambda x: x["audioBitrate"])

        return StreamDetails(
            provider=self.domain,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.try_parse(selected_stream["audioCodec"]),
            ),
            media_type=MediaType.RADIO,
            stream_type=StreamType.HTTP,
            path=fix_url(selected_stream["href"]),
            can_seek=seek,
            allow_seek=seek,
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

    @use_cache(3600)
    async def get_organizations(self, path: str) -> list[BrowseFolder]:
        """Create a list of all available organizations."""
        result = self._client.execute(organizations_query)["organizations"]["nodes"]
        organizations = []

        for org in result:
            if all(
                b["coreId"] is None for b in org["publicationServicesByOrganizationName"]["nodes"]
            ):
                # No available station
                continue
            image = None
            for pub in org["publicationServicesByOrganizationName"]["nodes"]:
                pub_title = pub["title"].lower()
                org_name = org["name"].lower()
                org_title = org["title"].lower()
                if pub_title in (org_name, org_title) or pub_title.replace(" ", "") == org_name:
                    image = create_media_image(self.domain, pub["imagesList"])
                    break
            organizations += [
                BrowseFolder(
                    item_id=org["coreId"],
                    provider=self.domain,
                    path=path + org["coreId"],
                    image=image,
                    name=org["title"],
                )
            ]

        return organizations

    @use_cache(3600)
    async def get_publication_services(self, path: str, core_id: str) -> list[BrowseFolder]:
        """Create a list of publications for a given organization."""
        result = self._client.execute(
            publication_services_query, variable_values={"coreId": core_id}
        )["organizationByCoreId"]["publicationServicesByOrganizationName"]["nodes"]
        publications = []

        for pub in result:
            if not pub["coreId"]:
                continue
            publications += [
                BrowseFolder(
                    item_id=pub["coreId"],
                    provider=self.domain,
                    path=path + "/" + pub["coreId"],
                    image=create_media_image(self.domain, pub["imagesList"]),
                    name=pub["title"],
                )
            ]

        return publications

    async def get_publications_list(self, core_id: str) -> list[Radio | Podcast]:
        """Create list of available radio stations and shows for a publication service."""
        result = self._client.execute(publications_list_query, variable_values={"coreId": core_id})[
            "publicationServiceByCoreId"
        ]

        metadata_query = self._client.execute(
            publication_service_metadata_query,
            variable_values={"coreId": core_id},
        )["publicationServiceByCoreId"]

        publications = []  # type: list[Radio | Podcast]

        for rad in result["permanentLivestreams"]["nodes"]:
            if not rad["coreId"]:
                continue

            radio = _parse_radio(
                self.domain, self.lookup_key, self.instance_id, metadata_query, rad, rad["coreId"]
            )

            publications += [radio]

        for pod in result["shows"]["nodes"]:
            if not pod["coreId"]:
                continue

            podcast = _parse_podcast(
                self.domain,
                self.lookup_key,
                self.instance_id,
                pod,
                pod["coreId"],
            )
            publications += [podcast]

        return publications


def _parse_podcast(
    domain: str,
    lookup_key: str,
    instance_id: str,
    podcast_query: dict[str, Any],
    podcast_id: str,
) -> Podcast:
    podcast = Podcast(
        name=podcast_query["title"],
        item_id=podcast_id,
        publisher=podcast_query["publicationService"]["title"],
        provider=lookup_key,
        provider_mappings={
            ProviderMapping(
                item_id="none",
                provider_domain=domain,
                provider_instance=instance_id,
            )
        },
        total_episodes=podcast_query["items"]["totalCount"],
    )

    media_links = None

    podcast.metadata.links = media_links
    # {
    #     MediaItemLink(
    #         type=LinkType.WEBSITE,
    #         url="http://www.br.de/on3/index.html",
    #     ),
    #     MediaItemLink(
    #         type=LinkType.TIKTOK,
    #         url="https://www.tiktok.com/@deinpuls",
    #     ),
    #     MediaItemLink(
    #         type=LinkType.INSTAGRAM,
    #         url="https://www.instagram.com/dein_puls",
    #     ),
    # }

    podcast.metadata.description = podcast_query["synopsis"]
    podcast.metadata.genres = {r["title"] for r in podcast_query["editorialCategoriesList"]}

    podcast.metadata.add_image(create_media_image(domain, podcast_query["imagesList"]))

    return podcast


def _parse_radio(
    domain: str,
    lookup_key: str,
    instance_id: str,
    metadata: dict[str, Any],
    radio_query: dict[str, Any],
    radio_id: str,
) -> Radio:
    radio = Radio(
        name=radio_query["title"],
        item_id=radio_id,
        provider=lookup_key,
        provider_mappings={
            ProviderMapping(
                item_id="none",
                provider_domain=domain,
                provider_instance=instance_id,
            )
        },
    )

    media_links = None

    radio.metadata.links = media_links
    # {
    #     MediaItemLink(
    #         type=LinkType.WEBSITE,
    #         url="http://www.br.de/on3/index.html",
    #     ),
    #     MediaItemLink(
    #         type=LinkType.TIKTOK,
    #         url="https://www.tiktok.com/@deinpuls",
    #     ),
    #     MediaItemLink(
    #         type=LinkType.INSTAGRAM,
    #         url="https://www.instagram.com/dein_puls",
    #     ),
    # }

    radio.metadata.description = metadata["synopsis"]
    radio.metadata.genres = {metadata["genre"]}

    radio.metadata.add_image(create_media_image(domain, radio_query["imagesList"]))

    return radio


def _parse_podcast_episode(
    domain: str,
    lookup_key: str,
    instance_id: str,
    episode: dict[str, Any],
    podcast_id: str,
    idx: int,
) -> PodcastEpisode:
    podcast_episode = PodcastEpisode(
        name=episode["title"],
        duration=episode["duration"],
        item_id=episode["coreId"],
        provider=lookup_key,
        podcast=ItemMapping(
            item_id=podcast_id,
            provider=lookup_key,
            name=episode["title"],
            media_type=MediaType.PODCAST,
        ),
        provider_mappings={
            ProviderMapping(
                item_id=episode["coreId"],
                provider_domain=domain,
                provider_instance=instance_id,
            )
        },
        position=idx,
    )

    podcast_episode.metadata.add_image(create_media_image(domain, episode["imagesList"]))
    podcast_episode.metadata.description = episode["summary"]
    return podcast_episode


def create_media_image(domain: str, image_list: list[dict[str, str]]) -> MediaItemImage:
    """Extract the image for hopefully all possible cases."""
    image_url = ""
    selected_img = image_list[0] if image_list else None
    for img in image_list:
        if img["aspectRatio"] == "1x1":
            selected_img = img
            break
    if selected_img:
        image_url = selected_img["url"].replace("{width}", str(selected_img["width"]))
    return MediaItemImage(
        type=ImageType.THUMB,
        path=image_url,
        provider=domain,
        remotely_accessible=True,
    )


def fix_url(url: str) -> str:
    """Fix some of the stream urls, which do not provide a protocol."""
    if url.startswith("//"):
        url = "https:" + url
    return url
