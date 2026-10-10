"""beets music provider for Music Assistant."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Collection
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from aiofiles.os import wrap
from music_assistant_models.enums import MediaType, ProviderFeature, StreamType
from music_assistant_models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    MusicAssistantError,
    ProviderUnavailableError,
    SetupFailedError,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import (
    DB_TABLE_ALBUM_ARTISTS,
    DB_TABLE_ALBUM_TRACKS,
    DB_TABLE_PROVIDER_MAPPINGS,
    DB_TABLE_TRACK_ARTISTS,
    VARIOUS_ARTISTS_MBID,
    VARIOUS_ARTISTS_NAME,
)
from music_assistant.controllers.tasks.context import (
    report_current_task_failure,
    update_current_task_progress_from_index,
)
from music_assistant.helpers.security import is_safe_path
from music_assistant.models.music_provider import MusicProvider

from .constants import (
    ARTIST_MBID_ID_PREFIX,
    ARTIST_NAME_ID_PREFIX,
    CONF_BEETS_DIRECTORY,
    CONF_ENTRY_R128_TARGET_LEVEL,
    CONF_ENTRY_REPLAYGAIN_TARGET_LEVEL,
    CONF_LIBRARY_DB,
    CONF_MUSIC_DIRECTORY,
    CONF_R128_TARGET_LEVEL,
    CONF_REPLAYGAIN_TARGET_LEVEL,
    IMAGE_PATH_PREFIX,
    ITEM_BATCH_SIZE,
)
from .library import BeetsLibrary, BeetsRow
from .parsers import (
    ParseContext,
    expand_path,
    item_checksum,
    loudness_from_gains,
    parse_album,
    parse_artist,
    parse_audio_format,
    parse_track,
    track_item_id,
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

# artists and albums are imported together with their tracks, so only the track sync exists
SUPPORTED_FEATURES = {ProviderFeature.LIBRARY_TRACKS}


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
        self._ctx = ParseContext(
            instance_id=self.instance_id,
            domain=self.domain,
            music_directory=self.music_directory,
            beets_directory=self.beets_directory,
            replaygain_target_level=self.get_config_value(
                CONF_REPLAYGAIN_TARGET_LEVEL, return_type=int
            ),
            r128_target_level=self.get_config_value(CONF_R128_TARGET_LEVEL, return_type=int),
        )

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return False

    @property
    def instance_name_postfix(self) -> str | None:
        """Return a (default) instance name postfix for this provider instance."""
        return PurePosixPath(self.music_directory).name or None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the (options) config entries to configure this provider instance."""
        return (CONF_ENTRY_REPLAYGAIN_TARGET_LEVEL, CONF_ENTRY_R128_TARGET_LEVEL)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        db_path = self.library.db_path
        if not await isfile(db_path):
            msg = f"beets library database {db_path} does not exist"
            raise SetupFailedError(
                msg,
                translation_key="library_db_not_found",
                translation_owner=self.translation_owner,
            )
        try:
            await self.library.open()
        except ProviderUnavailableError as err:
            msg = f"Unable to read beets library database {db_path}: {err}"
            raise SetupFailedError(
                msg,
                translation_key="library_db_invalid",
                translation_owner=self.translation_owner,
            ) from err
        if not await isdir(self.music_directory):
            await self.library.close()
            msg = f"Music directory {self.music_directory} does not exist"
            raise SetupFailedError(
                msg,
                translation_key="music_directory_not_found",
                translation_owner=self.translation_owner,
            )

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        await self.library.close()

    async def sync_library(self, media_type: MediaType) -> None:
        """Run library sync for this provider."""
        await self._sync_tracks()

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        item = await self._get_item(prov_track_id)
        album = await self._get_album_row(item.album_id) if item.album_id else None
        return parse_track(
            item,
            album,
            self._ctx,
            item_checksum(item, album, self._ctx),
        )

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        return parse_album(await self._get_album(prov_album_id), self._ctx)

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id."""
        album = await self._get_album(prov_album_id)
        return [
            parse_track(
                item,
                album,
                self._ctx,
                item_checksum(item, album, self._ctx),
            )
            for item in await self.library.get_album_items(album.id)
        ]

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        name, mbid = _parse_artist_id(prov_artist_id)
        if mbid == VARIOUS_ARTISTS_MBID:
            # compilations get this artist from beets' comp flag, whatever albumartist says
            return parse_artist(VARIOUS_ARTISTS_NAME, self._ctx, mbid=VARIOUS_ARTISTS_MBID)
        artist = await self.library.find_artist(name=name, mbid=mbid)
        if artist is None:
            msg = f"Artist not found: {prov_artist_id}"
            raise MediaNotFoundError(msg)
        return parse_artist(artist.name, self._ctx, artist.sort_name, artist.mbid)

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        item = await self._get_item(item_id)
        path = await self._library_file(item.fields.get("path"))
        if path is None:
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
        album = await self.library.get_album(int(reference.removeprefix(IMAGE_PATH_PREFIX)))
        if album is None:
            msg = f"Image not found: {path}"
            raise MediaNotFoundError(msg)
        art_path = await self._library_file(album.fields.get("artpath"))
        if art_path is None:
            msg = f"Image not found: {path}"
            raise MediaNotFoundError(msg)
        return art_path

    async def _library_file(self, value: object) -> str | None:
        """
        Return the path of an existing file a beets path column points at in the music directory.

        Returns None when the file is missing or lies outside the music directory, also when
        a symlink leads it out.

        :param value: The raw path value from beets.
        """
        path = expand_path(value, self.music_directory, self.beets_directory)
        if path is None or not await isfile(path):
            return None
        real_path, real_root = await asyncio.to_thread(
            lambda: (os.path.realpath(path), os.path.realpath(self.music_directory))
        )
        if not is_safe_path(real_path, real_root):
            return None
        return path

    async def _get_item(self, prov_item_id: str) -> BeetsRow:
        """Return the beets item for a provider item id, or raise when beets has none."""
        item = await self.library.get_item(int(prov_item_id))
        if item is None:
            msg = f"Track not found: {prov_item_id}"
            raise MediaNotFoundError(msg)
        return item

    async def _get_album(self, prov_album_id: str) -> BeetsRow:
        """Return the beets album for a provider album id, or raise when beets has none."""
        album = await self._get_album_row(int(prov_album_id))
        if album is None:
            msg = f"Album not found: {prov_album_id}"
            raise MediaNotFoundError(msg)
        return album

    async def _get_album_row(self, album_id: int) -> BeetsRow | None:
        """Return a beets album row with its cover stamp, or None when beets has none."""
        if (album := await self.library.get_album(album_id)) is not None:
            await self._stamp_art([album])
        return album

    async def _stamp_art(self, albums: Collection[BeetsRow]) -> None:
        """
        Record the size and modification time of each album's cover file on its row.

        :param albums: The beets album rows.
        """
        paths = {
            album.id: path
            for album in albums
            if (
                path := expand_path(
                    album.fields.get("artpath"), self.music_directory, self.beets_directory
                )
            )
        }

        def _stat_all() -> dict[int, str]:
            stamps: dict[int, str] = {}
            for album_id, path in paths.items():
                try:
                    stat = Path(path).stat()
                except OSError:
                    continue
                stamps[album_id] = f"{stat.st_mtime_ns}-{stat.st_size}"
            return stamps

        stamps = await asyncio.to_thread(_stat_all)
        for album in albums:
            album.art_stamp = stamps.get(album.id)

    async def _sync_tracks(self) -> None:
        """Import new and changed beets items, then remove what beets no longer has."""
        previous = await self._get_previous_checksums()
        current_ids: set[str] = set()
        try:
            total = await self.library.count_items()
            album_rows = await self.library.get_albums()
            await self._stamp_art(album_rows.values())
            albums: dict[int, BeetsRow | None] = dict(album_rows)
            processed = 0
            async for batch in self.library.iter_items(ITEM_BATCH_SIZE):
                for item in batch:
                    album = await self._album_for(item, albums)
                    item_id = track_item_id(item.id)
                    current_ids.add(item_id)
                    checksum = item_checksum(item, album, self._ctx)
                    if previous.get(item_id) != checksum:
                        await self._import_item(
                            item, album, checksum, overwrite=item_id in previous
                        )
                processed += len(batch)
                # beets may have added items after count_items() ran, including from 0
                update_current_task_progress_from_index(
                    processed, max(total, processed), f"Read {processed}/{total} beets items"
                )
        except ProviderUnavailableError as err:
            self.logger.error("Aborting sync for %s: %s", self.name, err)
            report_current_task_failure(f"Sync aborted: unable to read the beets library: {err}")
            return

        # an empty result for a previously filled library is far more likely a wrong mount
        # than a user who deleted everything, so keep the library as it is
        if previous and not current_ids:
            self.logger.error(
                "Aborting sync for %s: beets returned no items but %d were previously imported",
                self.name,
                len(previous),
            )
            report_current_task_failure(
                f"Sync aborted: beets returned no items but {len(previous)} "
                "were previously imported"
            )
            return
        if deleted_ids := set(previous) - current_ids:
            await self._process_deletions(deleted_ids)
        await self._process_orphaned_albums_and_artists()

    async def _album_for(
        self, item: BeetsRow, albums: dict[int, BeetsRow | None]
    ) -> BeetsRow | None:
        """Return an item's album row, reading albums beets added after the sync started."""
        if (album_id := item.album_id) is None:
            return None
        if album_id not in albums:
            albums[album_id] = await self._get_album_row(album_id)
        return albums[album_id]

    async def _import_item(
        self, item: BeetsRow, album: BeetsRow | None, checksum: str, overwrite: bool
    ) -> None:
        """Add or update one beets item in the Music Assistant library."""
        try:
            track = parse_track(item, album, self._ctx, checksum)
            # the library write stores the new checksum, so the loudness goes first: when it
            # fails, the item keeps its previous checksum and the next sync retries both
            if (loudness := loudness_from_gains(item.fields, "track", self._ctx)) is not None:
                await self.mass.streams.audio_analysis.set_track_loudness(
                    track.item_id,
                    self.instance_id,
                    loudness,
                    loudness_from_gains(item.fields, "album", self._ctx),
                )
            await self.mass.music.tracks.add_item_to_library(track, overwrite_existing=overwrite)
        except (MusicAssistantError, ValueError, TypeError) as err:
            # one broken item must not abort the sync; it keeps its previous checksum, so the
            # next sync retries it, and it stays out of the deletion pass
            unexpected = not isinstance(err, InvalidDataError)
            self.logger.error(
                "Error importing beets item %s: %s",
                item.id,
                err,
                exc_info=err if unexpected and self.logger.isEnabledFor(logging.DEBUG) else None,
            )
            report_current_task_failure(f"Failed to import beets item {item.id}: {err}")

    async def _get_previous_checksums(self) -> dict[str, str]:
        """Return the checksum stored for every beets item this instance imported before."""
        assert self.mass.music.database
        query = (
            f"SELECT provider_item_id, details FROM {DB_TABLE_PROVIDER_MAPPINGS} "
            "WHERE provider_instance = :instance_id AND media_type = 'track'"
        )
        rows = await self.mass.music.database.get_rows_from_query(
            query, {"instance_id": self.instance_id}, limit=0
        )
        return {str(row["provider_item_id"]): str(row["details"]) for row in rows}

    async def _process_deletions(self, deleted_ids: set[str]) -> None:
        """Unmap the tracks beets no longer has; the orphan pass cleans up their albums and artists."""
        assert self.mass.music.database
        tracks = self.mass.music.tracks
        ordered_ids = sorted(deleted_ids)
        async with self.mass.music.database.deferred_commit():
            for start in range(0, len(ordered_ids), ITEM_BATCH_SIZE):
                chunk = ordered_ids[start : start + ITEM_BATCH_SIZE]
                chunk_ids = set(chunk)
                library_items = await tracks.get_library_items_by_prov_id(
                    provider_instance=self.instance_id,
                    provider_item_ids=chunk,
                    limit=len(chunk),
                )
                for library_item in library_items:
                    # the library track may also hold another mapping (a re-imported beets
                    # item merged into it, or another provider), which keeps it together with
                    # its favorite and history
                    for mapping in library_item.provider_mappings:
                        if (
                            mapping.provider_instance == self.instance_id
                            and mapping.item_id in chunk_ids
                        ):
                            await tracks.remove_provider_mapping(
                                library_item.item_id, self.instance_id, mapping.item_id
                            )

    async def _process_orphaned_albums_and_artists(self) -> None:
        """Unmap this instance from albums and artists that no longer hold any of its tracks."""
        assert self.mass.music.database
        params = {"instance_id": self.instance_id}
        album_query = (
            f"SELECT DISTINCT item_id FROM ({_mapped_ids('album')}) "
            f"WHERE item_id NOT IN (SELECT album_id FROM {DB_TABLE_ALBUM_TRACKS} "
            f"WHERE track_id IN ({_mapped_ids('track')}))"
        )
        # albums go first, so an artist only kept by an album unmapped here is unmapped too
        artist_query = (
            f"SELECT DISTINCT item_id FROM ({_mapped_ids('artist')}) "
            f"WHERE item_id NOT IN (SELECT artist_id FROM {DB_TABLE_TRACK_ARTISTS} "
            f"WHERE track_id IN ({_mapped_ids('track')})) "
            f"AND item_id NOT IN (SELECT artist_id FROM {DB_TABLE_ALBUM_ARTISTS} "
            f"WHERE album_id IN ({_mapped_ids('album')}))"
        )
        # only this instance's mappings go; an album or artist another provider also maps (a
        # saved album, a followed artist) stays in the library with that provider's mapping,
        # and one without any other mapping is removed with its last mapping
        database = self.mass.music.database
        async with database.deferred_commit():
            for row in await database.get_rows_from_query(album_query, params, limit=0):
                await self.mass.music.albums.remove_provider_mappings(
                    row["item_id"], self.instance_id
                )
            for row in await database.get_rows_from_query(artist_query, params, limit=0):
                await self.mass.music.artists.remove_provider_mappings(
                    row["item_id"], self.instance_id
                )


def _mapped_ids(media_type: str) -> str:
    """
    Return a subquery of the library ids of a media type that the :instance_id instance maps.

    :param media_type: The media type value, as stored in the provider mappings table.
    """
    return (
        f"SELECT item_id FROM {DB_TABLE_PROVIDER_MAPPINGS} "
        f"WHERE provider_instance = :instance_id AND media_type = '{media_type}'"
    )


def _parse_artist_id(prov_artist_id: str) -> tuple[str | None, str | None]:
    """
    Return the name or the MusicBrainz id an artist provider item id is keyed by.

    :param prov_artist_id: The provider artist id.
    """
    if prov_artist_id.startswith(ARTIST_MBID_ID_PREFIX):
        return None, prov_artist_id.removeprefix(ARTIST_MBID_ID_PREFIX)
    return prov_artist_id.removeprefix(ARTIST_NAME_ID_PREFIX), None
