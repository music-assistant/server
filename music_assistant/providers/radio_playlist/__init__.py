"""
Radio Playlists provider for Music Assistant.

Generates dynamic "radio" playlists from a seed media item (artist / album / track / genre /
playlist) — a mix of the seed's own tracks and similar tracks. A radio playlist is a normal dynamic
playlist (``is_dynamic=True``): the queue and the rest of Music Assistant treat it exactly like any
other provider's dynamic playlist (a station, a smart playlist). The playlist's ``item_id`` is the
seed item's own URI, so ``radio_playlist://playlist/<seed-uri>`` round-trips straight back to the
seed.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING
from urllib.parse import unquote

from music_assistant_models.errors import (
    MediaNotFoundError,
    MusicAssistantError,
    UnsupportedFeaturedException,
)
from music_assistant_models.media_items import (
    BrowseFolder,
    MediaItemMetadata,
    Playlist,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.constants import DYNAMIC_PLAYLIST_SAMPLE_SIZE
from music_assistant.controllers.music.constants import (
    DYNAMIC_RADIO_BASE_SAMPLE_SIZE,
    DYNAMIC_RADIO_DYNAMIC_TARGET,
    RADIO_TRACK_MAX_DURATION_SECS,
)
from music_assistant.helpers.track_filter import get_track_filter
from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
    from music_assistant_models.media_items import MediaItemType
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return RadioPlaylistProvider(mass, manifest, config, set())


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider (none needed)."""
    return ()


def radio_playlist_uri(seed: MediaItemType) -> str:
    """
    Return the radio-playlist URI for a seed media item.

    :param seed: The media item (artist/album/track/genre/playlist) to base the radio playlist on.
    """
    return f"radio_playlist://playlist/{seed.uri}"


class RadioPlaylistProvider(PluginProvider):
    """Always-on provider that generates dynamic radio playlists from a seed media item."""

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """
        Return the (virtual, dynamic) radio playlist for the given seed.

        :param prov_playlist_id: The seed item's URI (raw or url-encoded).
        """
        seed = await self._resolve_seed(prov_playlist_id)
        playlist = Playlist(
            item_id=prov_playlist_id,
            provider=self.instance_id,
            name=f"{seed.name} Radio",
            provider_mappings={
                ProviderMapping(
                    item_id=prov_playlist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    is_unique=True,
                )
            },
        )
        playlist.is_dynamic = True
        if images := getattr(seed.metadata, "images", None):
            playlist.metadata = MediaItemMetadata(images=UniqueList(images))
        return playlist

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """
        Return a fresh sample of the radio playlist's tracks (the seed's own tracks + similar).

        :param prov_playlist_id: The seed item's URI (raw or url-encoded).
        :param page: Pagination; a dynamic playlist returns its full fresh batch on page 0 only.
        """
        if page > 0:
            return []
        seed = await self._resolve_seed(prov_playlist_id)
        try:
            return await self.get_dynamic_tracks(
                [seed], include_base_tracks=True, target_size=DYNAMIC_PLAYLIST_SAMPLE_SIZE
            )
        except MusicAssistantError:
            return []

    async def get_dynamic_tracks(
        self,
        seeds: list[MediaItemType],
        *,
        include_base_tracks: bool = False,
        target_size: int = 25,
        preferred_provider_instances: list[str] | None = None,
    ) -> list[Track]:
        """
        Generate a dynamic radio track pool from one or more seed media items.

        :param seeds: Seed media items (Track, Artist, Album, Playlist, ...) used as sources.
        :param include_base_tracks: When True, interleave the sampled base tracks into the result
            using the BDDBDD pattern. When False, only similar tracks are returned.
        :param target_size: Maximum number of dynamic (similar) tracks to sample into the result.
            When ``include_base_tracks`` is True, base tracks are added on top of this cap.
        :param preferred_provider_instances: Provider instance IDs preferred for similar lookups.
        :raises UnsupportedFeaturedException: When no base tracks could be derived from any seed.
        """
        seen: set[Track] = set()
        available_base_tracks: list[Track] = []
        for seed in random.sample(seeds, len(seeds)):
            for track in await self.mass.player_queues.get_tracks_for_playback(seed):
                if track not in seen:
                    seen.add(track)
                    available_base_tracks.append(track)
        if not available_base_tracks:
            raise UnsupportedFeaturedException("No base tracks available for the given seeds")

        base_tracks = random.sample(
            available_base_tracks,
            min(DYNAMIC_RADIO_BASE_SAMPLE_SIZE, len(available_base_tracks)),
        )
        # a consumer (e.g. the player queue) may publish a best-effort filter to pre-skip tracks it
        # would discard anyway; keep the original base sample if filtering would leave no seed
        active_filter = get_track_filter()
        if active_filter is not None:
            base_tracks = [t for t in base_tracks if active_filter.allows(t)] or base_tracks
        dynamic_tracks: set[Track] = set()
        for base_track in base_tracks:
            for allow_lookup in (False, True):
                try:
                    similar = await self.mass.music.tracks.similar_tracks(
                        base_track.item_id,
                        base_track.provider,
                        allow_lookup=allow_lookup,
                        preferred_provider_instances=preferred_provider_instances,
                    )
                except MusicAssistantError:
                    continue
                if similar:
                    break
            else:
                # A base track without a similar-tracks-capable provider should not abort generation.
                continue
            for track in similar:
                if track in base_tracks or track.duration > RADIO_TRACK_MAX_DURATION_SECS:
                    continue
                if active_filter is not None and not active_filter.allows(track):
                    continue
                dynamic_tracks.add(track)
            if len(dynamic_tracks) >= DYNAMIC_RADIO_DYNAMIC_TARGET:
                break

        result: list[Track] = []
        dynamic_tracks_list = list(dynamic_tracks)
        if include_base_tracks:
            result.append(base_tracks[0])
            if len(base_tracks) > 1:
                for base_track in base_tracks[1:]:
                    result.append(base_track)
                    if len(dynamic_tracks_list) > 2:
                        result += random.sample(dynamic_tracks_list, 2)
                    else:
                        result += dynamic_tracks_list
        remaining_dynamic = [t for t in dynamic_tracks_list if t not in result]
        if remaining_dynamic:
            result += random.sample(remaining_dynamic, min(len(remaining_dynamic), target_size))
        return result

    async def _resolve_seed(self, prov_playlist_id: str) -> MediaItemType:
        """Resolve a radio-playlist item id (the seed's URI, raw or url-encoded) to the seed item."""
        seed_uri = prov_playlist_id if "://" in prov_playlist_id else unquote(prov_playlist_id)
        try:
            seed = await self.mass.music.get_item_by_uri(seed_uri)
        except MusicAssistantError as err:
            raise MediaNotFoundError(f"Radio playlist seed not found: {seed_uri}") from err
        if isinstance(seed, BrowseFolder):
            raise MediaNotFoundError(f"Radio playlist seed is not a media item: {seed_uri}")
        return seed
