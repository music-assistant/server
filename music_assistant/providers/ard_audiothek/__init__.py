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
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from gql import Client
from gql.transport.aiohttp import AIOHTTPTransport
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    LinkType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import LoginFailed, MediaNotFoundError
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemLink,
    MediaItemType,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    Radio,
    SearchResults,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.cache import use_cache
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.ard_audiothek.database_queries import (
    get_history_query,
    get_subscriptions_query,
    livestream_query,
    organizations_query,
    publication_services_query,
    publications_list_query,
    search_radios_query,
    search_shows_query,
    show_episode_query,
    show_length_query,
    show_query,
    update_history_entry,
)

if TYPE_CHECKING:
    from aiohttp import ClientSession
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

# Config for login
CONF_USER_NAME = "username"
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_TOKEN_BEARER = "token"
CONF_EXPIRY_TIME = "token_expiry"
CONF_USERID = "login_url"

# Constants for config actions
CONF_ACTION_AUTH = "authenticate"
CONF_ACTION_CLEAR_AUTH = "clear_auth"

# General config
CONF_MAX_BITRATE = "max_num_episodes"
CONF_PODCAST_FINISHED = "podcast_finished_time"

IDENTITY_TOOLKIT_BASE_URL = "https://identitytoolkit.googleapis.com/v1/accounts"
ARD_ACCOUNTS_URL = "https://accounts.ard.de"
ARD_AUDIOTHEK_GRAPHQL = "https://api.ardaudiothek.de/graphql"

CACHE_CATEGORY_OTHER = 0


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    # setup is called when the user wants to setup a new provider instance.
    # you are free to do any preflight checks here and but you must return
    #  an instance of the provider.
    return ARDAudiothek(mass, manifest, config)


async def _login(session: ClientSession, email: str, password: str) -> tuple[str, str, str]:
    response = await session.post(
        f"{IDENTITY_TOOLKIT_BASE_URL}:signInWithPassword?key=AIzaSyCEvA_fVGNMRcS9F-Ubaaa0y0qBDUMlh90",
        headers={"User-Agent": "Music Assistant", "Origin": ARD_ACCOUNTS_URL},
        json={
            "returnSecureToken": True,
            "email": email,
            "password": password,
            "clientType": "CLIENT_TYPE_WEB",
        },
    )
    data = await response.json()
    if "error" in data:
        if data["error"]["message"] == "EMAIL_NOT_FOUND":
            raise LoginFailed("Email address is not registered")
        if data["error"]["message"] == "INVALID_PASSWORD":
            raise LoginFailed("Password is wrong")
    token = data["idToken"]
    uid = data["localId"]

    response = await session.post(
        f"{IDENTITY_TOOLKIT_BASE_URL}:lookup?key=AIzaSyCEvA_fVGNMRcS9F-Ubaaa0y0qBDUMlh90",
        headers={"User-Agent": "Music Assistant", "Origin": ARD_ACCOUNTS_URL},
        json={
            "idToken": token,
        },
    )
    data = await response.json()
    if "error" in data:
        if data["error"]["message"] == "EMAIL_NOT_FOUND":
            raise LoginFailed("Email address is not registered")
        if data["error"]["message"] == "INVALID_PASSWORD":
            raise LoginFailed("Password is wrong")

    return token, uid, data["users"][0]["displayName"]


def _create_aiohttptransport(headers: dict[str, str] | None = None) -> AIOHTTPTransport:
    return AIOHTTPTransport(url=ARD_AUDIOTHEK_GRAPHQL, headers=headers, ssl=True)


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
    if values is None:
        values = {}

    authenticated = True
    if values.get(CONF_TOKEN_BEARER) is None or values.get(CONF_USERID) is None:
        authenticated = False

    return (
        ConfigEntry(
            key="label_text",
            type=ConfigEntryType.LABEL,
            label=f"Successfully signed in as {values.get(CONF_USER_NAME)} {str(values.get(CONF_EMAIL, '')).replace('@', '(at)')}.",  # noqa: E501
            hidden=not authenticated,
        ),
        ConfigEntry(
            key=CONF_EMAIL,
            type=ConfigEntryType.STRING,
            label="E-Mail",
            required=False,
            description="E-Mail address of ARD account.",
            hidden=authenticated,
            value=values.get(CONF_EMAIL),
        ),
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Password",
            required=False,
            description="Password of ARD account.",
            hidden=authenticated,
            value=values.get(CONF_PASSWORD),
        ),
        ConfigEntry(
            key=CONF_MAX_BITRATE,
            type=ConfigEntryType.INTEGER,
            label="Maximum bitrate for streams (0 for unlimited)",
            required=False,
            description="Maximum bitrate for streams. Use 0 for unlimited",
            default_value=0,
            value=values.get(CONF_MAX_BITRATE),
        ),
        ConfigEntry(
            key=CONF_PODCAST_FINISHED,
            type=ConfigEntryType.INTEGER,
            label="Percentage reached until podcast episode is marked as finished",
            required=False,
            description="This setting defines the percentage of how much of a podcast "
            "has to be left unheard until an episode is marked as finished",
            default_value=95,
            value=values.get(CONF_PODCAST_FINISHED),
        ),
        ConfigEntry(
            key=CONF_TOKEN_BEARER,
            type=ConfigEntryType.SECURE_STRING,
            label="token",
            hidden=True,
            required=False,
            value=values.get(CONF_TOKEN_BEARER),
        ),
        ConfigEntry(
            key=CONF_USERID,
            type=ConfigEntryType.SECURE_STRING,
            label="uid",
            hidden=True,
            required=False,
            value=values.get(CONF_USERID),
        ),
        ConfigEntry(
            key=CONF_EXPIRY_TIME,
            type=ConfigEntryType.SECURE_STRING,
            label="token_expiry",
            hidden=True,
            required=False,
            default_value=0,
            value=values.get(CONF_EXPIRY_TIME),
        ),
        ConfigEntry(
            key=CONF_USER_NAME,
            type=ConfigEntryType.STRING,
            label="username",
            hidden=True,
            required=False,
            value=values.get(CONF_USER_NAME),
        ),
    )


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
            ProviderFeature.LIBRARY_RADIOS,
            ProviderFeature.LIBRARY_PODCASTS,
        }

    async def get_client(self) -> Client:
        """Wrap the client creation procedure to recreate client.

        This happens when the token is expired or user credentials are updated.
        """
        _email = self.config.get_value(CONF_EMAIL)
        _password = self.config.get_value(CONF_PASSWORD)
        self.token = self.config.get_value(CONF_TOKEN_BEARER)
        self.user_id = self.config.get_value(CONF_USERID)
        self.token_expire = datetime.fromtimestamp(
            float(str(self.config.get_value(CONF_EXPIRY_TIME)))
        )

        self.max_bitrate = int(float(str(self.config.get_value(CONF_MAX_BITRATE))))

        if (
            _email is not None
            and _password is not None
            and (self.token is None or self.user_id is None or self.token_expire < datetime.now())
        ):
            self.token, self.user_id, _username = await _login(
                self.mass.http_session, str(_email), str(_password)
            )
            self.update_config_value(CONF_TOKEN_BEARER, self.token, encrypted=True)
            self.update_config_value(CONF_USERID, self.user_id, encrypted=True)
            self.update_config_value(CONF_USER_NAME, _username)
            self.update_config_value(
                CONF_EXPIRY_TIME, str((datetime.now() + timedelta(hours=1)).timestamp())
            )
            self._client_initialized = False

        if not self._client_initialized:
            headers = None
            if self.token:
                headers = {"Authorization": f"Bearer {self.token}"}

            self._client = Client(
                transport=_create_aiohttptransport(headers),
                fetch_schema_from_transport=True,
            )
            self._client_initialized = True

        return self._client

    async def handle_async_init(self) -> None:
        """Pass config values to client and initialize."""
        self._client_initialized = False
        await self.get_client()

    async def _update_progress(self) -> None:
        if not self.user_id:
            return

        async with await self.get_client() as session:
            result = (
                await session.execute(get_history_query, variable_values={"loginId": self.user_id})
            )["allEndUsers"]["nodes"][0]["history"]["nodes"]

            new_progress = {}  # type: dict[str, tuple[bool, float]]
            time_limit = int(str(self.config.get_value(CONF_PODCAST_FINISHED)))
            for x in result:
                core_id = x["item"]["coreId"]
                if core_id is None:
                    continue
                duration = x["item"]["duration"]
                if duration is None:
                    continue
                progress = x["progress"]
                time_limit_reached = (progress / duration) * 100 > time_limit
                new_progress[core_id] = (time_limit_reached, progress)
            self.remote_progress = new_progress

    def _get_progress(self, episode_id: str) -> tuple[bool, int]:
        if episode_id in self.remote_progress:
            return self.remote_progress[episode_id][0], int(
                self.remote_progress[episode_id][1] * 1000
            )
        return False, 0

    async def get_resume_position(self, item_id: str, media_type: MediaType) -> tuple[bool, int]:
        """Return: finished, position_ms."""
        assert media_type == MediaType.PODCAST_EPISODE
        await self._update_progress()

        return self._get_progress(item_id)

    async def on_played(
        self,
        media_type: MediaType,
        prov_item_id: str,
        fully_played: bool,
        position: int,
        media_item: MediaItemType,
        is_playing: bool = False,
    ) -> None:
        """Update progress."""
        if not self.user_id:
            return
        if media_item is None or not isinstance(media_item, PodcastEpisode):
            return
        if media_type != MediaType.PODCAST_EPISODE:
            return
        async with await self.get_client() as session:
            await session.execute(
                update_history_entry,
                variable_values={"itemId": prov_item_id, "progress": position},
            )

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
        async with await self.get_client() as session:
            search_shows = (
                await session.execute(
                    search_shows_query, variable_values={"query": search_query, "limit": limit}
                )
            )["search"]["shows"]["nodes"]

        podcasts = []
        for element in search_shows:
            podcasts += [
                _parse_podcast(
                    self.domain,
                    self.lookup_key,
                    self.instance_id,
                    element,
                    element["coreId"],
                )
            ]
        async with await self.get_client() as session:
            search_radios = (
                await session.execute(
                    search_radios_query,
                    variable_values={
                        "filter": {"title": {"includes": search_query}},
                        "first": limit,
                    },
                )
            )["permanentLivestreams"]["nodes"]

        radios = []
        for element in search_radios:
            radios += [
                _parse_radio(
                    self.domain,
                    self.instance_id,
                    element,
                    element["coreId"],
                )
            ]

        return SearchResults(podcasts=podcasts, radio=radios)

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id."""
        # Get full details of a single Radio station.
        # Mandatory only if you reported LIBRARY_RADIOS in the supported_features.
        async with await self.get_client() as session:
            rad = (
                await session.execute(livestream_query, variable_values={"coreId": prov_radio_id})
            )["permanentLivestreamByCoreId"]

        return _parse_radio(
            self.domain,
            self.instance_id,
            rad,
            prov_radio_id,
        )

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast, None]:
        """Retrieve library/subscribed podcasts from the provider.

        Minified podcast information is enough.
        """
        if not self.user_id:
            return
        async with await self.get_client() as session:
            result = (
                await session.execute(
                    get_subscriptions_query, variable_values={"loginId": self.user_id}
                )
            )["allEndUsers"]["nodes"][0]["subscriptions"]["programSets"]["nodes"]
        for show in result:
            yield await self.get_podcast(show["subscribedProgramSet"]["coreId"])

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
        async with await self.get_client() as session:
            result = (
                await session.execute(show_query, variable_values={"showId": prov_podcast_id})
            )["show"]

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
        await self._update_progress()
        async with await self.get_client() as session:
            length = await session.execute(
                show_length_query, variable_values={"showId": prov_podcast_id}
            )
            length = length["show"]["items"]["totalCount"]
            step_size = 128
            for offset in range(0, length, step_size):
                result = (
                    await session.execute(
                        show_query,
                        variable_values={
                            "showId": prov_podcast_id,
                            "first": step_size,
                            "offset": offset,
                        },
                    )
                )["show"]
                for idx, episode in enumerate(result["items"]["nodes"]):
                    if len(episode["audioList"]) == 0:
                        continue
                    if episode["status"] == "DEPUBLISHED":
                        continue
                    episode_id = episode["coreId"]

                    progress = self._get_progress(episode_id)
                    yield _parse_podcast_episode(
                        self.domain,
                        self.lookup_key,
                        self.instance_id,
                        episode,
                        episode_id,
                        idx,
                        progress,
                    )

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get single podcast episode."""
        await self._update_progress()
        async with await self.get_client() as session:
            result = (
                await session.execute(
                    show_episode_query, variable_values={"coreId": prov_episode_id}
                )
            )["itemByCoreId"]
        if result is None:
            raise MediaNotFoundError("Episode not found")
        progress = self._get_progress(prov_episode_id)
        return _parse_podcast_episode(
            self.domain,
            self.lookup_key,
            self.instance_id,
            result,
            result["showId"],
            result["rowId"],
            progress,
        )

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a radio station."""
        async with await self.get_client() as session:
            if media_type == MediaType.RADIO:
                result = (
                    await session.execute(livestream_query, variable_values={"coreId": item_id})
                )["permanentLivestreamByCoreId"]
                seek = False
            elif media_type == MediaType.PODCAST_EPISODE:
                result = (
                    await session.execute(show_episode_query, variable_values={"coreId": item_id})
                )["itemByCoreId"]
                seek = True

        streams = result["audioList"]

        def filter_func(val: dict[str, Any]) -> bool:
            if self.max_bitrate == 0:
                return True
            return int(val["audioBitrate"]) < self.max_bitrate

        filtered_streams = filter(filter_func, streams)
        selected_stream = max(filtered_streams, key=lambda x: x["audioBitrate"])

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

    @use_cache(3600)
    async def get_organizations(self, path: str) -> list[BrowseFolder]:
        """Create a list of all available organizations."""
        async with await self.get_client() as session:
            result = (await session.execute(organizations_query))["organizations"]["nodes"]
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
        async with await self.get_client() as session:
            result = (
                await session.execute(
                    publication_services_query, variable_values={"coreId": core_id}
                )
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
        async with await self.get_client() as session:
            result = (
                await session.execute(publications_list_query, variable_values={"coreId": core_id})
            )["publicationServiceByCoreId"]

        publications = []  # type: list[Radio | Podcast]

        for rad in result["permanentLivestreams"]["nodes"]:
            if not rad["coreId"]:
                continue

            radio = _parse_radio(self.domain, self.instance_id, rad, rad["coreId"])

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


def _parse_social_media(
    homepage_url: str | None, social_media_accounts: list[dict[str, None | str]]
) -> set[MediaItemLink]:
    return_set = set()
    if homepage_url:
        return_set.add(MediaItemLink(type=LinkType.WEBSITE, url=homepage_url))
    for entry in social_media_accounts:
        if entry["url"]:
            link_type = None
            match entry["service"]:
                case "FACEBOOK":
                    link_type = LinkType.FACEBOOK
                case "INSTAGRAM":
                    link_type = LinkType.INSTAGRAM
                case "TIKTOK":
                    link_type = LinkType.TIKTOK
            if link_type:
                return_set.add(MediaItemLink(type=link_type, url=entry["url"]))
    return return_set


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
                item_id=podcast_id,
                provider_domain=domain,
                provider_instance=instance_id,
            )
        },
        total_episodes=podcast_query["items"]["totalCount"],
    )

    podcast.metadata.links = _parse_social_media(
        podcast_query["publicationService"]["homepageUrl"],
        podcast_query["publicationService"]["socialMediaAccounts"],
    )

    podcast.metadata.description = podcast_query["synopsis"]
    podcast.metadata.genres = {r["title"] for r in podcast_query["editorialCategoriesList"]}

    podcast.metadata.add_image(create_media_image(domain, podcast_query["imagesList"]))

    return podcast


def _parse_radio(
    domain: str,
    instance_id: str,
    radio_query: dict[str, Any],
    radio_id: str,
) -> Radio:
    radio = Radio(
        name=radio_query["title"],
        item_id=radio_id,
        provider=domain,
        provider_mappings={
            ProviderMapping(
                item_id=radio_id,
                provider_domain=domain,
                provider_instance=instance_id,
            )
        },
    )

    radio.metadata.links = _parse_social_media(
        radio_query["publicationService"]["homepageUrl"],
        radio_query["publicationService"]["socialMediaAccounts"],
    )

    radio.metadata.description = radio_query["publicationService"]["synopsis"]
    radio.metadata.genres = {radio_query["publicationService"]["genre"]}

    radio.metadata.add_image(create_media_image(domain, radio_query["imagesList"]))

    return radio


def _parse_podcast_episode(
    domain: str,
    lookup_key: str,
    instance_id: str,
    episode: dict[str, Any],
    podcast_id: str,
    idx: int,
    progress: tuple[bool, int],
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
        fully_played=progress[0],
        resume_position_ms=progress[1],
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
