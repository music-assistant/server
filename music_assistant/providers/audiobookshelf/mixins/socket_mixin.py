"""SocketsMixin for Audiobookshelf."""

from __future__ import annotations

import time
from contextlib import suppress
from typing import TYPE_CHECKING

from aioaudiobookshelf.schema.library import (
    LibraryItemExpanded,
    LibraryItemExpandedBook,
    LibraryItemExpandedPodcast,
)
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Playlist

from music_assistant.providers.audiobookshelf.constants import (
    CONF_HIDE_EMPTY_PODCASTS,
    CONF_URL,
)
from music_assistant.providers.audiobookshelf.helpers import NarratorHelper
from music_assistant.providers.audiobookshelf.mixins.mixin_base import MixinBase
from music_assistant.providers.audiobookshelf.parsers import (
    parse_audiobook,
    parse_playlist,
    parse_podcast,
)

if TYPE_CHECKING:
    from aioaudiobookshelf.schema.events_socket import LibraryItemRemoved
    from aioaudiobookshelf.schema.library import (
        LibraryItemExpandedBook as AbsLibraryItemExpandedBook,
    )
    from aioaudiobookshelf.schema.media_progress import MediaProgress
    from aioaudiobookshelf.schema.playlist import PlaylistExpanded as AbsPlaylistExpanded


class SocketMixin(MixinBase):
    """Event-based mixin for Audiobookshelf."""

    if TYPE_CHECKING:
        # part of audiobooks mixin
        async def _get_audiobook_narrators(
            self, book: AbsLibraryItemExpandedBook
        ) -> set[NarratorHelper]: ...
        async def _update_playlog_book(self, progress: MediaProgress) -> None: ...

        # part of podcasts mixin
        async def _update_playlog_episode(self, progress: MediaProgress) -> None: ...

    def set_socket_callbacks(self) -> None:
        """Set socket callback methods."""
        self._client_socket.set_item_callbacks(
            on_item_added=self._socket_abs_item_changed,
            on_item_updated=self._socket_abs_item_changed,
            on_item_removed=self._socket_abs_item_removed,
            on_items_added=self._socket_abs_item_changed,
            on_items_updated=self._socket_abs_item_changed,
        )

        self._client_socket.set_user_callbacks(
            on_user_item_progress_updated=self._socket_abs_user_item_progress_updated,
        )

        self._client_socket.set_refresh_token_expired_callback(
            on_refresh_token_expired=self._socket_abs_refresh_token_expired
        )

        self._client_socket.set_playlist_callbacks(
            on_playlist_added=self._socket_abs_playlist_changed,
            on_playlist_updated=self._socket_abs_playlist_changed,
            on_playlist_removed=self._socket_abs_playlist_removed,
        )

    async def _socket_abs_item_changed(
        self, items: LibraryItemExpanded | list[LibraryItemExpanded]
    ) -> None:
        """For added and updated."""
        abs_items = [items] if isinstance(items, LibraryItemExpanded) else items
        for abs_item in abs_items:
            if isinstance(abs_item, LibraryItemExpandedBook):
                # If the book has no audiofiles, we skip -> ebook only.
                if len(abs_item.media.tracks) == 0:
                    continue
                self.logger.debug(
                    'Updated book "%s" via socket.', abs_item.media.metadata.title or ""
                )
                await self.mass.music.audiobooks.add_item_to_library(
                    parse_audiobook(
                        abs_audiobook=abs_item,
                        audiobook_narrators=await self._get_audiobook_narrators(abs_item),
                        instance_id=self.instance_id,
                        domain=self.domain,
                        token=self._client.token,
                        base_url=str(self.config.get_value(CONF_URL)).rstrip("/"),
                    ),
                    overwrite_existing=True,
                )
                lib = self.libraries.audiobooks.get(abs_item.library_id, None)
                if lib is not None:
                    lib.item_ids.add(abs_item.id_)
            elif isinstance(abs_item, LibraryItemExpandedPodcast):
                self.logger.debug(
                    'Updated podcast "%s" via socket.', abs_item.media.metadata.title or ""
                )
                mass_podcast = parse_podcast(
                    abs_podcast=abs_item,
                    instance_id=self.instance_id,
                    domain=self.domain,
                    token=self._client.token,
                    base_url=str(self.config.get_value(CONF_URL)).rstrip("/"),
                )
                if not (
                    bool(self.config.get_value(CONF_HIDE_EMPTY_PODCASTS))
                    and mass_podcast.total_episodes == 0
                ):
                    await self.mass.music.podcasts.add_item_to_library(
                        mass_podcast,
                        overwrite_existing=True,
                    )
                    lib = self.libraries.podcasts.get(abs_item.library_id, None)
                    if lib is not None:
                        lib.item_ids.add(abs_item.id_)
        await self._cache_set_helper_libraries()

    async def _socket_abs_item_removed(self, item: LibraryItemRemoved) -> None:
        """Item removed."""
        media_type: MediaType | None = None
        for lib in self.libraries.audiobooks.values():
            if item.id_ in lib.item_ids:
                media_type = MediaType.AUDIOBOOK
                lib.item_ids.remove(item.id_)
                break
        for lib in self.libraries.podcasts.values():
            if item.id_ in lib.item_ids:
                media_type = MediaType.PODCAST
                lib.item_ids.remove(item.id_)
                break

        if media_type is not None:
            mass_item = await self.mass.music.get_library_item_by_prov_id(
                media_type=media_type,
                item_id=item.id_,
                provider_instance_id_or_domain=self.instance_id,
            )
            if mass_item is not None:
                await self.mass.music.remove_item_from_library(
                    media_type=media_type, library_item_id=mass_item.item_id
                )
                self.logger.debug('Removed %s "%s" via socket.', media_type.value, mass_item.name)

        await self._cache_set_helper_libraries()

    async def _socket_abs_user_item_progress_updated(
        self, id_: str, progress: MediaProgress
    ) -> None:
        """
        To update continue listening.

        ABS reports every 15s and immediately on play state change.
        This callback is called per item if a progress is changed:
            - a change in position
            - the item is finished
        But it is _not_called, if a progress is reset/ discarded.
        """
        # guard, see progress guard class docstrings for explanation
        if not self.progress_guard.guard_ok_abs(abs_progress=progress):
            return

        known_ids = self._get_all_known_item_ids()
        if progress.library_item_id not in known_ids:
            return

        self.logger.debug(f"Updated progress of item {progress.library_item_id} via socket.")

        if progress.episode_id is None:
            await self._update_playlog_book(progress)
            return
        await self._update_playlog_episode(progress)

    async def _socket_abs_playlist_changed(self, abs_playlist: AbsPlaylistExpanded) -> None:
        if time.time() - self.playlist_last < 5:
            return
        if abs_playlist.library_id in self.libraries.audiobooks:
            media_type = MediaType.AUDIOBOOK
        elif abs_playlist.library_id in self.libraries.podcasts:
            media_type = MediaType.PODCAST_EPISODE
        else:
            return
        async with self.playlist_lock:
            parsed_playlist = parse_playlist(
                abs_playlist=abs_playlist,
                instance_id=self.instance_id,
                domain=self.domain,
                token=self._client.token,
                base_url=str(self.config.get_value(CONF_URL)).rstrip("/"),
                owner=self.abs_username,
                media_type=media_type,
            )
            ma_library_playlist = await self.mass.music.get_library_item_by_prov_id(
                media_type=MediaType.PLAYLIST,
                item_id=abs_playlist.id_,
                provider_instance_id_or_domain=self.instance_id,
            )
            if ma_library_playlist is not None and isinstance(ma_library_playlist, Playlist):
                await self.mass.music.playlists.update_item_in_library(
                    item_id=ma_library_playlist.item_id, update=parsed_playlist, overwrite=True
                )
            else:
                await self.mass.music.playlists.add_item_to_library(item=parsed_playlist)
            if media_type == MediaType.AUDIOBOOK:
                self.libraries.playlists_audiobooks[abs_playlist.library_id].add(abs_playlist.id_)
            elif media_type == MediaType.PODCAST_EPISODE:
                self.libraries.playlists_podcasts[abs_playlist.library_id].add(abs_playlist.id_)
        await self._cache_set_helper_libraries()

    async def _socket_abs_playlist_removed(self, abs_playlist: AbsPlaylistExpanded) -> None:
        if time.time() - self.playlist_last < 5:
            return
        if mass_item := await self.mass.music.get_library_item_by_prov_id(
            media_type=MediaType.PLAYLIST,
            item_id=abs_playlist.id_,
            provider_instance_id_or_domain=self.instance_id,
        ):
            async with self.playlist_lock:
                await self.mass.music.playlists.remove_item_from_library(item_id=mass_item.item_id)
                playlist_set = self.libraries.playlists_audiobooks.get(abs_playlist.library_id)
                if playlist_set is None:
                    playlist_set = self.libraries.playlists_podcasts.get(abs_playlist.library_id)
                if playlist_set is not None:
                    with suppress(KeyError):
                        playlist_set.remove(abs_playlist.id_)
        await self._cache_set_helper_libraries()

    async def _socket_abs_refresh_token_expired(self) -> None:
        await self.reauthenticate()
