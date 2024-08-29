"""Manage MediaItems of type Track."""

from __future__ import annotations

import urllib.parse
from collections.abc import Iterable
from contextlib import suppress
from typing import Any

from music_assistant.common.helpers.json import serialize_to_json
from music_assistant.common.models.enums import MediaType, ProviderFeature
from music_assistant.common.models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    MusicAssistantError,
    UnsupportedFeaturedException,
)
from music_assistant.common.models.media_items import (
    Album,
    Artist,
    ItemMapping,
    ProviderMapping,
    Track,
    UniqueList,
)
from music_assistant.constants import (
    DB_TABLE_ALBUM_TRACKS,
    DB_TABLE_ALBUMS,
    DB_TABLE_TRACK_ARTISTS,
    DB_TABLE_TRACKS,
)
from music_assistant.server.helpers.compare import (
    compare_artists,
    compare_media_item,
    compare_track,
    loose_compare_strings,
)
from music_assistant.server.models.music_provider import MusicProvider

from .base import MediaControllerBase


class TracksController(MediaControllerBase[Track]):
    """Controller managing MediaItems of type Track."""

    db_table = DB_TABLE_TRACKS
    media_type = MediaType.TRACK
    item_cls = Track

    def __init__(self, *args, **kwargs) -> None:
        """Initialize class."""
        super().__init__(*args, **kwargs)
        self.base_query = """
        SELECT
            tracks.*,
            (SELECT JSON_GROUP_ARRAY(
                json_object(
                'item_id', provider_mappings.provider_item_id,
                    'provider_domain', provider_mappings.provider_domain,
                        'provider_instance', provider_mappings.provider_instance,
                        'available', provider_mappings.available,
                        'audio_format', json(provider_mappings.audio_format),
                        'url', provider_mappings.url,
                        'details', provider_mappings.details
                )) FROM provider_mappings WHERE provider_mappings.item_id = tracks.item_id AND media_type = 'track') AS provider_mappings,

            (SELECT JSON_GROUP_ARRAY(
                json_object(
                'item_id', artists.item_id,
                'provider', 'library',
                    'name', artists.name,
                    'sort_name', artists.sort_name,
                    'media_type', 'artist'
                )) FROM artists JOIN track_artists on track_artists.track_id = tracks.item_id  WHERE artists.item_id = track_artists.artist_id) AS artists,
            (SELECT
                json_object(
                'item_id', albums.item_id,
                'provider', 'library',
                    'name', albums.name,
                    'sort_name', albums.sort_name,
                    'media_type', 'album',
                    'disc_number', album_tracks.disc_number,
                    'track_number', album_tracks.track_number,
                    'images', json_extract(albums.metadata, '$.images')
                ) FROM albums JOIN album_tracks on album_tracks.track_id = tracks.item_id  WHERE albums.item_id = album_tracks.album_id) AS track_album
            FROM tracks"""  # noqa: E501
        # register (extra) api handlers
        api_base = self.api_base
        self.mass.register_api_command(f"music/{api_base}/track_versions", self.versions)
        self.mass.register_api_command(f"music/{api_base}/track_albums", self.albums)
        self.mass.register_api_command(f"music/{api_base}/preview", self.get_preview_url)

    async def get(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        recursive: bool = True,
        album_uri: str | None = None,
    ) -> Track:
        """Return (full) details for a single media item."""
        track = await super().get(
            item_id,
            provider_instance_id_or_domain,
        )
        if not recursive and album_uri is None:
            # return early if we do not want recursive full details and no album uri is provided
            return track

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
                    track.album.item_id, track.album.provider, recursive=False
                )
        except MusicAssistantError as err:
            # edge case where playlist track has invalid albumdetails
            self.logger.warning("Unable to fetch album details for %s - %s", track.uri, str(err))

        if not recursive:
            return track

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
                    )
                )
            except MusicAssistantError as err:
                # edge case where playlist track has invalid artistdetails
                self.logger.warning("Unable to fetch artist details %s - %s", artist.uri, str(err))
        track.artists = track_artists
        return track

    async def library_items(
        self,
        favorite: bool | None = None,
        search: str | None = None,
        limit: int = 500,
        offset: int = 0,
        order_by: str = "sort_name",
        provider: str | None = None,
        extra_query: str | None = None,
        extra_query_params: dict[str, Any] | None = None,
    ) -> list[Track]:
        """Get in-database tracks."""
        extra_query_params: dict[str, Any] = extra_query_params or {}
        extra_query_parts: list[str] = [extra_query] if extra_query else []
        extra_join_parts: list[str] = []
        if search and " - " in search:
            # handle combined artist + title search
            artist_str, title_str = search.split(" - ", 1)
            search = None
            extra_query_parts.append("tracks.name LIKE :search_title")
            extra_query_params["search_title"] = f"%{title_str}%"
            # use join with artists table to filter on artist name
            extra_join_parts.append(
                "JOIN track_artists ON track_artists.track_id = tracks.item_id "
                "JOIN artists ON artists.item_id = track_artists.artist_id "
                "AND artists.name LIKE :search_artist"
            )
            extra_query_params["search_artist"] = f"%{artist_str}%"
        result = await self._get_library_items_by_query(
            favorite=favorite,
            search=search,
            limit=limit,
            offset=offset,
            order_by=order_by,
            provider=provider,
            extra_query_parts=extra_query_parts,
            extra_query_params=extra_query_params,
            extra_join_parts=extra_join_parts,
        )
        if search and len(result) < 25 and not offset:
            # append artist items to result
            extra_join_parts.append(
                "JOIN track_artists ON track_artists.track_id = tracks.item_id "
                "JOIN artists ON artists.item_id = track_artists.artist_id "
                "AND artists.name LIKE :search_artist"
            )
            extra_query_params["search_artist"] = f"%{search}%"
            return result + await self._get_library_items_by_query(
                favorite=favorite,
                search=None,
                limit=limit,
                order_by=order_by,
                provider=provider,
                extra_query_parts=extra_query_parts,
                extra_query_params=extra_query_params,
                extra_join_parts=extra_join_parts,
            )
        return result

    async def versions(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> UniqueList[Track]:
        """Return all versions of a track we can find on all providers."""
        track = await self.get(item_id, provider_instance_id_or_domain)
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
        query = f"{DB_TABLE_ALBUMS}.item_id in ({subquery})"
        return await self.mass.music.albums._get_library_items_by_query(extra_query_parts=[query])

    async def match_providers(self, db_track: Track) -> None:
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
            provider_matches = await self.match_provider(
                provider, db_track, strict=True, ref_albums=track_albums
            )
            for provider_mapping in provider_matches:
                # 100% match, we update the db with the additional provider mapping(s)
                await self.add_provider_mapping(db_track.item_id, provider_mapping)
                db_track.provider_mappings.add(provider_mapping)

    async def match_provider(
        self,
        provider: MusicProvider,
        ref_track: Track,
        strict: bool = True,
        ref_albums: list[Album] | None = None,
    ) -> set[ProviderMapping]:
        """Try to find matching track on given provider."""
        if ref_albums is None:
            ref_albums = await self.albums(ref_track.item_id, ref_track.provider)
        if ProviderFeature.SEARCH not in provider.supported_features:
            raise UnsupportedFeaturedException("Provider does not support search")
        if not provider.is_streaming_provider:
            raise UnsupportedFeaturedException("Matching only possible for streaming providers")
        self.logger.debug("Trying to match track %s on provider %s", ref_track.name, provider.name)
        matches: set[ProviderMapping] = set()
        for artist in ref_track.artists:
            if matches:
                break
            search_str = f"{artist.name} - {ref_track.name}"
            search_result = await self.search(search_str, provider.domain)
            for search_result_item in search_result:
                if not search_result_item.available:
                    continue
                # do a basic compare first
                if not compare_media_item(ref_track, search_result_item, strict=False):
                    continue
                # we must fetch the full version, search results can be simplified objects
                prov_track = await self.get_provider_item(
                    search_result_item.item_id,
                    search_result_item.provider,
                    fallback=search_result_item,
                )
                if compare_track(ref_track, prov_track, strict=strict, track_albums=ref_albums):
                    matches.update(search_result_item.provider_mappings)

        if not matches:
            self.logger.debug(
                "Could not find match for Track %s on provider %s",
                ref_track.name,
                provider.name,
            )
        return matches

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
        db_id = await self.mass.music.database.insert(
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
        # update/set provider_mappings table
        await self._set_provider_mappings(db_id, item.provider_mappings)
        # set track artist(s)
        await self._set_track_artists(db_id, item.artists)
        # handle track album
        if item.album:
            await self._set_track_album(
                db_id=db_id,
                album=item.album,
                disc_number=getattr(item, "disc_number", 0),
                track_number=getattr(item, "track_number", 0),
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
                disc_number=update.disc_number or cur_item.disc_number,
                track_number=update.track_number or cur_item.track_number,
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
                artist, overwrite_existing=overwrite
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
