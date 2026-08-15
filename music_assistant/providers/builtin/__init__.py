"""Built-in/generic provider to handle media from files and (remote) urls."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncGenerator, Mapping
from typing import TYPE_CHECKING, Final, cast
from urllib.parse import urlparse

import aiofiles
from music_assistant_models.auth import Scope
from music_assistant_models.background_task import TaskSchedule
from music_assistant_models.enums import (
    ContentType,
    ExternalID,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    ProviderUnavailableError,
)
from music_assistant_models.media_items import (
    Artist,
    AudioFormat,
    MediaItem,
    MediaItemImage,
    MediaItemMetadata,
    MediaItemType,
    Playlist,
    ProviderMapping,
    Radio,
    SoundEffect,
    Track,
    UniqueList,
    media_from_dict,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import (
    CUSTOM_IMAGES_DIRNAME,
    GENRE_ICONS_DIR_NAME,
    MASS_LOGO,
    PLAYLIST_MEDIA_TYPES,
    RESOURCES_DIR,
    VARIOUS_ARTISTS_FANART,
    PlaylistPlayableItem,
)
from music_assistant.controllers.cache import use_cache
from music_assistant.controllers.tasks.context import (
    get_current_task_id,
    report_current_task_failure,
    update_current_task_progress_from_index,
    update_current_task_progress_text,
)
from music_assistant.helpers.compare import compare_strings
from music_assistant.helpers.playlists import (
    ImageInfo,
    IsHLSPlaylist,
    PlaylistItem,
    ProviderMappingInfo,
    construct_media_item_from_playlist_item,
    fetch_playlist,
    generate_m3u,
    media_item_to_playlist_item,
    parse_extinf_title,
    parse_m3u,
    parse_m3u_playlist_image,
    parse_m3u_playlist_name,
)
from music_assistant.helpers.security import is_safe_path
from music_assistant.helpers.tags import AudioTags, async_parse_tags
from music_assistant.helpers.track_filter import filter_tracks, get_track_filter
from music_assistant.helpers.uri import parse_uri
from music_assistant.models.music_provider import MusicProvider

from .constants import (
    ALL_FAVORITE_TRACKS,
    BUILTIN_PLAYLISTS,
    BUILTIN_PLAYLISTS_ENTRIES,
    COLLAGE_IMAGE_PLAYLISTS,
    CONF_ENTRY_LIBRARY_SYNC_BACK_HIDDEN,
    CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS_HIDDEN,
    CONF_ENTRY_LIBRARY_SYNC_RADIOS_HIDDEN,
    CONF_ENTRY_LIBRARY_SYNC_TRACKS_HIDDEN,
    CONF_KEY_PLAYLISTS,
    CONF_KEY_RADIOS,
    CONF_KEY_TRACKS,
    DEFAULT_FANART,
    DEFAULT_THUMB,
    DYNAMIC_BUILTIN_PLAYLISTS,
    INFINITE_MIX,
    INFINITE_MIX_FAVORITES,
    RANDOM_ALBUM,
    RANDOM_ARTIST,
    RANDOM_TRACKS,
    RECENTLY_ADDED_TRACKS,
    RECENTLY_PLAYED,
    StoredItem,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CACHE_CATEGORY_MEDIA_INFO: Final[int] = 1
CACHE_CATEGORY_PLAYLISTS: Final[int] = 2

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_RADIOS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.LIBRARY_TRACKS_EDIT,
    ProviderFeature.LIBRARY_RADIOS_EDIT,
    ProviderFeature.LIBRARY_PLAYLISTS_EDIT,
    ProviderFeature.PLAYLIST_CREATE,
    ProviderFeature.PLAYLIST_CREATE_AUDIOBOOKS,
    ProviderFeature.PLAYLIST_CREATE_PODCAST_EPISODES,
    ProviderFeature.PLAYLIST_CREATE_RADIOS,
    ProviderFeature.PLAYLIST_CREATE_MIXED,
    ProviderFeature.PLAYLIST_TRACKS_EDIT,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return BuiltinProvider(mass, manifest, config, SUPPORTED_FEATURES)


class BuiltinProvider(MusicProvider):
    """Built-in/generic provider to handle (manually added) media from files and (remote) urls."""

    _playlists_dir: str
    _playlist_lock: asyncio.Lock
    _playlist_locks: dict[str, asyncio.Lock]

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to setup this provider."""
        return (
            *BUILTIN_PLAYLISTS_ENTRIES,
            # hide some of the default (dynamic) entries for library management
            CONF_ENTRY_LIBRARY_SYNC_TRACKS_HIDDEN,
            CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS_HIDDEN,
            CONF_ENTRY_LIBRARY_SYNC_RADIOS_HIDDEN,
            CONF_ENTRY_LIBRARY_SYNC_BACK_HIDDEN,
        )

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        self._playlist_lock = asyncio.Lock()
        self._playlist_locks = {}
        self._playlists_dir = os.path.join(self.mass.storage_path, "playlists")
        if not await asyncio.to_thread(os.path.exists, self._playlists_dir):
            await asyncio.to_thread(os.mkdir, self._playlists_dir)
        await super().loaded_in_mass()
        # run in the background to avoid blocking startup. besides migrating old-style
        # playlists, this repairs entries whose manually set name or artwork no longer
        # matches the builtin config, which is not a one-off.
        # TODO: drop the config->M3U migration after MA 2.9, keep the repair pass
        self.mass.tasks.register_scheduled_task(
            task_id="migrate_builtin_playlists",
            name="Builtin provider playlist migration",
            handler=self._migrate_playlists,
            schedule=TaskSchedule.hourly(every=24),
            initial_delay=60,
        )
        # register API commands for manual item management
        self.mass.register_api_command(
            "builtin/add_radio", self.add_radio, required_scope=Scope.LIBRARY_WRITE
        )
        self.mass.register_api_command(
            "builtin/add_track", self.add_track, required_scope=Scope.LIBRARY_WRITE
        )

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return False

    def get_default_library_sync_schedule(self, media_type: MediaType) -> TaskSchedule:
        """Return the default recurring schedule for builtin library sync tasks."""
        return TaskSchedule.hourly(every=3)

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        parsed_item = await self.parse_item(prov_track_id, requested_media_type=MediaType.TRACK)
        assert isinstance(parsed_item, Track)
        return parsed_item

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id."""
        parsed_item = await self.parse_item(prov_radio_id, force_radio=True)
        assert isinstance(parsed_item, Radio)
        return parsed_item

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        artist = prov_artist_id
        # this is here for compatibility reasons only
        return Artist(
            item_id=artist,
            provider=self.domain,
            name=artist,
            provider_mappings={
                ProviderMapping(
                    item_id=artist,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    available=False,
                )
            },
        )

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        if prov_playlist_id in BUILTIN_PLAYLISTS:
            # this is one of our builtin/default playlists
            return Playlist(
                item_id=prov_playlist_id,
                provider=self.instance_id,
                name=BUILTIN_PLAYLISTS[prov_playlist_id],
                translation_key=prov_playlist_id,
                provider_mappings={
                    ProviderMapping(
                        item_id=prov_playlist_id,
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                    )
                },
                owner="Music Assistant",
                is_editable=False,
                is_dynamic=prov_playlist_id in DYNAMIC_BUILTIN_PLAYLISTS,
                metadata=MediaItemMetadata(
                    images=UniqueList([DEFAULT_THUMB])
                    if prov_playlist_id in COLLAGE_IMAGE_PLAYLISTS
                    else UniqueList([DEFAULT_THUMB, DEFAULT_FANART]),
                ),
            )
        # user created playlist - read from M3U file on disk
        playlist_file = os.path.join(self._playlists_dir, f"{prov_playlist_id}.m3u")
        if not await asyncio.to_thread(os.path.isfile, playlist_file):
            raise MediaNotFoundError(f"Playlist file not found: {prov_playlist_id}")
        # read playlist name and image from M3U
        m3u_data = await self._read_m3u_file(prov_playlist_id)
        playlist_name = parse_m3u_playlist_name(m3u_data) or prov_playlist_id
        metadata = MediaItemMetadata()
        if image_url := parse_m3u_playlist_image(m3u_data):
            metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=image_url,
                        provider=self.domain,
                        remotely_accessible=image_url.startswith("http"),
                    )
                ]
            )
        return Playlist(
            item_id=prov_playlist_id,
            provider=self.instance_id,
            name=playlist_name,
            provider_mappings={
                ProviderMapping(
                    item_id=prov_playlist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            owner="Music Assistant",
            # MediaType.SOUND_EFFECT is deliberately left out here: clients that do not
            # know this media type yet reject the entire playlist listing when they
            # receive it. Sound effects can still be added to these playlists, as the
            # builtin provider accepts any uri regardless of this (advisory) set.
            supported_mediatypes={
                MediaType.AUDIOBOOK,
                MediaType.PODCAST_EPISODE,
                MediaType.RADIO,
                MediaType.TRACK,
            },
            is_editable=True,
            metadata=metadata,
        )

    async def get_library_tracks(self) -> AsyncGenerator[Track]:
        """Retrieve library tracks from the provider."""
        stored_items: list[StoredItem] = self.mass.config.get(CONF_KEY_TRACKS, [])
        for item in stored_items:
            try:
                yield await self.get_track(item["item_id"])
            except MediaNotFoundError as err:
                self.logger.warning("Track %s not found: %s", item, err)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist]:
        """Retrieve library/subscribed playlists from the provider."""
        # return user stored playlists from M3U files on disk
        for filename in await asyncio.to_thread(os.listdir, self._playlists_dir):
            if not filename.endswith(".m3u"):
                continue
            playlist_id = filename[:-4]  # strip .m3u extension
            try:
                yield await self.get_playlist(playlist_id)
            except MediaNotFoundError:
                self.logger.warning("Playlist file %s not found", filename)
        # return builtin playlists
        for item_id in BUILTIN_PLAYLISTS:
            if self.config.get_value(item_id) is False:
                continue
            yield await self.get_playlist(item_id)

    async def get_library_radios(self) -> AsyncGenerator[Radio]:
        """Retrieve library/subscribed radio stations from the provider."""
        stored_items: list[StoredItem] = self.mass.config.get(CONF_KEY_RADIOS, [])
        for item in stored_items:
            try:
                yield await self.get_radio(item["item_id"])
            except (MediaNotFoundError, InvalidDataError) as err:
                self.logger.warning("Radio station %s not found: %s", item, err)
                yield Radio(
                    item_id=item["item_id"],
                    provider=self.instance_id,
                    name=item["name"],
                    provider_mappings={
                        ProviderMapping(
                            item_id=item["item_id"],
                            provider_domain=self.domain,
                            provider_instance=self.instance_id,
                            available=False,
                        )
                    },
                )

    async def library_add(self, item: MediaItemType) -> bool:
        """Add item to provider's library. Return true on success."""
        if item.media_type == MediaType.TRACK:
            key = CONF_KEY_TRACKS
        elif item.media_type == MediaType.RADIO:
            key = CONF_KEY_RADIOS
        else:
            return False
        stored_item = StoredItem(item_id=item.item_id, name=item.name)
        if item.image:
            stored_item["image_url"] = item.image.path
        stored_items: list[StoredItem] = self.mass.config.get(key, [])
        # filter out existing
        stored_items = [x for x in stored_items if x["item_id"] != item.item_id]
        stored_items.append(stored_item)
        self.mass.config.set(key, stored_items)
        return True

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove item from provider's library. Return true on success."""
        if media_type == MediaType.PLAYLIST and prov_item_id in BUILTIN_PLAYLISTS:
            # user wants to disable/remove one of our builtin playlists
            # to prevent it comes back, we mark it as disabled in config
            self._update_config_value(prov_item_id, False)
            return True
        if media_type == MediaType.TRACK:
            # regular manual track URL/path
            key = CONF_KEY_TRACKS
        elif media_type == MediaType.RADIO:
            # regular manual radio URL/path
            key = CONF_KEY_RADIOS
        elif media_type == MediaType.PLAYLIST:
            # user-created playlist removal - delete the M3U file
            playlist_file = os.path.join(self._playlists_dir, f"{prov_item_id}.m3u")
            if await asyncio.to_thread(os.path.isfile, playlist_file):
                async with self._playlist_lock:
                    await asyncio.to_thread(os.remove, playlist_file)
            return True
        else:
            return False
        stored_items: list[StoredItem] = self.mass.config.get(key, [])
        stored_items = [x for x in stored_items if x["item_id"] != prov_item_id]
        self.mass.config.set(key, stored_items)
        return True

    async def on_item_updated(self, item: MediaItemType) -> None:
        """
        Update stored item config when a library item is edited.

        :param item: The updated media item with new metadata.
        """
        # find the builtin provider mapping to get the item_id
        builtin_mapping = next(
            (pm for pm in item.provider_mappings if pm.provider_domain == self.domain),
            None,
        )
        if not builtin_mapping:
            return

        if item.media_type == MediaType.PLAYLIST:
            image_url = item.image.path if item.image else None
            await self._update_playlist_metadata(builtin_mapping.item_id, item.name, image_url)
            return

        if item.media_type == MediaType.RADIO:
            key = CONF_KEY_RADIOS
        elif item.media_type == MediaType.TRACK:
            key = CONF_KEY_TRACKS
        else:
            return

        # TODO: also allow updating description and other image types
        stored_items: list[StoredItem] = self.mass.config.get(key, [])
        for stored_item in stored_items:
            if stored_item["item_id"] == builtin_mapping.item_id:
                stored_item["name"] = item.name
                if item.image:
                    stored_item["image_url"] = item.image.path
                elif "image_url" in stored_item:
                    del stored_item["image_url"]
                break
        self.mass.config.set(key, stored_items)

    async def add_radio(self, url: str, name: str, image_url: str | None = None) -> Radio:
        """
        Add a radio station.

        :param url: Stream URL.
        :param name: Display name.
        :param image_url: Image URL.
        """
        stored_items: list[StoredItem] = self.mass.config.get(CONF_KEY_RADIOS, [])
        # Remove existing entry with same URL if present
        stored_items = [x for x in stored_items if x["item_id"] != url]
        stored_item = StoredItem(item_id=url, name=name)
        if image_url:
            stored_item["image_url"] = image_url
        stored_items.append(stored_item)
        self.mass.config.set(CONF_KEY_RADIOS, stored_items)
        # Trigger library sync
        self.mass.call_later(
            1,
            self.mass.music.start_sync,
            [MediaType.RADIO],
            [self.instance_id],
        )
        return await self.get_radio(url)

    async def add_track(self, url: str, name: str, image_url: str | None = None) -> Track:
        """
        Add a track.

        :param url: URL or local path.
        :param name: Display name.
        :param image_url: Image URL.
        """
        stored_items: list[StoredItem] = self.mass.config.get(CONF_KEY_TRACKS, [])
        # Remove existing entry with same URL if present
        stored_items = [x for x in stored_items if x["item_id"] != url]
        stored_item = StoredItem(item_id=url, name=name)
        if image_url:
            stored_item["image_url"] = image_url
        stored_items.append(stored_item)
        self.mass.config.set(CONF_KEY_TRACKS, stored_items)
        # Trigger library sync
        self.mass.call_later(
            1,
            self.mass.music.start_sync,
            [MediaType.TRACK],
            [self.instance_id],
        )
        return await self.get_track(url)

    async def get_playlist_tracks(
        self, prov_playlist_id: str, page: int = 0
    ) -> list[PlaylistPlayableItem]:
        """Get playlist tracks (paginated, 500 items per page)."""
        if prov_playlist_id in BUILTIN_PLAYLISTS:
            if page > 0:
                return []
            return list(await self._get_builtin_playlist_tracks(prov_playlist_id))
        return await self._get_user_playlist_tracks(prov_playlist_id, page)

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add track(s) to playlist with full metadata and deduplication."""
        async with self._get_playlist_lock(prov_playlist_id):
            m3u_data = await self._read_m3u_file(prov_playlist_id)
            existing_items = parse_m3u(m3u_data)
            # build dedup set from existing URIs and provider item_ids
            existing_item_ids: set[str] = set()
            for item in existing_items:
                existing_item_ids.add(item.path)
                for prov in item.providers:
                    existing_item_ids.add(f"{prov.domain}:{prov.item_id}")
            entries: list[PlaylistItem] = list(existing_items)
            for uri in prov_track_ids:
                if uri in existing_item_ids:
                    continue
                try:
                    entry = await self._build_m3u_entry_from_uri(uri)
                except MediaNotFoundError, InvalidDataError, ProviderUnavailableError:
                    self.logger.warning("Can't add %s to playlist - item not found", uri)
                    continue
                # check dedup against the newly built entry's providers too
                new_ids = {entry.path}
                if entry.providers:
                    new_ids.update(f"{p.domain}:{p.item_id}" for p in entry.providers)
                if new_ids & existing_item_ids:
                    continue
                existing_item_ids.update(new_ids)
                entries.append(entry)
            # write updated M3U file
            playlist = await self.get_playlist(prov_playlist_id)
            await self._write_m3u_file(
                prov_playlist_id,
                playlist.name,
                entries,
                self._get_playlist_image_url(playlist),
            )

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist."""
        async with self._get_playlist_lock(prov_playlist_id):
            m3u_data = await self._read_m3u_file(prov_playlist_id)
            existing_items = parse_m3u(m3u_data)
            # remove items by position (1-indexed)
            for i in sorted(positions_to_remove, reverse=True):
                del existing_items[i - 1]
            playlist = await self.get_playlist(prov_playlist_id)
            await self._write_m3u_file(
                prov_playlist_id,
                playlist.name,
                list(existing_items),
                self._get_playlist_image_url(playlist),
            )

    async def create_playlist(self, name: str, media_types: set[MediaType]) -> Playlist:
        """
        Create a new playlist on provider with given name.

        The playlist name is used as the filename (sanitized for filesystem safety).
        """
        playlist_id = self._sanitize_playlist_id(name)
        # ensure uniqueness
        counter = 1
        base_id = playlist_id
        while await asyncio.to_thread(
            os.path.isfile, os.path.join(self._playlists_dir, f"{playlist_id}.m3u")
        ):
            playlist_id = f"{base_id} ({counter})"
            counter += 1
        # create empty M3U file with header
        await self._write_m3u_file(playlist_id, name, [])
        return await self.get_playlist(playlist_id)

    async def import_playlist(self, m3u_data: str) -> Playlist:
        """
        Import a playlist from M3U8 format.

        Creates a new playlist and populates it with items from the M3U data.
        Items with valid MA URIs are added directly. Plain URLs or unresolvable
        URIs are stored as-is for later matching.

        :param m3u_data: The M3U8 playlist data as a string.
        """
        parsed_items = parse_m3u(m3u_data)
        if not parsed_items:
            msg = "No items found in M3U data"
            raise InvalidDataError(msg)
        playlist_name = parse_m3u_playlist_name(m3u_data) or "Imported Playlist"
        playlist = await self.create_playlist(
            playlist_name,
            media_types={MediaType.TRACK, MediaType.RADIO},
        )
        playlist_image_url = parse_m3u_playlist_image(m3u_data)
        # Write the parsed items directly as the M3U file, preserving all
        # metadata from the source. This avoids re-resolving items that
        # already have rich metadata (e.g. exported from another MA instance).
        await self._write_m3u_file(
            playlist.item_id,
            playlist_name,
            parsed_items,
            playlist_image_url,
        )
        return await self.get_playlist(playlist.item_id)

    async def match_imported_playlist_tracks(
        self,
        prov_playlist_id: str,
        match_providers: list[str] | None = None,
    ) -> None:
        """
        Match imported playlist tracks against available providers.

        Iterates through playlist items whose provider is unavailable,
        searching other providers for matches using metadata stored in
        the M3U file. Matched tracks are replaced in-place.

        :param prov_playlist_id: The provider-side playlist ID.
        :param match_providers: Optional list of provider instance IDs or
            domains to search. When None, all providers are searched.
        """
        m3u_data = await self._read_m3u_file(prov_playlist_id)
        parsed_items = parse_m3u(m3u_data)
        if not parsed_items:
            return

        total = len(parsed_items)
        matched_count = 0
        unmatched_count = 0
        changed = False

        for index, item in enumerate(parsed_items):
            update_current_task_progress_from_index(
                index, total, f"Matching track {index + 1}/{total}"
            )
            if not item.title:
                continue
            # check if the URI's provider is available
            needs_matching = False
            media_type = MediaType.TRACK
            try:
                media_type, prov_instance, _item_id = await parse_uri(item.path)
                if media_type == MediaType.RADIO:
                    continue
                if not self.mass.get_provider(prov_instance):
                    needs_matching = True
            except Exception:
                needs_matching = True

            if not needs_matching:
                continue

            matched_uri = await self._match_track_by_metadata(item, match_providers=match_providers)
            if matched_uri:
                # enrich the entry with full metadata (#EXTPROV etc.) so it resolves to a
                # playable item - just storing the URI leaves it without provider mappings
                try:
                    parsed_items[index] = await self._build_m3u_entry_from_uri(matched_uri)
                except MediaNotFoundError, InvalidDataError, ProviderUnavailableError:
                    item.path = matched_uri
                changed = True
                matched_count += 1
            else:
                report_current_task_failure(f"No match found for: {item.title}")
                unmatched_count += 1

        if changed:
            playlist = await self.get_playlist(prov_playlist_id)
            await self._write_m3u_file(
                prov_playlist_id,
                playlist.name,
                parsed_items,
                self._get_playlist_image_url(playlist),
            )

        self.logger.info(
            "Import matching: %d matched, %d unmatched out of %d items",
            matched_count,
            unmatched_count,
            total,
        )
        update_current_task_progress_from_index(total, total, "Matching complete")

    async def parse_item(
        self,
        url: str,
        force_refresh: bool = False,
        force_radio: bool = False,
        requested_media_type: MediaType | None = None,
    ) -> Track | Radio | SoundEffect:
        """Parse a plain URL to a Track, Radio, or SoundEffect item."""
        media_info = await self._get_media_info(url, force_refresh)
        is_radio = media_info.get("icyname") or not media_info.duration
        provider_mappings = {
            ProviderMapping(
                item_id=url,
                provider_domain=self.domain,
                provider_instance=self.instance_id,
                audio_format=AudioFormat(
                    content_type=ContentType.try_parse(media_info.format),
                    sample_rate=media_info.sample_rate,
                    bit_depth=media_info.bits_per_sample,
                    bit_rate=media_info.bit_rate,
                ),
            )
        }
        media_item: Track | Radio | SoundEffect
        if requested_media_type == MediaType.SOUND_EFFECT:
            media_item = SoundEffect(
                item_id=url,
                provider=self.domain,
                name=media_info.title or url,
                provider_mappings=provider_mappings,
            )
            if media_info.duration:
                media_item.duration = int(media_info.duration or 0)
        elif (is_radio or force_radio) and requested_media_type != MediaType.TRACK:
            # treat as radio, unless a track was explicitly requested: such a track
            # stays a track, also when its stream carries an ICY name or no duration
            media_item = Radio(
                item_id=url,
                provider=self.domain,
                name=media_info.get("icyname")
                or media_info.get("programtitle")
                or media_info.title
                or url,
                provider_mappings=provider_mappings,
            )
        else:
            media_item = Track(
                item_id=url,
                provider=self.domain,
                name=media_info.title or url,
                duration=int(media_info.duration or 0),
                artists=UniqueList(
                    [await self.get_artist(artist) for artist in media_info.artists]
                ),
                provider_mappings=provider_mappings,
            )

        if media_info.has_cover_image:
            media_item.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=url,
                        provider=self.domain,
                        remotely_accessible=False,
                    )
                ]
            )
        if isinstance(media_item, Track | Radio):
            self._apply_stored_details(media_item)
        return media_item

    async def resolve_image(self, path: str) -> str | bytes:
        """
        Resolve an image from an image path.

        This either returns (a generator to get) raw bytes of the image or
        a string with an http(s) URL or local path that is accessible from the server.
        """
        if path == "logo.png":
            return MASS_LOGO
        if path in ("fanart.jpg", "fallback_fanart.jpeg"):
            return VARIOUS_ARTISTS_FANART
        if path.startswith(f"{GENRE_ICONS_DIR_NAME}/"):
            icon_name = path[len(GENRE_ICONS_DIR_NAME) + 1 :]
            icons_base = RESOURCES_DIR.joinpath(GENRE_ICONS_DIR_NAME)
            if not is_safe_path(icon_name, str(icons_base)):
                raise FileNotFoundError(f"Invalid genre icon reference: {path}")
            return str(icons_base.joinpath(icon_name))
        if path.startswith(f"{CUSTOM_IMAGES_DIRNAME}/"):
            file_name = path[len(CUSTOM_IMAGES_DIRNAME) + 1 :]
            images_base = os.path.join(self.mass.storage_path, CUSTOM_IMAGES_DIRNAME)
            if not is_safe_path(file_name, images_base):
                raise FileNotFoundError(f"Invalid custom image reference: {path}")
            # return raw bytes (instead of the local path) so the format is not
            # restricted by the extension allowlist of the local file handling
            async with aiofiles.open(os.path.join(images_base, file_name), "rb") as imgfile:
                return cast("bytes", await imgfile.read())
        return path

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get stream details for a track, radio stream, or sound effect."""
        media_info = await self._get_media_info(item_id)
        is_radio = media_info.get("icyname") or not media_info.duration
        stream_media_type = (
            MediaType.SOUND_EFFECT
            if media_type == MediaType.SOUND_EFFECT
            else MediaType.RADIO
            if is_radio
            else MediaType.TRACK
        )
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.try_parse(media_info.format),
                sample_rate=media_info.sample_rate,
                bit_depth=media_info.bits_per_sample,
                channels=media_info.channels,
            ),
            media_type=stream_media_type,
            stream_type=StreamType.HTTP,
            path=item_id,
            can_seek=not is_radio,
            allow_seek=not is_radio,
        )

    @staticmethod
    def _get_playlist_image_url(playlist: Playlist) -> str | None:
        """Return the playlist-level image URL to persist in the M3U header."""
        return playlist.image.path if playlist.image else None

    async def _update_playlist_metadata(
        self, playlist_id: str, new_name: str, image_url: str | None
    ) -> None:
        """Update the name and image of a playlist in its M3U file."""
        if playlist_id in BUILTIN_PLAYLISTS:
            # builtin playlists are not editable
            return
        m3u_data = await self._read_m3u_file(playlist_id)
        if not m3u_data:
            return
        existing_items = parse_m3u(m3u_data)
        try:
            await self._write_m3u_file(playlist_id, new_name, list(existing_items), image_url)
        except OSError as err:
            self.logger.warning("Failed to update playlist metadata: %s", err)

    async def _match_track_by_metadata(
        self,
        item: PlaylistItem,
        match_providers: list[str] | None = None,
    ) -> str | None:
        """
        Search providers for a track matching the given PlaylistItem metadata.

        Uses ISRC/MusicBrainz ID for exact matching first, then falls back
        to fuzzy title/artist/duration matching.

        :param item: The PlaylistItem with metadata from the M3U file.
        :param match_providers: Optional list of provider instance IDs or
            domains to limit the search.
        """
        artist_name, track_name = parse_extinf_title(item.title)
        if not track_name:
            return None

        search_query = f"{artist_name} - {track_name}" if artist_name else track_name

        all_providers = self.mass.music.get_unique_providers()
        if match_providers:
            provider_domains: dict[str, str] = {}
            for pid in all_providers:
                prov = self.mass.get_provider(pid)
                if prov:
                    provider_domains[pid] = prov.domain
            all_providers = [
                pid
                for pid in all_providers
                if pid in match_providers or provider_domains.get(pid) in match_providers
            ]

        best_match: tuple[int, str] | None = None

        for provider_id in all_providers:
            try:
                results = await self.mass.music.tracks.search(search_query, provider_id, limit=5)
            except Exception:
                self.logger.debug(
                    "Search failed on provider %s for '%s'", provider_id, search_query
                )
                continue

            for result in results:
                if not result.uri:
                    continue
                score = self._score_track_match(result, item)
                if score >= 10:
                    self.logger.debug("Exact ID match for '%s' -> %s", search_query, result.uri)
                    return result.uri
                if score > 0 and (best_match is None or score > best_match[0]):
                    best_match = (score, result.uri)

        if best_match:
            self.logger.debug(
                "Matched '%s' -> %s (score=%d)",
                search_query,
                best_match[1],
                best_match[0],
            )
            return best_match[1]

        self.logger.info("No match found for '%s'", search_query)
        return None

    def _score_track_match(
        self,
        candidate: Track,
        item: PlaylistItem,
    ) -> int:
        """
        Score how well a candidate track matches the PlaylistItem metadata.

        Returns 0 for no match, higher scores for better matches.
        ISRC or MusicBrainz Recording ID match returns 10 (maximum).

        :param candidate: The track from search results.
        :param item: The PlaylistItem with metadata from the M3U file.
        """
        metadata = item.metadata or {}
        artist_name, track_name = parse_extinf_title(item.title)
        if not track_name:
            return 0

        isrc = metadata.get("isrc")
        mbid = metadata.get("mbid")

        # exact ID matches (cross-provider definitive match)
        if isrc:
            candidate_isrc = candidate.get_external_id(ExternalID.ISRC)
            if candidate_isrc and candidate_isrc.upper() == isrc.upper():
                return 10
        if mbid:
            candidate_mbid = candidate.get_external_id(ExternalID.MB_RECORDING)
            if candidate_mbid and candidate_mbid.lower() == mbid.lower():
                return 10

        # media type gate
        if metadata.get("media_type"):
            candidate_type = getattr(candidate, "media_type", None)
            if candidate_type and candidate_type.value != metadata["media_type"]:
                return 0

        return self._score_fuzzy_metadata(candidate, artist_name, track_name, metadata, item)

    def _score_fuzzy_metadata(
        self,
        candidate: Track,
        artist_name: str | None,
        track_name: str,
        metadata: dict[str, str],
        item: PlaylistItem,
    ) -> int:
        """
        Score fuzzy metadata fields (title, artist, album, duration, version).

        :param candidate: The track from search results.
        :param artist_name: Parsed artist name from EXTINF, or None.
        :param track_name: Parsed track title from EXTINF.
        :param metadata: The #EXTMA metadata dict.
        :param item: The PlaylistItem (for duration from item.length).
        """
        if not compare_strings(candidate.name, track_name, strict=False):
            return 0
        score = 1

        if artist_name:
            candidate_artists = [a.name for a in candidate.artists] if candidate.artists else []
            if not any(compare_strings(a, artist_name, strict=False) for a in candidate_artists):
                return 0
            score += 2

        score += self._score_bonus_fields(candidate, metadata)
        score += self._score_duration(candidate, item)
        return score

    @staticmethod
    def _score_bonus_fields(candidate: Track, metadata: dict[str, str]) -> int:
        """Score bonus metadata fields: podcast, authors, album, version."""
        score = 0
        if metadata.get("podcast"):
            candidate_podcast = getattr(candidate, "podcast", None)
            if candidate_podcast and hasattr(candidate_podcast, "name"):
                if compare_strings(candidate_podcast.name, metadata["podcast"], strict=False):
                    score += 2

        if metadata.get("authors"):
            candidate_authors = getattr(candidate, "authors", None)
            if candidate_authors:
                if compare_strings("; ".join(candidate_authors), metadata["authors"], strict=False):
                    score += 2

        if metadata.get("album"):
            candidate_album = getattr(candidate, "album", None)
            if candidate_album and hasattr(candidate_album, "name"):
                if compare_strings(candidate_album.name, metadata["album"], strict=False):
                    score += 1

        if metadata.get("version"):
            candidate_version = getattr(candidate, "version", None) or ""
            if candidate_version and compare_strings(
                candidate_version, metadata["version"], strict=False
            ):
                score += 1
            elif candidate_version:
                score -= 1

        return score

    @staticmethod
    def _score_duration(candidate: Track, item: PlaylistItem) -> int:
        """Score duration proximity between candidate and playlist item."""
        try:
            duration = int(item.length) if item.length else None
        except ValueError:
            return 0
        if duration is None or duration <= 0 or candidate.duration <= 0:
            return 0
        diff = abs(candidate.duration - duration)
        if diff <= 2:
            return 2
        if diff <= 5:
            return 1
        return 0

    def _get_stored_item(
        self, item: PlaylistItem, stored_by_media_type: Mapping[str, Mapping[str, StoredItem]]
    ) -> StoredItem | None:
        """
        Return the stored details of a manually added playlist entry, if it is one.

        :param item: The playlist entry as parsed from an M3U file.
        :param stored_by_media_type: Stored items per media type, each keyed on item_id.
        """
        prov_mapping = next((x for x in item.providers if x.domain == self.domain), None)
        if prov_mapping is None:
            return None
        media_type = (item.metadata or {}).get("media_type", "")
        return stored_by_media_type.get(media_type, {}).get(prov_mapping.item_id)

    def _stored_details_differ(
        self, item: PlaylistItem, stored_by_media_type: Mapping[str, Mapping[str, StoredItem]]
    ) -> bool:
        """
        Return True when a playlist entry no longer carries its manually set name or image.

        :param item: The playlist entry as parsed from an M3U file.
        :param stored_by_media_type: Stored items per media type, each keyed on item_id.
        """
        stored_item = self._get_stored_item(item, stored_by_media_type)
        if stored_item is None:
            return False
        # an M3U file cannot hold the surrounding whitespace of a name, so comparing
        # against the raw stored name would report a difference that no rewrite can settle
        if stored_item["name"].strip() != (item.metadata or {}).get("name"):
            return True
        # a stored item without an image is not a difference: cover art from the stream
        # is a valid fallback for as long as the user has set no image of their own
        if image_url := stored_item.get("image_url"):
            # only the thumbnail counts: the same url as another image type still leaves
            # the stream's cover art as the one that shows
            return not any(
                image.type == ImageType.THUMB.value and image.path == image_url
                for image in item.images
            )
        return False

    def _restore_stored_details(
        self, item: PlaylistItem, stored_by_media_type: Mapping[str, Mapping[str, StoredItem]]
    ) -> None:
        """
        Write the manually set name and image of a playlist entry back into it.

        :param item: The playlist entry to update in place.
        :param stored_by_media_type: Stored items per media type, each keyed on item_id.
        """
        stored_item = self._get_stored_item(item, stored_by_media_type)
        if stored_item is None:
            return
        name = stored_item["name"]
        item.metadata = {**(item.metadata or {}), "name": name}
        # #EXTINF holds "<artists> - <name>" for a track and the plain name for a radio
        # station, which has no artists; mirror how the entry would have been written
        item.title = f"{', '.join(x.name for x in item.artists)} - {name}" if item.artists else name
        if image_url := stored_item.get("image_url"):
            item.images = [
                ImageInfo(
                    type=ImageType.THUMB.value,
                    path=image_url,
                    provider=self.domain,
                    remotely_accessible=image_url.startswith("http"),
                ),
                *(x for x in item.images if x.type != ImageType.THUMB.value),
            ]

    def _apply_stored_details(self, media_item: Track | Radio) -> None:
        """Apply the name and image stored for a manually added track or radio station."""
        key = CONF_KEY_RADIOS if isinstance(media_item, Radio) else CONF_KEY_TRACKS
        stored_items: list[StoredItem] = self.mass.config.get(key, [])
        stored_item = next(
            (x for x in stored_items if x["item_id"] == media_item.item_id),
            None,
        )
        if stored_item is None:
            return
        media_item.name = stored_item["name"]
        if image_url := stored_item.get("image_url"):
            # the stored image replaces any cover art on the stream, so exactly one
            # thumbnail is left to serialise into a playlist entry
            media_item.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=image_url,
                        provider=self.domain,
                        remotely_accessible=image_url.startswith("http"),
                    ),
                    *(x for x in (media_item.metadata.images or []) if x.type != ImageType.THUMB),
                ]
            )

    async def _resolve_url(self, url: str) -> str:
        """
        Resolve a URL to the actual stream URL.

        ffprobe cannot analyze PLS/M3U files directly as it sees them as text.
        This method extracts the actual audio stream URL from playlist files.

        :param url: The URL to check and potentially resolve.
        :returns: The resolved stream URL, or the original URL if not a playlist.
        """
        parsed = urlparse(url)
        path_lower = parsed.path.lower()
        is_playlist = path_lower.endswith(".pls")
        if not is_playlist:
            return url

        try:
            playlist_items = await fetch_playlist(self.mass, url, raise_on_hls=False)
            for item in playlist_items:
                if item.is_url:
                    return item.path
        except (InvalidDataError, IsHLSPlaylist) as err:
            self.logger.debug("Failed to resolve playlist URL %s: %s", url, err)

        return url

    async def _get_media_info(self, url: str, force_refresh: bool = False) -> AudioTags:
        """Retrieve mediainfo for url."""
        # do we have some cached info for this url ?
        cached_info = await self.mass.cache.get(
            url, provider=self.instance_id, category=CACHE_CATEGORY_MEDIA_INFO
        )
        if cached_info and not force_refresh:
            return AudioTags.parse(cached_info)
        resolved_url = await self._resolve_url(url)
        # parse info with ffprobe (and store in cache)
        media_info = await async_parse_tags(resolved_url)
        if "authSig" in url:
            media_info.has_cover_image = False
        await self.mass.cache.set(
            url, media_info.raw, provider=self.instance_id, category=CACHE_CATEGORY_MEDIA_INFO
        )
        return media_info

    @use_cache(expiration=120, category=CACHE_CATEGORY_PLAYLISTS)
    async def _get_builtin_playlist_random_favorite_tracks(self) -> list[Track]:
        result: list[Track] = []
        res = await self.mass.music.tracks.library_items(
            favorite=True, limit=250000, order_by="random_play_count", summary=False
        )
        for idx, item in enumerate(res, 1):
            item.position = idx
            result.append(item)
        return result

    @use_cache(expiration=120, category=CACHE_CATEGORY_PLAYLISTS)
    async def _get_builtin_playlist_random_tracks(self) -> list[Track]:
        result: list[Track] = []
        res = await self.mass.music.tracks.library_items(
            limit=500, order_by="random_play_count", summary=False
        )
        for idx, item in enumerate(res, 1):
            item.position = idx
            result.append(item)
        return result

    @use_cache(expiration=3600, category=CACHE_CATEGORY_PLAYLISTS)
    async def _get_builtin_playlist_random_album(self) -> list[Track]:
        for random_album in await self.mass.music.albums.get_library_items_by_query(
            limit=1,
            order_by="random",
            extra_query_parts=["album_type != :excluded_album_type"],
            extra_query_params={"excluded_album_type": "single"},
        ):
            tracks = await self.mass.music.albums.tracks(
                random_album.item_id, random_album.provider
            )
            for idx, track in enumerate(tracks, 1):
                track.position = idx
            return tracks
        return []

    @use_cache(expiration=3600, category=CACHE_CATEGORY_PLAYLISTS)
    async def _get_builtin_playlist_random_artist(self) -> list[Track]:
        for source in ("library", "top"):
            for min_tracks_required in (25, 10, 5, 1):
                for random_artist in await self.mass.music.artists.library_items(
                    limit=25, order_by="random", summary=False
                ):
                    if source == "library":
                        tracks = await self.mass.music.artists.tracks(
                            random_artist.item_id, "library"
                        )
                    else:
                        tracks = await self.mass.music.artists.top_tracks(
                            random_artist.item_id, random_artist.provider
                        )
                    if len(tracks) < min_tracks_required:
                        continue
                    for idx, track in enumerate(tracks, 1):
                        track.position = idx
                    return tracks
        return []

    @use_cache(expiration=30, category=CACHE_CATEGORY_PLAYLISTS)
    async def _get_builtin_playlist_recently_played(self) -> list[Track]:
        result: list[Track] = []
        recent_tracks = await self.mass.music.recently_played(100, [MediaType.TRACK])
        for idx, item in enumerate(recent_tracks, 1):
            if not (item_provider := self.mass.get_provider(item.provider)):
                continue
            track = Track(
                item_id=item.item_id,
                provider=item.provider,
                name=item.name,
                provider_mappings={
                    ProviderMapping(
                        item_id=item.item_id,
                        provider_domain=item_provider.domain,
                        provider_instance=item_provider.instance_id,
                    )
                },
            )
            if item.image:
                track.metadata.add_image(item.image)
            track.position = idx
            result.append(track)
        return result

    @use_cache(expiration=60, category=CACHE_CATEGORY_PLAYLISTS)
    async def _get_builtin_playlist_recently_added_tracks(self) -> list[Track]:
        result: list[Track] = []
        recent_tracks = await self.mass.music.recently_added_tracks(100)
        for idx, track in enumerate(recent_tracks, 1):
            track.position = idx
            result.append(track)
        return result

    async def _get_builtin_playlist_infinite_mix(self) -> list[Track]:
        """Return 25 random library tracks for the Infinite Mix dynamic playlist."""
        return await self._infinite_mix_tracks(favorite=None)

    async def _get_builtin_playlist_infinite_mix_favorites(self) -> list[Track]:
        """Return 25 random favorited tracks for the Infinite Mix (favorites) dynamic playlist."""
        return await self._infinite_mix_tracks(favorite=True)

    async def _infinite_mix_tracks(self, *, favorite: bool | None) -> list[Track]:
        """
        Return up to 25 random (optionally favorited) library tracks for an Infinite Mix.

        :param favorite: Restrict to favorited tracks when True; all library tracks when None.
        """
        # over-fetch when a recency filter is published so dropping recently-played tracks still
        # leaves a full mix; the pool is trimmed back to the mix size after filtering
        limit = 25 * 3 if get_track_filter() is not None else 25
        candidates = list(
            await self.mass.music.tracks.library_items(
                favorite=favorite, limit=limit, order_by="random", summary=False
            )
        )
        tracks = filter_tracks(candidates)[:25]
        for idx, track in enumerate(tracks, 1):
            track.position = idx
        return tracks

    async def _get_builtin_playlist_tracks(
        self, builtin_playlist_id: str
    ) -> list[Track] | UniqueList[Track]:
        """Get all playlist tracks for given builtin playlist id."""
        try:
            return await {
                ALL_FAVORITE_TRACKS: self._get_builtin_playlist_random_favorite_tracks,
                RANDOM_TRACKS: self._get_builtin_playlist_random_tracks,
                RANDOM_ALBUM: self._get_builtin_playlist_random_album,
                RANDOM_ARTIST: self._get_builtin_playlist_random_artist,
                RECENTLY_PLAYED: self._get_builtin_playlist_recently_played,
                RECENTLY_ADDED_TRACKS: self._get_builtin_playlist_recently_added_tracks,
                INFINITE_MIX: self._get_builtin_playlist_infinite_mix,
                INFINITE_MIX_FAVORITES: self._get_builtin_playlist_infinite_mix_favorites,
            }[builtin_playlist_id]()
        except KeyError:
            raise MediaNotFoundError(f"No built in playlist: {builtin_playlist_id}")

    async def _read_m3u_file(self, playlist_id: str) -> str:
        """Read the raw M3U file content for a playlist."""
        playlist_file = os.path.join(self._playlists_dir, f"{playlist_id}.m3u")
        if not await asyncio.to_thread(os.path.isfile, playlist_file):
            return ""
        async with (
            self._playlist_lock,
            aiofiles.open(playlist_file, encoding="utf-8") as _file,
        ):
            result: str = await _file.read()
            return result

    async def _write_m3u_file(
        self,
        playlist_id: str,
        playlist_name: str,
        entries: list[PlaylistItem],
        playlist_image_url: str | None = None,
    ) -> None:
        """Write an M3U playlist file to disk."""
        m3u_content = generate_m3u(playlist_name, entries, playlist_image_url)
        playlist_file = os.path.join(self._playlists_dir, f"{playlist_id}.m3u")
        async with (
            self._playlist_lock,
            aiofiles.open(playlist_file, "w", encoding="utf-8") as _file,
        ):
            await _file.write(m3u_content)

    def _get_playlist_lock(self, playlist_id: str) -> asyncio.Lock:
        """Get or create a per-playlist lock for concurrent access protection."""
        if playlist_id not in self._playlist_locks:
            self._playlist_locks[playlist_id] = asyncio.Lock()
        return self._playlist_locks[playlist_id]

    async def _resolve_playlist_item(self, item: PlaylistItem) -> MediaItemType | None:
        """
        Resolve a PlaylistItem to a MediaItem.

        Constructs from stored metadata first. If no providers are available,
        falls back to a library lookup by domain.
        """
        media_item = construct_media_item_from_playlist_item(item, self.mass)
        if media_item is None:
            return None
        # if at least one provider mapping is available, we're done
        if any(pm.available for pm in media_item.provider_mappings):
            return media_item
        # all stored provider instances are unavailable - try library lookup by domain
        media_type = MediaType((item.metadata or {}).get("media_type", "track"))
        if media_type == MediaType.SOUND_EFFECT:
            return media_item
        media_controller = self.mass.music.get_controller(media_type)
        for prov_info in item.providers:
            try:
                library_item = await media_controller.get_library_item_by_prov_id(
                    prov_info.item_id, prov_info.domain
                )
                if library_item is not None:
                    return library_item
            except InvalidDataError, KeyError, NotImplementedError:
                continue
        # return unresolved media item so the entry still shows in the playlist
        return media_item

    async def _get_user_playlist_tracks(
        self, prov_playlist_id: str, page: int
    ) -> list[PlaylistPlayableItem]:
        """Get user-created playlist tracks with caching and parallel resolution."""
        playlist_file = os.path.join(self._playlists_dir, f"{prov_playlist_id}.m3u")
        # use file mtime as cache checksum so edits invalidate the cache
        try:
            stat = await asyncio.to_thread(os.stat, playlist_file)
            cache_checksum = str(int(stat.st_mtime))
        except OSError:
            cache_checksum = "0"

        cache_key = f"playlist_tracks.{prov_playlist_id}.{page}"
        cached = await self.mass.cache.get(
            cache_key,
            provider=self.instance_id,
            checksum=cache_checksum,
            category=CACHE_CATEGORY_PLAYLISTS,
        )
        if cached is not None:
            # cached data is a list of dicts, deserialize back to media items
            return [
                cast("PlaylistPlayableItem", media_from_dict(item_dict))
                if isinstance(item_dict, dict)
                else item_dict
                for item_dict in cached
            ]

        async with self._get_playlist_lock(prov_playlist_id):
            m3u_data = await self._read_m3u_file(prov_playlist_id)
        all_items = parse_m3u(m3u_data)
        page_size = 500
        start = page * page_size
        if start >= len(all_items):
            return []
        page_items = all_items[start : start + page_size]

        # resolve items in parallel with bounded concurrency
        semaphore = asyncio.Semaphore(50)

        async def _resolve(index: int, item: PlaylistItem) -> PlaylistPlayableItem | None:
            async with semaphore:
                try:
                    media_item = await self._resolve_playlist_item(item)
                    if media_item is None:
                        return None
                    if media_item.media_type not in PLAYLIST_MEDIA_TYPES:
                        self.logger.warning(
                            "Unsupported media type in playlist %s: %s",
                            prov_playlist_id,
                            type(media_item),
                        )
                        return None
                    playlist_item = cast("PlaylistPlayableItem", media_item)
                    playlist_item.position = index
                    return playlist_item
                except (
                    MediaNotFoundError,
                    InvalidDataError,
                    ProviderUnavailableError,
                ) as err:
                    self.logger.warning(
                        "Skipping %s in playlist %s: %s",
                        item.path,
                        prov_playlist_id,
                        str(err),
                    )
                return None

        tasks = [_resolve(start + idx + 1, item) for idx, item in enumerate(page_items)]
        resolved = await asyncio.gather(*tasks)
        result = [item for item in resolved if item is not None]

        await self.mass.cache.set(
            key=cache_key,
            data=result,
            expiration=3600 * 24,
            provider=self.instance_id,
            checksum=cache_checksum,
            category=CACHE_CATEGORY_PLAYLISTS,
        )
        return result

    async def _build_m3u_entry_from_uri(self, uri: str) -> PlaylistItem:
        """Fetch a media item by URI and convert it to a PlaylistItem with full metadata."""
        full_item = await self.mass.music.get_item_by_uri(uri, allow_update_metadata=False)
        if not isinstance(full_item, MediaItem):
            msg = f"Unsupported media type for playlist: {uri}"
            raise InvalidDataError(msg)
        return media_item_to_playlist_item(full_item)

    @staticmethod
    def _sanitize_playlist_id(name: str) -> str:
        """Sanitize a playlist name for use as a filename (without extension)."""
        # replace invalid filename characters
        sanitized = re.sub(r'[<>:"/\\|?*]', "_", name)
        # remove leading/trailing spaces and dots
        sanitized = sanitized.strip(" .")
        return sanitized or "untitled"

    async def _migrate_playlists(self) -> None:  # noqa: PLR0915
        """
        Migrate old-style playlists to M3U files and repair incomplete or stale entries.

        Raises RuntimeError when too many entries could not be resolved to keep a broken
        install from rewriting every playlist.
        """
        # migrate playlists stored in config to M3U files on disk with enriched metadata
        stored_items: list[StoredItem] = self.mass.config.get(CONF_KEY_PLAYLISTS, [])
        for stored_item in stored_items:
            # keep the original item_id as filename so library DB references stay valid
            playlist_id = stored_item["item_id"]
            playlist_name = stored_item["name"]
            self.logger.info("Migrating playlist '%s' to M3U format...", playlist_name)
            update_current_task_progress_text(
                f"Migrating playlist '{playlist_name}' to M3U format..."
            )
            old_file = os.path.join(self._playlists_dir, playlist_id)
            # read old URI file and enrich each entry with full metadata
            uris: list[str] = []
            if await asyncio.to_thread(os.path.isfile, old_file):
                async with aiofiles.open(old_file, encoding="utf-8") as _file:
                    lines = await _file.readlines()
                    uris = [line.strip() for line in lines if line.strip()]
            entries: list[PlaylistItem] = []
            for uri in uris:
                try:
                    entries.append(await self._build_m3u_entry_from_uri(uri))
                except MediaNotFoundError, InvalidDataError, ProviderUnavailableError:
                    # parse URI for minimal provider info so the entry is resolvable later
                    entry = PlaylistItem(path=uri)
                    if "://" in uri:
                        try:
                            domain, rest = uri.split("://", 1)
                            media_type_str, item_id = rest.split("/", 1)
                            entry.metadata = {"media_type": media_type_str}
                            entry.providers = [ProviderMappingInfo(domain=domain, item_id=item_id)]
                        except ValueError:
                            pass
                    entries.append(entry)
                    self.logger.debug("Could not enrich migrated entry: %s", uri)
            # write as {item_id}.m3u with the display name in #PLAYLIST
            await self._write_m3u_file(playlist_id, playlist_name, entries)
            # clean up old file (without .m3u extension)
            if await asyncio.to_thread(os.path.isfile, old_file):
                await asyncio.to_thread(os.remove, old_file)
            self.logger.debug("Migrated playlist '%s' -> %s.m3u", playlist_name, playlist_id)
        # clear old config entries
        self.mass.config.remove(CONF_KEY_PLAYLISTS)
        # fix (already migrated) user playlists that have unresolved URIs, or entries whose
        # manually set name or artwork was lost, by re-saving them with enriched metadata
        errors = 0
        # built once: a lookup per entry would rescan the entire config list each time
        stored_by_media_type = {
            MediaType.RADIO.value: {
                x["item_id"]: x for x in self.mass.config.get(CONF_KEY_RADIOS, [])
            },
            MediaType.TRACK.value: {
                x["item_id"]: x for x in self.mass.config.get(CONF_KEY_TRACKS, [])
            },
        }
        for filename in await asyncio.to_thread(os.listdir, self._playlists_dir):
            if not filename.endswith(".m3u"):
                continue
            playlist_id = filename[:-4]  # strip .m3u extension
            m3u_data = await self._read_m3u_file(playlist_id)
            playlist = await self.get_playlist(playlist_id)
            self.logger.debug("Checking playlist '%s' for unresolved entries...", playlist.name)
            update_current_task_progress_text(f"Checking playlist '{playlist.name}'")
            all_items = parse_m3u(m3u_data)
            has_changes = False
            for item in all_items:
                force_migration = item.metadata and item.metadata.get("album") and not item.album
                unresolved = bool(force_migration) or not (
                    item.title and item.providers and item.metadata
                )
                if not unresolved and not self._stored_details_differ(item, stored_by_media_type):
                    continue
                self.logger.debug(
                    "Found %s entry in playlist '%s': %s",
                    "unresolved" if unresolved else "outdated",
                    playlist_id,
                    item.path,
                )
                try:
                    enriched = await self._build_m3u_entry_from_uri(item.path)
                    item.length = enriched.length
                    item.title = enriched.title
                    item.images = enriched.images
                    item.providers = enriched.providers
                    item.metadata = enriched.metadata
                    item.album = enriched.album
                    item.artists = enriched.artists
                    item.podcast = enriched.podcast
                except (MediaNotFoundError, InvalidDataError, ProviderUnavailableError) as err:
                    if unresolved:
                        self.logger.warning(
                            "Could not enrich playlist entry %s during migration: %s",
                            item.path,
                            err,
                        )
                        report_current_task_failure(f"Could not enrich playlist entry: {item.path}")
                        errors += 1
                        continue
                    # an outdated entry is still playable, so failing to reach the stream is
                    # no migration error; restore the stored details without any IO so a
                    # permanently unreachable stream keeps its name and image
                    self.logger.debug(
                        "Could not refresh playlist entry %s, restoring stored details: %s",
                        item.path,
                        err,
                    )
                    self._restore_stored_details(item, stored_by_media_type)
                else:
                    # writing an entry the refresh did not bring back in step would leave
                    # it outdated, and every later run would rewrite the file again
                    if self._stored_details_differ(item, stored_by_media_type):
                        self._restore_stored_details(item, stored_by_media_type)
                    self.logger.debug("Enriched playlist entry %s", item.path)
                has_changes = True
            if has_changes:
                await self._write_m3u_file(
                    playlist_id,
                    playlist.name,
                    list(all_items),
                    self._get_playlist_image_url(playlist),
                )
                self.logger.info("Updated playlist '%s' with enriched metadata", playlist.name)
            if errors > 25:
                raise RuntimeError("Too many errors during playlist migration")
        self.logger.info("Playlist migration completed with %d errors", errors)
        # if there were no errors, we can safely unregister the migration task
        if errors == 0 and (current_task_id := get_current_task_id()):
            # defer unregistering the scheduled task to avoid cancelling the current task
            self.mass.call_later(0, self.mass.tasks.unregister_scheduled_task, current_task_id)
