"""Pocket Casts music provider for Music Assistant."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import (
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ProviderUnavailableError,
    ResourceTemporarilyUnavailable,
    RetriesExhausted,
)
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemMetadata,
    MediaItemType,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    SearchResults,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant import MusicAssistant
from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.controllers.cache import use_cache
from music_assistant.models.music_provider import MusicProvider

from .api_client import PocketCastsClient

if TYPE_CHECKING:
    from datetime import datetime

    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.models import ProviderInstanceType

LOGGER = logging.getLogger(__name__)

FULLY_PLAYED_THRESHOLD = 0.9
SPECIAL_FOLDERS = ("up_next", "new_releases", "in_progress", "starred", "history")

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_PODCASTS,
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.LIBRARY_PODCASTS_EDIT,
}

BROWSE_FOLDER_ICONS: dict[str, str] = {
    "up_next": (
        "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIH"
        "ZpZXdCb3g9IjAgMCAyNCAyNCIgd2lkdGg9IjI0MCIgaGVpZ2h0PSIyNDAiPgogICAgPHBhdGggZmls"
        "bD0iIzhGOTdBNCIgZD0iTTMgMi45NzE1OUMzIDIuNTY0OSAzLjQ1OTY4IDIuMzI4MzQgMy43OTA2Mi"
        "AyLjU2NDcyTDkuNDMwMzkgNi41OTMxM0M5LjcwOTU2IDYuNzkyNTQgOS43MDk1NiA3LjIwNzQ1IDku"
        "NDMwMzkgNy40MDY4NkwzLjc5MDYyIDExLjQzNTNDMy40NTk2OSAxMS42NzE2IDMgMTEuNDM1MSAzID"
        "ExLjAyODRWMi45NzE1OVoiLz4KICAgIDxwYXRoIGZpbGw9IiM4Rjk3QTQiIG9wYWNpdHk9IjAuNS"
        "IgZD0iTTEyIDdDMTIgNi40NDc3MiAxMi40NDc3IDYgMTMgNkgyMUMyMS41NTIzIDYgMjIgNi40NDc3"
        "MiAyMiA3QzIyIDcuNTUyMjggMjEuNTUyMyA4IDIxIDhIMTNDMTIuNDQ3NyA4IDEyIDcuNTUyMjggMT"
        "IgN1pNOSAxMkM5IDExLjQ0NzcgOS40NDc3MiAxMSAxMCAxMUgyMUMyMS41NTIzIDExIDIyIDExLjQ0Nz"
        "cgMjIgMTJDMjIgMTIuNTUyMyAyMS41NTIzIDEzIDIxIDEzSDEwQzkuNDQ3NzIgMTMgOSAxMi41NTIz"
        "IDkgMTJaTTEwIDE2QzkuNDQ3NzIgMTYgOSAxNi40NDc3IDkgMTdDOSAxNy41NTIzIDkuNDQ3NzIgMT"
        "ggMTAgMThIMjFDMjEuNTUyMyAxOCAyMiAxNy41NTIzIDIyIDE3QzIyIDE2LjQ0NzcgMjEuNTUyMyAx"
        "NiAyMSAxNkgxMFoiLz4KPC9zdmc+Cg=="
    ),
    "new_releases": (
        "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIH"
        "ZpZXdCb3g9IjAgMCAyNCAyNCIgd2lkdGg9IjI0MCIgaGVpZ2h0PSIyNDAiPgogICAgPGcgZmlsbD0i"
        "IzhGOTdBNCIgdHJhbnNmb3JtPSJ0cmFuc2xhdGUoMCwgMSkiPgogICAgICAgIDxwYXRoIGQ9Ik0xNC"
        "43OTM1IDQuNTU3MzVMMTUuNzc0MSAwLjc4OTIwOUMxNS45Njg4IDAuMDQxMDUwOSAxNy4wMzExIDAu"
        "MDQxMDcxOSAxNy4yMjU4IDAuNzg5MjM3TDE4LjIwNjIgNC41NTczMkMxOC4yNzcgNC44MjkxNSAxOC"
        "40OTM3IDUuMDM4NjggMTguNzY3NyA1LjEwMDIzTDIxLjc0MTkgNS43NjgyM0MyMi41MjI4IDUuOTQz"
        "NjIgMjIuNTIyOCA3LjA1NjM5IDIxLjc0MTkgNy4yMzE3N0wxOC43Njc3IDcuODk5NzdDMTguNDkzNy"
        "A3Ljk2MTMyIDE4LjI3NyA4LjE3MDg1IDE4LjIwNjIgOC40NDI2OEwxNy4yMjU4IDEyLjIxMDhDMTcu"
        "MDMxMSAxMi45NTg5IDE1Ljk2ODggMTIuOTU4OSAxNS43NzQxIDEyLjIxMDhMMTQuNzkzNSA4LjQ0Mj"
        "Y1QzE0LjcyMjcgOC4xNzA4MyAxNC41MDYxIDcuOTYxMzIgMTQuMjMyIDcuODk5NzdMMTEuMjU4IDcu"
        "MjMxNzdDMTAuNDc3MSA3LjA1NjM5IDEwLjQ3NzEgNS45NDM2MiAxMS4yNTggNS43NjgyNEwxNC4yMz"
        "IgNS4xMDAyM0MxNC41MDYxIDUuMDM4NjggMTQuNzIyNyA0LjgyOTE3IDE0Ljc5MzUgNC41NTczNVoi"
        "Lz4KICAgICAgICA8cGF0aCBvcGFjaXR5PSIwLjgiIGQ9Ik01LjI5OTQgOS4yNjgzTDYuMDMwNjYgNy"
        "4yNzc2M0M2LjE5MTEyIDYuODQwODUgNi44MDg4OCA2Ljg0MDg1IDYuOTY5MzQgNy4yNzc2M0w3Ljcw"
        "MDYgOS4yNjgzQzcuNzU0MjUgOS40MTQzNSA3Ljg3Mjg0IDkuNTI3MSA4LjAyMTQxIDkuNTczMzJMOS"
        "40NjU0MSAxMC4wMjI2QzkuOTM0MDMgMTAuMTY4MyA5LjkzNDAzIDEwLjgzMTYgOS40NjU0MSAxMC45"
        "Nzc0TDguMDIxNCAxMS40MjY3QzcuODcyODMgMTEuNDcyOSA3Ljc1NDI1IDExLjU4NTYgNy43MDA2ID"
        "ExLjczMTdMNi45NjkzMyAxMy43MjI0QzYuODA4ODggMTQuMTU5MiA2LjE5MTEyIDE0LjE1OTIgNi4w"
        "MzA2NiAxMy43MjI0TDUuMjk5NCAxMS43MzE3QzUuMjQ1NzUgMTEuNTg1NiA1LjEyNzE3IDExLjQ3Mj"
        "kgNC45Nzg2IDExLjQyNjdMMy41MzQ1OSAxMC45Nzc0QzMuMDY1OTcgMTAuODMxNiAzLjA2NTk3IDEw"
        "LjE2ODMgMy41MzQ1OSAxMC4wMjI2TDQuOTc4NTkgOS41NzMzMkM1LjEyNzE2IDkuNTI3MSA1LjI0NT"
        "c1IDkuNDE0MzUgNS4yOTk0IDkuMjY4M1oiLz4KICAgICAgICA8cGF0aCBvcGFjaXR5PSIwLjYiIGQ9"
        "Ik0xMC42ODgyIDE2LjAxNEwxMS41MjQ3IDEzLjQ1NDNDMTEuNjc0OSAxMi45OTQ3IDEyLjMyNTEgMT"
        "IuOTk0NyAxMi40NzUzIDEzLjQ1NDNMMTMuMzExOCAxNi4wMTRDMTMuMzYwNCAxNi4xNjI3IDEzLjQ3"
        "NTcgMTYuMjggMTMuNjIzNSAxNi4zMzEyTDE1LjYzNSAxNy4wMjc1QzE2LjA4MzYgMTcuMTgyOCAxNi"
        "4wODM2IDE3LjgxNzIgMTUuNjM1IDE3Ljk3MjVMMTMuNjIzNSAxOC42Njg4QzEzLjQ3NTcgMTguNzIg"
        "MTMuMzYwNCAxOC44MzczIDEzLjMxMTggMTguOTg2TDEyLjQ3NTMgMjEuNTQ1N0MxMi4zMjUxIDIyLj"
        "AwNTMgMTEuNjc0OSAyMi4wMDUzIDExLjUyNDcgMjEuNTQ1N0wxMC42ODgyIDE4Ljk4NkMxMC42Mzk2"
        "IDE4LjgzNzMgMTAuNTI0MyAxOC43MiAxMC4zNzY1IDE4LjY2ODhMOC4zNjQ5OCAxNy45NzI1QzcuOT"
        "E2MzggMTcuODE3MiA3LjkxNjM5IDE3LjE4MjggOC4zNjQ5OCAxNy4wMjc1TDEwLjM3NjUgMTYuMzMx"
        "MkMxMC41MjQzIDE2LjI4IDEwLjYzOTYgMTYuMTYyNyAxMC42ODgyIDE2LjAxNFoiLz4KICAgIDwvZz"
        "4KPC9zdmc+Cg=="
    ),
    "in_progress": (
        "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIH"
        "ZpZXdCb3g9IjAgMCAyNCAyNCIgd2lkdGg9IjI0MCIgaGVpZ2h0PSIyNDAiPgogICAgPHBhdGggZmls"
        "bD0iIzhGOTdBNCIgb3BhY2l0eT0iMC41IiBkPSJNNCAxMkM0IDE2LjQxODMgNy41ODE3MiAyMCAxMi"
        "AyMEMxNi40MTgzIDIwIDIwIDE2LjQxODMgMjAgMTJDMjAgNy41ODE3MiAxNi40MTgzIDQgMTIgNEM3"
        "LjU4MTcyIDQgNCA3LjU4MTcyIDQgMTJaTTE4IDEyQzE4IDE1LjMxMzcgMTUuMzEzNyAxOCAxMiAxOE"
        "M4LjY4NjI5IDE4IDYgMTUuMzEzNyA2IDEyQzYgOC42ODYyOSA4LjY4NjI5IDYgMTIgNkMxNS4zMTM3"
        "IDYgMTggOC42ODYyOSAxOCAxMloiLz4KICAgIDxwYXRoIGZpbGw9IiM4Rjk3QTQiIGQ9Ik0xNi45Mj"
        "UzIDE4LjMwNDFDMjAuNDA2OSAxNS41ODM5IDIxLjAyNDMgMTAuNTU2NCAxOC4zMDQxIDcuMDc0NzJD"
        "MTYuODI2OSA1LjE4Mzk0IDE0LjYxNjcgNC4wODMyNyAxMi4yNjUgNC4wMDQyNEMxMS43MTMxIDMuOT"
        "g1NjkgMTEuMjUwNiA0LjQxODExIDExLjIzMiA0Ljk3MDA4QzExLjIxMzUgNS41MjIwNiAxMS42NDU5"
        "IDUuOTg0NTYgMTIuMTk3OSA2LjAwMzExQzEzLjk2MzkgNi4wNjI0NiAxNS42MTkzIDYuODg2ODYgMT"
        "YuNzI4MSA4LjMwNjA1QzE4Ljc2ODIgMTAuOTE3MyAxOC4zMDUyIDE0LjY4OCAxNS42OTQgMTYuNzI4"
        "MUMxNS4yNTg4IDE3LjA2ODEgMTUuMTgxNiAxNy42OTY1IDE1LjUyMTYgMTguMTMxOEMxNS44NjE2ID"
        "E4LjU2NyAxNi40OTAxIDE4LjY0NDEgMTYuOTI1MyAxOC4zMDQxWiIvPgo8L3N2Zz4K"
    ),
    "starred": (
        "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIH"
        "ZpZXdCb3g9IjAgMCAyNCAyNCIgd2lkdGg9IjI0MCIgaGVpZ2h0PSIyNDAiPgogICAgPHBhdGggZmls"
        "bD0iIzhGOTdBNCIgdHJhbnNmb3JtPSJ0cmFuc2xhdGUoMSwgLTAuNSkiIGQ9Ik0xMS41ODMxIDE3Lj"
        "Q3NzZMOC4wNzM4NCAxOS4yOTMzQzcuMDk0MTMgMTkuODAwMiA2LjQ0NjgxIDE5LjMxOTcgNi42MjQ2"
        "NiAxOC4yNDA0TDcuMjY3MDcgMTQuMzQxOEw0LjQ1NTgxIDExLjU2NTRDMy42NzA5NyAxMC43OTAzID"
        "MuOTI3OTEgMTAuMDI2MiA1LjAwOTM1IDkuODYxNzdMOC45MTU2NSA5LjI2ODAxTDEwLjY4NzUgNS43"
        "MzYzOEMxMS4xODIxIDQuNzUwNDIgMTEuOTg4MiA0Ljc1ODY3IDEyLjQ3ODggNS43MzYzOEwxNC4yNT"
        "A2IDkuMjY4MDFMMTguMTU2OSA5Ljg2MTc3QzE5LjI0NzQgMTAuMDI3NSAxOS40ODg3IDEwLjc5Njgg"
        "MTguNzEwNCAxMS41NjU0TDE1Ljg5OTIgMTQuMzQxOEwxNi41NDE2IDE4LjI0MDRDMTYuNzIwOSAxOS"
        "4zMjg4IDE2LjA2MzkgMTkuNzk2IDE1LjA5MjQgMTkuMjkzM0wxMS41ODMxIDE3LjQ3NzZaIi8+Cjwv"
        "c3ZnPgo="
    ),
    "history": (
        "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIH"
        "ZpZXdCb3g9IjAgMCAyNCAyNCIgd2lkdGg9IjI0MCIgaGVpZ2h0PSIyNDAiPgogICAgPHBhdGggZmls"
        "bD0iIzhGOTdBNCIgb3BhY2l0eT0iMC41IiBjbGlwLXJ1bGU9ImV2ZW5vZGQiIGQ9Ik0xMC41IDEzQz"
        "EwLjUgMTMuNTUyMyAxMC45NDc3IDE0IDExLjUgMTRIMTQuNUMxNS4wNTIzIDE0IDE1LjUgMTMuNTUy"
        "MyAxNS41IDEzQzE1LjUgMTIuNDQ3NyAxNS4wNTIzIDEyIDE0LjUgMTJIMTIuNUwxMi41IDEwQzEyLj"
        "UgOS40NDc3MiAxMi4wNTIzIDkgMTEuNSA5QzEwLjk0NzcgOSAxMC41IDkuNDQ3NzIgMTAuNSAxMFYx"
        "M1oiLz4KICAgIDxwYXRoIGZpbGw9IiM4Rjk3QTQiIGZpbGwtcnVsZT0iZXZlbm9kZCIgY2xpcC1y"
        "dWxlPSJldmVub2RkIiBkPSJNMTIgMThDMTUuMzEzNyAxOCAxOCAxNS4zMTM3IDE4IDEyQzE4IDguNj"
        "g2MjkgMTUuMzEzNyA2IDEyIDZDOC42ODYyOSA2IDYgOC42ODYyOSA2IDEyQzYgMTUuMzEzNyA4LjY4"
        "NjI5IDE4IDEyIDE4Wk0xMiAyMEMxNi40MTgzIDIwIDIwIDE2LjQxODMgMjAgMTJDMjAgNy41ODE3Mi"
        "AxNi40MTgzIDQgMTIgNEM3LjU4MTcyIDQgNCA3LjU4MTcyIDQgMTJDNCAxNi40MTgzIDcuNTgxNzIg"
        "MjAgMTIgMjBaIi8+Cjwvc3ZnPgo="
    ),
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return PocketCastsProvider(mass, manifest, config, SUPPORTED_FEATURES)


class PocketCastsProvider(MusicProvider):
    """Provider for Pocket Casts podcast service."""

    _client: PocketCastsClient
    # episode uuids already mirrored to Pocket Casts Up Next/history this session, keyed as a
    # set since multi-room playback can have several episodes in progress on one instance
    _announced_episodes: set[str]

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        return ()

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        email = self.get_setup_value(CONF_USERNAME)
        password = self.get_setup_value(CONF_PASSWORD)
        if not email or not password:
            raise LoginFailed("Email and password are required for Pocket Casts")
        self._announced_episodes = set()
        self._client = PocketCastsClient(self.mass.http_session, self.logger)
        await self._client.login(str(email), str(password))

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast]:
        """Get all podcasts from the user's library."""
        for podcast_data in await self._client.get_subscribed_podcasts():
            yield self._convert_podcast(podcast_data)

    async def library_add(self, item: MediaItemType) -> bool:
        """
        Subscribe to a podcast.

        :param item: The media item to add to the library.
        """
        if not isinstance(item, Podcast):
            return await super().library_add(item)
        await self._client.subscribe_podcast(item.item_id)
        return True

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """
        Unsubscribe from a podcast.

        :param prov_item_id: The provider item ID to remove from the library.
        :param media_type: The media type of the item.
        """
        if media_type != MediaType.PODCAST:
            return await super().library_remove(prov_item_id, media_type)
        await self._client.unsubscribe_podcast(prov_item_id)
        return True

    @use_cache(3600 * 24)
    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """
        Get full details for a podcast.

        :param prov_podcast_id: The provider podcast id.
        """
        podcast_data = await self._client.get_podcast(prov_podcast_id)
        if not podcast_data:
            raise MediaNotFoundError(
                f"podcast://{prov_podcast_id} not found on provider {self.domain}"
            )
        return self._convert_podcast(podcast_data)

    async def get_podcast_episodes(self, prov_podcast_id: str) -> AsyncGenerator[PodcastEpisode]:
        """
        Get all episodes for a podcast, enriched with playback status.

        :param prov_podcast_id: The provider podcast id.
        """
        # fetch episode metadata and user status in parallel
        episodes, in_progress, history = await asyncio.gather(
            self._client.get_podcast_episodes(prov_podcast_id),
            self._client.get_in_progress_episodes(),
            self._client.get_history(),
        )
        in_progress_map = {ep.get("uuid"): ep for ep in in_progress}
        history_map = {ep.get("uuid"): ep for ep in history}

        for episode_data in episodes:
            episode_item = self._convert_episode(episode_data, prov_podcast_id)
            if episode_item:
                self._enrich_episode_with_status(
                    episode_item, episode_data, in_progress_map, history_map
                )
                yield episode_item

    async def browse(self, path: str) -> list[MediaItemType | BrowseFolder]:
        """
        Browse this provider's items.

        :param path: The browse path to resolve.
        """
        item_path = path.split("://", 1)[1] if "://" in path else path

        if not item_path:
            # root level - special folders followed by subscribed podcasts
            items: list[MediaItemType | BrowseFolder] = list(self._create_browse_folders())
            for podcast_data in await self._client.get_subscribed_podcasts():
                items.append(self._convert_podcast(podcast_data))
            return items

        if item_path in SPECIAL_FOLDERS:
            return await self._get_special_folder_episodes(item_path)

        return [episode async for episode in self.get_podcast_episodes(item_path)]

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Get streamable URL and details for the given media item.

        :param item_id: The episode item id (format: podcast_uuid:episode_uuid).
        :param media_type: The media type of the item.
        """
        _, episode_uuid = item_id.split(":", 1)
        episode_data = await self._client.get_episode_details(episode_uuid)

        url = episode_data.get("url", "")
        if not url:
            raise MediaNotFoundError(f"No URL found for episode {item_id}")

        return StreamDetails(
            item_id=item_id,
            provider=self.instance_id,
            audio_format=AudioFormat(
                content_type=ContentType.try_parse(episode_data.get("fileType", "audio/mpeg")),
            ),
            media_type=MediaType.PODCAST_EPISODE,
            stream_type=StreamType.HTTP,
            path=url,
            duration=episode_data.get("duration"),
            can_seek=True,
            allow_seek=True,
        )

    @use_cache(3600 * 24)
    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """
        Search for podcasts.

        :param search_query: The search query.
        :param media_types: The media types to include in the search.
        :param limit: The maximum number of items to return per type.
        """
        results = SearchResults()
        if media_types and MediaType.PODCAST not in media_types:
            return results
        podcasts = await self._client.search_podcasts(search_query)
        results.podcasts = [self._convert_podcast(podcast) for podcast in podcasts[:limit]]
        return results

    @use_cache(3600)
    async def get_podcast_episode(self, prov_item_id: str) -> PodcastEpisode:
        """
        Get full details for a podcast episode.

        :param prov_item_id: The episode item id (format: podcast_uuid:episode_uuid).
        """
        podcast_uuid, episode_uuid = prov_item_id.split(":", 1)
        episode_data = await self._client.get_episode_details(episode_uuid)
        episode_item = self._convert_episode(episode_data, podcast_uuid)
        if episode_item is None:
            raise MediaNotFoundError(f"Episode {episode_uuid} not found in podcast {podcast_uuid}")

        played_up_to = episode_data.get("playedUpTo", 0)
        duration = episode_data.get("duration", 0)
        playing_status = episode_data.get("playingStatus", 1)  # 1=unplayed, 2=in_progress, 3=played
        if duration > 0:
            episode_item.duration = duration
        completed = playing_status == 3 or (
            duration > 0 and (played_up_to / duration) > FULLY_PLAYED_THRESHOLD
        )
        episode_item.fully_played = completed
        episode_item.resume_position_ms = 0 if completed else played_up_to * 1000

        return episode_item

    async def get_resume_position(
        self, item_id: str, media_type: MediaType
    ) -> tuple[bool, int, datetime | None]:
        """
        Return the (fully_played, position_ms, timestamp) resume point for an episode.

        PocketCasts does not expose a reliable last-played timestamp, so the timestamp
        is always None.

        :param item_id: The episode item id (format: podcast_uuid:episode_uuid).
        :param media_type: The media type (should be PODCAST_EPISODE).
        """
        _, episode_uuid = item_id.split(":", 1)

        try:
            in_progress = await self._client.get_in_progress_episodes()
        except (ProviderUnavailableError, ResourceTemporarilyUnavailable, RetriesExhausted) as err:
            # resume is best-effort; a transient failure should not break playback
            LOGGER.warning("Could not fetch resume position for %s: %s", episode_uuid, err)
            return (False, 0, None)

        for ep in in_progress:
            if ep.get("uuid") == episode_uuid:
                played_up_to = int(ep.get("playedUpTo", 0))  # seconds from API
                duration = int(ep.get("duration", 0))
                fully_played = duration > 0 and (played_up_to / duration) > FULLY_PLAYED_THRESHOLD
                LOGGER.debug(
                    "Resume position for %s: %d ms (fully_played=%s)",
                    episode_uuid,
                    played_up_to * 1000,
                    fully_played,
                )
                return (fully_played, played_up_to * 1000, None)

        LOGGER.debug("No in-progress entry for %s; resuming from start", episode_uuid)
        return (False, 0, None)

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
        Sync playback progress for a podcast episode back to Pocket Casts.

        Called by the Queue controller when a track is played, stopped/skipped, and
        periodically while playing.

        :param media_type: The media type of the played item.
        :param prov_item_id: The provider item id (format: podcast_uuid:episode_uuid).
        :param fully_played: Whether the episode was played to the end.
        :param position: Last known position in seconds.
        :param media_item: The full media item details.
        :param is_playing: Whether the episode is currently playing.
        """
        if media_type != MediaType.PODCAST_EPISODE or not isinstance(media_item, PodcastEpisode):
            return
        podcast_uuid, episode_uuid = prov_item_id.split(":", 1)

        # MA reports fully_played=True when an episode is skipped/stopped, not only when it
        # truly ends, so confirm completion against the real position before marking it played.
        duration = media_item.duration or 0
        completed = fully_played and duration > 0 and position >= duration * FULLY_PLAYED_THRESHOLD
        if completed:
            self._announced_episodes.discard(episode_uuid)
            await self._client.mark_episode_played(podcast_uuid, episode_uuid)
            await self._client.remove_from_up_next(episode_uuid)
            await self._client.archive_episode(podcast_uuid, episode_uuid, archive=True)
        elif position == 0 and not is_playing:
            # the user explicitly marked the episode as unplayed
            self._announced_episodes.discard(episode_uuid)
            await self._client.mark_episode_unplayed(podcast_uuid, episode_uuid)
            await self._client.archive_episode(podcast_uuid, episode_uuid, archive=False)
        else:
            # on_played fires every progress tick, so mirror the start to Up Next/history only
            # once per session - re-announcing each tick would re-bump Up Next and spam the API.
            # A resume within the same session is intentionally not re-announced.
            if is_playing and episode_uuid not in self._announced_episodes:
                self._announced_episodes.add(episode_uuid)
                await self._announce_playback_start(podcast_uuid, episode_uuid, media_item)
            await self._client.update_episode_progress(podcast_uuid, episode_uuid, position)

    def _convert_podcast(self, podcast_data: dict[str, Any]) -> Podcast:
        """
        Convert raw Pocket Casts podcast data to a Podcast object.

        :param podcast_data: Raw podcast data from the subscribed-list or full-podcast endpoint.
        """
        uuid = podcast_data["uuid"]
        return Podcast(
            item_id=uuid,
            provider=self.instance_id,
            name=podcast_data.get("title", ""),
            provider_mappings={
                ProviderMapping(
                    item_id=uuid,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            metadata=MediaItemMetadata(
                description=podcast_data.get("description"),
                images=UniqueList(
                    [
                        MediaItemImage(
                            type=ImageType.THUMB,
                            path=f"https://static.pocketcasts.com/discover/images/280/{uuid}.jpg",
                            provider=self.instance_id,
                            remotely_accessible=True,
                        )
                    ]
                ),
            ),
        )

    def _convert_episode(
        self, episode_data: dict[str, Any], podcast_uuid: str
    ) -> PodcastEpisode | None:
        """
        Convert Pocket Casts episode data to a PodcastEpisode object.

        Returns None when the data has no episode UUID to key on.

        :param episode_data: Raw episode data dict from the API.
        :param podcast_uuid: The UUID of the parent podcast.
        """
        episode_uuid = episode_data.get("uuid")
        if not episode_uuid:
            return None

        # this is fed by two endpoints with different field schemas: the full-podcast JSON
        # uses snake_case (file_type) while /user/episode uses camelCase (fileType,
        # episodeNumber). Neither carries show notes or episode artwork, so the description is
        # left empty and the parent podcast image is used for every episode.
        item_id = f"{podcast_uuid}:{episode_uuid}"
        file_type = episode_data.get("fileType") or episode_data.get("file_type", "audio/mpeg")
        episode_item = PodcastEpisode(
            item_id=item_id,
            provider=self.instance_id,
            name=episode_data.get("title", "Unknown Episode"),
            podcast=ItemMapping(
                media_type=MediaType.PODCAST,
                item_id=podcast_uuid,
                provider=self.instance_id,
                name="",
            ),
            position=episode_data.get("episodeNumber", 0),
            provider_mappings={
                ProviderMapping(
                    item_id=item_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=AudioFormat(content_type=ContentType.try_parse(file_type)),
                    url=episode_data.get("url", ""),
                )
            },
        )
        if episode_data.get("duration"):
            episode_item.duration = int(episode_data["duration"])
        if title := episode_data.get("title"):
            episode_item.metadata.label = title
        episode_item.metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=f"https://static.pocketcasts.com/discover/images/280/{podcast_uuid}.jpg",
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            ]
        )
        return episode_item

    def _enrich_episode_with_status(
        self,
        episode_item: PodcastEpisode,
        episode_data: dict[str, Any],
        in_progress_map: dict[str | None, dict[str, Any]],
        history_map: dict[str | None, dict[str, Any]],
    ) -> None:
        """
        Apply playback status to a PodcastEpisode from the in-progress/history data.

        :param episode_item: The episode object to enrich in place.
        :param episode_data: Raw episode data dict from the API.
        :param in_progress_map: UUID-keyed map of in-progress episode data.
        :param history_map: UUID-keyed map of listen-history episode data.
        """
        episode_uuid = episode_data.get("uuid")
        # history is "recently played", not "completed" - rely on the entry's real progress,
        # never on mere history membership. Both fields are always set so the library sync can
        # clear a stale completed/resume value (it only updates when both are non-None).
        status_data = in_progress_map.get(episode_uuid) or history_map.get(episode_uuid) or {}
        played_up_to = status_data.get("playedUpTo", 0)
        duration = status_data.get("duration") or episode_data.get("duration", 0)
        completed = status_data.get("playingStatus") == 3 or (
            duration > 0 and (played_up_to / duration) > FULLY_PLAYED_THRESHOLD
        )
        episode_item.fully_played = completed
        episode_item.resume_position_ms = 0 if completed else played_up_to * 1000

    async def _get_special_folder_episodes(
        self, folder_name: str
    ) -> list[MediaItemType | BrowseFolder]:
        """
        Get episodes for a special browse folder.

        :param folder_name: Name of the special folder (up_next, new_releases, etc.)
        """
        folder_getters = {
            "up_next": self._client.get_up_next_episodes,
            "new_releases": self._client.get_new_releases,
            "in_progress": self._client.get_in_progress_episodes,
            "starred": self._client.get_starred_episodes,
            "history": self._client.get_history,
        }
        episode_list = await folder_getters[folder_name]()

        items: list[MediaItemType | BrowseFolder] = []
        for episode_data in episode_list:
            # the podcast reference is a string on some endpoints and an object on others
            podcast_field = episode_data.get("podcast")
            podcast_uuid: str | None
            if isinstance(podcast_field, str):
                podcast_uuid = podcast_field
            elif isinstance(podcast_field, dict):
                podcast_uuid = podcast_field.get("uuid")
            else:
                podcast_uuid = episode_data.get("podcastUuid")

            if podcast_uuid and (episode_item := self._convert_episode(episode_data, podcast_uuid)):
                items.append(episode_item)
        return items

    def _create_browse_folders(self) -> list[BrowseFolder]:
        """Create special browse folders for root level."""
        folders = [
            ("up_next", "Up Next"),
            ("new_releases", "New Releases"),
            ("in_progress", "In Progress"),
            ("starred", "Starred"),
            ("history", "History"),
        ]
        return [
            BrowseFolder(
                item_id=folder_id,
                provider=self.instance_id,
                path=f"{self.instance_id}://{folder_id}",
                name=name,
                image=MediaItemImage(
                    type=ImageType.THUMB,
                    path=BROWSE_FOLDER_ICONS[folder_id],
                    provider=self.instance_id,
                    remotely_accessible=True,
                ),
            )
            for folder_id, name in folders
        ]

    async def _announce_playback_start(
        self, podcast_uuid: str, episode_uuid: str, episode: PodcastEpisode
    ) -> None:
        """
        Mirror a playback start to Pocket Casts by adding the episode to Up Next and history.

        :param podcast_uuid: The podcast UUID.
        :param episode_uuid: The episode UUID.
        :param episode: The episode that started playing.
        """
        # source the url from the already-loaded item so no extra API call is needed; filtered
        # to our own mapping since merged library items can carry other providers' mappings
        url = next(
            (
                mapping.url
                for mapping in episode.provider_mappings
                if mapping.provider_instance == self.instance_id and mapping.url
            ),
            "",
        )
        await self._client.play_now(
            episode_uuid=episode_uuid, podcast_uuid=podcast_uuid, title=episode.name, url=url
        )
        await self._client.add_to_history(
            episode_uuid=episode_uuid, podcast_uuid=podcast_uuid, title=episode.name, url=url
        )
