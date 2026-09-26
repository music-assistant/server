"""
Per-user favorites store for the Music controller.

Every user has their own like, dislike or nothing on a library item, stored as one row per
user in the favorites table. A row with favorite 1 is a like, 0 a dislike and NULL a state
the user explicitly cleared; no row at all means the user never expressed anything. The
NULL row is what keeps a provider sync, which only ever fills in what it has not seen
before, from re-liking something the user removed or from overriding a dislike.

This store owns every statement on that table; reading a user's own state into the media
items themselves happens in the listing queries of the media controllers.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from music_assistant.constants import DB_TABLE_FAVORITES
from music_assistant.helpers.provider_access import source_owner

if TYPE_CHECKING:
    from music_assistant_models.enums import MediaType

    from music_assistant import MusicAssistant

# user id the library migration parks a favorite under that it can not attribute yet:
# the users live in another database that is not open while the library migrates
PENDING_USER_ID = "__pending__"


class FavoritesStore:
    """Stores the per-user like/dislike state of library items."""

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize the favorites store."""
        self.mass = mass

    async def set(
        self,
        media_type: MediaType,
        item_id: int,
        favorite: bool | None,
        user_ids: list[str],
    ) -> None:
        """
        Store the state of the given users on a library item.

        :param media_type: Media type of the library item.
        :param item_id: Library (database) id of the item.
        :param favorite: True to like, False to dislike, None to clear the state.
        :param user_ids: The users whose state is stored.
        """
        timestamp = int(time.time())
        await self.mass.music.database.upsert_many(
            DB_TABLE_FAVORITES,
            [
                {
                    "user_id": user_id,
                    "media_type": media_type.value,
                    "item_id": item_id,
                    "favorite": favorite,
                    "timestamp": timestamp,
                }
                for user_id in user_ids
            ],
        )

    async def record_from_provider(
        self,
        provider_instance_id: str,
        media_type: MediaType,
        item_id: int,
        favorite: bool,
    ) -> None:
        """
        Record the state a music source reports for a library item.

        Only fills in a state for a user who has never expressed one, so a choice the user
        made (including clearing a like) is never overridden.

        :param provider_instance_id: The music source reporting the state.
        :param media_type: Media type of the library item.
        :param item_id: Library (database) id of the item.
        :param favorite: True for a like, False for a dislike.
        """
        if owner := source_owner(self.mass, provider_instance_id):
            user_ids = [owner]
        else:
            # a source of the whole home speaks for everyone, the rule the play log follows
            user_ids = [user.user_id for user in await self.mass.webserver.auth.list_users()]
        timestamp = int(time.time())
        for user_id in user_ids:
            await self.mass.music.database.execute_write(
                f"INSERT OR IGNORE INTO {DB_TABLE_FAVORITES}"
                "(user_id, media_type, item_id, favorite, timestamp) "
                "VALUES(:user_id, :media_type, :item_id, :favorite, :timestamp)",
                {
                    "user_id": user_id,
                    "media_type": media_type.value,
                    "item_id": item_id,
                    "favorite": favorite,
                    "timestamp": timestamp,
                },
            )

    async def move_item(self, media_type: MediaType, source_id: int, target_id: int) -> None:
        """
        Move the states of a merged library item onto the item it was merged into.

        A state the target already holds wins.

        :param media_type: Media type of the merged items.
        :param source_id: Library id of the item that is merged away.
        :param target_id: Library id of the item that survives.
        """
        values = {
            "media_type": media_type.value,
            "source_id": source_id,
            "target_id": target_id,
        }
        # copy before delete, so nothing is lost if the two statements are cut apart
        await self.mass.music.database.execute_write(
            f"INSERT OR IGNORE INTO {DB_TABLE_FAVORITES}"
            "(user_id, media_type, item_id, favorite, timestamp) "
            "SELECT user_id, media_type, :target_id, favorite, timestamp "
            f"FROM {DB_TABLE_FAVORITES} "
            "WHERE media_type = :media_type AND item_id = :source_id",
            values,
        )
        await self.remove_item(media_type, source_id)

    async def remove_item(self, media_type: MediaType, item_id: int) -> None:
        """
        Remove every user's state on a library item.

        :param media_type: Media type of the library item.
        :param item_id: Library (database) id of the item.
        """
        await self.mass.music.database.delete(
            DB_TABLE_FAVORITES,
            {"media_type": media_type.value, "item_id": item_id},
        )

    async def clear_likes(self, media_type: MediaType, item_id: int) -> None:
        """
        Remove every user's like on a library item, keeping the dislikes.

        Used when no music source holds the item in its library anymore: the likes came
        from those sources, a dislike is the user's own.

        :param media_type: Media type of the library item.
        :param item_id: Library (database) id of the item.
        """
        await self.mass.music.database.execute_write(
            f"DELETE FROM {DB_TABLE_FAVORITES} "
            "WHERE media_type = :media_type AND item_id = :item_id AND favorite = 1",
            {"media_type": media_type.value, "item_id": item_id},
        )

    async def release_user(self, user_id: str) -> None:
        """
        Drop the states of a user that no longer exists.

        :param user_id: Id of the removed user.
        """
        await self.mass.music.database.delete(DB_TABLE_FAVORITES, {"user_id": user_id})

    async def expand_pending(self) -> None:
        """Hand the favorites the library migration could not attribute to every user."""
        if not await self.mass.music.database.get_rows(
            DB_TABLE_FAVORITES, {"user_id": PENDING_USER_ID}, limit=1
        ):
            return
        if not (users := await self.mass.webserver.auth.list_users()):
            # nobody to hand them to yet; leave them parked for a next start
            return
        for user in users:
            await self.mass.music.database.execute_write(
                f"INSERT OR IGNORE INTO {DB_TABLE_FAVORITES}"
                "(user_id, media_type, item_id, favorite, timestamp) "
                "SELECT :user_id, media_type, item_id, favorite, timestamp "
                f"FROM {DB_TABLE_FAVORITES} WHERE user_id = :pending_user_id",
                {"user_id": user.user_id, "pending_user_id": PENDING_USER_ID},
            )
        await self.release_user(PENDING_USER_ID)
