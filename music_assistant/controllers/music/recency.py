"""
Shared recency engine for the Music controller.

Reads the playlog once and exposes fast in-memory "was this heard recently?" tests, used by
smart shuffle, the smart playlist dedup and (later) radio refills. Song recency is matched on
provider/item-id pairs resolved across a track's provider mappings; artist recency is matched by
(lowercased) name, which is provider-agnostic (artist playlog rows are keyed by the library id).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType

from music_assistant.constants import DB_TABLE_PLAYLOG
from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user

if TYPE_CHECKING:
    from music_assistant_models.media_items import MediaItemType

    from music_assistant import MusicAssistant


@dataclass(slots=True)
class RecencyWindows:
    """Lookback windows (in seconds) for the recency engine; None/0 disables that dimension."""

    song_seconds: int | None = None
    artist_seconds: int | None = None
    duplicate_gap_seconds: int | None = None

    @property
    def song_lookback(self) -> int | None:
        """Furthest-back we must read track rows: the larger of song window and duplicate gap."""
        candidates = [value for value in (self.song_seconds, self.duplicate_gap_seconds) if value]
        return max(candidates) if candidates else None


@dataclass(slots=True)
class RecencySnapshot:
    """In-memory last-played lookup built from a single playlog query."""

    now: int
    song_ts: dict[tuple[str, str], int] = field(default_factory=dict)
    artist_ts: dict[str, int] = field(default_factory=dict)

    def track_recent(self, item: MediaItemType, within_seconds: int | None) -> bool:
        """
        Return True if the track was last played within the given window.

        :param item: The track (or queue media item) to test.
        :param within_seconds: The lookback window in seconds; None/0 disables the test.
        """
        if not within_seconds:
            return False
        timestamp = self.last_played(item)
        return timestamp is not None and timestamp >= self.now - within_seconds

    def last_played(self, item: MediaItemType) -> int | None:
        """
        Return the most recent play timestamp for the track, or None if it has no play recorded.

        Matched across the track's own ``(provider, item_id)`` and all of its provider mappings.

        :param item: The track (or queue media item) to look up.
        """
        timestamps = []
        timestamp = self.song_ts.get((item.provider, item.item_id))
        if timestamp is not None:
            timestamps.append(timestamp)
        for mapping in getattr(item, "provider_mappings", None) or ():
            timestamp = self.song_ts.get((mapping.provider_instance, mapping.item_id))
            if timestamp is not None:
                timestamps.append(timestamp)
        return max(timestamps) if timestamps else None

    def artist_recent(self, name: str, within_seconds: int | None) -> bool:
        """
        Return True if the (named) artist was last played within the given window.

        :param name: The artist name, matched case-insensitively.
        :param within_seconds: The lookback window in seconds; None/0 disables the test.
        """
        if not within_seconds:
            return False
        timestamp = self.artist_ts.get(name.lower())
        return timestamp is not None and timestamp >= self.now - within_seconds


class RecencyEngine:
    """Builds RecencySnapshots from the playlog for recency-aware features."""

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize the recency engine."""
        self.mass = mass

    async def snapshot(
        self, windows: RecencyWindows, *, userid: str | None = None
    ) -> RecencySnapshot:
        """
        Build a recency snapshot from a single batched, user-scoped playlog query.

        :param windows: The lookback windows to read.
        :param userid: The user whose play history to read; falls back to the current user.
        """
        now = int(time.time())
        snapshot = RecencySnapshot(now=now)
        clauses: list[str] = []
        params: dict[str, int | str] = {}
        if song_lookback := windows.song_lookback:
            clauses.append(
                f"(media_type = '{MediaType.TRACK.value}' AND timestamp >= :song_cutoff)"
            )
            params["song_cutoff"] = now - song_lookback
        if windows.artist_seconds:
            clauses.append(
                f"(media_type = '{MediaType.ARTIST.value}' AND timestamp >= :artist_cutoff)"
            )
            params["artist_cutoff"] = now - windows.artist_seconds
        if not clauses:
            return snapshot
        # only fully-played items count as "recently heard" (artist credit rows are always
        # written fully_played=1, so artist recency is unaffected)
        where = f"fully_played = 1 AND ({' OR '.join(clauses)})"
        if not userid and (user := get_current_user()):
            userid = user.user_id
        if userid:
            where = f"{where} AND userid = :userid"
            params["userid"] = userid
        # one row per item is guaranteed when scoped to a user (unique playlog index); the
        # MAX(timestamp) group keeps the most recent play when no user scope is applied.
        query = (
            f"SELECT item_id, provider, media_type, name, MAX(timestamp) AS ts "
            f"FROM {DB_TABLE_PLAYLOG} WHERE {where} "
            f"GROUP BY item_id, provider, media_type"
        )
        for row in await self.mass.music.database.get_rows_from_query(
            query, params=params, limit=0
        ):
            timestamp = int(row["ts"])
            if row["media_type"] == MediaType.TRACK.value:
                snapshot.song_ts[(row["provider"], row["item_id"])] = timestamp
            elif name := row["name"]:
                snapshot.artist_ts[name.lower()] = timestamp
        return snapshot
