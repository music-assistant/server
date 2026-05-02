"""Recently Played Plugin Provider for Music Assistant.

Tracks songs heard on radio streams via ICY metadata and surfaces them
as recommendations alongside forgotten library tracks and albums.
"""

from __future__ import annotations

import json
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, EventType, MediaType, ProviderFeature
from music_assistant_models.media_items import (
    ItemMapping,
    RecommendationFolder,
    Track,
)

from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.event import MassEvent
    from music_assistant_models.player_queue import PlayerQueue
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

HISTORY_FILENAME = "recently_played.json"
MAX_HISTORY = 50
# Fire a RECOMMENDATIONS event (and invalidate the recommendations cache)
# after this many ICY tracks have been successfully resolved, so the frontend
# refreshes without waiting for the full 5-minute cache TTL.
RESOLVED_EVENT_THRESHOLD = 5

CONF_FORGOTTEN_TRACKS = "forgotten_tracks"
CONF_FORGOTTEN_ALBUMS = "forgotten_albums"
CONF_FORGOTTEN_ARTISTS = "forgotten_artists"
CONF_RECENTLY_PLAYED_RADIO = "recently_played_radio"
CONF_MOST_PLAYED_TRACKS = "most_played_tracks"
CONF_RECENTLY_ADDED = "recently_added"
CONF_NEVER_PLAYED = "never_played"
CONF_RANDOM_ARTISTS = "random_artists"
CONF_RANDOM_ALBUMS = "random_albums"
CONF_FAVORITED_TRACKS = "favorited_tracks"
CONF_FAVORITED_ALBUMS = "favorited_albums"


SUPPORTED_FEATURES: set[ProviderFeature] = {
    ProviderFeature.RECOMMENDATIONS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return RecentlyPlayedProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    return (
        ConfigEntry(
            key=CONF_RECENTLY_PLAYED_RADIO,
            type=ConfigEntryType.BOOLEAN,
            label="Show Recently Played on Radio Station recommendations",
            description="Track songs heard on radio streams via ICY metadata and show them as recommendations.",
            default_value=True,
        ),
        ConfigEntry(
            key=CONF_FORGOTTEN_TRACKS,
            type=ConfigEntryType.BOOLEAN,
            label="Show Forgotten Tracks recommendations",
            description="Surface library tracks that have never been played or were played the longest time ago.",
            default_value=True,
        ),
        ConfigEntry(
            key=CONF_FORGOTTEN_ALBUMS,
            type=ConfigEntryType.BOOLEAN,
            label="Show Forgotten Albums recommendations",
            description="Surface library albums that have never been played or were played the longest time ago.",
            default_value=True,
        ),
        ConfigEntry(
            key=CONF_FORGOTTEN_ARTISTS,
            type=ConfigEntryType.BOOLEAN,
            label="Show Forgotten Artists recommendations",
            description="Surface library artists that were not listened to for the longest time.",
            default_value=True,
        ),
        ConfigEntry(
            key=CONF_MOST_PLAYED_TRACKS,
            type=ConfigEntryType.BOOLEAN,
            label="Show Most Played Tracks recommendations",
            description="Surface library tracks with the highest play count.",
            default_value=True,
        ),
        ConfigEntry(
            key=CONF_NEVER_PLAYED,
            type=ConfigEntryType.BOOLEAN,
            label="Show Never / Rarely Played Tracks recommendations",
            description="Surface library tracks that have never or rarely been played.",
            default_value=True,
        ),
        ConfigEntry(
            key=CONF_FAVORITED_TRACKS,
            type=ConfigEntryType.BOOLEAN,
            label="Show Recently Favorited Tracks recommendations",
            description="Surface library tracks that were recently added to favorites. Disable if the builtin provider already shows this.",
            default_value=False,
        ),
        ConfigEntry(
            key=CONF_FAVORITED_ALBUMS,
            type=ConfigEntryType.BOOLEAN,
            label="Show Recently Favorited Albums recommendations",
            description="Surface library albums that were recently added to favorites. Disable if the builtin provider already shows this.",
            default_value=False,
        ),
        ConfigEntry(
            key=CONF_RECENTLY_ADDED,
            type=ConfigEntryType.BOOLEAN,
            label="Show Recently Added Tracks recommendations",
            description="Surface library tracks that were recently added. Disable if the builtin provider already shows this.",
            default_value=False,
        ),
        ConfigEntry(
            key=CONF_RANDOM_ARTISTS,
            type=ConfigEntryType.BOOLEAN,
            label="Show Random Artists recommendations",
            description="Surface a random selection of library artists. Disable if the builtin provider already shows this.",
            default_value=False,
        ),
        ConfigEntry(
            key=CONF_RANDOM_ALBUMS,
            type=ConfigEntryType.BOOLEAN,
            label="Show Random Albums recommendations",
            description="Surface a random selection of library albums. Disable if the builtin provider already shows this.",
            default_value=False,
        ),
    )


class RecentlyPlayedProvider(PluginProvider):
    """Plugin that tracks ICY radio stream tracks and surfaces them as recommendations."""

    _history: deque[dict[str, Any]]
    _history_file: Path
    _unregister_handles: list[Any]
    _last_stream_titles: dict[str, str]  # queue_id -> last recorded stream_title
    _resolved_since_event: int  # counts resolved ICY tracks since last RECOMMENDATIONS event

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._unregister_handles = []
        self._last_stream_titles = {}
        self._resolved_since_event = 0
        self._history_file = Path(self.mass.storage_path) / self.instance_id / HISTORY_FILENAME
        self._history_file.parent.mkdir(parents=True, exist_ok=True)
        self._history = deque(maxlen=MAX_HISTORY)
        await self._load_history()
        self._unregister_handles.append(
            self.mass.subscribe(self._on_queue_updated, EventType.QUEUE_UPDATED)
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unloading of the provider."""
        for handle in self._unregister_handles:
            handle()
        self._unregister_handles.clear()
        if is_removed:
            self._history.clear()
            self._last_stream_titles.clear()
            if self._history_file.exists():
                self._history_file.unlink()

    async def recommendations(self) -> list[RecommendationFolder]:
        """Return recommendation folders for recently played, forgotten tracks and albums."""
        folders: list[RecommendationFolder] = []

        if self._history:
            recently_played = await self._build_recently_played_items()
            if recently_played:
                folders.append(
                    RecommendationFolder(
                        item_id="recently_played",
                        provider=self.domain,
                        name="Recently Played on Radio Station",
                        items=recently_played,  # type: ignore[arg-type]
                    )
                )

        if self.config.get_value(CONF_FORGOTTEN_TRACKS):
            forgotten_tracks = await self.mass.music.tracks.library_items(
                order_by="last_played", limit=25
            )
            if forgotten_tracks:
                folders.append(
                    RecommendationFolder(
                        item_id="forgotten_tracks",
                        provider=self.domain,
                        name="Forgotten Tracks",
                        items=forgotten_tracks,  # type: ignore[arg-type]
                    )
                )

        if self.config.get_value(CONF_FORGOTTEN_ALBUMS):
            forgotten_albums = await self.mass.music.albums.library_items(
                order_by="last_played", limit=25
            )
            if forgotten_albums:
                folders.append(
                    RecommendationFolder(
                        item_id="forgotten_albums",
                        provider=self.domain,
                        name="Forgotten Albums",
                        items=forgotten_albums,  # type: ignore[arg-type]
                    )
                )

        if self.config.get_value(CONF_FORGOTTEN_ARTISTS):
            forgotten_artists = await self.mass.music.artists.library_items(
                order_by="last_played", limit=25
            )
            if forgotten_artists:
                folders.append(
                    RecommendationFolder(
                        item_id="forgotten_artists",
                        provider=self.domain,
                        name="Forgotten Artists",
                        items=forgotten_artists,  # type: ignore[arg-type]
                    )
                )

        if self.config.get_value(CONF_MOST_PLAYED_TRACKS):
            most_played = await self.mass.music.tracks.library_items(
                order_by="play_count_desc", limit=25
            )
            if most_played:
                folders.append(
                    RecommendationFolder(
                        item_id="most_played_tracks",
                        provider=self.domain,
                        name="Most Played Tracks",
                        items=most_played,  # type: ignore[arg-type]
                    )
                )

        if self.config.get_value(CONF_NEVER_PLAYED):
            never_played = await self.mass.music.tracks.library_items(
                order_by="play_count", limit=25
            )
            if never_played:
                folders.append(
                    RecommendationFolder(
                        item_id="never_played",
                        provider=self.domain,
                        name="Never / Rarely Played",
                        items=never_played,  # type: ignore[arg-type]
                    )
                )

        if self.config.get_value(CONF_FAVORITED_TRACKS):
            favorited_tracks = await self.mass.music.tracks.library_items(
                favorite=True, order_by="timestamp_modified_desc", limit=25
            )
            if favorited_tracks:
                folders.append(
                    RecommendationFolder(
                        item_id="favorited_tracks",
                        provider=self.domain,
                        name="Recently Favorited Tracks",
                        items=favorited_tracks,  # type: ignore[arg-type]
                    )
                )

        if self.config.get_value(CONF_FAVORITED_ALBUMS):
            favorited_albums = await self.mass.music.albums.library_items(
                favorite=True, order_by="timestamp_modified_desc", limit=25
            )
            if favorited_albums:
                folders.append(
                    RecommendationFolder(
                        item_id="favorited_albums",
                        provider=self.domain,
                        name="Recently Favorited Albums",
                        items=favorited_albums,  # type: ignore[arg-type]
                    )
                )

        if self.config.get_value(CONF_RECENTLY_ADDED):
            recently_added = await self.mass.music.tracks.library_items(
                order_by="timestamp_added_desc", limit=25
            )
            if recently_added:
                folders.append(
                    RecommendationFolder(
                        item_id="recently_added",
                        provider=self.domain,
                        name="Recently Added",
                        items=recently_added,  # type: ignore[arg-type]
                    )
                )

        if self.config.get_value(CONF_RANDOM_ARTISTS):
            random_artists = await self.mass.music.artists.library_items(
                order_by="random", limit=25
            )
            if random_artists:
                folders.append(
                    RecommendationFolder(
                        item_id="random_artists",
                        provider=self.domain,
                        name="Random Artists",
                        items=random_artists,  # type: ignore[arg-type]
                    )
                )

        if self.config.get_value(CONF_RANDOM_ALBUMS):
            random_albums = await self.mass.music.albums.library_items(order_by="random", limit=25)
            if random_albums:
                folders.append(
                    RecommendationFolder(
                        item_id="random_albums",
                        provider=self.domain,
                        name="Random Albums",
                        items=random_albums,  # type: ignore[arg-type]
                    )
                )

        return folders

    async def _on_queue_updated(self, event: MassEvent) -> None:
        """Handle QUEUE_UPDATED event to track ICY radio stream metadata changes."""
        queue: PlayerQueue = event.data
        if not queue.current_item or not queue.current_item.streamdetails:
            self._last_stream_titles.pop(queue.queue_id, None)
            return
        if not self.config.get_value(CONF_RECENTLY_PLAYED_RADIO):
            return
        if queue.current_item.media_type != MediaType.RADIO:
            return
        stream_metadata = queue.current_item.streamdetails.stream_metadata
        if not stream_metadata or not stream_metadata.title:
            return
        stream_title = queue.current_item.streamdetails.stream_title
        if not stream_title:
            return
        # Only record when the ICY title actually changed (new song started)
        if self._last_stream_titles.get(queue.queue_id) == stream_title:
            return
        self._last_stream_titles[queue.queue_id] = stream_title
        artist = stream_metadata.artist or ""
        title = stream_metadata.title
        dedup_key = f"{artist}|{title}"
        self._history = deque(
            (e for e in self._history if f"{e.get('artist', '')}|{e.get('name', '')}" != dedup_key),
            maxlen=MAX_HISTORY,
        )
        entry = {
            "uri": queue.current_item.uri,
            "name": title,
            "artist": artist or None,
            "album": None,
            "image_url": stream_metadata.image_url,
            "played_at": time.time(),
        }
        self._history.appendleft(entry)
        # Resolve to a real track URI in the background; history entry is only
        # shown in recommendations once resolved_uri is set.
        self.mass.create_task(self._resolve_and_update_entry(dedup_key, artist, title))
        self.logger.debug("Queued ICY track for resolution: %s", stream_title)
        await self._save_history()

    async def _resolve_and_update_entry(self, dedup_key: str, artist: str, title: str) -> None:
        """Try to resolve an ICY track to a library/provider Track and update the history entry."""
        query = f"{artist} - {title}" if artist else title
        # Search library first, fall back to all providers
        for library_only in (True, False):
            try:
                results = await self.mass.music.search(
                    search_query=query,
                    media_types=[MediaType.TRACK],
                    limit=5,
                    library_only=library_only,
                )
            except Exception as exc:
                self.logger.debug("Search failed for %r: %s", query, exc)
                return
            for track in results.tracks:
                if not isinstance(track, Track):
                    continue
                track_artist = track.artists[0].name if track.artists else ""
                if title.lower() in track.name.lower() and (
                    not artist or artist.lower() in track_artist.lower()
                ):
                    # Update the history entry with the resolved URI
                    for entry in self._history:
                        if f"{entry.get('artist', '')}|{entry.get('name', '')}" == dedup_key:
                            entry["resolved_uri"] = track.uri
                            self.logger.debug(
                                "Resolved ICY track %r to library URI %s", query, track.uri
                            )
                            await self._save_history()
                            self._resolved_since_event += 1
                            if self._resolved_since_event >= RESOLVED_EVENT_THRESHOLD:
                                self._resolved_since_event = 0
                                # Invalidate the recommendations cache so the next
                                # frontend request gets fresh data without waiting
                                # for the full 5-minute TTL.
                                await self.mass.cache.delete(
                                    key="recommendations", provider=self.instance_id
                                )
                                self.logger.debug(
                                    "Invalidated recommendations cache after %s resolved ICY tracks",
                                    RESOLVED_EVENT_THRESHOLD,
                                )
                            return
                    return  # entry was already evicted from history

    async def _build_recently_played_items(self) -> list[ItemMapping]:
        """Return resolved tracks from ICY history."""
        result: list[ItemMapping] = []
        for entry in self._history:
            resolved_uri = entry.get("resolved_uri")
            if not resolved_uri:
                continue
            try:
                item = await self.mass.music.get_item_by_uri(resolved_uri)
                if isinstance(item, Track):
                    result.append(item)  # type: ignore[arg-type]
            except Exception as exc:
                self.logger.debug("Could not fetch resolved URI %s: %s", resolved_uri, exc)
        return result

    async def _load_history(self) -> None:
        """Load history from disk."""
        if not self._history_file.exists():
            return
        try:
            raw = await self.mass.loop.run_in_executor(None, self._history_file.read_text, "utf-8")
            entries = json.loads(raw)
            if isinstance(entries, list):
                self._history = deque(entries[:MAX_HISTORY], maxlen=MAX_HISTORY)
        except Exception as exc:
            self.logger.warning("Could not load recently played history: %s", exc)

    async def _save_history(self) -> None:
        """Persist history to disk."""
        try:
            data = json.dumps(list(self._history), indent=2)
            await self.mass.loop.run_in_executor(None, self._history_file.write_text, data, "utf-8")
        except Exception as exc:
            self.logger.warning("Could not save recently played history: %s", exc)
