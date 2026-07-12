"""PlaylistsMixin for Audiobookshelf."""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import TYPE_CHECKING

from aioaudiobookshelf.exceptions import (
    NotFoundError as AbsNotFoundError,
)
from aioaudiobookshelf.schema.calls_playlists import (
    CreatePlaylistParameters as AbsCreatePlaylistParameters,
)
from aioaudiobookshelf.schema.playlist import PlaylistItem as AbsPlaylistItem
from aioaudiobookshelf.schema.playlist import (
    PlaylistItemExpandedBook as AbsPlaylistItemExpandedBook,
)
from aioaudiobookshelf.schema.playlist import (
    PlaylistItemExpandedPodcast as AbsPlaylistItemExpandedPodcast,
)
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError

from music_assistant.constants import PlaylistPlayableItem
from music_assistant.providers.audiobookshelf.constants import CONF_URL
from music_assistant.providers.audiobookshelf.helpers import NarratorHelper, handle_refresh_token
from music_assistant.providers.audiobookshelf.mixins.mixin_base import MixinBase
from music_assistant.providers.audiobookshelf.parsers import (
    parse_audiobook,
    parse_playlist,
    parse_podcast_episode,
)

if TYPE_CHECKING:
    from aioaudiobookshelf.schema.library import (
        LibraryItemExpandedBook as AbsLibraryItemExpandedBook,
    )
    from music_assistant_models.media_items import (
        MediaItemType,
        Playlist,
    )


class PlaylistMixin(MixinBase):
    """PlaylistMixin for Audiobookshelf."""

    if TYPE_CHECKING:
        # part of audiobooks mixin
        async def _get_audiobook_narrators(
            self, book: AbsLibraryItemExpandedBook
        ) -> set[NarratorHelper]: ...

    async def get_library_playlists(self) -> AsyncGenerator[Playlist]:
        """Retrieve playlists from abs."""
        for playlist_dict, media_type in zip(
            [
                self.libraries.playlists_audiobooks,
                self.libraries.playlists_podcasts,
            ],
            [MediaType.AUDIOBOOK, MediaType.PODCAST_EPISODE],
            strict=True,
        ):
            for library_id in playlist_dict:
                async for response in self._client.get_library_playlists(library_id=library_id):
                    if not response.results:
                        break
                    for abs_playlist in response.results:
                        playlist_dict[library_id].add(abs_playlist.id_)
                        yield parse_playlist(
                            abs_playlist=abs_playlist,
                            instance_id=self.instance_id,
                            domain=self.domain,
                            token=self._client.token,
                            base_url=str(self.config.get_value(CONF_URL)).rstrip("/"),
                            owner=self.abs_username,
                            media_type=media_type,
                        )

    @handle_refresh_token
    async def get_playlist_tracks(
        self, prov_playlist_id: str, page: int = 0
    ) -> list[PlaylistPlayableItem]:
        """Get playlist items."""
        if page > 0:
            # no pages in abs' playlist items api
            return []
        playlist_items: list[PlaylistPlayableItem] = []
        try:
            playlist = await self._client.get_playlist(playlist_id=prov_playlist_id)
        except AbsNotFoundError:
            # this is an edge case - abs deletes the playlist automatically, when
            # the last item is removed, but the frontend then still asks for tracks.
            # Due to our guard, we also block playlist removal via a socket update, so we can
            # do that here
            if ma_playlist := await self.mass.music.get_library_item_by_prov_id(
                media_type=MediaType.PLAYLIST,
                item_id=prov_playlist_id,
                provider_instance_id_or_domain=self.instance_id,
            ):
                self.logger.debug(
                    "Removing a playlist with no tracks from MA library, %s", ma_playlist.name
                )
                await self.mass.music.remove_item_from_library(
                    media_type=MediaType.PLAYLIST, library_item_id=ma_playlist.item_id
                )
            return []
        for item in playlist.items:
            if isinstance(item, AbsPlaylistItemExpandedBook):
                progress = await self._client.get_my_media_progress(item_id=item.library_item.id_)
                playlist_items.append(
                    parse_audiobook(
                        abs_audiobook=item.library_item,
                        instance_id=self.instance_id,
                        audiobook_narrators=await self._get_audiobook_narrators(item.library_item),
                        domain=self.domain,
                        token=self._client.token,
                        media_progress=progress,
                        base_url=str(self.config.get_value(CONF_URL)).rstrip("/"),
                    )
                )
            elif isinstance(item, AbsPlaylistItemExpandedPodcast):
                progress = await self._client.get_my_media_progress(
                    item_id=item.library_item.id_, episode_id=item.episode_id
                )
                playlist_items.append(
                    parse_podcast_episode(
                        episode=item.episode,
                        prov_podcast_id=item.library_item.id_,
                        prov_podcast_name=item.library_item.media.metadata.title,
                        fallback_episode_cnt=None,
                        instance_id=self.instance_id,
                        domain=self.domain,
                        token=self._client.token,
                        base_url=str(self.config.get_value(CONF_URL)).rstrip("/"),
                        media_progress=progress,
                        cover_path=item.library_item.media.cover_path,
                        cover_version=item.library_item.updated_at,
                    )
                )
        for cnt, playlist_item in enumerate(playlist_items):
            playlist_item.position = cnt

        return playlist_items

    @handle_refresh_token
    async def create_playlist(self, name: str, media_types: set[MediaType]) -> Playlist:
        """
        Create a playlist in ABS.

        This method may only be called, if we have not more than one library per media item in ABS.
        """
        error_msg = (
            "The ABS provider only supports playlists of _either_ audiobooks, or podcast episodes."
        )
        if len(media_types) != 1:
            raise InvalidDataError(error_msg)
        media_type = next(iter(media_types))
        if media_type == MediaType.AUDIOBOOK:
            library_id = next(iter(self.libraries.audiobooks.keys()))
        elif media_type == MediaType.PODCAST_EPISODE:
            library_id = next(iter(self.libraries.podcasts.keys()))
        else:
            raise InvalidDataError(error_msg)
        async with self.playlist_lock:
            self.playlist_last = time.time()
            abs_playlist = await self._client.create_playlist(
                parameters=AbsCreatePlaylistParameters(name=name, library_id=library_id)
            )
            return parse_playlist(
                abs_playlist=abs_playlist,
                instance_id=self.instance_id,
                domain=self.domain,
                token=self._client.token,
                base_url=str(self.config.get_value(CONF_URL)).rstrip("/"),
                owner=self.abs_username,
                media_type=media_type,
            )

    @handle_refresh_token
    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add items to playlist."""

        def get_playlist_item(ma_id: str) -> AbsPlaylistItem:
            item_ids = ma_id.split(" ")
            abs_item_id = item_ids[0]
            episode_id = item_ids[1] if len(item_ids) == 2 else None
            return AbsPlaylistItem(library_item_id=abs_item_id, episode_id=episode_id)

        abs_items = [get_playlist_item(ma_id) for ma_id in prov_track_ids]
        async with self.playlist_lock:
            self.playlist_last = time.time()
            await self._client.add_item_to_playlist_batch(
                playlist_id=prov_playlist_id, items=abs_items
            )

    @handle_refresh_token
    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove items from playlist."""
        try:
            abs_playlist = await self._client.get_playlist(playlist_id=prov_playlist_id)
        except AbsNotFoundError:
            return
        items_to_remove: list[AbsPlaylistItem] = []
        for item_cnt, item in enumerate(abs_playlist.items):
            if item_cnt in positions_to_remove:
                items_to_remove.append(
                    AbsPlaylistItem(
                        library_item_id=item.library_item_id, episode_id=item.episode_id
                    )
                )
        if items_to_remove:
            async with self.playlist_lock:
                self.playlist_last = time.time()
                await self._client.remove_item_from_playlist_batch(
                    playlist_id=prov_playlist_id, items=items_to_remove
                )

    @handle_refresh_token
    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove item from ABS."""
        if media_type != MediaType.PLAYLIST:
            raise InvalidDataError(
                "Library remove is only implemented for playlists in the Audiobookshelf provider."
            )
        async with self.playlist_lock:
            self.playlist_last = time.time()
            with suppress(AbsNotFoundError):
                # suppress due to edge case in add_library_tracks
                await self._client.delete_playlist(playlist_id=prov_item_id)
            return True

    @handle_refresh_token
    async def library_add(self, item: MediaItemType) -> bool:
        """
        Add library item.

        This method is only called, if this item in question is not part of your library
        yet, e.g. a "top 500 mix playlist". This doesn't exist in ABS.
        """
        self.logger.error(
            "The library_add is not implemented on the ABS provider. Please reach out to us, "
            "should you see this message in your log."
        )
        return False
