"""Podcast-specific functionality for Spotify provider."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import MediaItemType, Podcast, PodcastEpisode

from .parsers import parse_podcast, parse_podcast_episode

if TYPE_CHECKING:
    from .provider import SpotifyProvider


class PodcastManager:
    """Handles podcast-specific functionality for Spotify provider."""

    def __init__(self, provider: SpotifyProvider):
        """Initialize the PodcastManager with a reference to the Spotify provider."""
        self.provider = provider
        self.logger = provider.logger

    @property
    def sync_played_status_enabled(self) -> bool:
        """Check if played status sync is enabled."""
        return self.provider.sync_played_status_enabled

    @property
    def played_threshold(self) -> float:
        """Get the played threshold percentage."""
        return self.provider.played_threshold

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get full podcast details by id."""
        podcast_obj = await self.provider._get_data(f"shows/{prov_podcast_id}")
        if not podcast_obj:
            raise ValueError(f"No podcast data returned from API for ID: {prov_podcast_id}")
        return parse_podcast(podcast_obj, self.provider)

    async def get_podcast_episodes(
        self, prov_podcast_id: str
    ) -> AsyncGenerator[PodcastEpisode, None]:
        """Get all podcast episodes using the provider's _get_all_items helper."""
        podcast = await self.get_podcast(prov_podcast_id)
        episode_position = 1

        # Use the provider's _get_all_items helper as suggested by reviewer
        async for item in self.provider._get_all_items(
            f"shows/{prov_podcast_id}/episodes", market="from_token"
        ):
            if not (item and item["id"]):
                continue

            episode = parse_podcast_episode(item, self.provider, podcast=podcast)
            episode.position = episode_position
            episode_position += 1
            yield episode

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get full podcast episode details by id."""
        episode_obj = await self.provider._get_data(
            f"episodes/{prov_episode_id}", market="from_token"
        )
        if not episode_obj:
            raise ValueError(f"No episode data returned from API for ID: {prov_episode_id}")
        return parse_podcast_episode(episode_obj, self.provider)

    async def get_resume_position(self, item_id: str, media_type: MediaType) -> tuple[bool, int]:
        """
        Get resume position for episode from Spotify.

        Returns:
            tuple[bool, int]: (is_fully_played, position_in_milliseconds)
        """
        if media_type != MediaType.PODCAST_EPISODE:
            raise NotImplementedError("Resume position only supported for podcast episodes")

        try:
            # Get latest episode data from Spotify
            episode_obj = await self.provider._get_data(f"episodes/{item_id}", market="from_token")

            if (
                not episode_obj
                or "resume_point" not in episode_obj
                or not episode_obj["resume_point"]
            ):
                # No resume point data available, let MA use its stored position
                raise NotImplementedError("No resume point data from Spotify")

            resume_point = episode_obj["resume_point"]
            fully_played = resume_point.get("fully_played", False)
            position_ms = resume_point.get("resume_position_ms", 0)

            # Apply played threshold logic
            if not fully_played and episode_obj.get("duration_ms", 0) > 0:
                completion_ratio = position_ms / episode_obj["duration_ms"]
                if completion_ratio >= self.played_threshold:
                    fully_played = True
                    self.logger.debug(
                        f"Episode {item_id} marked as played due to "
                        f"{completion_ratio:.1%} completion"
                    )

            self.logger.debug(
                f"Resume position from Spotify for {item_id}: "
                f"{position_ms}ms, played: {fully_played}"
            )
            return fully_played, position_ms

        except Exception as e:
            self.logger.debug(f"Failed to get resume position from Spotify for {item_id}: {e}")
            # Let MA fall back to its stored resume position
            raise NotImplementedError("Failed to get resume position from Spotify")

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
        Call when an episode is played in MA.

        Note: This CANNOT sync back to Spotify as there's no API for it.
        This is just for logging/monitoring purposes.
        """
        if media_type != MediaType.PODCAST_EPISODE:
            return

        if not isinstance(media_item, PodcastEpisode):
            return

        # Handle case where position might be None (e.g., when marked as played in UI)
        safe_position = position or 0
        if media_item.duration > 0:
            completion_percentage = (safe_position / media_item.duration) * 100
        else:
            completion_percentage = 0

        self.logger.debug(
            f"Episode played in MA: {prov_item_id} "
            f"({completion_percentage:.1f}%, fully_played: {fully_played}) "
            f"- Cannot sync back to Spotify due to API limitations"
        )
