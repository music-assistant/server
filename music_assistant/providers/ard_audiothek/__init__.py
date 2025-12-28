"""ARD Audiotek Music Provider for Music Assistant."""

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
from music_assistant_models.errors import LoginFailed, MediaNotFoundError, UnplayableMediaError
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

from music_assistant.constants import CONF_PASSWORD
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
CONF_EMAIL = "email"
CONF_TOKEN_BEARER = "token"
CONF_EXPIRY_TIME = "token_expiry"
CONF_USERID = "user_id"
CONF_DISPLAY_NAME = "display_name"

# Constants for config actions
CONF_ACTION_AUTH = "authenticate"
CONF_ACTION_CLEAR_AUTH = "clear_auth"

# General config
CONF_MAX_BITRATE = "max_num_episodes"
CONF_PODCAST_FINISHED = "podcast_finished_time"

IDENTITY_TOOLKIT_BASE_URL = "https://identitytoolkit.googleapis.com/v1/accounts"
IDENTITY_TOOLKIT_TOKEN = "AIzaSyCEvA_fVGNMRcS9F-Ubaaa0y0qBDUMlh90"
ARD_ACCOUNTS_URL = "https://accounts.ard.de"
ARD_AUDIOTHEK_GRAPHQL = "https://api.ardaudiothek.de/graphql"

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.LIBRARY_RADIOS,
    ProviderFeature.LIBRARY_PODCASTS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return ARDAudiothek(mass, manifest, config, SUPPORTED_FEATURES)


async def _login(session: ClientSession, email: str, password: str) -> tuple[str, str, str]:
    response = await session.post(
        f"{IDENTITY_TOOLKIT_BASE_URL}:signInWithPassword?key={IDENTITY_TOOLKIT_TOKEN}",
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
        f"{IDENTITY_TOOLKIT_BASE_URL}:lookup?key={IDENTITY_TOOLKIT_TOKEN}",
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
    if values is None:
        values = {}

    authenticated = True
    if values.get(CONF_TOKEN_BEARER) is None or values.get(CONF_USERID) is None:
        authenticated = False

    return (
        ConfigEntry(
            key="label_text",
            type=ConfigEntryType.LABEL,
            label=f"Successfully signed in as {values.get(CONF_DISPLAY_NAME)} {str(values.get(CONF_EMAIL, '')).replace('@', '(at)')}.",  # noqa: E501
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
            label="Percentage required before podcast episode is marked as fully played",
            required=False,
            description="This setting defines how much of a podcast must be listened to before an "
            "episode is marked as fully played",
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
            key=CONF_DISPLAY_NAME,
            type=ConfigEntryType.STRING,
            label="username",
            hidden=True,
            required=False,
            value=values.get(CONF_DISPLAY_NAME),
        ),
    )


class ARDAudiothek(MusicProvider):
    """ARD Audiothek Music provider."""

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
            self.token, self.user_id, _display_name = await _login(
                self.mass.http_session, str(_email), str(_password)
            )
            self.update_config_value(CONF_TOKEN_BEARER, self.token, encrypted=True)
            self.update_config_value(CONF_USERID, self.user_id, encrypted=True)
            self.update_config_value(CONF_DISPLAY_NAME, _display_name)
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
            self.remote_progress = {}
            return

        async with await self.get_client() as session:
            get_history_query.variable_values = {"loginId": self.user_id}
            result = (await session.execute(get_history_query))["allEndUsers"]["nodes"][0][
                "history"
            ]["nodes"]

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
            update_history_entry.variable_values = {"itemId": prov_item_id, "progress": position}
            await session.execute(
                update_history_entry,
            )

    @property
    def is_streaming_provider(self) -> bool:
        """Search and lookup always search remote."""
        return True

    @use_cache(3600 * 24 * 7)  # cache for 7 days
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
        podcasts = []
        radios = []

        if MediaType.PODCAST in media_types:
            async with await self.get_client() as session:
                search_shows_query.variable_values = {"query": search_query, "limit": limit}
                search_shows = (await session.execute(search_shows_query))["search"]["shows"][
                    "nodes"
                ]

            for element in search_shows:
                podcasts += [
                    _parse_podcast(
                        self.domain,
                        self.instance_id,
                        element,
                        element["coreId"],
                    )
                ]

        if MediaType.RADIO in media_types:
            async with await self.get_client() as session:
                search_radios_query.variable_values = {
                    "filter": {"title": {"includesInsensitive": search_query}},
                    "first": limit,
                }
                search_radios = (await session.execute(search_radios_query))[
                    "permanentLivestreams"
                ]["nodes"]

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

    @use_cache(3600 * 24 * 7)  # cache for 7 days
    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id."""
        # Get full details of a single Radio station.
        # Mandatory only if you reported LIBRARY_RADIOS in the supported_features.
        async with await self.get_client() as session:
            livestream_query.variable_values = {"coreId": prov_radio_id}
            rad = (await session.execute(livestream_query))["permanentLivestreamByCoreId"]
        if not rad:
            raise MediaNotFoundError("Radio not found.")
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
            get_subscriptions_query.variable_values = {"loginId": self.user_id}
            result = (await session.execute(get_subscriptions_query))["allEndUsers"]["nodes"][0][
                "subscriptions"
            ]["programSets"]["nodes"]
        for show in result:
            yield await self.get_podcast(show["subscribedProgramSet"]["coreId"])

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse through the ARD Audiothek.

        This supports browsing through Podcasts and Radio stations.
        :param path: The path to browse, (e.g. provider_id://artists).
        """
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

    @use_cache(3600 * 24 * 7)  # cache for 7 days
    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get podcast."""
        async with await self.get_client() as session:
            show_query.variable_values = {"showId": prov_podcast_id}
            result = (await session.execute(show_query))["show"]
            if not result:
                raise MediaNotFoundError("Podcast not found.")

        return _parse_podcast(
            self.domain,
            self.instance_id,
            result,
            prov_podcast_id,
        )

    async def get_podcast_episodes(
        self, prov_podcast_id: str
    ) -> AsyncGenerator[PodcastEpisode, None]:
        """Get podcast episodes."""
        await self._update_progress()
        depublished_filter = {"isPublished": {"equalTo": True}}
        async with await self.get_client() as session:
            show_length_query.variable_values = {
                "showId": prov_podcast_id,
                "filter": depublished_filter,
            }
            length = await session.execute(show_length_query)
            length = length["show"]["items"]["totalCount"]
            step_size = 128
            for offset in range(0, length, step_size):
                show_query.variable_values = {
                    "showId": prov_podcast_id,
                    "first": step_size,
                    "offset": offset,
                    "filter": depublished_filter,
                }
                result = (await session.execute(show_query))["show"]
                for idx, episode in enumerate(result["items"]["nodes"]):
                    if len(episode["audioList"]) == 0:
                        continue
                    if episode["status"] == "DEPUBLISHED":
                        continue
                    episode_id = episode["coreId"]

                    progress = self._get_progress(episode_id)
                    yield _parse_podcast_episode(
                        self.domain,
                        self.instance_id,
                        episode,
                        episode_id,
                        result["title"],
                        offset + idx,
                        progress,
                    )

    @use_cache(3600 * 24)  # cache for 24 hours
    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get single podcast episode."""
        await self._update_progress()
        async with await self.get_client() as session:
            show_episode_query.variable_values = {"coreId": prov_episode_id}
            result = (await session.execute(show_episode_query))["itemByCoreId"]
        if not result:
            raise MediaNotFoundError("Podcast episode not found")
        progress = self._get_progress(prov_episode_id)
        return _parse_podcast_episode(
            self.domain,
            self.instance_id,
            result,
            result["showId"],
            result["show"]["title"],
            result["rowId"],
            progress,
        )

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a radio station."""
        async with await self.get_client() as session:
            if media_type == MediaType.RADIO:
                livestream_query.variable_values = {"coreId": item_id}
                result = (await session.execute(livestream_query))["permanentLivestreamByCoreId"]
                seek = False
            elif media_type == MediaType.PODCAST_EPISODE:
                show_episode_query.variable_values = {"coreId": item_id}
                result = (await session.execute(show_episode_query))["itemByCoreId"]
                seek = True

        streams = result["audioList"]
        if len(streams) == 0:
            raise MediaNotFoundError("No stream available.")

        def filter_func(val: dict[str, Any]) -> bool:
            if self.max_bitrate == 0:
                return True
            return int(val["audioBitrate"]) < self.max_bitrate

        filtered_streams = list(filter(filter_func, streams))
        if len(filtered_streams) == 0:
            raise UnplayableMediaError("No stream exceeding the minimum bitrate available.")
        selected_stream = max(filtered_streams, key=lambda x: x["audioBitrate"])

        return StreamDetails(
            provider=self.domain,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.try_parse(selected_stream["audioCodec"]),
            ),
            media_type=media_type,
            stream_type=StreamType.HTTP,
            path=fix_url(selected_stream["href"]),
            can_seek=seek,
            allow_seek=seek,
        )

    @use_cache(3600 * 24 * 7)  # cache for 7 days
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

    @use_cache(3600 * 24 * 7)  # cache for 7 days
    async def get_publication_services(self, path: str, core_id: str) -> list[BrowseFolder]:
        """Create a list of publications for a given organization."""
        async with await self.get_client() as session:
            publication_services_query.variable_values = {"coreId": core_id}
            result = (await session.execute(publication_services_query))["organizationByCoreId"][
                "publicationServicesByOrganizationName"
            ]["nodes"]
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

    @use_cache(3600 * 24 * 7)  # cache for 7 days
    async def get_publications_list(self, core_id: str) -> list[Radio | Podcast]:
        """Create list of available radio stations and shows for a publication service."""
        async with await self.get_client() as session:
            publications_list_query.variable_values = {"coreId": core_id}
            result = (await session.execute(publications_list_query))["publicationServiceByCoreId"]

        publications = []  # type: list[Radio | Podcast]

        if not result:
            raise MediaNotFoundError("Publication service not found.")

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
    instance_id: str,
    podcast_query: dict[str, Any],
    podcast_id: str,
) -> Podcast:
    podcast = Podcast(
        name=podcast_query["title"],
        item_id=podcast_id,
        publisher=podcast_query["publicationService"]["title"],
        provider=instance_id,
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
    instance_id: str,
    episode: dict[str, Any],
    podcast_id: str,
    podcast_title: str,
    idx: int,
    progress: tuple[bool, int],
) -> PodcastEpisode:
    podcast_episode = PodcastEpisode(
        name=episode["title"],
        duration=episode["duration"],
        item_id=episode["coreId"],
        provider=instance_id,
        podcast=ItemMapping(
            item_id=podcast_id,
            provider=instance_id,
            name=podcast_title,
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
