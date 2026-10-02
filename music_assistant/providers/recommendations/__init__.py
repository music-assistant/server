"""
Builtin Library Recommendations Provider.

Surfaces library-based discovery rows as recommendations on the Discover page.
"""

from __future__ import annotations

from copy import copy
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from music_assistant_models.background_task import TaskSchedule
from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.media_items import PodcastEpisode, RecommendationFolder, UniqueList

from music_assistant.controllers.tasks.context import update_current_task_progress_from_index
from music_assistant.helpers.audio import get_probed_duration
from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    from collections.abc import Sequence

    from music_assistant_models.config_entries import (
        ConfigEntry,
        ConfigValueType,
        ProviderConfig,
    )
    from music_assistant_models.media_items import BrowseFolder, ItemMapping, MediaItemType
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES: set[ProviderFeature] = {
    ProviderFeature.RECOMMENDATIONS,
}

LATEST_EPISODES_TASK_ID: Final[str] = "recommendations_refresh_latest_episodes"
LATEST_EPISODES_CACHE_KEY: Final[str] = "latest_episodes_by_podcast"


class LibraryRowID(StrEnum):
    """The item_ids of library recommendation rows."""

    IN_PROGRESS = "in_progress"
    RECENTLY_PLAYED = "recently_played"
    RECENTLY_ADDED_TRACKS = "recently_added_tracks"
    RECENTLY_ADDED_ALBUMS = "recently_added_albums"
    RANDOM_ARTISTS = "random_artists"
    RANDOM_ALBUMS = "random_albums"
    RECENT_FAVORITE_TRACKS = "recent_favorite_tracks"
    FAVORITE_PLAYLISTS = "favorite_playlists"
    FAVORITE_RADIO = "favorite_radio"
    RECENT_ARTISTS = "recent_artists"
    RECENT_TRACKS = "recent_tracks"
    FORGOTTEN_TRACKS = "forgotten_tracks"
    FORGOTTEN_ALBUMS = "forgotten_albums"
    FORGOTTEN_ARTISTS = "forgotten_artists"
    MOST_PLAYED_TRACKS = "most_played_tracks"
    NEVER_PLAYED_TRACKS = "never_played_tracks"
    LATEST_EPISODES = "latest_episodes"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return LibraryRecommendationsProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return config entries for this provider."""
    return ()


class LibraryRecommendationsProvider(PluginProvider):
    """Builtin provider for library-based recommendation rows."""

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        cached = await self.mass.cache.get(LATEST_EPISODES_CACHE_KEY, provider=self.instance_id)
        # library podcast id with the latest episode of one of its provider mappings
        self._latest_episodes: list[tuple[str, PodcastEpisode]] = [
            (x["podcast_id"], PodcastEpisode.from_dict(x["episode"])) for x in cached or []
        ]
        self.mass.tasks.register_scheduled_task(
            task_id=LATEST_EPISODES_TASK_ID,
            name="Refresh latest podcast episodes",
            handler=self._refresh_latest_episodes,
            schedule=TaskSchedule.hourly(every=6),
            # runs this long after startup when the task is overdue or never ran,
            # giving the podcast providers time to finish loading
            initial_delay=20,
            translation_key="refresh_latest_episodes",
            translation_owner=self.translation_owner,
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Unload the provider."""
        self.mass.tasks.unregister_scheduled_task(
            LATEST_EPISODES_TASK_ID, clear_persisted_state=is_removed
        )

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Get all library recommendation rows, without items."""
        return [
            _folder(
                LibraryRowID.IN_PROGRESS, "In progress", "in_progress_items", "mdi-motion-play"
            ),
            _folder(
                LibraryRowID.RECENTLY_PLAYED,
                "Recently played",
                "recently_played",
                "mdi-motion-play",
            ),
            _folder(
                LibraryRowID.RECENTLY_ADDED_TRACKS,
                "Recently added tracks",
                "recently_added_tracks",
                "mdi-music-note-plus",
                False,
            ),
            _folder(
                LibraryRowID.RECENTLY_ADDED_ALBUMS,
                "Recently added albums",
                "recently_added_albums",
                "mdi-album",
            ),
            _folder(
                LibraryRowID.LATEST_EPISODES,
                "Latest podcast episodes",
                "latest_episodes",
                "mdi-podcast",
            ),
            _folder(
                LibraryRowID.RANDOM_ARTISTS,
                "Random artists",
                "random_artists",
                "mdi-account-music",
                False,
            ),
            _folder(
                LibraryRowID.RANDOM_ALBUMS, "Random albums", "random_albums", "mdi-album", False
            ),
            _folder(
                LibraryRowID.RECENT_FAVORITE_TRACKS,
                "Recently favorited tracks",
                "recent_favorite_tracks",
                "mdi-file-music",
                False,
            ),
            _folder(
                LibraryRowID.FAVORITE_PLAYLISTS,
                "Favorite playlists",
                "favorite_playlists",
                "mdi-playlist-music",
                False,
            ),
            _folder(
                LibraryRowID.FAVORITE_RADIO,
                "Favorite Radio stations",
                "favorite_radio_stations",
                "mdi-access-point",
                False,
            ),
            _folder(
                LibraryRowID.RECENT_ARTISTS,
                "Recent artists",
                "recent_artists",
                "mdi-account-music",
                False,
            ),
            _folder(
                LibraryRowID.RECENT_TRACKS,
                "Recent tracks",
                "recent_tracks",
                "mdi-music-note",
                False,
            ),
            _folder(
                LibraryRowID.FORGOTTEN_TRACKS,
                "Forgotten Tracks",
                "forgotten_tracks",
                "mdi-timer-sand",
                False,
            ),
            _folder(
                LibraryRowID.FORGOTTEN_ALBUMS,
                "Forgotten Albums",
                "forgotten_albums",
                "mdi-timer-sand",
                False,
            ),
            _folder(
                LibraryRowID.FORGOTTEN_ARTISTS,
                "Forgotten Artists",
                "forgotten_artists",
                "mdi-timer-sand",
                False,
            ),
            _folder(
                LibraryRowID.MOST_PLAYED_TRACKS,
                "Most Played Tracks",
                "most_played_tracks",
                "mdi-trophy",
                False,
            ),
            _folder(
                LibraryRowID.NEVER_PLAYED_TRACKS,
                "Never / Rarely Played",
                "never_played_tracks",
                "mdi-sleep",
                False,
            ),
        ]

    async def get_recommendation_items(
        self, item_id: str, providers: list[str] | None = None
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Get the items for a single library recommendation row.

        :param item_id: The item_id of the row, as returned by get_recommendations.
        :param providers: Restrict items to those reachable through one of these provider
            instance ids (OR semantics). An explicit empty list returns no items; None
            applies no filter.
        """
        if providers is not None and not providers:
            return UniqueList()
        items: Sequence[MediaItemType | ItemMapping | BrowseFolder]
        match item_id:
            case LibraryRowID.IN_PROGRESS:
                items = await self.mass.music.in_progress_items(limit=10, providers=providers)
            case LibraryRowID.RECENTLY_PLAYED:
                items = await self.mass.music.recently_played(
                    limit=10,
                    media_types=[
                        MediaType.ALBUM,
                        MediaType.TRACK,
                        MediaType.PLAYLIST,
                        MediaType.ARTIST,
                        MediaType.GENRE,
                    ],
                    user_initiated_only=True,
                    always_include_media_types=[MediaType.PODCAST, MediaType.AUDIOBOOK],
                    providers=providers,
                )
            case LibraryRowID.RECENTLY_ADDED_TRACKS:
                items = await self.mass.music.tracks.library_items(
                    limit=10, order_by="timestamp_added_desc", reachable_via=providers
                )
            case LibraryRowID.RECENTLY_ADDED_ALBUMS:
                items = await self.mass.music.albums.library_items(
                    limit=10, order_by="timestamp_added_desc", reachable_via=providers
                )
            case LibraryRowID.RANDOM_ARTISTS:
                items = await self.mass.music.artists.library_items(
                    limit=10, order_by="random_play_count", reachable_via=providers
                )
            case LibraryRowID.RANDOM_ALBUMS:
                items = await self.mass.music.albums.library_items(
                    limit=10, order_by="random_play_count", reachable_via=providers
                )
            case LibraryRowID.RECENT_FAVORITE_TRACKS:
                items = await self.mass.music.tracks.library_items(
                    favorite=True,
                    limit=10,
                    order_by="favorite_timestamp_desc",
                    reachable_via=providers,
                )
            case LibraryRowID.FAVORITE_PLAYLISTS:
                items = await self.mass.music.playlists.library_items(
                    favorite=True, limit=10, order_by="random", reachable_via=providers
                )
            case LibraryRowID.FAVORITE_RADIO:
                items = await self.mass.music.radio.library_items(
                    favorite=True, limit=10, order_by="play_count_desc", reachable_via=providers
                )
            case LibraryRowID.RECENT_ARTISTS:
                items = await self.mass.music.recently_played(
                    limit=10,
                    media_types=[MediaType.ARTIST],
                    user_initiated_only=False,
                    providers=providers,
                )
            case LibraryRowID.RECENT_TRACKS:
                items = await self.mass.music.recently_played(
                    limit=10,
                    media_types=[MediaType.TRACK],
                    user_initiated_only=False,
                    providers=providers,
                )
            case LibraryRowID.FORGOTTEN_TRACKS:
                items = await self.mass.music.tracks.library_items(
                    limit=10, order_by="last_played", played_only=True, reachable_via=providers
                )
            case LibraryRowID.FORGOTTEN_ALBUMS:
                items = await self.mass.music.albums.library_items(
                    limit=10, order_by="last_played", played_only=True, reachable_via=providers
                )
            case LibraryRowID.FORGOTTEN_ARTISTS:
                items = await self.mass.music.artists.library_items(
                    limit=10, order_by="last_played", played_only=True, reachable_via=providers
                )
            case LibraryRowID.MOST_PLAYED_TRACKS:
                items = await self.mass.music.tracks.library_items(
                    limit=10, order_by="play_count_desc", reachable_via=providers
                )
            case LibraryRowID.NEVER_PLAYED_TRACKS:
                items = await self.mass.music.tracks.library_items(
                    limit=10, order_by="play_count", reachable_via=providers
                )
            case LibraryRowID.LATEST_EPISODES:
                items = await self._get_latest_episodes(providers)
            case _:
                items = []
        return UniqueList(items)

    async def _get_latest_episodes(self, providers: list[str] | None) -> list[PodcastEpisode]:
        """Return the stored latest episodes, with the current user's played state."""
        allowed = set(self.mass.music.get_active_provider_instances())
        if providers is not None:
            allowed.intersection_update(providers)
        result: list[PodcastEpisode] = []
        seen_podcasts: set[str] = set()
        for podcast_id, stored in self._latest_episodes:
            if podcast_id in seen_podcasts:
                continue
            if stored.provider not in allowed and not any(
                mapping.provider_instance in allowed for mapping in stored.provider_mappings
            ):
                continue
            seen_podcasts.add(podcast_id)
            # MA's own play history wins, the state the provider reported at the last
            # refresh covers progress made outside MA
            episode = copy(stored)
            episode.fully_played = None
            episode.resume_position_ms = None
            await self.mass.music.podcasts.restore_resume_position(episode, episode.provider)
            if episode.fully_played is None and not episode.resume_position_ms:
                episode.fully_played = stored.fully_played
                episode.resume_position_ms = stored.resume_position_ms
            result.append(episode)
        return result

    async def _refresh_latest_episodes(self) -> None:
        """Store the newest episode of every library podcast, newest release first."""
        active_providers = set(self.mass.music.get_active_provider_instances())
        podcasts = [x async for x in self.mass.music.podcasts.iter_library_items()]
        latest_episodes: list[tuple[str, PodcastEpisode]] = []
        for index, podcast in enumerate(podcasts):
            update_current_task_progress_from_index(index, len(podcasts), podcast.name)
            # every mapping is kept, users may only have access to some of them
            for mapping in podcast.provider_mappings:
                if mapping.provider_instance not in active_providers:
                    continue
                if latest := await self._get_latest_episode(
                    mapping.provider_instance, mapping.item_id, podcast.name
                ):
                    latest_episodes.append((podcast.item_id, latest))
        latest_episodes.sort(key=lambda x: _release_timestamp(x[1]), reverse=True)
        self._latest_episodes = latest_episodes
        await self.mass.cache.set(
            LATEST_EPISODES_CACHE_KEY,
            [{"podcast_id": x[0], "episode": x[1].to_dict()} for x in latest_episodes],
            provider=self.instance_id,
            persistent=True,
        )

    async def _get_latest_episode(
        self, provider_instance: str, prov_podcast_id: str, podcast_name: str
    ) -> PodcastEpisode | None:
        """
        Return the newest episode of a podcast on one provider, None when unavailable.

        :param provider_instance: The provider instance to read the podcast from.
        :param prov_podcast_id: The podcast's item id on that provider.
        :param podcast_name: The podcast's name, for logging.
        """
        prov = self.mass.get_provider(provider_instance)
        if not isinstance(prov, MusicProvider):
            return None
        # read straight from the provider so only its own played state is kept,
        # never the playlog of whichever user the controller would fall back to
        try:
            episodes = [x async for x in prov.get_podcast_episodes(prov_podcast_id)]
        except MusicAssistantError as err:
            self.logger.debug("Skipping latest episode of %s: %s", podcast_name, err)
            return None
        latest = max(episodes, key=lambda x: x.position, default=None)
        if latest is None:
            return None
        if (
            not latest.duration
            and latest.uri
            and (probed_duration := await get_probed_duration(self.mass, latest.uri))
        ):
            latest.duration = probed_duration
        return latest


def _folder(
    item_id: LibraryRowID,
    name: str,
    translation_key: str,
    icon: str,
    enabled_by_default: bool = True,
) -> RecommendationFolder:
    """Create a recommendation folder metadata object."""
    return RecommendationFolder(
        item_id=item_id.value,
        provider="recommendations",
        name=name,
        translation_key=translation_key,
        icon=icon,
        enabled_by_default=enabled_by_default,
        uri=f"library://folder/{item_id.value}",
        supports_provider_filter=True,
    )


def _release_timestamp(episode: PodcastEpisode) -> float:
    """Return the release date of an episode as a timestamp, 0 when undated."""
    release_date = episode.metadata.release_date
    return release_date.timestamp() if release_date else 0
