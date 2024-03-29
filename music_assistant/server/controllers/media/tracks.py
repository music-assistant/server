"""Manage MediaItems of type Track."""

from __future__ import annotations

import asyncio
import urllib.parse
from contextlib import suppress

from music_assistant.common.helpers.datetime import utc_timestamp
from music_assistant.common.helpers.json import serialize_to_json
from music_assistant.common.models.enums import AlbumType, EventType, MediaType, ProviderFeature
from music_assistant.common.models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    UnsupportedFeaturedException,
)
from music_assistant.common.models.media_items import Album, ItemMapping, Track
from music_assistant.constants import DB_TABLE_ALBUM_TRACKS, DB_TABLE_TRACKS
from music_assistant.server.helpers.compare import (
    compare_artists,
    compare_track,
    loose_compare_strings,
)

from .base import MediaControllerBase


class TracksController(MediaControllerBase[Track]):
    """Controller managing MediaItems of type Track."""

    db_table = DB_TABLE_TRACKS
    media_type = MediaType.TRACK
    item_cls = Track

    def __init__(self, *args, **kwargs) -> None:
        """Initialize class."""
        super().__init__(*args, **kwargs)
        self.base_query = (
            "SELECT tracks.*, albums.item_id as album_id, "
            "albums.name AS album_name, albums.version as album_version, "
            "albums.metadata as album_metadata FROM tracks "
            "LEFT JOIN albumtracks on albumtracks.track_id = tracks.item_id  "
            "LEFT JOIN albums on albums.item_id = albumtracks.album_id"
        )
        self._db_add_lock = asyncio.Lock()
        # register api handlers
        self.mass.register_api_command("music/tracks/library_items", self.library_items)
        self.mass.register_api_command("music/tracks/get_track", self.get)
        self.mass.register_api_command("music/tracks/track_versions", self.versions)
        self.mass.register_api_command("music/tracks/track_albums", self.albums)
        self.mass.register_api_command(
            "music/tracks/update_item_in_library", self.update_item_in_library
        )
        self.mass.register_api_command(
            "music/tracks/remove_item_from_library", self.remove_item_from_library
        )
        self.mass.register_api_command("music/tracks/preview", self.get_preview_url)

    async def get(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        force_refresh: bool = False,
        lazy: bool = True,
        details: Track = None,
        album_uri: str | None = None,
        add_to_library: bool = False,
    ) -> Track:
        """Return (full) details for a single media item."""
        track = await super().get(
            item_id,
            provider_instance_id_or_domain,
            force_refresh=force_refresh,
            lazy=lazy,
            details=details,
            add_to_library=add_to_library,
        )
        # append full album details to full track item
        try:
            if album_uri and (album := await self.mass.music.get_item_by_uri(album_uri)):
                track.album = album
            elif track.album:
                track.album = await self.mass.music.albums.get(
                    track.album.item_id,
                    track.album.provider,
                    lazy=lazy,
                    details=None if isinstance(track.album, ItemMapping) else track.album,
                    add_to_library=add_to_library,
                )
            elif provider_instance_id_or_domain == "library":
                # grab the first album this track is attached to
                for album_track_row in await self.mass.music.database.get_rows(
                    DB_TABLE_ALBUM_TRACKS, {"track_id": int(item_id)}, limit=1
                ):
                    track.album = await self.mass.music.albums.get_library_item(
                        album_track_row["album_id"]
                    )
        except MediaNotFoundError:
            # edge case where playlist track has invalid albumdetails
            self.logger.warning("Unable to fetch album details %s", track.album.uri)
        # prefer album image if album explicitly given or track has no image on its own
        if (
            (album_uri or not track.metadata.images)
            and isinstance(track.album, Album)
            and track.album.image
        ):
            track.metadata.images = [track.album.image]
        # append full artist details to full track item
        full_artists = []
        for artist in track.artists:
            full_artists.append(
                await self.mass.music.artists.get(
                    artist.item_id,
                    artist.provider,
                    lazy=lazy,
                    details=None if isinstance(artist, ItemMapping) else artist,
                    add_to_library=add_to_library,
                )
            )
        track.artists = full_artists
        return track

    async def add_item_to_library(self, item: Track, metadata_lookup: bool = True) -> Track:
        """Add track to library and return the new database item."""
        if not isinstance(item, Track):
            msg = "Not a valid Track object (ItemMapping can not be added to db)"
            raise InvalidDataError(msg)
        if not item.artists:
            msg = "Track is missing artist(s)"
            raise InvalidDataError(msg)
        if not item.provider_mappings:
            msg = "Track is missing provider mapping(s)"
            raise InvalidDataError(msg)
        # grab additional metadata
        if metadata_lookup:
            await self.mass.metadata.get_track_metadata(item)
        # allow track image from album (only if albumtype = single)
        if (
            not item.image
            and isinstance(item.album, Album)
            and item.album.image
            and item.album.album_type == AlbumType.SINGLE
        ):
            item.metadata.images = item.album.metadata.images
        elif item.image and isinstance(item.album, Album) and item.image == item.album.image:
            item.metadata.images = []
        if item.image and isinstance(item.album, Album) and not item.album.image:
            item.album.metadata.images = item.metadata.images
        # check for existing item first
        library_item = None
        if cur_item := await self.get_library_item_by_prov_id(item.item_id, item.provider):
            # existing item match by provider id
            library_item = await self.update_item_in_library(cur_item.item_id, item)
        elif cur_item := await self.get_library_item_by_external_ids(item.external_ids):
            # existing item match by external id
            library_item = await self.update_item_in_library(cur_item.item_id, item)
        else:
            # search by name
            async for db_item in self.iter_library_items(search=item.name):
                if compare_track(db_item, item):
                    # existing item found: update it
                    library_item = await self.update_item_in_library(db_item.item_id, item)
                    break
        if not library_item:
            # actually add a new item in the library db
            # use the lock to prevent a race condition of the same item being added twice
            async with self._db_add_lock:
                library_item = await self._add_library_item(item)
        # also fetch same track on all providers (will also get other quality versions)
        if metadata_lookup:
            await self._match(library_item)
            library_item = await self.get_library_item(library_item.item_id)
        self.mass.signal_event(
            EventType.MEDIA_ITEM_ADDED,
            library_item.uri,
            library_item,
        )
        # return final library_item after all match/metadata actions
        return library_item

    async def update_item_in_library(
        self, item_id: str | int, update: Track, overwrite: bool = False
    ) -> Track:
        """Update Track record in the database, merging data."""
        db_id = int(item_id)  # ensure integer
        cur_item = await self.get_library_item(db_id)
        metadata = cur_item.metadata.update(getattr(update, "metadata", None), overwrite)
        provider_mappings = self._get_provider_mappings(cur_item, update, overwrite)
        track_artists = await self._get_artist_mappings(cur_item, update, overwrite=overwrite)
        cur_item.external_ids.update(update.external_ids)
        await self.mass.music.database.update(
            self.db_table,
            {"item_id": db_id},
            {
                "name": update.name or cur_item.name,
                "sort_name": update.sort_name or cur_item.sort_name,
                "version": update.version or cur_item.version,
                "duration": getattr(update, "duration", None) or cur_item.duration,
                "artists": serialize_to_json(track_artists),
                "metadata": serialize_to_json(metadata),
                "provider_mappings": serialize_to_json(provider_mappings),
                "timestamp_modified": int(utc_timestamp()),
                "external_ids": serialize_to_json(
                    update.external_ids if overwrite else cur_item.external_ids
                ),
            },
        )
        # update/set provider_mappings table
        await self._set_provider_mappings(db_id, provider_mappings)
        # handle track album
        if update.album:
            await self._set_track_album(
                db_id=db_id,
                album=update.album,
                disc_number=getattr(update, "disc_number", None) or 0,
                track_number=getattr(update, "track_number", None) or 0,
            )
        # get full created object
        library_item = await self.get_library_item(db_id)
        # only signal event if we're not running a sync (to prevent a floodstorm of events)
        if not self.mass.music.get_running_sync_tasks():
            self.mass.signal_event(
                EventType.MEDIA_ITEM_UPDATED,
                library_item.uri,
                library_item,
            )
        self.logger.debug("updated %s in database: %s", update.name, db_id)
        # return the full item we just updated
        return library_item

    async def versions(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> list[Track]:
        """Return all versions of a track we can find on all providers."""
        track = await self.get(item_id, provider_instance_id_or_domain, add_to_library=False)
        search_query = f"{track.artists[0].name} - {track.name}"
        result: list[Track] = []
        for provider_id in self.mass.music.get_unique_providers():
            provider = self.mass.get_provider(provider_id)
            if not provider:
                continue
            if not provider.library_supported(MediaType.TRACK):
                continue
            result += [
                prov_item
                for prov_item in await self.search(search_query, provider_id)
                if loose_compare_strings(track.name, prov_item.name)
                and compare_artists(prov_item.artists, track.artists, any_match=True)
                # make sure that the 'base' version is NOT included
                and prov_item.item_id != item_id
            ]
        return result

    async def albums(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> list[Album]:
        """Return all albums the track appears on."""
        if provider_instance_id_or_domain == "library":
            return [
                await self.mass.music.albums.get_library_item(album_track_row["album_id"])
                async for album_track_row in self.mass.music.database.iter_items(
                    DB_TABLE_ALBUM_TRACKS, {"track_id": int(item_id)}
                )
            ]
        # use search to get all items on the provider
        # TODO: we could use musicbrainz info here to get a list of all releases known
        track = await self.get(item_id, provider_instance_id_or_domain, add_to_library=False)
        search_query = f"{track.artists[0].name} - {track.name}"
        return [
            prov_item.album
            for prov_item in await self.search(search_query, provider_instance_id_or_domain)
            if loose_compare_strings(track.name, prov_item.name)
            and prov_item.album
            and compare_artists(prov_item.artists, track.artists, any_match=True)
        ]

    async def remove_item_from_library(self, item_id: str | int) -> None:
        """Delete record from the database."""
        db_id = int(item_id)  # ensure integer
        # delete entry(s) from albumtracks table
        await self.mass.music.database.delete(DB_TABLE_ALBUM_TRACKS, {"track_id": db_id})
        # delete the track itself from db
        await super().remove_item_from_library(db_id)

    async def get_preview_url(self, provider_instance_id_or_domain: str, item_id: str) -> str:
        """Return url to short preview sample."""
        track = await self.get_provider_item(item_id, provider_instance_id_or_domain)
        # prefer provider-provided preview
        if preview := track.metadata.preview:
            return preview
        # fallback to a preview/sample hosted by our own webserver
        enc_track_id = urllib.parse.quote(item_id)
        return (
            f"{self.mass.webserver.base_url}/preview?"
            f"provider={provider_instance_id_or_domain}&item_id={enc_track_id}"
        )

    async def _match(self, db_track: Track) -> None:
        """Try to find matching track on all providers for the provided (database) track_id.

        This is used to link objects of different providers/qualities together.
        """
        if db_track.provider != "library":
            return  # Matching only supported for database items
        track_albums = await self.albums(db_track.item_id, db_track.provider)
        for provider in self.mass.music.providers:
            if ProviderFeature.SEARCH not in provider.supported_features:
                continue
            if not provider.is_streaming_provider:
                # matching on unique providers is pointless as they push (all) their content to MA
                continue
            if not provider.library_supported(MediaType.TRACK):
                continue
            self.logger.debug(
                "Trying to match track %s on provider %s", db_track.name, provider.name
            )
            match_found = False
            for search_str in (
                db_track.name,
                f"{db_track.artists[0].name} - {db_track.name}",
                f"{db_track.artists[0].name} {db_track.name}",
            ):
                if match_found:
                    break
                search_result = await self.search(search_str, provider.domain)
                for search_result_item in search_result:
                    if not search_result_item.available:
                        continue
                    # do a basic compare first
                    if not compare_track(db_track, search_result_item, strict=False):
                        continue
                    # we must fetch the full version, search results are simplified objects
                    prov_track = await self.get_provider_item(
                        search_result_item.item_id,
                        search_result_item.provider,
                        fallback=search_result_item,
                    )
                    if compare_track(db_track, prov_track, strict=True, track_albums=track_albums):
                        # 100% match, we update the db with the additional provider mapping(s)
                        match_found = True
                        for provider_mapping in search_result_item.provider_mappings:
                            await self.add_provider_mapping(db_track.item_id, provider_mapping)

            if not match_found:
                self.logger.debug(
                    "Could not find match for Track %s on provider %s",
                    db_track.name,
                    provider.name,
                )

    async def _get_provider_dynamic_tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        limit: int = 25,
    ):
        """Generate a dynamic list of tracks based on the track."""
        assert provider_instance_id_or_domain != "library"
        prov = self.mass.get_provider(provider_instance_id_or_domain)
        if prov is None:
            return []
        if ProviderFeature.SIMILAR_TRACKS not in prov.supported_features:
            return []
        # Grab similar tracks from the music provider
        return await prov.get_similar_tracks(prov_track_id=item_id, limit=limit)

    async def _get_dynamic_tracks(
        self,
        media_item: Track,
        limit: int = 25,
    ) -> list[Track]:
        """Get dynamic list of tracks for given item, fallback/default implementation."""
        # TODO: query metadata provider(s) to get similar tracks (or tracks from similar artists)
        msg = "No Music Provider found that supports requesting similar tracks."
        raise UnsupportedFeaturedException(msg)

    async def _add_library_item(self, item: Track) -> Track:
        """Add a new item record to the database."""
        track_artists = await self._get_artist_mappings(item)
        sort_artist = track_artists[0].sort_name
        new_item = await self.mass.music.database.insert(
            self.db_table,
            {
                "name": item.name,
                "sort_name": item.sort_name,
                "version": item.version,
                "duration": item.duration,
                "favorite": item.favorite,
                "external_ids": serialize_to_json(item.external_ids),
                "metadata": serialize_to_json(item.metadata),
                "provider_mappings": serialize_to_json(item.provider_mappings),
                "artists": serialize_to_json(track_artists),
                "sort_artist": sort_artist,
                "timestamp_added": int(utc_timestamp()),
                "timestamp_modified": int(utc_timestamp()),
            },
        )
        db_id = new_item["item_id"]
        # update/set provider_mappings table
        await self._set_provider_mappings(db_id, item.provider_mappings)
        # handle track album
        if item.album:
            await self._set_track_album(
                db_id=db_id,
                album=item.album,
                disc_number=getattr(item, "disc_number", None) or 0,
                track_number=getattr(item, "track_number", None) or 0,
            )
        self.logger.debug("added %s to database: %s", item.name, db_id)
        # return the full item we just added
        return await self.get_library_item(db_id)

    async def _set_track_album(
        self, db_id: int, album: Album, disc_number: int, track_number: int
    ) -> None:
        """Store AlbumTrack info."""
        db_album = None
        if album.provider == "library":
            db_album = album
        elif existing := await self.mass.music.artists.get_library_item_by_prov_id(
            album.item_id, album.provider
        ):
            db_album = existing
        else:
            # not an existing album, we need to fetch and add it
            if isinstance(album, ItemMapping):
                album = await self.mass.music.albums.get_provider_item(
                    album.item_id, album.provider, fallback=album
                )
            with suppress(MediaNotFoundError, AssertionError, InvalidDataError):
                db_album = await self.mass.music.albums.add_item_to_library(
                    album, metadata_lookup=False, add_album_tracks=False
                )

        if not db_album:
            self.logger.warning(
                "Unable to resolve Album for track %s, "
                "track will be added to the library without album",
                album.uri,
            )
            return
        album_mapping = {"track_id": db_id, "album_id": int(db_album.item_id)}
        if db_row := await self.mass.music.database.get_row(DB_TABLE_ALBUM_TRACKS, album_mapping):
            # update existing
            await self.mass.music.database.update(
                DB_TABLE_ALBUM_TRACKS,
                album_mapping,
                {
                    "disc_number": disc_number or db_row["disc_number"],
                    "track_number": track_number or db_row["track_number"],
                },
            )
        else:
            # create new albumtrack record
            await self.mass.music.database.insert_or_replace(
                DB_TABLE_ALBUM_TRACKS,
                {
                    **album_mapping,
                    "disc_number": disc_number,
                    "track_number": track_number,
                },
            )
