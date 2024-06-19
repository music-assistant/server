"""Manage MediaItems of type Track."""

from __future__ import annotations

import urllib.parse
from collections.abc import Iterable
from contextlib import suppress

from music_assistant.common.helpers.json import serialize_to_json
from music_assistant.common.models.enums import MediaType, ProviderFeature
from music_assistant.common.models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    MusicAssistantError,
    UnsupportedFeaturedException,
)
from music_assistant.common.models.media_items import Album, Artist, ItemMapping, Track, UniqueList
from music_assistant.constants import (
    DB_TABLE_ALBUM_TRACKS,
    DB_TABLE_ALBUMS,
    DB_TABLE_ARTISTS,
    DB_TABLE_PROVIDER_MAPPINGS,
    DB_TABLE_TRACK_ARTISTS,
    DB_TABLE_TRACKS,
)
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
        self.base_query = f"""
        SELECT DISTINCT
            {self.db_table}.*,
            CASE WHEN albums.item_id IS NULL THEN NULL ELSE
            json_object(
                'item_id', {DB_TABLE_ALBUMS}.item_id,
                'provider', 'library',
                'name', {DB_TABLE_ALBUMS}.name,
                'sort_name', {DB_TABLE_ALBUMS}.sort_name,
                'version', {DB_TABLE_ALBUMS}.version,
                'images',  json_extract({DB_TABLE_ALBUMS}.metadata, '$.images'),
                'media_type', 'album') END as album,
            {DB_TABLE_ALBUM_TRACKS}.disc_number,
            {DB_TABLE_ALBUM_TRACKS}.track_number
        FROM {self.db_table}
        LEFT JOIN {DB_TABLE_ALBUM_TRACKS} on {DB_TABLE_ALBUM_TRACKS}.track_id = {self.db_table}.item_id
        LEFT JOIN {DB_TABLE_ALBUMS} on {DB_TABLE_ALBUMS}.item_id = {DB_TABLE_ALBUM_TRACKS}.album_id
        LEFT JOIN {DB_TABLE_TRACK_ARTISTS} on {DB_TABLE_TRACK_ARTISTS}.track_id = {self.db_table}.item_id
        LEFT JOIN {DB_TABLE_ARTISTS} on {DB_TABLE_ARTISTS}.item_id = {DB_TABLE_TRACK_ARTISTS}.artist_id
        LEFT JOIN {DB_TABLE_PROVIDER_MAPPINGS} ON
            {DB_TABLE_PROVIDER_MAPPINGS}.item_id = {self.db_table}.item_id AND media_type = '{self.media_type}'
        """  # noqa: E501
        # register (extra) api handlers
        api_base = self.api_base
        self.mass.register_api_command(f"music/{api_base}/track_versions", self.versions)
        self.mass.register_api_command(f"music/{api_base}/track_albums", self.albums)
        self.mass.register_api_command(f"music/{api_base}/preview", self.get_preview_url)

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
        # append full album details to full track item (resolve ItemMappings)
        try:
            if album_uri and (album := await self.mass.music.get_item_by_uri(album_uri)):
                track.album = album
            elif provider_instance_id_or_domain == "library":
                # grab the first album this track is attached to
                for album_track_row in await self.mass.music.database.get_rows(
                    DB_TABLE_ALBUM_TRACKS, {"track_id": int(item_id)}, limit=1
                ):
                    track.album = await self.mass.music.albums.get_library_item(
                        album_track_row["album_id"]
                    )
            elif isinstance(track.album, ItemMapping) or (track.album and not track.album.image):
                track.album = await self.mass.music.albums.get(
                    track.album.item_id,
                    track.album.provider,
                    lazy=lazy,
                    details=None if isinstance(track.album, ItemMapping) else track.album,
                    add_to_library=False,  # TODO: make this configurable
                )
        except MusicAssistantError as err:
            # edge case where playlist track has invalid albumdetails
            self.logger.warning("Unable to fetch album details %s - %s", track.album.uri, str(err))

        # append artist details to full track item (resolve ItemMappings)
        track_artists = []
        for artist in track.artists:
            if not isinstance(artist, ItemMapping):
                track_artists.append(artist)
                continue
            try:
                track_artists.append(
                    await self.mass.music.artists.get(
                        artist.item_id,
                        artist.provider,
                        lazy=lazy,
                        add_to_library=False,  # TODO: make this configurable
                    )
                )
            except MusicAssistantError as err:
                # edge case where playlist track has invalid artistdetails
                self.logger.warning("Unable to fetch artist details %s - %s", artist.uri, str(err))
        track.artists = track_artists
        return track

    async def versions(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> UniqueList[Track]:
        """Return all versions of a track we can find on all providers."""
        track = await self.get(item_id, provider_instance_id_or_domain, add_to_library=False)
        search_query = f"{track.artist_str} - {track.name}"
        result: UniqueList[Track] = UniqueList()
        for provider_id in self.mass.music.get_unique_providers():
            provider = self.mass.get_provider(provider_id)
            if not provider:
                continue
            if not provider.library_supported(MediaType.TRACK):
                continue
            result.extend(
                prov_item
                for prov_item in await self.search(search_query, provider_id)
                if loose_compare_strings(track.name, prov_item.name)
                and compare_artists(prov_item.artists, track.artists, any_match=True)
                # make sure that the 'base' version is NOT included
                and not track.provider_mappings.intersection(prov_item.provider_mappings)
            )
        return result

    async def albums(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        in_library_only: bool = False,
    ) -> UniqueList[Album]:
        """Return all albums the track appears on."""
        full_track = await self.get(item_id, provider_instance_id_or_domain)
        db_items = (
            await self.get_library_track_albums(full_track.item_id)
            if full_track.provider == "library"
            else []
        )
        # return all (unique) items from all providers
        result: UniqueList[Album] = UniqueList(db_items)
        if full_track.provider == "library" and in_library_only:
            # return in-library items only
            return result
        # use search to get all items on the provider
        search_query = f"{full_track.artist_str} - {full_track.name}"
        # TODO: we could use musicbrainz info here to get a list of all releases known
        unique_ids: set[str] = set()
        for prov_item in (await self.mass.music.search(search_query, [MediaType.TRACK])).tracks:
            if not loose_compare_strings(full_track.name, prov_item.name):
                continue
            if not prov_item.album:
                continue
            if not compare_artists(full_track.artists, prov_item.artists, any_match=True):
                continue
            unique_id = f"{prov_item.album.name}.{prov_item.album.version}"
            if unique_id in unique_ids:
                continue
            unique_ids.add(unique_id)
            # prefer db item
            if db_item := await self.mass.music.albums.get_library_item_by_prov_id(
                prov_item.album.item_id, prov_item.album.provider
            ):
                result.append(db_item)
            elif not in_library_only:
                result.append(prov_item.album)
        return result

    async def remove_item_from_library(self, item_id: str | int) -> None:
        """Delete record from the database."""
        db_id = int(item_id)  # ensure integer
        # delete entry(s) from albumtracks table
        await self.mass.music.database.delete(DB_TABLE_ALBUM_TRACKS, {"track_id": db_id})
        # delete entry(s) from trackartists table
        await self.mass.music.database.delete(DB_TABLE_TRACK_ARTISTS, {"track_id": db_id})
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

    async def get_library_track_albums(
        self,
        item_id: str | int,
    ) -> list[Album]:
        """Return all in-library albums for a track."""
        subquery = (
            f"SELECT album_id FROM {DB_TABLE_ALBUM_TRACKS} "
            f"WHERE {DB_TABLE_ALBUM_TRACKS}.track_id = {item_id}"
        )
        query = f"WHERE {DB_TABLE_ALBUMS}.item_id in ({subquery})"
        return await self.mass.music.albums._get_library_items_by_query(extra_query=query)

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

    async def _add_library_item(self, item: Track) -> int:
        """Add a new item record to the database."""
        if not isinstance(item, Track):
            msg = "Not a valid Track object (ItemMapping can not be added to db)"
            raise InvalidDataError(msg)
        if not item.artists:
            msg = "Track is missing artist(s)"
            raise InvalidDataError(msg)
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
            },
        )
        db_id = new_item["item_id"]
        # update/set provider_mappings table
        await self._set_provider_mappings(db_id, item.provider_mappings)
        # set track artist(s)
        await self._set_track_artists(db_id, item.artists)
        # handle track album
        if item.album:
            await self._set_track_album(
                db_id=db_id,
                album=item.album,
                disc_number=getattr(item, "disc_number", None) or 0,
                track_number=getattr(item, "track_number", None) or 0,
            )
        self.logger.debug("added %s to database (id: %s)", item.name, db_id)
        return db_id

    async def _update_library_item(
        self, item_id: str | int, update: Track, overwrite: bool = False
    ) -> None:
        """Update Track record in the database, merging data."""
        db_id = int(item_id)  # ensure integer
        cur_item = await self.get_library_item(db_id)
        metadata = update.metadata if overwrite else cur_item.metadata.update(update.metadata)
        cur_item.external_ids.update(update.external_ids)
        await self.mass.music.database.update(
            self.db_table,
            {"item_id": db_id},
            {
                "name": update.name if overwrite else cur_item.name,
                "sort_name": update.sort_name
                if overwrite
                else cur_item.sort_name or update.sort_name,
                "version": update.version if overwrite else cur_item.version or update.version,
                "duration": update.duration if overwrite else cur_item.duration or update.duration,
                "metadata": serialize_to_json(metadata),
                "external_ids": serialize_to_json(
                    update.external_ids if overwrite else cur_item.external_ids
                ),
            },
        )
        # update/set provider_mappings table
        provider_mappings = (
            update.provider_mappings
            if overwrite
            else {*cur_item.provider_mappings, *update.provider_mappings}
        )
        await self._set_provider_mappings(db_id, provider_mappings, overwrite)
        # set track artist(s)
        artists = update.artists if overwrite else cur_item.artists + update.artists
        await self._set_track_artists(db_id, artists, overwrite=overwrite)
        # update/set track album
        if update.album:
            await self._set_track_album(
                db_id=db_id,
                album=update.album,
                disc_number=getattr(update, "disc_number", None) or 0,
                track_number=getattr(update, "track_number", None) or 1,
                overwrite=overwrite,
            )
        self.logger.debug("updated %s in database: (id %s)", update.name, db_id)

    async def _set_track_album(
        self,
        db_id: int,
        album: Album | ItemMapping,
        disc_number: int,
        track_number: int,
        overwrite: bool = False,
    ) -> None:
        """
        Store Track Album info.

        A track can exist on multiple albums so we have a mapping table between
        albums and tracks which stores the relation between the two and it also
        stores the track and disc number of the track within an album.
        For digital releases, the discnumber will be just 0 or 1.
        Track number should start counting at 1.
        """
        db_album: Album | ItemMapping = None
        if album.provider == "library":
            db_album = album
        elif existing := await self.mass.music.albums.get_library_item_by_prov_id(
            album.item_id, album.provider
        ):
            db_album = existing

        if not db_album or overwrite:
            # ensure we have an actual album object
            if isinstance(album, ItemMapping):
                album = await self.mass.music.albums.get_provider_item(
                    album.item_id, album.provider, fallback=album
                )
            with suppress(MediaNotFoundError, AssertionError, InvalidDataError):
                db_album = await self.mass.music.albums.add_item_to_library(
                    album,
                    metadata_lookup=False,
                    overwrite_existing=overwrite,
                )
        if not db_album:
            # this should not happen but streaming providers can be awful sometimes
            self.logger.warning(
                "Unable to resolve Album %s for track %s, "
                "track will be added to the library without this album!",
                album.uri,
                db_id,
            )
            return
        # write (or update) record in album_tracks table
        await self.mass.music.database.insert_or_replace(
            DB_TABLE_ALBUM_TRACKS,
            {
                "track_id": db_id,
                "album_id": int(db_album.item_id),
                "disc_number": disc_number,
                "track_number": track_number,
            },
        )

    async def _set_track_artists(
        self, db_id: int, artists: Iterable[Artist | ItemMapping], overwrite: bool = False
    ) -> None:
        """Store Track Artists."""
        if overwrite:
            # on overwrite, clear the track_artists table first
            await self.mass.music.database.delete(
                DB_TABLE_TRACK_ARTISTS,
                {
                    "track_id": db_id,
                },
            )
        artist_mappings: UniqueList[ItemMapping] = UniqueList()
        for artist in artists:
            mapping = await self._set_track_artist(db_id, artist=artist, overwrite=overwrite)
            artist_mappings.append(mapping)
        # we (temporary?) duplicate the artist mappings in a separate column of the media
        # item's table, because the json_group_array query is superslow
        await self.mass.music.database.update(
            self.db_table,
            {"item_id": db_id},
            {"artists": serialize_to_json(artist_mappings)},
        )

    async def _set_track_artist(
        self, db_id: int, artist: Artist | ItemMapping, overwrite: bool = False
    ) -> ItemMapping:
        """Store Track Artist info."""
        db_artist: Artist | ItemMapping = None
        if artist.provider == "library":
            db_artist = artist
        elif existing := await self.mass.music.artists.get_library_item_by_prov_id(
            artist.item_id, artist.provider
        ):
            db_artist = existing

        if not db_artist or overwrite:
            db_artist = await self.mass.music.artists.add_item_to_library(
                artist, metadata_lookup=False, overwrite_existing=overwrite
            )
        # write (or update) record in album_artists table
        await self.mass.music.database.insert_or_replace(
            DB_TABLE_TRACK_ARTISTS,
            {
                "track_id": db_id,
                "artist_id": int(db_artist.item_id),
            },
        )
        return ItemMapping.from_item(db_artist)
