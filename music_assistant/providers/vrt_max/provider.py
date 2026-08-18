"""VRT MAX music provider implementation."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncGenerator, Sequence
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import MediaNotFoundError, UnplayableMediaError
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemChapter,
    MediaItemImage,
    MediaItemType,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    Radio,
    SearchResults,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import (
    CONF_ENTRY_UNOFFICIAL_PROVIDER,
    CONF_PASSWORD,
    CONF_USERNAME,
)
from music_assistant.controllers.cache import use_cache
from music_assistant.models.music_provider import MusicProvider

from .constants import (
    BROWSE_PODCASTS,
    BROWSE_RADIO_PROGRAMS,
    BROWSE_RADIOS,
    MAX_TRACKLIST_EPISODES,
    PODCAST_ROW_TYPE,
    RADIO_ROW_TYPE,
)
from .helpers import (
    PODCAST_LANDING_PAGE,
    RADIO_LANDING_PAGE,
    STATIONS,
    STATIONS_BY_ID,
    VrtApiError,
    VrtAuthError,
    VrtEpisode,
    VrtMaxAuth,
    VrtMaxClient,
    VrtNotFoundError,
    VrtProgram,
    VrtProgramTile,
    VrtStation,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.LIBRARY_PODCASTS,
    ProviderFeature.LIBRARY_PODCASTS_EDIT,
}


class VrtMaxProvider(MusicProvider):
    """VRT MAX provider."""

    _client: VrtMaxClient
    _auth: VrtMaxAuth

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize the VRT MAX provider."""
        super().__init__(mass, manifest, config, SUPPORTED_FEATURES)

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return provider configuration entries (optional VRT account login)."""
        return (
            CONF_ENTRY_UNOFFICIAL_PROVIDER,
            ConfigEntry(key=CONF_USERNAME, type=ConfigEntryType.STRING, required=False),
            ConfigEntry(key=CONF_PASSWORD, type=ConfigEntryType.SECURE_STRING, required=False),
        )

    @property
    def is_streaming_provider(self) -> bool:
        """Return True for streaming providers."""
        return True

    async def handle_async_init(self) -> None:
        """Initialize the VRT MAX GraphQL client and (optional) auth manager."""
        self._client = VrtMaxClient(self.mass.http_session, self.logger)
        username = str(self.config.get_value(CONF_USERNAME) or "")
        password = str(self.config.get_value(CONF_PASSWORD) or "")
        self._auth = VrtMaxAuth(self.mass.http_session, self.logger, username, password)

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse this provider's live radio, radio programme archives and podcasts.

        :param path: The path to browse (e.g. provider_id://radio).
        """
        subpath = path.split("://", 1)[1] if "://" in path else ""

        if subpath == BROWSE_RADIOS:
            return [self._radio_item(station) for station in STATIONS]
        if subpath == BROWSE_RADIO_PROGRAMS:
            return await self._browse_landing(
                RADIO_LANDING_PAGE, RADIO_ROW_TYPE, BROWSE_RADIO_PROGRAMS
            )
        if subpath == BROWSE_PODCASTS:
            return await self._browse_landing(
                PODCAST_LANDING_PAGE, PODCAST_ROW_TYPE, BROWSE_PODCASTS
            )
        if subpath.startswith((f"{BROWSE_RADIO_PROGRAMS}/", f"{BROWSE_PODCASTS}/")):
            _, encoded = subpath.split("/", 1)
            return await self._browse_programs(_decode(encoded))

        return self._browse_root()

    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """Search live radio stations, podcasts and radio programme archives."""
        results = SearchResults()
        query = search_query.strip()
        if not query:
            return results

        if MediaType.RADIO in media_types:
            needle = query.lower()
            results.radio = [
                self._radio_item(station) for station in STATIONS if needle in station.name.lower()
            ][:limit]

        if MediaType.PODCAST in media_types:
            podcasts: list[Podcast] = []
            seen: set[str] = set()
            for tile in await self._client.search_podcast_programs(query, limit):
                if tile.page_id not in seen:
                    seen.add(tile.page_id)
                    podcasts.append(self._podcast_from_tile(tile))
            # Radio archives are only searchable as episodes; fold them up to their
            # parent programme so search returns the show, not individual broadcasts.
            for episode in await self._client.search_radio_episodes(query, limit):
                program_id = _program_id_from_episode(episode.page_id)
                if program_id not in seen:
                    seen.add(program_id)
                    podcasts.append(self._podcast_base(program_id, episode.title))
            results.podcasts = podcasts[:limit]

        return results

    @use_cache(3600 * 24)
    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get a single radio station by id."""
        station = STATIONS_BY_ID.get(prov_radio_id)
        if station is None:
            raise MediaNotFoundError("Radio not found.")
        return self._radio_item(station)

    @use_cache(3600 * 6)
    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get a single podcast / radio programme archive by page id."""
        # Only a genuine "not found" maps to MediaNotFoundError; a transient
        # VrtApiError propagates so the library sync aborts instead of pruning.
        try:
            program = await self._client.get_program(prov_podcast_id)
        except VrtNotFoundError as err:
            raise MediaNotFoundError(f"Podcast not found: {prov_podcast_id}") from err
        return self._podcast_from_program(program)

    async def get_podcast_episodes(self, prov_podcast_id: str) -> AsyncGenerator[PodcastEpisode]:
        """Get all listen-back episodes of a podcast / radio programme archive."""
        try:
            program = await self._client.get_program(prov_podcast_id)
        except VrtApiError as err:
            raise MediaNotFoundError(f"Podcast not found: {prov_podcast_id}") from err
        podcast_mapping = ItemMapping(
            media_type=MediaType.PODCAST,
            item_id=prov_podcast_id,
            provider=self.instance_id,
            name=program.title,
        )
        # With credentials, fetch the per-user played/resume progress alongside the list.
        access_token: str | None = None
        if self._auth.enabled:
            try:
                access_token = await self._auth.get_access_token()
            except VrtAuthError as err:
                self.logger.debug("No access token for episode progress: %s", err)
        episodes: list[PodcastEpisode] = []
        for season in program.seasons:
            try:
                async for episode in self._client.iter_season_episodes(
                    season.component_id, access_token
                ):
                    episodes.append(self._episode_item(episode, podcast_mapping, len(episodes) + 1))
            except VrtApiError as err:
                # A transient failure mid-pagination shouldn't drop the whole list.
                self.logger.warning("Stopped listing episodes for %s: %s", prov_podcast_id, err)
                break
        # MA plays the episode object from this list directly (there is no episode-detail
        # fetch on the play path), so a radio episode's played-songs tracklist must be
        # attached here rather than only in get_podcast_episode.
        await self._attach_tracklists(episodes)
        for item in episodes:
            yield item

    @use_cache(3600 * 6)
    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get a single podcast / radio programme episode by page id."""
        try:
            episode = await self._client.get_episode(prov_episode_id)
        except VrtApiError as err:
            raise MediaNotFoundError(f"Episode not found: {prov_episode_id}") from err
        podcast_id = _program_id_from_episode(prov_episode_id)
        podcast_mapping = ItemMapping(
            media_type=MediaType.PODCAST,
            item_id=podcast_id,
            provider=self.instance_id,
            name=episode.title,
        )
        item = self._episode_item(episode, podcast_mapping, 0)
        chapters = await self._episode_chapters(prov_episode_id)
        if chapters:
            item.metadata.chapters = chapters
        return item

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast]:
        """Sync the user's 'Mijn lijst' favourites (podcasts + radio archives) to the library."""
        if not self._auth.enabled:
            return
        try:
            access_token = await self._auth.get_access_token()
        except VrtAuthError as err:
            self.logger.warning("Cannot sync VRT favourites: %s", err)
            return
        async for page_id in self._client.iter_favourite_ids(access_token):
            try:
                yield await self.get_podcast(page_id)
            except MediaNotFoundError:
                self.logger.debug("Skipping unresolvable favourite: %s", page_id)

    async def library_add(self, item: MediaItemType) -> bool:
        """Add a podcast to the user's 'Mijn lijst' (mirrors an MA library add)."""
        if item.media_type != MediaType.PODCAST:
            return True
        return await self._set_favourite(item.item_id, favourite=True)

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove a podcast from the user's 'Mijn lijst' (mirrors an MA library remove)."""
        if media_type != MediaType.PODCAST:
            return True
        return await self._set_favourite(prov_item_id, favourite=False)

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Resolve a playable stream."""
        if media_type == MediaType.RADIO:
            return self._radio_stream_details(item_id)
        if media_type == MediaType.PODCAST_EPISODE:
            return await self._episode_stream_details(item_id)
        raise UnplayableMediaError("Unsupported media type")

    async def get_resume_position(
        self, item_id: str, media_type: MediaType
    ) -> tuple[bool, int, None]:
        """Return the user's VRT playback progress for an episode (fully_played, ms, None)."""
        if media_type != MediaType.PODCAST_EPISODE or not self._auth.enabled:
            raise NotImplementedError
        try:
            access_token = await self._auth.get_access_token()
            progress = await self._client.get_progress(item_id, access_token)
        except (VrtAuthError, VrtApiError) as err:
            self.logger.debug("Could not read VRT progress for %s: %s", item_id, err)
            raise NotImplementedError from err
        return progress.completed, progress.position * 1000, None

    async def on_played(
        self,
        media_type: MediaType,
        prov_item_id: str,
        fully_played: bool,
        position: int,
        media_item: MediaItemType,
        is_playing: bool = False,
    ) -> None:
        """Write the playback position back to VRT ('resume point')."""
        if media_type != MediaType.PODCAST_EPISODE or not self._auth.enabled:
            return
        try:
            access_token = await self._auth.get_access_token()
            target = await self._client.get_resume_target(prov_item_id)
            total = target.duration or getattr(media_item, "duration", 0) or 0
            at = total if (fully_played and total) else position
            await self._client.post_resume_point(target, at, access_token, total=total)
        except (VrtAuthError, VrtApiError) as err:
            self.logger.debug("Could not write VRT progress for %s: %s", prov_item_id, err)

    async def _set_favourite(self, page_id: str, favourite: bool) -> bool:
        """Add/remove a programme in 'Mijn lijst'; returns False if it can't be synced."""
        if not self._auth.enabled:
            # No credentials: keep the change local to MA only.
            return True
        try:
            access_token = await self._auth.get_access_token()
            action_id, already = await self._client.get_favourite_action(page_id, access_token)
            if not action_id:
                self.logger.warning("No favourite action for %s; not synced to Mijn lijst", page_id)
                return False
            if already != favourite:
                await self._client.set_favourite(action_id, favourite, access_token)
        except (VrtAuthError, VrtApiError) as err:
            self.logger.warning("Failed to update Mijn lijst for %s: %s", page_id, err)
            return False
        return True

    # ----------------------------
    # Browse helpers
    # ----------------------------

    def _browse_root(self) -> list[BrowseFolder]:
        return [
            BrowseFolder(
                item_id=BROWSE_RADIOS,
                provider=self.instance_id,
                path=f"{self.instance_id}://{BROWSE_RADIOS}",
                name="Live Radio",
                translation_key="radio_stations",
            ),
            BrowseFolder(
                item_id=BROWSE_RADIO_PROGRAMS,
                provider=self.instance_id,
                path=f"{self.instance_id}://{BROWSE_RADIO_PROGRAMS}",
                name="Radio programs",
                translation_key="radio_programs",
            ),
            BrowseFolder(
                item_id=BROWSE_PODCASTS,
                provider=self.instance_id,
                path=f"{self.instance_id}://{BROWSE_PODCASTS}",
                name="Podcasts",
                translation_key="podcasts",
            ),
        ]

    async def _browse_landing(self, page_id: str, row_type: str, prefix: str) -> list[BrowseFolder]:
        """Return one folder per program/podcast row of a landing page."""
        rows = await self._client.get_landing_rows(page_id)
        folders: list[BrowseFolder] = []
        for row in rows:
            if row.tile_type != row_type or not row.title:
                continue
            encoded = _encode(row.component_id)
            folders.append(
                BrowseFolder(
                    item_id=encoded,
                    provider=self.instance_id,
                    path=f"{self.instance_id}://{prefix}/{encoded}",
                    name=row.title.strip(),
                )
            )
        return folders

    async def _browse_programs(self, component_id: str) -> list[Podcast]:
        """Return all programs/podcasts of a landing-page component as Podcast items."""
        podcasts: list[Podcast] = []
        async for tile in self._client.iter_programs(component_id):
            podcasts.append(self._podcast_from_tile(tile))
        return podcasts

    # ----------------------------
    # Media item constructors
    # ----------------------------

    def _radio_item(self, station: VrtStation) -> Radio:
        """Build a Radio media item from a VrtStation."""
        radio = Radio(
            name=station.name,
            item_id=station.id,
            provider=self.instance_id,
            provider_mappings={
                ProviderMapping(
                    item_id=station.id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        if station.logo_url:
            radio.metadata.add_image(self._image(station.logo_url))
        if station.tagline:
            radio.metadata.description = station.tagline
        return radio

    def _podcast_from_tile(self, tile: VrtProgramTile) -> Podcast:
        podcast = self._podcast_base(tile.page_id, tile.title)
        if tile.description:
            podcast.metadata.description = tile.description
        if tile.image_url:
            podcast.metadata.add_image(self._image(tile.image_url))
        return podcast

    def _podcast_from_program(self, program: VrtProgram) -> Podcast:
        podcast = self._podcast_base(program.page_id, program.title)
        # MA's podcast view surfaces the description but not publisher/performers,
        # so prepend the presenter(s) to the description to keep them visible.
        description_parts: list[str] = []
        if program.presenters:
            description_parts.append("Presentatie: " + ", ".join(program.presenters))
        if program.description:
            description_parts.append(program.description)
        if description_parts:
            podcast.metadata.description = "\n\n".join(description_parts)
        if program.image_url:
            podcast.metadata.add_image(self._image(program.image_url))
        if program.publisher:
            podcast.publisher = program.publisher
        if program.presenters:
            podcast.metadata.performers = set(program.presenters)
        return podcast

    def _podcast_base(self, page_id: str, title: str) -> Podcast:
        return Podcast(
            name=title,
            item_id=page_id,
            provider=self.instance_id,
            provider_mappings={
                ProviderMapping(
                    item_id=page_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

    def _episode_item(
        self, episode: VrtEpisode, podcast: ItemMapping, position: int
    ) -> PodcastEpisode:
        name = episode.title
        if episode.date_label and episode.date_label not in name:
            name = f"{episode.date_label} - {name}"
        item = PodcastEpisode(
            name=name,
            item_id=episode.page_id,
            provider=self.instance_id,
            position=position,
            duration=episode.duration,
            podcast=podcast,
            provider_mappings={
                ProviderMapping(
                    item_id=episode.page_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        if episode.description:
            item.metadata.description = episode.description
        if episode.image_url:
            item.metadata.add_image(self._image(episode.image_url))
        item.fully_played = episode.fully_played
        if episode.resume_position:
            item.resume_position_ms = episode.resume_position * 1000
        return item

    def _image(self, url: str) -> MediaItemImage:
        return MediaItemImage(
            type=ImageType.THUMB,
            path=url,
            provider=self.instance_id,
            remotely_accessible=True,
        )

    # ----------------------------
    # Playback
    # ----------------------------

    def _radio_stream_details(self, item_id: str) -> StreamDetails:
        station = STATIONS_BY_ID.get(item_id)
        if station is None:
            raise MediaNotFoundError("Radio not found.")
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            media_type=MediaType.RADIO,
            stream_type=StreamType.HTTP,
            path=station.stream_url,
            audio_format=AudioFormat(content_type=ContentType.try_parse("mp3")),
            can_seek=False,
            allow_seek=False,
        )

    async def _episode_stream_details(self, item_id: str) -> StreamDetails:
        if not self._auth.enabled:
            raise UnplayableMediaError(
                "Log in with your VRT account in the provider settings to play on-demand audio."
            )
        try:
            info = await self._client.get_stream_info(item_id)
            token = await self._auth.get_player_token()
            hls_url = await self._client.resolve_ondemand_hls(info.stream_id, token)
        except VrtAuthError as err:
            raise UnplayableMediaError(f"VRT authentication failed: {err}") from err
        except VrtApiError as err:
            raise MediaNotFoundError(f"Could not resolve episode stream: {err}") from err
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            media_type=MediaType.PODCAST_EPISODE,
            stream_type=StreamType.HLS,
            path=hls_url,
            audio_format=AudioFormat(content_type=ContentType.try_parse("aac")),
            duration=info.duration or None,
            can_seek=True,
            allow_seek=True,
        )

    async def _attach_tracklists(self, episodes: list[PodcastEpisode]) -> None:
        """Concurrently attach the played-songs tracklist (as chapters) to radio episodes."""
        targets = [ep for ep in episodes if _has_tracklist(ep.item_id)][:MAX_TRACKLIST_EPISODES]
        if not targets:
            return

        async def attach(episode: PodcastEpisode) -> None:
            chapters = await self._episode_chapters(episode.item_id)
            if chapters:
                episode.metadata.chapters = chapters

        await asyncio.gather(*(attach(episode) for episode in targets))

    async def _episode_chapters(self, page_id: str) -> list[MediaItemChapter] | None:
        """Return a radio episode's played-songs tracklist as chapters, if it has one."""
        if not _has_tracklist(page_id):
            return None
        try:
            chapters = await self._fetch_chapters(page_id)
        except VrtApiError as err:
            # A transient failure raises out of the cached fetch, so it is not cached
            # and the tracklist is retried the next time the episode is opened.
            self.logger.debug("Could not fetch chapters for %s: %s", page_id, err)
            return None
        return chapters or None

    @use_cache(3600 * 6, base_class=MediaItemChapter)
    async def _fetch_chapters(self, page_id: str) -> list[MediaItemChapter]:
        """
        Fetch a radio episode's played-songs tracklist as chapters.

        Cached because a past broadcast's tracklist is immutable, so re-opening a show
        (or switching between its episodes) reuses the result instead of re-querying.
        """
        chapters = await self._client.get_episode_chapters(page_id)
        return [
            MediaItemChapter(
                position=chapter.position,
                name=chapter.name,
                start=chapter.start,
                end=chapter.end,
            )
            for chapter in chapters
        ]


def _has_tracklist(page_id: str) -> bool:
    """Only radio-archive episodes expose a played-songs tracklist; podcasts never do."""
    return page_id.startswith("/vrtmax/luister/radio/")


def _encode(component_id: str) -> str:
    """Encode a base64 componentId into a URL/path-safe browse segment."""
    return base64.urlsafe_b64encode(component_id.encode()).decode().rstrip("=")


def _decode(encoded: str) -> str:
    """Reverse _encode()."""
    padded = encoded + "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(padded.encode()).decode()


def _program_id_from_episode(episode_id: str) -> str:
    """
    Derive the parent programme/podcast page id from an episode page id.

    Podcast episodes nest one extra (season) path segment under the podcast,
    radio episodes nest directly under the programme archive.
    """
    segments = [seg for seg in episode_id.split("/") if seg]
    trim = 2 if episode_id.startswith("/vrtmax/podcasts/") else 1
    parent = segments[:-trim] if len(segments) > trim else segments
    return "/" + "/".join(parent) + "/"
