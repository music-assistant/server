"""beets music provider for Music Assistant."""

from __future__ import annotations

import os
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from aiofiles.os import wrap
from music_assistant_models.enums import MediaType, ProviderFeature, StreamType
from music_assistant_models.errors import MediaNotFoundError, SetupFailedError
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.models.music_provider import MusicProvider

from .constants import (
    CONF_BEETS_DIRECTORY,
    CONF_ENTRY_FAVORITE_RATING_THRESHOLD,
    CONF_FAVORITE_RATING_THRESHOLD,
    CONF_LIBRARY_DB,
    CONF_MUSIC_DIRECTORY,
    IMAGE_PATH_PREFIX,
)
from .library import BeetsLibrary, BeetsLibraryError, BeetsRow
from .parsers import (
    ParseContext,
    expand_path,
    item_checksum,
    parse_album,
    parse_artist,
    parse_audio_format,
    parse_track,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.media_items import Album, Artist, Track
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

isdir = wrap(os.path.isdir)
isfile = wrap(os.path.isfile)
getsize = wrap(os.path.getsize)

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return BeetsProvider(mass, manifest, config)


class BeetsProvider(MusicProvider):
    """Music provider that plays a library managed by beets, reading its database read-only."""

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize the provider from its setup data."""
        super().__init__(mass, manifest, config, SUPPORTED_FEATURES)
        self.library = BeetsLibrary(str(self.get_setup_value(CONF_LIBRARY_DB)))
        self.music_directory = str(self.get_setup_value(CONF_MUSIC_DIRECTORY))
        self.beets_directory: str | None = (
            str(self.get_setup_value(CONF_BEETS_DIRECTORY) or "") or None
        )
        self.sync_running = False
        self._ctx = ParseContext(
            instance_id=self.instance_id,
            domain=self.domain,
            music_directory=self.music_directory,
            beets_directory=self.beets_directory,
            favorite_rating_threshold=None,
        )

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        return (CONF_ENTRY_FAVORITE_RATING_THRESHOLD,)

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return False

    @property
    def instance_name_postfix(self) -> str | None:
        """Return a (default) instance name postfix for this provider instance."""
        return PurePosixPath(self.music_directory).name or None

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        db_path = self.library.db_path
        if not await isfile(db_path):
            msg = f"beets library database {db_path} does not exist"
            raise SetupFailedError(
                msg,
                translation_key="library_db_not_found",
                translation_owner=self.translation_owner,
                translation_args=[db_path],
            )
        try:
            await self.library.open()
        except BeetsLibraryError as err:
            msg = f"Unable to read beets library database {db_path}: {err}"
            raise SetupFailedError(
                msg,
                translation_key="library_db_invalid",
                translation_owner=self.translation_owner,
                translation_args=[db_path],
            ) from err
        if not await isdir(self.music_directory):
            await self.library.close()
            msg = f"Music directory {self.music_directory} does not exist"
            raise SetupFailedError(
                msg,
                translation_key="music_directory_not_found",
                translation_owner=self.translation_owner,
                translation_args=[self.music_directory],
            )
        threshold = self.config.get_value(CONF_FAVORITE_RATING_THRESHOLD)
        self._ctx = ParseContext(
            instance_id=self.instance_id,
            domain=self.domain,
            music_directory=self.music_directory,
            beets_directory=self.beets_directory,
            favorite_rating_threshold=(
                float(threshold) if isinstance(threshold, int | float) else None
            ),
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        await self.library.close()

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        item = await self._get_item(prov_track_id)
        album = await self.library.get_album(item.album_id) if item.album_id else None
        return parse_track(item, album, self._ctx, item_checksum(item, album))

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        return parse_album(await self._get_album(prov_album_id), self._ctx)

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id."""
        album = await self._get_album(prov_album_id)
        return [
            parse_track(item, album, self._ctx, item_checksum(item, album))
            for item in await self.library.get_album_items(album.id)
        ]

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        details = await self.library.get_artist_details(prov_artist_id)
        if details is None:
            msg = f"Artist not found: {prov_artist_id}"
            raise MediaNotFoundError(msg)
        sort_name, mbid = details
        return parse_artist(prov_artist_id, self._ctx, sort_name, mbid)

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        item = await self._get_item(item_id)
        path = expand_path(item.fields.get("path"), self.music_directory, self.beets_directory)
        if path is None or not await isfile(path):
            msg = f"Media file not found: {item_id}"
            raise MediaNotFoundError(msg)
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=parse_audio_format(item.fields),
            media_type=MediaType.TRACK,
            stream_type=StreamType.LOCAL_FILE,
            duration=int(item.fields.get("length") or 0),
            size=await getsize(path),
            path=path,
            can_seek=True,
            allow_seek=True,
        )

    async def resolve_image(self, path: str) -> str | bytes:
        """Return the local path of an album's cover art for an album image path."""
        reference = path.split("?cs=", 1)[0]
        if not reference.startswith(IMAGE_PATH_PREFIX):
            msg = f"Image not found: {path}"
            raise MediaNotFoundError(msg)
        album = await self._get_album(reference.removeprefix(IMAGE_PATH_PREFIX))
        art_path = expand_path(
            album.fields.get("artpath"), self.music_directory, self.beets_directory
        )
        if art_path is None or not await isfile(art_path):
            msg = f"Image not found: {path}"
            raise MediaNotFoundError(msg)
        return art_path

    async def _get_item(self, prov_item_id: str) -> BeetsRow:
        """Return the beets item for a provider item id, or raise when beets has none."""
        item = await self.library.get_item(_parse_id(prov_item_id))
        if item is None:
            msg = f"Track not found: {prov_item_id}"
            raise MediaNotFoundError(msg)
        return item

    async def _get_album(self, prov_album_id: str) -> BeetsRow:
        """Return the beets album for a provider album id, or raise when beets has none."""
        album = await self.library.get_album(_parse_id(prov_album_id))
        if album is None:
            msg = f"Album not found: {prov_album_id}"
            raise MediaNotFoundError(msg)
        return album


def _parse_id(prov_item_id: str) -> int:
    """Return the beets row id encoded in a provider item id."""
    try:
        return int(prov_item_id)
    except ValueError as err:
        msg = f"Invalid beets id: {prov_item_id}"
        raise MediaNotFoundError(msg) from err
