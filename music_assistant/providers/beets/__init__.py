"""beets music provider for Music Assistant."""

from __future__ import annotations

import asyncio
import logging
import os
from copy import copy
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from aiofiles.os import wrap
from music_assistant_models.enums import MediaType, ProviderFeature, StreamType
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError, SetupFailedError
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import (
    DB_TABLE_ALBUM_ARTISTS,
    DB_TABLE_ALBUM_TRACKS,
    DB_TABLE_ALBUMS,
    DB_TABLE_ARTISTS,
    DB_TABLE_PROVIDER_MAPPINGS,
    DB_TABLE_TRACK_ARTISTS,
)
from music_assistant.controllers.tasks.context import (
    report_current_task_failure,
    update_current_task_progress_from_index,
)
from music_assistant.helpers.compare import compare_track
from music_assistant.helpers.util import TaskManager
from music_assistant.models.music_provider import MusicProvider

from .constants import (
    CONF_BEETS_DIRECTORY,
    CONF_ENTRY_FAVORITE_RATING_THRESHOLD,
    CONF_FAVORITE_RATING_THRESHOLD,
    CONF_LIBRARY_DB,
    CONF_MUSIC_DIRECTORY,
    IMAGE_PATH_PREFIX,
    ITEM_BATCH_SIZE,
    SYNC_CONCURRENCY,
)
from .library import BeetsLibrary, BeetsLibraryError, BeetsRow
from .parsers import (
    ParseContext,
    album_id_prefix,
    expand_path,
    item_checksum,
    loudness_from_gains,
    parse_album,
    parse_artist,
    parse_audio_format,
    parse_track,
    track_id_prefix,
    track_item_id,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.media_items import Album, Artist, ProviderMapping, Track
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
        self._merged_track_lock = asyncio.Lock()
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
            )
        try:
            await self.library.open()
        except BeetsLibraryError as err:
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

    async def sync_library(self, media_type: MediaType) -> None:
        """Run library sync for this provider."""
        if media_type != MediaType.TRACK:
            # artists and albums are imported together with their tracks
            return
        if self.sync_running:
            self.logger.warning("Library sync already running for %s", self.name)
            return
        self.sync_running = True
        try:
            await self._sync_tracks()
        finally:
            self.sync_running = False

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        item = await self._get_item(prov_track_id)
        album = await self.library.get_album(item.album_id) if item.album_id else None
        return parse_track(
            item,
            album,
            self._ctx,
            item_checksum(item, album, self._ctx.favorite_rating_threshold),
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
                item_checksum(item, album, self._ctx.favorite_rating_threshold),
            )
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
        # image paths carry the bare beets album id, not the namespaced provider album id
        album = await self.library.get_album(_parse_id(reference, IMAGE_PATH_PREFIX))
        if album is None:
            msg = f"Image not found: {path}"
            raise MediaNotFoundError(msg)
        art_path = expand_path(
            album.fields.get("artpath"), self.music_directory, self.beets_directory
        )
        if art_path is None or not await isfile(art_path):
            msg = f"Image not found: {path}"
            raise MediaNotFoundError(msg)
        return art_path

    async def _get_item(self, prov_item_id: str) -> BeetsRow:
        """Return the beets item for a provider item id, or raise when beets has none."""
        item = await self.library.get_item(
            _parse_id(prov_item_id, track_id_prefix(self.instance_id))
        )
        if item is None:
            msg = f"Track not found: {prov_item_id}"
            raise MediaNotFoundError(msg)
        return item

    async def _get_album(self, prov_album_id: str) -> BeetsRow:
        """Return the beets album for a provider album id, or raise when beets has none."""
        album = await self.library.get_album(
            _parse_id(prov_album_id, album_id_prefix(self.instance_id))
        )
        if album is None:
            msg = f"Album not found: {prov_album_id}"
            raise MediaNotFoundError(msg)
        return album

    async def _sync_tracks(self) -> None:
        """Import new and changed beets items, then remove what beets no longer has."""
        previous = await self._get_previous_checksums()
        current_ids: set[str] = set()
        try:
            total = await self.library.count_items()
            albums: dict[int, BeetsRow | None] = dict(await self.library.get_albums())
            processed = 0
            async with TaskManager(self.mass, SYNC_CONCURRENCY) as task_manager:
                async for batch in self.library.iter_items(ITEM_BATCH_SIZE):
                    for item in batch:
                        album = await self._album_for(item, albums)
                        item_id = track_item_id(self._ctx, item.id)
                        current_ids.add(item_id)
                        checksum = item_checksum(item, album, self._ctx.favorite_rating_threshold)
                        if previous.get(item_id) == checksum:
                            continue
                        await task_manager.create_task_with_limit(
                            self._import_item(item, album, checksum, overwrite=item_id in previous)
                        )
                    processed += len(batch)
                    # beets may have added items after count_items() ran, including from 0
                    update_current_task_progress_from_index(
                        processed, max(total, processed), f"Read {processed}/{total} beets items"
                    )
        except BeetsLibraryError as err:
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
            albums[album_id] = await self.library.get_album(album_id)
        return albums[album_id]

    async def _import_item(
        self, item: BeetsRow, album: BeetsRow | None, checksum: str, overwrite: bool
    ) -> None:
        """Add or update one beets item in the Music Assistant library."""
        try:
            track = parse_track(item, album, self._ctx, checksum)
            if overwrite:
                library_item = await self._overwrite_library_track(track)
            else:
                library_item = await self.mass.music.tracks.add_item_to_library(
                    track, overwrite_existing=False
                )
            if track.favorite and not library_item.favorite:
                await self.mass.music.tracks.set_favorite(library_item.item_id, True)
            if (loudness := loudness_from_gains(item.fields, "track")) is not None:
                await self.mass.streams.audio_analysis.set_track_loudness(
                    track.item_id,
                    self.instance_id,
                    loudness,
                    loudness_from_gains(item.fields, "album"),
                )
        except Exception as err:
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

    async def _overwrite_library_track(self, track: Track) -> Track:
        """Replace a library track with a changed beets item, keeping other items merged into it."""
        tracks = self.mass.music.tracks
        current = await tracks.get_library_item_by_prov_id(track.item_id, self.instance_id)
        if current is None or not _mappings_to_carry(current, track):
            return await tracks.add_item_to_library(track, overwrite_existing=True)
        # an overwrite replaces every mapping of this instance on the library track, so the
        # other beets items' mappings are passed along; the lock keeps two merged items that
        # changed together from writing back each other's previous mapping
        async with self._merged_track_lock:
            current = await tracks.get_library_item_by_prov_id(track.item_id, self.instance_id)
            if current is not None:
                track.provider_mappings.update(_mappings_to_carry(current, track))
            return await tracks.add_item_to_library(track, overwrite_existing=True)

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
        """Unmap tracks beets no longer has, and remove the albums and artists left empty."""
        album_ids: set[str] = set()
        artist_ids: set[str] = set()
        for item_id in deleted_ids:
            library_item = await self.mass.music.tracks.get_library_item_by_prov_id(
                item_id, self.instance_id
            )
            if library_item is None:
                continue
            if library_item.album:
                try:
                    # the track's album is an ItemMapping; the library album carries its artists
                    db_album = await self.mass.music.albums.get_library_item(
                        library_item.album.item_id
                    )
                except MediaNotFoundError:
                    pass
                else:
                    album_ids.add(library_item.album.item_id)
                    artist_ids.update(artist.item_id for artist in db_album.artists)
            artist_ids.update(artist.item_id for artist in library_item.artists)
            # the library track may also hold another mapping (a re-imported beets item merged
            # into it, or another provider), which keeps it together with its favorite and history
            await self.mass.music.tracks.remove_provider_mapping(
                library_item.item_id, self.instance_id, item_id
            )
        # emptiness is read from the library alone: the albums and artists still carry this
        # instance's mappings, and beets may no longer have them
        for album_id in album_ids:
            try:
                if not await self.mass.music.albums.get_library_album_tracks(album_id):
                    await self.mass.music.albums.remove_item_from_library(album_id)
            except (MediaNotFoundError, InvalidDataError) as err:
                self.logger.warning("Unable to clean up library album %s: %s", album_id, err)
        for artist_id in artist_ids:
            try:
                if not (
                    await self.mass.music.artists.get_library_artist_albums(artist_id)
                    or await self.mass.music.artists.get_library_artist_tracks(artist_id)
                ):
                    await self.mass.music.artists.remove_item_from_library(artist_id)
            except (MediaNotFoundError, InvalidDataError) as err:
                self.logger.warning("Unable to clean up library artist %s: %s", artist_id, err)

    async def _process_orphaned_albums_and_artists(self) -> None:
        """Remove albums and artists of this instance that no longer have any tracks."""
        assert self.mass.music.database
        params = {"instance_id": self.instance_id}
        album_query = (
            f"SELECT item_id FROM {DB_TABLE_ALBUMS} "
            f"WHERE item_id NOT IN (SELECT album_id FROM {DB_TABLE_ALBUM_TRACKS}) "
            f"AND item_id IN (SELECT item_id FROM {DB_TABLE_PROVIDER_MAPPINGS} "
            "WHERE provider_instance = :instance_id AND media_type = 'album')"
        )
        for row in await self.mass.music.database.get_rows_from_query(album_query, params, limit=0):
            await self.mass.music.albums.remove_item_from_library(row["item_id"])
        artist_query = (
            f"SELECT item_id FROM {DB_TABLE_ARTISTS} "
            f"WHERE item_id NOT IN (SELECT artist_id FROM {DB_TABLE_TRACK_ARTISTS} "
            f"UNION SELECT artist_id FROM {DB_TABLE_ALBUM_ARTISTS}) "
            f"AND item_id IN (SELECT item_id FROM {DB_TABLE_PROVIDER_MAPPINGS} "
            "WHERE provider_instance = :instance_id AND media_type = 'artist')"
        )
        for row in await self.mass.music.database.get_rows_from_query(
            artist_query, params, limit=0
        ):
            await self.mass.music.artists.remove_item_from_library(row["item_id"])


def _mappings_to_carry(library_track: Track, track: Track) -> set[ProviderMapping]:
    """
    Return the mappings of this instance's other beets items to keep on an overwrite.

    None are kept when the changed item no longer matches the library track as the same
    recording, since it was retagged as another one and the other items must split off.

    :param library_track: The library track the changed beets item is mapped to.
    :param track: The provider track of the changed beets item.
    """
    other_mappings = {
        mapping
        for mapping in library_track.provider_mappings
        if mapping.provider_instance == track.provider and mapping.item_id != track.item_id
    }
    if not other_mappings:
        return set()
    # without its mappings the library track cannot match the changed item on its own
    # mapping, so the comparison decides on the metadata alone
    reference = copy(library_track)
    reference.provider_mappings = set()
    return other_mappings if compare_track(reference, track, strict=True) else set()


def _parse_id(prov_item_id: str, prefix: str) -> int:
    """
    Return the beets row id encoded in a provider item id or image path.

    :param prov_item_id: The provider item id or image path.
    :param prefix: The prefix the id must start with, followed by the beets row id.
    :raises MediaNotFoundError: If the id lacks the prefix or the rest is not the canonical
        decimal form of the beets row id (no sign, whitespace, underscore or leading zero).
    """
    if not prov_item_id.startswith(prefix):
        msg = f"Invalid beets id: {prov_item_id}"
        raise MediaNotFoundError(msg)
    remainder = prov_item_id.removeprefix(prefix)
    if not (remainder and remainder.isascii() and remainder.isdigit()):
        msg = f"Invalid beets id: {prov_item_id}"
        raise MediaNotFoundError(msg)
    value = int(remainder)
    if remainder != str(value):
        msg = f"Invalid beets id: {prov_item_id}"
        raise MediaNotFoundError(msg)
    return value
