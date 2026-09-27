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

from music_assistant_models.enums import MediaType

from music_assistant.constants import DB_TABLE_FAVORITES, DB_TABLE_PROVIDER_MAPPINGS
from music_assistant.helpers.provider_access import (
    access_allows,
    music_sources_access,
    source_access,
)

if TYPE_CHECKING:
    from music_assistant_models.auth import User
    from music_assistant_models.config_entries import ProviderAccess
    from music_assistant_models.media_items import Track

    from music_assistant import MusicAssistant

# user id the library migration parks every favorite under: whose like it becomes depends on
# the owners of the music sources and on the users, both only known once the webserver is up
PENDING_USER_ID = "__pending__"
# how long a resolved user list serves the reports of one library sync
USERS_TTL = 30
# a user's disliked tracks: their library ids plus the (provider domain, provider item id)
# pairs of every mapping of those tracks
type DislikedTrackKeys = tuple[set[int], set[tuple[str, str]]]


class FavoritesStore:
    """Stores the per-user like/dislike state of library items."""

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize the favorites store."""
        self.mass = mass
        self._users: tuple[float, list[User]] | None = None

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

        The state goes to the owner of the source, or to every user the source serves when
        it has no owner. It only fills in a state for a user who never expressed one, so a
        choice the user made (including clearing a like) is never overridden.

        :param provider_instance_id: The music source reporting the state.
        :param media_type: Media type of the library item.
        :param item_id: Library (database) id of the item.
        :param favorite: True for a like, False for a dislike.
        """
        access = source_access(self.mass, provider_instance_id)
        if access and access.owner:
            user_ids = [access.owner]
        else:
            users = await self._recent_users()
            user_ids = [user.user_id for user in users if _holds_favorites_for(access, user)]
        if not user_ids:
            return
        timestamp = int(time.time())
        rows = ", ".join(
            f"(:user_{idx}, :media_type, :item_id, :favorite, :timestamp)"
            for idx in range(len(user_ids))
        )
        await self.mass.music.database.execute_write(
            f"INSERT OR IGNORE INTO {DB_TABLE_FAVORITES}"
            f"(user_id, media_type, item_id, favorite, timestamp) VALUES {rows}",
            {
                "media_type": media_type.value,
                "item_id": item_id,
                "favorite": favorite,
                "timestamp": timestamp,
                **{f"user_{idx}": user_id for idx, user_id in enumerate(user_ids)},
            },
        )

    async def disliked_track_keys(self, user_id: str) -> DislikedTrackKeys:
        """
        Return the tracks the given user disliked, in one query.

        The library ids identify the tracks as library items, the provider keys recognize the
        same track when it arrives straight from a music source. Feed the result to
        :func:`filter_disliked`.

        :param user_id: The user whose dislikes are returned.
        """
        item_ids: set[int] = set()
        provider_keys: set[tuple[str, str]] = set()
        # LEFT JOIN: a disliked track without any mapping left still counts by its library id
        query = (
            "SELECT f.item_id, pm.provider_domain, pm.provider_item_id "
            f"FROM {DB_TABLE_FAVORITES} f "
            f"LEFT JOIN {DB_TABLE_PROVIDER_MAPPINGS} pm "
            "ON pm.media_type = f.media_type AND pm.item_id = f.item_id "
            "WHERE f.user_id = :user_id AND f.media_type = :media_type AND f.favorite = 0"
        )
        for row in await self.mass.music.database.get_rows_from_query(
            query,
            {"user_id": user_id, "media_type": MediaType.TRACK.value},
            limit=0,
        ):
            item_ids.add(int(row["item_id"]))
            if row["provider_domain"] and row["provider_item_id"]:
                provider_keys.add((row["provider_domain"], row["provider_item_id"]))
        return item_ids, provider_keys

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
        # a sync in progress must not hand the user anything after this
        self._users = None
        await self.mass.music.database.delete(DB_TABLE_FAVORITES, {"user_id": user_id})

    async def settle_pending(self) -> None:
        """
        Hand the favorites the library migration parked to the users they belong to.

        A favorite goes to the owner of every music source that holds the item in its
        library, or to every user such a source serves when it has no owner. A favorite
        no configured source holds in its library goes to every user.
        """
        if not await self.mass.music.database.get_rows(
            DB_TABLE_FAVORITES, {"user_id": PENDING_USER_ID}, limit=1
        ):
            return
        if not (users := await self.mass.webserver.auth.list_users()):
            # nobody to hand them to yet; leave them parked for a next start
            return
        sources = music_sources_access(self.mass)
        # bound one by one: execute_write has no list parameter support
        configured = {f"source_{idx}": instance_id for idx, instance_id in enumerate(sources)}
        in_library_on = (
            f"SELECT 1 FROM {DB_TABLE_PROVIDER_MAPPINGS} pm "
            "WHERE pm.media_type = f.media_type AND pm.item_id = f.item_id AND pm.in_library = 1"
        )
        for user in users:
            served_by = [
                name
                for name, instance_id in configured.items()
                if _holds_favorites_for(sources[instance_id], user)
            ]
            # a mapping on a source that is no longer configured counts as one of the whole home
            holders = [f"pm.provider_instance NOT IN ({', '.join(f':{x}' for x in configured)})"]
            if served_by:
                holders.append(f"pm.provider_instance IN ({', '.join(f':{x}' for x in served_by)})")
            holds_for_user = (
                f"EXISTS({in_library_on} AND ({' OR '.join(holders)}))"
                if configured
                else f"EXISTS({in_library_on})"
            )
            await self.mass.music.database.execute_write(
                f"INSERT OR IGNORE INTO {DB_TABLE_FAVORITES}"
                "(user_id, media_type, item_id, favorite, timestamp) "
                "SELECT :user_id, f.media_type, f.item_id, f.favorite, f.timestamp "
                f"FROM {DB_TABLE_FAVORITES} f WHERE f.user_id = :pending_user_id "
                f"AND (NOT EXISTS({in_library_on}) OR {holds_for_user})",
                {"user_id": user.user_id, "pending_user_id": PENDING_USER_ID, **configured},
            )
        await self.release_user(PENDING_USER_ID)

    async def _recent_users(self) -> list[User]:
        """
        Return the users, resolved at most once per USERS_TTL seconds.

        A library sync reports one item after another; a user created in between gets what
        it missed on the next sync, since a report only fills in what is not there yet.
        """
        now = time.monotonic()
        if self._users is None or now - self._users[0] > USERS_TTL:
            self._users = (now, await self.mass.webserver.auth.list_users())
        return self._users[1]


async def without_disliked_tracks(
    mass: MusicAssistant, user_id: str | None, tracks: list[Track]
) -> list[Track]:
    """
    Return the given tracks without the ones the user disliked.

    Only for playback Music Assistant picks itself; what a user asks for by name is never
    filtered.

    :param mass: The MusicAssistant instance.
    :param user_id: The playback user; an anonymous queue (None) is not filtered.
    :param tracks: The candidate tracks.
    """
    if not user_id or not tracks:
        return tracks
    return filter_disliked(tracks, await mass.music.favorites.disliked_track_keys(user_id))


def filter_disliked(tracks: list[Track], keys: DislikedTrackKeys) -> list[Track]:
    """
    Drop the tracks a user disliked from a list of candidates.

    A hard filter, unlike the advisory :func:`music_assistant.helpers.track_filter.filter_tracks`:
    an empty result is a valid answer, since a disliked track must never be played.

    :param tracks: The candidate tracks.
    :param keys: The user's dislikes, from :meth:`FavoritesStore.disliked_track_keys`.
    """
    item_ids, provider_keys = keys
    if not item_ids and not provider_keys:
        return tracks
    return [track for track in tracks if not _is_disliked(track, item_ids, provider_keys)]


def _holds_favorites_for(access: ProviderAccess | None, user: User) -> bool:
    """
    Return whether a music source with this access record holds favorites of the given user.

    The favorites of an owned source are the owner's alone, whoever it is shared with; a
    source without an owner holds them for every user it serves.
    """
    if access and access.owner:
        return access.owner == user.user_id
    return access_allows(access, user)


def _is_disliked(track: Track, item_ids: set[int], provider_keys: set[tuple[str, str]]) -> bool:
    """Return whether the track is one of the disliked ones, by library id or by mapping."""
    if track.provider == "library" and int(track.item_id) in item_ids:
        return True
    return any(
        (mapping.provider_domain, mapping.item_id) in provider_keys
        for mapping in track.provider_mappings
    )
