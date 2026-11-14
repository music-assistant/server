"""ARD Audiotek Music Provider for Music Assistant."""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator, Sequence
from datetime import datetime
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

from music_assistant.constants import CONF_PASSWORD, DEFAULT_AUDIOBOOK_PODCAST_GENRE
from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.datetime import from_utc_timestamp, utc
from music_assistant.helpers.podcast_parsers import rank_episodes_by_date
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.ard_audiothek.auth import (
    PlayHistoryEntry,
    add_subscription,
    get_play_history,
    get_subscribed_podcast_ids,
    refresh_tokens,
    remove_play_history_entry,
    remove_subscription,
    set_play_history_entry,
)
from music_assistant.providers.ard_audiothek.database_queries import (
    episode_asset_id_query,
    livestream_query,
    organizations_query,
    publication_services_query,
    publications_list_query,
    search_radios_query,
    search_shows_query,
    show_episode_query,
    show_length_query,
    show_query,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

# Config for login
CONF_EMAIL = "email"
CONF_TOKEN_BEARER = "token"  # noqa: S105 - config key
CONF_EXPIRY_TIME = "token_expiry"
CONF_USERID = "user_id"
CONF_DISPLAY_NAME = "display_name"
CONF_REFRESH_TOKEN = "refresh_token"  # noqa: S105 - config key

# Constants for config actions
CONF_ACTION_AUTH = "authenticate"
CONF_ACTION_CLEAR_AUTH = "clear_auth"

# General config
CONF_MAX_BITRATE = "max_num_episodes"
CONF_PODCAST_FINISHED = "podcast_finished_time"

ARD_AUDIOTHEK_GRAPHQL = "https://api.ardaudiothek.de/graphql"
PROGRESS_CACHE_SECONDS = 60
MIN_HISTORY_PLAYTIME = 30

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.LIBRARY_PODCASTS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    features = set(SUPPORTED_FEATURES)
    # subscriptions can only be changed in the ARD account when one is linked
    if mass.config.get_provider_setup_value(config.instance_id, CONF_REFRESH_TOKEN):
        features.add(ProviderFeature.LIBRARY_PODCASTS_EDIT)
    return ARDAudiothek(mass, manifest, config, features)


def _create_aiohttptransport(headers: dict[str, str] | None = None) -> AIOHTTPTransport:
    return AIOHTTPTransport(url=ARD_AUDIOTHEK_GRAPHQL, headers=headers, ssl=True)


class ARDAudiothek(MusicProvider):
    """ARD Audiothek Music provider."""

    @property
    def max_concurrent_streams(self) -> None:
        """Allow unlimited concurrent upstream source streams."""
        return None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """
        Return the configuration (options) entries for the ARD Audiothek provider.

        Credentials and the token/user-id/expiry stash are collected/persisted by the
        setup flow; the options surface only shows who is signed in plus the tunables.
        """
        display_name = str(self.get_setup_value(CONF_DISPLAY_NAME) or "")
        email = str(self.get_setup_value(CONF_EMAIL) or "")
        return (
            ConfigEntry(
                key="label_text",
                type=ConfigEntryType.LABEL,
                translation_params=[display_name, email.replace("@", "(at)")],
                hidden=not display_name,
            ),
            ConfigEntry(
                key=CONF_MAX_BITRATE,
                type=ConfigEntryType.INTEGER,
                required=False,
                default_value=0,
            ),
            ConfigEntry(
                key=CONF_PODCAST_FINISHED,
                type=ConfigEntryType.INTEGER,
                required=False,
                default_value=95,
            ),
        )

    async def get_client(self) -> Client:
        """
        Wrap the client creation procedure to recreate client.

        This happens when the token is expired or user credentials are updated.
        """
        _refresh_token = self.get_setup_value(CONF_REFRESH_TOKEN)
        self.token = self.get_setup_value(CONF_TOKEN_BEARER)
        self.user_id = self.get_setup_value(CONF_USERID)
        self.token_expire = from_utc_timestamp(
            float(str(self.get_setup_value(CONF_EXPIRY_TIME) or 0))
        )

        self.max_bitrate = int(float(str(self.config.get_value(CONF_MAX_BITRATE))))

        if not _refresh_token and self.get_setup_value(CONF_PASSWORD):
            # set up with email and password, which ARD no longer accepts outside its login page
            msg = "ARD no longer accepts password logins, please sign in to your ARD account again"
            self.logger.warning(msg)
            raise LoginFailed(msg)

        if _refresh_token and (
            self.token is None or self.user_id is None or self.token_expire < utc()
        ):
            try:
                tokens = await refresh_tokens(self.mass.http_session, str(_refresh_token))
            except LoginFailed as err:
                self.logger.warning("ARD account login failed: %s", err)
                raise
            self.token = tokens.id_token
            self.user_id = tokens.user_id
            self.token_expire = from_utc_timestamp(tokens.expires_at - 60)
            self.logger.debug(
                "Refreshed the ARD login, valid until %s",
                self.token_expire.astimezone().isoformat(timespec="seconds"),
            )
            self._update_setup_data(CONF_REFRESH_TOKEN, tokens.refresh_token)
            self._update_setup_data(CONF_TOKEN_BEARER, tokens.id_token)
            self._update_setup_data(CONF_USERID, tokens.user_id)
            self._update_setup_data(CONF_DISPLAY_NAME, tokens.display_name)
            self._update_setup_data(CONF_EXPIRY_TIME, str(tokens.expires_at - 60))
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
        self.remote_progress: dict[str, PlayHistoryEntry] = {}
        # the play history is keyed by an episode's asset id, not by the core id we use
        self._asset_ids: dict[str, str] = {}
        self._progress_fetched_at: float = -PROGRESS_CACHE_SECONDS
        await self.get_client()

    async def get_resume_position(
        self, item_id: str, media_type: MediaType
    ) -> tuple[bool, int, datetime | None]:
        """Return: finished, position_ms."""
        assert media_type == MediaType.PODCAST_EPISODE
        # called right before playback, so it must see progress made elsewhere just now
        await self._update_progress(force=True)
        if self.remote_progress:
            await self._get_asset_id(item_id)
        entry = self._get_history_entry(item_id)
        return *self._get_progress(item_id), entry.updated_on if entry else None

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
        time_limit = int(str(self.config.get_value(CONF_PODCAST_FINISHED)))
        duration = media_item.duration or 0
        completed = fully_played or (duration > 0 and position / duration * 100 >= time_limit)
        # Music Assistant reports an episode marked as unplayed with position 0
        mark_unplayed = position == 0 and not completed and not is_playing
        # like the ARD Sounds apps, only episodes listened to for a while enter the history
        if not mark_unplayed and position < MIN_HISTORY_PLAYTIME and not completed:
            self.logger.debug("Not storing progress of %s, played only %ss", prov_item_id, position)
            return
        await self.get_client()
        if not self.token:
            return
        if not (asset_id := await self._get_asset_id(prov_item_id)):
            self.logger.debug("Not storing progress of %s, ARD knows no asset id", prov_item_id)
            return
        if mark_unplayed:
            await remove_play_history_entry(
                self.mass.http_session, str(self.token), str(self.user_id), asset_id
            )
            self.remote_progress.pop(asset_id, None)
            self.logger.debug("Removed %s (%s) from the ARD play history", prov_item_id, asset_id)
            return
        await set_play_history_entry(
            self.mass.http_session,
            str(self.token),
            str(self.user_id),
            asset_id,
            position,
            completed,
        )
        self.logger.debug(
            "Stored progress of %s (%s) in the ARD play history: %ss, completed: %s",
            prov_item_id,
            asset_id,
            position,
            completed,
        )
        self.remote_progress[asset_id] = PlayHistoryEntry(completed, float(position), utc())

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
        """
        Perform search on musicprovider.

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

    async def library_add(self, item: MediaItemType) -> bool:
        """Subscribe to a podcast in the ARD account."""
        if item.media_type != MediaType.PODCAST:
            return await super().library_add(item)
        await self.get_client()
        if not self.user_id or not self.token:
            return False
        await add_subscription(
            self.mass.http_session, str(self.token), str(self.user_id), item.item_id
        )
        return True

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Unsubscribe from a podcast in the ARD account."""
        if media_type != MediaType.PODCAST:
            return await super().library_remove(prov_item_id, media_type)
        await self.get_client()
        if not self.user_id or not self.token:
            return False
        await remove_subscription(
            self.mass.http_session, str(self.token), str(self.user_id), prov_item_id
        )
        return True

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast]:
        """
        Retrieve library/subscribed podcasts from the provider.

        Minified podcast information is enough.
        """
        await self.get_client()
        if not self.user_id or not self.token:
            return
        podcast_ids = await get_subscribed_podcast_ids(
            self.mass.http_session, str(self.token), str(self.user_id)
        )
        self.logger.debug(
            "ARD account has %s subscribed podcasts: %s", len(podcast_ids), podcast_ids
        )
        for podcast_id in podcast_ids:
            try:
                podcast = await self.get_podcast(podcast_id)
            except Exception as err:
                # one podcast ARD cannot serve must not end the sync of the others
                self.logger.warning("Skipping subscribed podcast %s: %r", podcast_id, err)
                self.report_skipped_sync_item(MediaType.PODCAST, podcast_id, err)
                continue
            yield podcast

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse through the ARD Audiothek.

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

    async def get_podcast_episodes(self, prov_podcast_id: str) -> AsyncGenerator[PodcastEpisode]:
        """Get podcast episodes."""
        await self._update_progress()
        depublished_filter = {"isPublished": {"equalTo": True}}
        show_title = ""
        episodes: list[dict[str, Any]] = []
        async with await self.get_client() as session:
            show_length_query.variable_values = {
                "showId": prov_podcast_id,
                "filter": depublished_filter,
            }
            length = await session.execute(show_length_query)
            length = length["show"]["items"]["totalCount"]
            # ranking by date needs every page in before anything can be yielded, so pull
            # them in large chunks. A long running archive costs 6 requests here rather than 21
            step_size = 512
            for offset in range(0, length, step_size):
                show_query.variable_values = {
                    "showId": prov_podcast_id,
                    "first": step_size,
                    "offset": offset,
                    "filter": depublished_filter,
                }
                result = (await session.execute(show_query))["show"]
                show_title = result["title"]
                episodes += [
                    episode
                    for episode in result["items"]["nodes"]
                    if episode["audioList"] and episode["status"] != "DEPUBLISHED"
                ]
        # the API lists the episodes newest first, so every page has to be in before they can
        # be ranked oldest to newest
        positions = rank_episodes_by_date([_publish_date(episode) for episode in episodes])
        for position, episode in zip(positions, episodes, strict=True):
            episode_id = episode["coreId"]
            if episode.get("assetId"):
                self._asset_ids[episode_id] = episode["assetId"]
            yield _parse_podcast_episode(
                self.domain,
                self.instance_id,
                episode,
                prov_podcast_id,
                show_title,
                position,
                self._get_progress(episode_id),
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
        if result.get("assetId"):
            self._asset_ids[prov_episode_id] = result["assetId"]
        progress = self._get_progress(prov_episode_id)
        return _parse_podcast_episode(
            self.domain,
            self.instance_id,
            result,
            result["showId"],
            result["show"]["title"],
            0,
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
            else:
                raise UnplayableMediaError(f"Unsupported media type: {media_type}")

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

    async def _update_progress(self, force: bool = False) -> None:
        await self.get_client()
        if not self.user_id or not self.token:
            self.remote_progress = {}
            return
        # listing episodes asks for the progress many times in a row
        if not force and time.monotonic() - self._progress_fetched_at < PROGRESS_CACHE_SECONDS:
            return
        self.remote_progress = await get_play_history(
            self.mass.http_session, str(self.token), str(self.user_id)
        )
        self._progress_fetched_at = time.monotonic()

    def _get_progress(self, episode_id: str) -> tuple[bool, int]:
        if entry := self._get_history_entry(episode_id):
            return entry.completed, int(entry.playtime * 1000)
        return False, 0

    def _get_history_entry(self, episode_id: str) -> PlayHistoryEntry | None:
        asset_id = self._asset_ids.get(episode_id)
        return self.remote_progress.get(asset_id) if asset_id else None

    async def _get_asset_id(self, episode_id: str) -> str | None:
        if asset_id := self._asset_ids.get(episode_id):
            return asset_id
        async with await self.get_client() as session:
            episode_asset_id_query.variable_values = {"coreId": episode_id}
            result = (await session.execute(episode_asset_id_query))["itemByCoreId"]
        if not result or not result.get("assetId"):
            return None
        self._asset_ids[episode_id] = str(result["assetId"])
        return self._asset_ids[episode_id]


def _parse_social_media(
    homepage_url: str | None, social_media_accounts: list[dict[str, str | None]] | None
) -> set[MediaItemLink]:
    return_set = set()
    if homepage_url:
        return_set.add(MediaItemLink(type=LinkType.WEBSITE, url=homepage_url))
    for entry in social_media_accounts or []:
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
    podcast.metadata.genres = {r["title"] for r in podcast_query["editorialCategoriesList"]} or {
        DEFAULT_AUDIOBOOK_PODCAST_GENRE
    }

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


def _publish_date(episode: dict[str, Any]) -> datetime | None:
    """Return the publication date of an episode, or None when it has none we can read."""
    if not (raw := episode.get("publishDate")):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _parse_podcast_episode(
    domain: str,
    instance_id: str,
    episode: dict[str, Any],
    podcast_id: str,
    podcast_title: str,
    position: int,
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
        position=position,
        # no episode_number: ARD's episodeNumber is the part in a "(2/5)" series, not the show's
        fully_played=progress[0],
        resume_position_ms=progress[1],
    )

    podcast_episode.metadata.add_image(create_media_image(domain, episode["imagesList"]))
    podcast_episode.metadata.description = episode["summary"]
    podcast_episode.metadata.release_date = _publish_date(episode)
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
