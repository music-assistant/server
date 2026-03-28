"""Built-in/generic provider to handle media from files and (remote) urls."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Final, cast
from urllib.parse import urlparse

import aiofiles
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
from music_assistant_models.helpers import create_uri
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
    Track,
    UniqueList,
    media_from_dict,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import (
    MASS_LOGO,
    PLAYLIST_MEDIA_TYPES,
    VARIOUS_ARTISTS_FANART,
    PlaylistPlayableItem,
)
from music_assistant.controllers.cache import use_cache
from music_assistant.controllers.tasks.context import (
    get_current_task_id,
    report_current_task_failure,
    update_current_task_progress_text,
)
from music_assistant.helpers.playlists import (
    ImageInfo,
    IsHLSPlaylist,
    PlaylistItem,
    ProviderMappingInfo,
    collect_album_info,
    collect_artist_infos,
    collect_podcast_info,
    construct_media_item_from_playlist_item,
    fetch_playlist,
    generate_m3u,
    parse_m3u,
    parse_m3u_playlist_name,
)
from music_assistant.helpers.tags import AudioTags, async_parse_tags
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
    RANDOM_ALBUM,
    RANDOM_ARTIST,
    RANDOM_TRACKS,
    RECENTLY_ADDED_TRACKS,
    RECENTLY_PLAYED,
    StoredItem,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
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


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    return (
        *BUILTIN_PLAYLISTS_ENTRIES,
        # hide some of the default (dynamic) entries for library management
        CONF_ENTRY_LIBRARY_SYNC_TRACKS_HIDDEN,
        CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS_HIDDEN,
        CONF_ENTRY_LIBRARY_SYNC_RADIOS_HIDDEN,
        CONF_ENTRY_LIBRARY_SYNC_BACK_HIDDEN,
    )


class BuiltinProvider(MusicProvider):
    """Built-in/generic provider to handle (manually added) media from files and (remote) urls."""

    _playlists_dir: str
    _playlist_lock: asyncio.Lock
    _playlist_locks: dict[str, asyncio.Lock]

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        self._playlist_lock = asyncio.Lock()
        self._playlist_locks = {}
        self._playlists_dir = os.path.join(self.mass.storage_path, "playlists")
        if not await asyncio.to_thread(os.path.exists, self._playlists_dir):
            await asyncio.to_thread(os.mkdir, self._playlists_dir)
        await super().loaded_in_mass()
        # migrate old-style playlists in the background to avoid blocking startup
        # TODO: remove after MA 2.9
        self.mass.tasks.register_scheduled_task(
            task_id="migrate_builtin_playlists",
            name="Builtin provider playlist migration",
            handler=self._migrate_playlists,
            schedule=TaskSchedule.hourly(every=24),
            initial_delay=60,
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
        parsed_item = cast("Track", await self.parse_item(prov_track_id))
        stored_items: list[StoredItem] = self.mass.config.get(CONF_KEY_TRACKS, [])
        if stored_item := next((x for x in stored_items if x["item_id"] == prov_track_id), None):
            # always prefer the stored info, such as the name
            parsed_item.name = stored_item["name"]
            if image_url := stored_item.get("image_url"):
                parsed_item.metadata.add_image(
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=image_url,
                        provider=self.domain,
                        remotely_accessible=image_url.startswith("http"),
                    )
                )
        return parsed_item

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id."""
        parsed_item = await self.parse_item(prov_radio_id, force_radio=True)
        assert isinstance(parsed_item, Radio)
        stored_items: list[StoredItem] = self.mass.config.get(CONF_KEY_RADIOS, [])
        if stored_item := next((x for x in stored_items if x["item_id"] == prov_radio_id), None):
            # always prefer the stored info, such as the name
            parsed_item.name = stored_item["name"]
            if image_url := stored_item.get("image_url"):
                parsed_item.metadata.add_image(
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=image_url,
                        provider=self.domain,
                        remotely_accessible=image_url.startswith("http"),
                    )
                )
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
                provider_mappings={
                    ProviderMapping(
                        item_id=prov_playlist_id,
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                    )
                },
                owner="Music Assistant",
                is_editable=False,
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
        # read playlist name from M3U #PLAYLIST directive, fall back to filename
        m3u_data = await self._read_m3u_file(prov_playlist_id)
        playlist_name = parse_m3u_playlist_name(m3u_data) or prov_playlist_id
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
            supported_mediatypes={
                MediaType.AUDIOBOOK,
                MediaType.PODCAST_EPISODE,
                MediaType.RADIO,
                MediaType.TRACK,
            },
            is_editable=True,
        )

    async def get_item(self, media_type: MediaType, prov_item_id: str) -> MediaItemType:
        """Get single MediaItem from provider."""
        if media_type == MediaType.ARTIST:
            return await self.get_artist(prov_item_id)
        if media_type == MediaType.TRACK:
            return await self.get_track(prov_item_id)
        if media_type == MediaType.RADIO:
            return await self.get_radio(prov_item_id)
        if media_type == MediaType.PLAYLIST:
            return await self.get_playlist(prov_item_id)
        if media_type == MediaType.UNKNOWN:
            return await self.parse_item(prov_item_id)
        raise NotImplementedError

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from the provider."""
        stored_items: list[StoredItem] = self.mass.config.get(CONF_KEY_TRACKS, [])
        for item in stored_items:
            try:
                yield await self.get_track(item["item_id"])
            except MediaNotFoundError as err:
                self.logger.warning("Track %s not found: %s", item, err)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
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

    async def get_library_radios(self) -> AsyncGenerator[Radio, None]:
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
                except (MediaNotFoundError, InvalidDataError, ProviderUnavailableError):
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
            await self._write_m3u_file(prov_playlist_id, playlist.name, entries)

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
            await self._write_m3u_file(prov_playlist_id, playlist.name, list(existing_items))

    async def create_playlist(self, name: str, media_types: set[MediaType]) -> Playlist:
        """Create a new playlist on provider with given name.

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

    async def parse_item(
        self,
        url: str,
        force_refresh: bool = False,
        force_radio: bool = False,
    ) -> Track | Radio:
        """Parse plain URL to MediaItem of type Radio or Track."""
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
        media_item: Track | Radio
        if is_radio or force_radio:
            # treat as radio
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
        return path

    async def _resolve_url(self, url: str) -> str:
        """Resolve a URL to the actual stream URL.

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

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a track/radio."""
        media_info = await self._get_media_info(item_id)
        is_radio = media_info.get("icy-name") or not media_info.duration
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.try_parse(media_info.format),
                sample_rate=media_info.sample_rate,
                bit_depth=media_info.bits_per_sample,
                channels=media_info.channels,
            ),
            media_type=MediaType.RADIO if is_radio else MediaType.TRACK,
            stream_type=StreamType.HTTP,
            path=item_id,
            can_seek=not is_radio,
            allow_seek=not is_radio,
        )

    @use_cache(expiration=120, category=CACHE_CATEGORY_PLAYLISTS)
    async def _get_builtin_playlist_random_favorite_tracks(self) -> list[Track]:
        result: list[Track] = []
        res = await self.mass.music.tracks.library_items(
            favorite=True, limit=250000, order_by="random_play_count"
        )
        for idx, item in enumerate(res, 1):
            item.position = idx
            result.append(item)
        return result

    @use_cache(expiration=120, category=CACHE_CATEGORY_PLAYLISTS)
    async def _get_builtin_playlist_random_tracks(self) -> list[Track]:
        result: list[Track] = []
        res = await self.mass.music.tracks.library_items(limit=500, order_by="random_play_count")
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
        for in_library_only in (True, False):
            for min_tracks_required in (25, 10, 5, 1):
                for random_artist in await self.mass.music.artists.library_items(
                    limit=25, order_by="random"
                ):
                    tracks = await self.mass.music.artists.tracks(
                        random_artist.item_id,
                        random_artist.provider,
                        in_library_only=in_library_only,
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
    ) -> None:
        """Write an M3U playlist file to disk."""
        m3u_content = generate_m3u(playlist_name, entries)
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
        media_controller = self.mass.music.get_controller(media_type)
        for prov_info in item.providers:
            try:
                library_item = await media_controller.get_library_item_by_prov_id(
                    prov_info.item_id, prov_info.domain
                )
                if library_item is not None:
                    return library_item
            except (InvalidDataError, KeyError, NotImplementedError):
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

        # build M3U-compliant EXTINF title
        if hasattr(full_item, "artists") and full_item.artists:
            artist_names = ", ".join(a.name for a in full_item.artists)
            title = f"{artist_names} - {full_item.name}"
        elif hasattr(full_item, "podcast") and full_item.podcast:
            title = f"{full_item.podcast.name} - {full_item.name}"
        else:
            title = full_item.name

        duration = getattr(full_item, "duration", None) or 0

        # build EXTMA metadata
        metadata: dict[str, str] = {
            "media_type": full_item.media_type.value,
            "name": full_item.name,
        }
        if hasattr(full_item, "authors") and full_item.authors:
            metadata["authors"] = "; ".join(full_item.authors)
        if hasattr(full_item, "narrators") and full_item.narrators:
            metadata["narrators"] = "; ".join(full_item.narrators)
        if full_item.version:
            metadata["version"] = full_item.version
        if isrc := full_item.get_external_id(ExternalID.ISRC):
            metadata["isrc"] = isrc
        if mbid := full_item.get_external_id(ExternalID.MB_RECORDING):
            metadata["mbid"] = mbid

        # collect one provider mapping per domain (highest quality)
        prov_infos: list[ProviderMappingInfo] = []
        seen_domains: set[str] = set()
        if not full_item.provider_mappings:
            # this should not happen, but just in case
            msg = f"No provider mappings found for: {uri}"
            raise ProviderUnavailableError(msg)
        sorted_mappings = sorted(full_item.provider_mappings, key=lambda x: x.quality, reverse=True)
        for prov_mapping in sorted_mappings:
            domain = prov_mapping.provider_domain
            if domain in seen_domains:
                continue
            seen_domains.add(domain)
            prov_infos.append(
                ProviderMappingInfo(
                    domain=domain,
                    item_id=prov_mapping.item_id,
                    instance_id=prov_mapping.provider_instance,
                    content_type=prov_mapping.audio_format.content_type.value,
                    sample_rate=prov_mapping.audio_format.sample_rate,
                    bit_depth=prov_mapping.audio_format.bit_depth,
                    bit_rate=prov_mapping.audio_format.bit_rate or 0,
                )
            )

        # primary URI = highest quality provider
        primary = prov_infos[0]
        primary_uri = create_uri(full_item.media_type, primary.domain, primary.item_id)

        artist_infos = collect_artist_infos(full_item)
        album_info = collect_album_info(full_item)
        podcast_info = collect_podcast_info(full_item)

        # collect images
        images: list[ImageInfo] = []
        if hasattr(full_item, "metadata") and full_item.metadata and full_item.metadata.images:
            for img in full_item.metadata.images:
                images.append(
                    ImageInfo(
                        type=img.type.value,
                        path=img.path,
                        provider=img.provider,
                        remotely_accessible=img.remotely_accessible,
                    )
                )

        return PlaylistItem(
            path=primary_uri,
            title=title,
            length=str(duration),
            metadata=metadata,
            providers=prov_infos,
            images=images,
            artists=artist_infos,
            album=album_info,
            podcast=podcast_info,
        )

    @staticmethod
    def _sanitize_playlist_id(name: str) -> str:
        """Sanitize a playlist name for use as a filename (without extension)."""
        # replace invalid filename characters
        sanitized = re.sub(r'[<>:"/\\|?*]', "_", name)
        # remove leading/trailing spaces and dots
        sanitized = sanitized.strip(" .")
        return sanitized or "untitled"

    async def _migrate_playlists(self) -> None:  # noqa: PLR0915
        """Migrate old-style playlists (config + plain URI files) to M3U files."""
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
                except (MediaNotFoundError, InvalidDataError, ProviderUnavailableError):
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
        # fix (already migrated) user playlists that have unresolved URIs
        # by re-saving them with enriched metadata
        errors = 0
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
                if item.title and item.providers and item.metadata and not force_migration:
                    continue
                self.logger.debug(
                    "Found unresolved entry in playlist '%s': %s", playlist_id, item.path
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
                    self.logger.warning(
                        "Could not enrich playlist entry %s during migration: %s", item.path, err
                    )
                    report_current_task_failure(f"Could not enrich playlist entry: {item.path}")
                    errors += 1
                else:
                    has_changes = True
                    self.logger.debug(
                        "Enriched playlist entry %s",
                        item.path,
                    )
            if has_changes:
                await self._write_m3u_file(playlist_id, playlist.name, list(all_items))
                self.logger.info("Updated playlist '%s' with enriched metadata", playlist.name)
            if errors > 25:
                raise RuntimeError("Too many errors during playlist migration")
        self.logger.info("Playlist migration completed with %d errors", errors)
        # if there were no errors, we can safely unregister the migration task
        if errors == 0 and (current_task_id := get_current_task_id()):
            # defer unregistering the scheduled task to avoid cancelling the current task
            self.mass.call_later(0, self.mass.tasks.unregister_scheduled_task, current_task_id)
