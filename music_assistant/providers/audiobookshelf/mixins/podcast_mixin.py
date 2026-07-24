"""PodcastMixin for Audiobookshelf."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from aioaudiobookshelf.schema.library import (
    LibraryItemExpandedPodcast as AbsLibraryItemExpandedPodcast,
)
from aioaudiobookshelf.schema.library import LibraryItemMinifiedPodcast
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.audiobookshelf.constants import (
    CONF_HIDE_EMPTY_PODCASTS,
    CONF_URL,
)
from music_assistant.providers.audiobookshelf.helpers import handle_refresh_token
from music_assistant.providers.audiobookshelf.mixins.mixin_base import AbsMixinBase
from music_assistant.providers.audiobookshelf.parsers import (
    parse_podcast,
    parse_podcast_episode,
)

if TYPE_CHECKING:
    from aioaudiobookshelf.schema.media_progress import MediaProgress
    from music_assistant_models.media_items import Podcast, PodcastEpisode


class AbsPodcastsMixin(AbsMixinBase):
    """PodcastMixin for Audiobookshelf."""

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast]:
        """
        Retrieve library/subscribed podcasts from the provider.

        Minified podcast information is enough.
        """
        for pod_lib_id in self.libraries.podcasts:
            async for response in self._client.get_library_items(library_id=pod_lib_id):
                if not response.results:
                    break
                podcast_ids = [x.id_ for x in response.results]
                # store uuids
                self.libraries.podcasts[pod_lib_id].item_ids.update(podcast_ids)
                for podcast_minified in response.results:
                    assert isinstance(podcast_minified, LibraryItemMinifiedPodcast)
                    mass_podcast = parse_podcast(
                        abs_podcast=podcast_minified,
                        instance_id=self.instance_id,
                        domain=self.domain,
                        token=self._client.token,
                        base_url=str(self.config.get_value(CONF_URL)).rstrip("/"),
                    )
                    if (
                        bool(self.config.get_value(CONF_HIDE_EMPTY_PODCASTS))
                        and mass_podcast.total_episodes == 0
                    ):
                        continue
                    yield mass_podcast

    @handle_refresh_token
    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get single podcast."""
        abs_podcast = await self._get_abs_expanded_podcast(prov_podcast_id=prov_podcast_id)
        return parse_podcast(
            abs_podcast=abs_podcast,
            instance_id=self.instance_id,
            domain=self.domain,
            token=self._client.token,
            base_url=str(self.config.get_value(CONF_URL)).rstrip("/"),
        )

    async def get_podcast_episodes(self, prov_podcast_id: str) -> AsyncGenerator[PodcastEpisode]:
        """
        Get all podcast episodes of podcast.

        Adds progress information.
        """
        abs_podcast = await self._get_abs_expanded_podcast(prov_podcast_id=prov_podcast_id)
        episode_cnt = 1
        # the user has the progress of all media items
        # so we use a single api call here to obtain possibly many
        # progresses for episodes
        user = await self._client.get_my_user()
        abs_progresses = {
            x.episode_id: x
            for x in user.media_progress
            if x.episode_id is not None and x.library_item_id == prov_podcast_id
        }
        for abs_episode in abs_podcast.media.episodes:
            progress = abs_progresses.get(abs_episode.id_)
            mass_episode = parse_podcast_episode(
                episode=abs_episode,
                prov_podcast_id=prov_podcast_id,
                prov_podcast_name=abs_podcast.media.metadata.title,
                fallback_episode_cnt=episode_cnt,
                instance_id=self.instance_id,
                domain=self.domain,
                token=self._client.token,
                base_url=str(self.config.get_value(CONF_URL)).rstrip("/"),
                media_progress=progress,
                cover_path=abs_podcast.media.cover_path,
                cover_version=abs_podcast.updated_at,
            )
            yield mass_episode
            episode_cnt += 1

    @handle_refresh_token
    async def get_podcast_episode(
        self, prov_episode_id: str, add_progress: bool = True
    ) -> PodcastEpisode:
        """Get single podcast episode."""
        prov_podcast_id, e_id = prov_episode_id.split(" ")
        abs_podcast = await self._get_abs_expanded_podcast(prov_podcast_id=prov_podcast_id)
        episode_cnt = 1
        for abs_episode in abs_podcast.media.episodes:
            if abs_episode.id_ == e_id:
                progress = None
                if add_progress:
                    progress = await self._client.get_my_media_progress(
                        item_id=prov_podcast_id, episode_id=abs_episode.id_
                    )
                return parse_podcast_episode(
                    episode=abs_episode,
                    prov_podcast_id=prov_podcast_id,
                    prov_podcast_name=abs_podcast.media.metadata.title,
                    fallback_episode_cnt=episode_cnt,
                    instance_id=self.instance_id,
                    domain=self.domain,
                    token=self._client.token,
                    base_url=str(self.config.get_value(CONF_URL)).rstrip("/"),
                    media_progress=progress,
                    cover_path=abs_podcast.media.cover_path,
                    cover_version=abs_podcast.updated_at,
                )

            episode_cnt += 1
        raise MediaNotFoundError("Episode not found")

    @handle_refresh_token
    async def _get_abs_expanded_podcast(
        self, prov_podcast_id: str
    ) -> AbsLibraryItemExpandedPodcast:
        abs_podcast = await self._client.get_library_item_podcast(
            podcast_id=prov_podcast_id, expanded=True
        )
        assert isinstance(abs_podcast, AbsLibraryItemExpandedPodcast)

        return abs_podcast

    async def _update_playlog_episode(self, progress: MediaProgress) -> None:
        # helper progress also ensures no useless progress updates,
        # see comment above
        self.progress_guard.add_progress(progress.library_item_id, progress.episode_id)
        if progress.current_time is None:
            return
        _episode_id = f"{progress.library_item_id} {progress.episode_id}"
        try:
            # need to obtain full podcast, and then search for episode
            mass_episode = await self.get_podcast_episode(_episode_id, add_progress=False)
        except MediaNotFoundError:
            return
        if int(progress.current_time) == 0 and not progress.is_finished:
            await self.mass.music.mark_item_unplayed(mass_episode)
        else:
            await self.mass.music.mark_item_played(
                mass_episode,
                fully_played=progress.is_finished,
                seconds_played=int(progress.current_time),
            )
