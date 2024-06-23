"""Built-in/generic provider to handle media from files and (remote) urls."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, NotRequired, TypedDict

import aiofiles
import shortuuid

from music_assistant.common.helpers.uri import parse_uri
from music_assistant.common.models.config_entries import ConfigEntry
from music_assistant.common.models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant.common.models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    ProviderUnavailableError,
)
from music_assistant.common.models.media_items import (
    Artist,
    AudioFormat,
    MediaItemImage,
    MediaItemMetadata,
    MediaItemType,
    Playlist,
    ProviderMapping,
    Radio,
    Track,
)
from music_assistant.common.models.streamdetails import StreamDetails
from music_assistant.constants import DB_SCHEMA_VERSION, MASS_LOGO, VARIOUS_ARTISTS_FANART
from music_assistant.server.helpers.tags import AudioTags, parse_tags
from music_assistant.server.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType


class StoredItem(TypedDict):
    """Definition of an media item (for the builtin provider) stored in persistent storage."""

    item_id: str  # url or (locally accessible) file path (or id in case of playlist)
    name: str
    image_url: NotRequired[str]
    last_updated: NotRequired[int]


CONF_KEY_RADIOS = "stored_radios"
CONF_KEY_TRACKS = "stored_tracks"
CONF_KEY_PLAYLISTS = "stored_playlists"


ALL_FAVORITE_TRACKS = "all_favorite_tracks"
RANDOM_ARTIST = "random_artist"
RANDOM_ALBUM = "random_album"
RANDOM_TRACKS = "random_tracks"
RECENTLY_PLAYED = "recently_played"

BUILTIN_PLAYLISTS = {
    ALL_FAVORITE_TRACKS: "All favorited tracks",
    RANDOM_ARTIST: "Random Artist (from library)",
    RANDOM_ALBUM: "Random Album (from library)",
    RANDOM_TRACKS: "500 Random tracks (from library)",
    RECENTLY_PLAYED: "Recently played tracks",
}

COLLAGE_IMAGE_PLAYLISTS = (ALL_FAVORITE_TRACKS, RANDOM_TRACKS)

DEFAULT_THUMB = MediaItemImage(
    type=ImageType.THUMB,
    path=MASS_LOGO,
    provider="builtin",
    remotely_accessible=False,
)

DEFAULT_FANART = MediaItemImage(
    type=ImageType.FANART,
    path=VARIOUS_ARTISTS_FANART,
    provider="builtin",
    remotely_accessible=False,
)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return BuiltinProvider(mass, manifest, config)


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
    return tuple(
        ConfigEntry(
            key=key,
            type=ConfigEntryType.BOOLEAN,
            label=name,
            default_value=True,
            category="builtin_playlists",
        )
        for key, name in BUILTIN_PLAYLISTS.items()
    )


class BuiltinProvider(MusicProvider):
    """Built-in/generic provider to handle (manually added) media from files and (remote) urls."""

    _playlists_dir: str
    _playlist_lock: asyncio.Lock

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        self._playlist_lock = asyncio.Lock()
        # make sure that our directory with collage images exists
        self._playlists_dir = os.path.join(self.mass.storage_path, "playlists")
        if not await asyncio.to_thread(os.path.exists, self._playlists_dir):
            await asyncio.to_thread(os.mkdir, self._playlists_dir)

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return False

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return (
            ProviderFeature.BROWSE,
            ProviderFeature.LIBRARY_TRACKS,
            ProviderFeature.LIBRARY_RADIOS,
            ProviderFeature.LIBRARY_PLAYLISTS,
            ProviderFeature.LIBRARY_TRACKS_EDIT,
            ProviderFeature.LIBRARY_RADIOS_EDIT,
            ProviderFeature.PLAYLIST_CREATE,
            ProviderFeature.PLAYLIST_TRACKS_EDIT,
        )

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        parsed_item = await self.parse_item(prov_track_id)
        stored_items: list[StoredItem] = self.mass.config.get(CONF_KEY_TRACKS, [])
        if stored_item := next((x for x in stored_items if x["item_id"] == prov_track_id), None):
            # always prefer the stored info, such as the name
            parsed_item.name = stored_item["name"]
            if image_url := stored_item.get("image_url"):
                parsed_item.metadata.images = [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=image_url,
                        provider=self.instance_id,
                        remotely_accessible=image_url.startswith("http"),
                    )
                ]
        return parsed_item

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id."""
        parsed_item = await self.parse_item(prov_radio_id, force_radio=True)
        stored_items: list[StoredItem] = self.mass.config.get(CONF_KEY_RADIOS, [])
        if stored_item := next((x for x in stored_items if x["item_id"] == prov_radio_id), None):
            # always prefer the stored info, such as the name
            parsed_item.name = stored_item["name"]
            if image_url := stored_item.get("image_url"):
                parsed_item.metadata.images = [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=image_url,
                        provider=self.instance_id,
                        remotely_accessible=image_url.startswith("http"),
                    )
                ]
        return parsed_item

    async def get_artist(self, prov_artist_id: str) -> Track:
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
                    images=[DEFAULT_THUMB]
                    if prov_playlist_id in COLLAGE_IMAGE_PLAYLISTS
                    else [DEFAULT_THUMB, DEFAULT_FANART],
                    cache_checksum=str(int(time.time())),
                ),
            )
        # user created universal playlist
        stored_items: list[StoredItem] = self.mass.config.get(CONF_KEY_PLAYLISTS, [])
        stored_item = next((x for x in stored_items if x["item_id"] == prov_playlist_id), None)
        if not stored_item:
            raise MediaNotFoundError
        playlist = Playlist(
            item_id=prov_playlist_id,
            provider=self.instance_id,
            name=stored_item["name"],
            provider_mappings={
                ProviderMapping(
                    item_id=prov_playlist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            owner="Music Assistant",
            is_editable=True,
        )
        playlist.metadata.cache_checksum = f"{DB_SCHEMA_VERSION}.{stored_item.get('last_updated')}"
        if image_url := stored_item.get("image_url"):
            playlist.metadata.images = [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=image_url,
                    provider=self.instance_id,
                    remotely_accessible=image_url.startswith("http"),
                )
            ]
        return playlist

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
            yield await self.get_track(item["item_id"])

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve library/subscribed playlists from the provider."""
        # return user stored playlists
        stored_items: list[StoredItem] = self.mass.config.get(CONF_KEY_PLAYLISTS, [])
        for item in stored_items:
            yield await self.get_playlist(item["item_id"])
        # return builtin playlists
        for item_id in BUILTIN_PLAYLISTS:
            if self.config.get_value(item_id) is False:
                continue
            yield await self.get_playlist(item_id)

    async def get_library_radios(self) -> AsyncGenerator[Radio, None]:
        """Retrieve library/subscribed radio stations from the provider."""
        stored_items: list[StoredItem] = self.mass.config.get(CONF_KEY_RADIOS, [])
        for item in stored_items:
            yield await self.get_radio(item["item_id"])

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
            await self.mass.config.set_provider_config_value(self.instance_id, prov_item_id, False)
            return True
        if media_type == MediaType.TRACK:
            # regular manual track URL/path
            key = CONF_KEY_TRACKS
        elif media_type == MediaType.RADIO:
            # regular manual radio URL/path
            key = CONF_KEY_RADIOS
        elif media_type == MediaType.PLAYLIST:
            # manually added (multi provider) playlist removal
            key = CONF_KEY_PLAYLISTS
        else:
            return False
        stored_items: list[StoredItem] = self.mass.config.get(key, [])
        stored_items = [x for x in stored_items if x["item_id"] != prov_item_id]
        self.mass.config.set(key, stored_items)
        return True

    async def get_playlist_tracks(
        self, prov_playlist_id: str, offset: int, limit: int
    ) -> list[Track]:
        """Get playlist tracks."""
        if prov_playlist_id in BUILTIN_PLAYLISTS:
            if offset:
                # paging not supported, we always return the whole list at once
                return []
            return await self._get_builtin_playlist_tracks(prov_playlist_id)
        # user created universal playlist
        result: list[Track] = []
        playlist_items = await self._read_playlist_file_items(prov_playlist_id, offset, limit)
        for index, uri in enumerate(playlist_items):
            try:
                media_type, provider_instance_id_or_domain, item_id = await parse_uri(uri)
                media_controller = self.mass.music.get_controller(media_type)
                # prefer item already in the db
                track = await media_controller.get_library_item_by_prov_id(
                    item_id, provider_instance_id_or_domain
                )
                if track is None:
                    # get the provider item and not the full track from a regular 'get' call
                    # as we only need basic track info here
                    track = await media_controller.get_provider_item(
                        item_id, provider_instance_id_or_domain
                    )
                track.position = offset + index
                result.append(track)
            except (MediaNotFoundError, InvalidDataError, ProviderUnavailableError) as err:
                self.logger.warning(
                    "Skipping %s in playlist %s: %s", uri, prov_playlist_id, str(err)
                )
        return result

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add track(s) to playlist."""
        playlist_items = await self._read_playlist_file_items(prov_playlist_id)
        for uri in prov_track_ids:
            if uri not in playlist_items:
                playlist_items.append(uri)
        # store playlist file
        await self._write_playlist_file_items(prov_playlist_id, playlist_items)
        # mark last_updated on playlist object
        stored_items: list[StoredItem] = self.mass.config.get(CONF_KEY_PLAYLISTS, [])
        stored_item = next((x for x in stored_items if x["item_id"] == prov_playlist_id), None)
        stored_item["last_updated"] = int(time.time())
        self.mass.config.set(CONF_KEY_PLAYLISTS, stored_items)

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist."""
        playlist_items = await self._read_playlist_file_items(prov_playlist_id)
        # remove items by index
        for i in sorted(positions_to_remove, reverse=True):
            del playlist_items[i]
        # store playlist file
        await self._write_playlist_file_items(prov_playlist_id, playlist_items)
        # mark last_updated on playlist object
        stored_items: list[StoredItem] = self.mass.config.get(CONF_KEY_PLAYLISTS, [])
        stored_item = next((x for x in stored_items if x["item_id"] == prov_playlist_id), None)
        stored_item["last_updated"] = int(time.time())
        self.mass.config.set(CONF_KEY_PLAYLISTS, stored_items)

    async def create_playlist(self, name: str) -> Playlist:  # type: ignore[return]
        """Create a new playlist on provider with given name."""
        item_id = shortuuid.random(8)
        stored_item = StoredItem(item_id=item_id, name=name)
        stored_items: list[StoredItem] = self.mass.config.get(CONF_KEY_PLAYLISTS, [])
        stored_items.append(stored_item)
        self.mass.config.set(CONF_KEY_PLAYLISTS, stored_items)
        return await self.get_playlist(item_id)

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
                artists=[await self.get_artist(artist) for artist in media_info.artists],
                provider_mappings=provider_mappings,
            )

        if media_info.has_cover_image:
            media_item.metadata.images = [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=url,
                    provider=self.instance_id,
                    remotely_accessible=False,
                )
            ]
        return media_item

    async def _get_media_info(self, url: str, force_refresh: bool = False) -> AudioTags:
        """Retrieve mediainfo for url."""
        # do we have some cached info for this url ?
        cache_key = f"{self.instance_id}.media_info.{url}"
        cached_info = await self.mass.cache.get(cache_key)
        if cached_info and not force_refresh:
            return AudioTags.parse(cached_info)
        # parse info with ffprobe (and store in cache)
        media_info = await parse_tags(url)
        if "authSig" in url:
            media_info.has_cover_image = False
        await self.mass.cache.set(cache_key, media_info.raw)
        return media_info

    async def get_stream_details(self, item_id: str) -> StreamDetails:
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
        )

    async def _get_builtin_playlist_tracks(
        self, builtin_playlist_id: str
    ) -> AsyncGenerator[Track, None]:
        """Get all playlist tracks for given builtin playlist id."""
        result: list[Track] = []
        if builtin_playlist_id == ALL_FAVORITE_TRACKS:
            res = await self.mass.music.tracks.library_items(
                favorite=True, limit=250000, order_by="random"
            )
            for idx, item in enumerate(res, 1):
                item.position = idx
                result.append(item)
            return result
        if builtin_playlist_id == RANDOM_TRACKS:
            res = await self.mass.music.tracks.library_items(limit=500, order_by="random_fast")
            for idx, item in enumerate(res, 1):
                item.position = idx
                result.append(item)
            return result
        if builtin_playlist_id == RANDOM_ALBUM:
            for random_album in await self.mass.music.albums.library_items(
                limit=1, order_by="random_fast"
            ):
                # use the function specified in the queue controller as that
                # already handles unwrapping an album by user preference
                tracks = await self.mass.music.albums.tracks(
                    random_album.item_id, random_album.provider
                )
                for idx, track in enumerate(tracks, 1):
                    track.position = idx
                    result.append(track)
                return result
        if builtin_playlist_id == RANDOM_ARTIST:
            for random_artist in await self.mass.music.artists.library_items(
                limit=1, order_by="random_fast"
            ):
                # use the function specified in the queue controller as that
                # already handles unwrapping an artist by user preference
                tracks = await self.mass.music.artists.tracks(
                    random_artist.item_id, random_artist.provider
                )
                for idx, track in enumerate(tracks, 1):
                    track.position = idx
                    result.append(track)
                return result
        if builtin_playlist_id == RECENTLY_PLAYED:
            tracks = await self.mass.music.recently_played(100, [MediaType.TRACK])
            for idx, track in enumerate(tracks, 1):
                track.position = idx
                result.append(track)
            return result
        return result

    async def _read_playlist_file_items(
        self, playlist_id: str, offset: int = 0, limit: int = 100000
    ) -> list[str]:
        """Return lines of a playlist file."""
        playlist_file = os.path.join(self._playlists_dir, playlist_id)
        if not await asyncio.to_thread(os.path.isfile, playlist_file):
            return []
        async with (
            self._playlist_lock,
            aiofiles.open(playlist_file, "r", encoding="utf-8") as _file,
        ):
            lines = await _file.readlines()
            return [x.strip() for x in lines[offset : offset + limit]]

    async def _write_playlist_file_items(self, playlist_id: str, lines: list[str]) -> None:
        """Return lines of a playlist file."""
        playlist_file = os.path.join(self._playlists_dir, playlist_id)
        async with (
            self._playlist_lock,
            aiofiles.open(playlist_file, "w", encoding="utf-8") as _file,
        ):
            await _file.write("\n".join(lines))
