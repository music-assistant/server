"""
Per-user bookmarks of audiobooks and podcast episodes.

A bookmark belongs to one user and one copy of the item. The provider of that copy keeps it
when it can edit bookmarks, otherwise the bookmarks table does, for library copies only; an
account the user may not write to takes none. Episodes have no library row, so their rows carry
the podcast copy and leave with it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant_models.auth import Scope
from music_assistant_models.bookmarks_update import BookmarksUpdate
from music_assistant_models.enums import EventType, MediaType, ProviderFeature
from music_assistant_models.errors import (
    AuthenticationRequired,
    InsufficientPermissions,
    InvalidDataError,
    MediaNotFoundError,
    ProviderUnavailableError,
)
from music_assistant_models.helpers import create_uri
from music_assistant_models.media_items import MediaItemBookmark

from music_assistant.constants import DB_TABLE_BOOKMARKS
from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.helpers.provider_access import exact_provider, visible_music_sources
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.media_items import Audiobook, Podcast

    from music_assistant import MusicAssistant

# the read and the edit feature of a provider per media type that has bookmarks
BOOKMARK_FEATURES: dict[MediaType, tuple[ProviderFeature, ProviderFeature]] = {
    MediaType.AUDIOBOOK: (
        ProviderFeature.AUDIOBOOK_BOOKMARKS,
        ProviderFeature.AUDIOBOOK_BOOKMARKS_EDIT,
    ),
    MediaType.PODCAST_EPISODE: (
        ProviderFeature.PODCAST_EPISODE_BOOKMARKS,
        ProviderFeature.PODCAST_EPISODE_BOOKMARKS_EDIT,
    ),
}


class BookmarksController:
    """Manages the user's bookmarks of audiobooks and podcast episodes."""

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize the bookmarks controller."""
        self.mass = mass
        self.mass.register_api_command(
            "music/bookmarks", self.bookmarks, required_scope=Scope.LIBRARY_READ
        )
        self.mass.register_api_command(
            "music/set_bookmark", self.set_bookmark, required_scope=Scope.LIBRARY_WRITE
        )
        self.mass.register_api_command(
            "music/remove_bookmark", self.remove_bookmark, required_scope=Scope.LIBRARY_WRITE
        )

    async def bookmarks(
        self, media_type: MediaType, item_id: str, provider_instance_id_or_domain: str
    ) -> list[MediaItemBookmark]:
        """
        Return the user's bookmarks of an audiobook or podcast episode, per copy the user may see.

        :param media_type: MediaType.AUDIOBOOK or MediaType.PODCAST_EPISODE.
        :param item_id: The id on the provider, or the library id of an audiobook.
        :param provider_instance_id_or_domain: The provider the id belongs to, or "library".
        """
        if not (user := get_current_user()):
            raise AuthenticationRequired("Bookmarks belong to a user")
        features = self._features(media_type)
        if media_type == MediaType.AUDIOBOOK:
            audiobook = await self.mass.music.audiobooks.get(
                item_id, provider_instance_id_or_domain, allow_update_metadata=False
            )
            copies = sorted((x.provider_instance, x.item_id) for x in audiobook.provider_mappings)
        elif provider := self.mass.get_provider(provider_instance_id_or_domain):
            copies = [(provider.instance_id, item_id)]
        else:
            raise ProviderUnavailableError(f"Provider {provider_instance_id_or_domain} not found")
        visible = visible_music_sources(self.mass, user)
        bookmarks: list[MediaItemBookmark] = []
        for provider_instance_id, provider_item_id in copies:
            if visible is not None and provider_instance_id not in visible:
                continue
            copy_bookmarks = [
                MediaItemBookmark(
                    name=row["name"],
                    start=row["start"],
                    provider_instance_id=provider_instance_id,
                    provider_item_id=provider_item_id,
                    stored_by_music_assistant=True,
                )
                for row in await self.mass.music.database.get_rows(
                    DB_TABLE_BOOKMARKS,
                    {
                        "user_id": user.user_id,
                        "media_type": media_type,
                        "provider_instance": provider_instance_id,
                        "provider_item_id": provider_item_id,
                    },
                    limit=0,
                )
            ]
            copy_provider = exact_provider(self.mass, provider_instance_id)
            if isinstance(copy_provider, MusicProvider) and copy_provider.supports_feature(
                features[0]
            ):
                copy_bookmarks += await copy_provider.get_bookmarks(media_type, provider_item_id)
            bookmarks += sorted(copy_bookmarks, key=lambda x: x.start)
        return bookmarks

    async def set_bookmark(
        self,
        media_type: MediaType,
        provider_instance_id: str,
        provider_item_id: str,
        start: float,
        name: str,
    ) -> None:
        """
        Add a bookmark of the user to a library copy, or rename the one at start.

        :param media_type: MediaType.AUDIOBOOK or MediaType.PODCAST_EPISODE.
        :param provider_instance_id: The provider instance of the copy.
        :param provider_item_id: The id of the audiobook or episode on the provider.
        :param start: Position of the bookmark in seconds.
        :param name: Name of the bookmark.
        """
        row, provider = await self._bookmark_storage(
            media_type, provider_instance_id, provider_item_id
        )
        if provider:
            await provider.set_bookmark(media_type, provider_item_id, start, name)
        else:
            await self.mass.music.database.insert_or_replace(
                DB_TABLE_BOOKMARKS, {**row, "start": start, "name": name}
            )
        self._signal_updated(row)

    async def remove_bookmark(
        self, media_type: MediaType, provider_instance_id: str, provider_item_id: str, start: float
    ) -> None:
        """
        Remove a bookmark of the user from a library copy.

        :param media_type: MediaType.AUDIOBOOK or MediaType.PODCAST_EPISODE.
        :param provider_instance_id: The provider instance of the copy.
        :param provider_item_id: The id of the audiobook or episode on the provider.
        :param start: Position of the bookmark in seconds.
        """
        row, provider = await self._bookmark_storage(
            media_type, provider_instance_id, provider_item_id
        )
        if provider:
            await provider.remove_bookmark(media_type, provider_item_id, start)
        else:
            match = {**row, "start": start}
            if not await self.mass.music.database.get_row(DB_TABLE_BOOKMARKS, match):
                raise MediaNotFoundError(f"No bookmark at {start}s on {provider_item_id}")
            await self.mass.music.database.delete(DB_TABLE_BOOKMARKS, match)
        self._signal_updated(row)

    def _signal_updated(self, row: dict[str, Any]) -> None:
        media_type = MediaType(row["media_type"])
        self.mass.signal_event(
            EventType.BOOKMARKS_UPDATED,
            create_uri(media_type, row["provider_instance"], row["provider_item_id"]),
            BookmarksUpdate(
                media_type=media_type,
                provider_instance_id=row["provider_instance"],
                provider_item_id=row["provider_item_id"],
                user_id=row["user_id"],
            ),
        )

    @staticmethod
    def _features(media_type: MediaType) -> tuple[ProviderFeature, ProviderFeature]:
        if media_type not in BOOKMARK_FEATURES:
            raise InvalidDataError(f"{media_type} has no bookmarks")
        return BOOKMARK_FEATURES[media_type]

    async def _bookmark_storage(
        self, media_type: MediaType, provider_instance_id: str, provider_item_id: str
    ) -> tuple[dict[str, Any], MusicProvider | None]:
        """
        Return the user's row of a library copy, and the provider storing its bookmarks.

        The provider is None when Music Assistant stores the bookmarks of the copy.
        """
        if not (user := get_current_user()):
            raise AuthenticationRequired("Bookmarks belong to a user")
        edit_feature = self._features(media_type)[1]
        visible = visible_music_sources(self.mass, user)
        if visible is not None and provider_instance_id not in visible:
            raise MediaNotFoundError(f"{provider_item_id} on {provider_instance_id} not found")
        provider = exact_provider(self.mass, provider_instance_id)
        if not isinstance(provider, MusicProvider):
            raise ProviderUnavailableError(f"Provider {provider_instance_id} is not available")
        row: dict[str, Any] = {
            "user_id": user.user_id,
            "media_type": media_type,
            "provider_instance": provider_instance_id,
            "provider_item_id": provider_item_id,
        }
        library_item: Audiobook | Podcast | None
        # the library copy of an episode is its podcast
        if media_type == MediaType.PODCAST_EPISODE:
            episode = await provider.get_podcast_episode(provider_item_id)
            row["podcast_item_id"] = mapping_item_id = episode.podcast.item_id
            library_item = await self.mass.music.podcasts.get_library_item_by_prov_id(
                mapping_item_id, provider_instance_id
            )
        else:
            mapping_item_id = provider_item_id
            library_item = await self.mass.music.audiobooks.get_library_item_by_prov_id(
                mapping_item_id, provider_instance_id
            )
        mapping = next(
            (
                x
                for x in (library_item.provider_mappings if library_item else ())
                if x.provider_instance == provider_instance_id and x.item_id == mapping_item_id
            ),
            None,
        )
        if mapping is None:
            raise MediaNotFoundError(
                f"{mapping_item_id} on {provider_instance_id} is not in the library"
            )
        if not provider.supports_feature(edit_feature):
            return row, None
        # the provider keeps them, MA does not stand in for an account the user may not write to
        if not self.mass.music._write_target_mappings([mapping]):
            raise InsufficientPermissions(
                f"Bookmarks on {provider.name} can only be changed by its owner"
            )
        return row, provider
