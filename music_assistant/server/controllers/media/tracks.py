"""Manage MediaItems of type Track."""
from __future__ import annotations

import asyncio

from music_assistant.common.helpers.datetime import utc_timestamp
from music_assistant.common.helpers.json import serialize_to_json
from music_assistant.common.models.enums import EventType, MediaType, ProviderFeature
from music_assistant.common.models.errors import MediaNotFoundError, UnsupportedFeaturedException
from music_assistant.common.models.media_items import (
    Album,
    Artist,
    DbTrack,
    ItemMapping,
    Track,
    TrackAlbumMapping,
)
from music_assistant.constants import DB_TABLE_TRACKS
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
    item_cls = DbTrack

    def __init__(self, *args, **kwargs):
        """Initialize class."""
        super().__init__(*args, **kwargs)
        # register api handlers
        self.mass.register_api_command("music/tracks", self.db_items)
        self.mass.register_api_command("music/track", self.get)
        self.mass.register_api_command("music/track/versions", self.versions)
        self.mass.register_api_command("music/track/update", self.update_db_item)
        self.mass.register_api_command("music/track/delete", self.delete_db_item)
        self.mass.register_api_command("music/track/preview", self.get_preview_url)

    async def get(
        self,
        item_id: str,
        provider_domain: str | None = None,
        provider_instance: str | None = None,
        force_refresh: bool = False,
        lazy: bool = True,
        details: Track = None,
        force_provider_item: bool = False,
    ) -> Track:
        """Return (full) details for a single media item."""
        track = await super().get(
            item_id=item_id,
            provider_domain=provider_domain,
            provider_instance=provider_instance,
            force_refresh=force_refresh,
            lazy=lazy,
            details=details,
            force_provider_item=force_provider_item,
        )
        # append full album details to full track item
        if track.album:
            try:
                track.album = await self.mass.music.albums.get(
                    track.album.item_id,
                    track.album.provider,
                    lazy=True,
                    details=track.album,
                )
            except MediaNotFoundError:
                # edge case where playlist track has invalid albumdetails
                self.logger.warning("Unable to fetch album details %s", track.album.uri)
        # append full artist details to full track item
        full_artists = []
        for artist in track.artists:
            full_artists.append(
                await self.mass.music.artists.get(
                    artist.item_id, artist.provider, lazy=True, details=artist
                )
            )
        track.artists = full_artists
        return track

    async def add(self, item: Track) -> Track:
        """Add track to local db and return the new database item."""
        # make sure we have artists
        assert item.artists
        # grab additional metadata
        await self.mass.metadata.get_track_metadata(item)
        existing = await self.get_db_item_by_prov_id(item.item_id, item.provider)
        if existing:
            db_item = await self.update_db_item(existing.item_id, item)
        else:
            db_item = await self.add_db_item(item)
        # also fetch same track on all providers (will also get other quality versions)
        await self._match(db_item)
        # return final db_item after all match/metadata actions
        db_item = await self.get_db_item(db_item.item_id)
        self.mass.signal_event(
            EventType.MEDIA_ITEM_UPDATED if existing else EventType.MEDIA_ITEM_ADDED,
            db_item.uri,
            db_item,
        )
        return db_item

    async def versions(
        self,
        item_id: str,
        provider_domain: str | None = None,
        provider_instance: str | None = None,
    ) -> list[Track]:
        """Return all versions of a track we can find on all providers."""
        assert provider_domain or provider_instance, "Provider type or ID must be specified"
        track = await self.get(item_id, provider_domain or provider_instance)
        # perform a search on all provider(types) to collect all versions/variants
        provider_domains = {prov.domain for prov in self.mass.music.providers}
        search_query = f"{track.artist.name} - {track.name}"
        all_versions = {
            prov_item.item_id: prov_item
            for prov_items in await asyncio.gather(
                *[
                    self.search(search_query, provider_domain)
                    for provider_domain in provider_domains
                ]
            )
            for prov_item in prov_items
            if loose_compare_strings(track.name, prov_item.name)
            and compare_artists(prov_item.artists, track.artists, any_match=True)
        }
        # make sure that the 'base' version is NOT included
        for prov_version in track.provider_mappings:
            all_versions.pop(prov_version.item_id, None)

        # return the aggregated result
        return all_versions.values()

    async def get_preview_url(self, provider_domain: str, item_id: str) -> str:
        """Return url to short preview sample."""
        track = await self.get_provider_item(item_id, provider_domain)
        # prefer provider-provided preview
        if preview := track.metadata.preview:
            return preview
        # fallback to a preview/sample hosted by our own webserver
        return self.mass.streams.get_preview_url(provider_domain, item_id)

    async def _match(self, db_track: Track) -> None:
        """Try to find matching track on all providers for the provided (database) track_id.

        This is used to link objects of different providers/qualities together.
        """
        if db_track.provider != "database":
            return  # Matching only supported for database items
        for provider in self.mass.music.providers:
            if ProviderFeature.SEARCH not in provider.supported_features:
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
                    if compare_track(search_result_item, db_track):
                        # 100% match, we can simply update the db with additional provider ids
                        match_found = True
                        await self.update_db_item(db_track.item_id, search_result_item)

            if not match_found:
                self.logger.debug(
                    "Could not find match for Track %s on provider %s",
                    db_track.name,
                    provider.name,
                )

    async def _get_provider_dynamic_tracks(
        self,
        item_id: str,
        provider_domain: str | None = None,
        provider_instance: str | None = None,
        limit: int = 25,
    ):
        """Generate a dynamic list of tracks based on the track."""
        prov = self.mass.get_provider(provider_instance or provider_domain)
        if prov is None:
            return []
        if ProviderFeature.SIMILAR_TRACKS not in prov.supported_features:
            return []
        # Grab similar tracks from the music provider
        similar_tracks = await prov.get_similar_tracks(prov_track_id=item_id, limit=limit)
        return similar_tracks

    async def _get_dynamic_tracks(
        self, media_item: Track, limit: int = 25  # noqa: ARG002
    ) -> list[Track]:
        """Get dynamic list of tracks for given item, fallback/default implementation."""
        # TODO: query metadata provider(s) to get similar tracks (or tracks from similar artists)
        raise UnsupportedFeaturedException(
            "No Music Provider found that supports requesting similar tracks."
        )

    async def add_db_item(self, item: Track) -> Track:
        """Add a new item record to the database."""
        assert isinstance(item, Track), "Not a full Track object"
        assert item.artists, "Track is missing artist(s)"
        assert item.provider_mappings, "Track is missing provider id(s)"
        async with self._db_add_lock:
            cur_item = None

            # always try to grab existing item by external_id
            if item.musicbrainz_id:
                match = {"musicbrainz_id": item.musicbrainz_id}
                cur_item = await self.mass.music.database.get_row(self.db_table, match)
            for isrc in item.isrc:
                if search_result := await self.mass.music.database.search(
                    self.db_table, isrc, "isrc"
                ):
                    cur_item = Track.from_db_row(search_result[0])
                    break
            if not cur_item:
                # fallback to matching
                match = {"sort_name": item.sort_name}
                for row in await self.mass.music.database.get_rows(self.db_table, match):
                    row_track = Track.from_db_row(row)
                    if compare_track(row_track, item):
                        cur_item = row_track
                        break
            if cur_item:
                # update existing
                return await self.update_db_item(cur_item.item_id, item)

            # no existing match found: insert new item
            track_artists = await self._get_track_artists(item)
            track_albums = await self._get_track_albums(item)
            sort_artist = track_artists[0].sort_name if track_artists else ""
            sort_album = track_albums[0].sort_name if track_albums else ""
            new_item = await self.mass.music.database.insert(
                self.db_table,
                {
                    **item.to_db_row(),
                    "artists": serialize_to_json(track_artists),
                    "albums": serialize_to_json(track_albums),
                    "sort_artist": sort_artist,
                    "sort_album": sort_album,
                    "timestamp_added": int(utc_timestamp()),
                    "timestamp_modified": int(utc_timestamp()),
                },
            )
            item_id = new_item["item_id"]
            # update/set provider_mappings table
            await self._set_provider_mappings(item_id, item.provider_mappings)
            # return created object
            self.logger.debug("added %s to database: %s", item.name, item_id)
            return await self.get_db_item(item_id)

    async def update_db_item(
        self,
        item_id: int,
        item: Track,
    ) -> Track:
        """Update Track record in the database, merging data."""
        cur_item = await self.get_db_item(item_id)
        is_file_provider = item.provider.startswith("filesystem")
        metadata = cur_item.metadata.update(item.metadata, is_file_provider)
        provider_mappings = {*cur_item.provider_mappings, *item.provider_mappings}
        cur_item.isrc.update(item.isrc)
        if is_file_provider:
            track_artists = await self._get_track_artists(item)
        else:
            track_artists = await self._get_track_artists(cur_item, item)
        track_albums = await self._get_track_albums(cur_item, item)

        await self.mass.music.database.update(
            self.db_table,
            {"item_id": item_id},
            {
                "name": item.name if is_file_provider else cur_item.name,
                "sort_name": item.sort_name if is_file_provider else cur_item.sort_name,
                "version": item.version if is_file_provider else cur_item.version,
                "duration": item.duration or cur_item.duration,
                "artists": serialize_to_json(track_artists),
                "albums": serialize_to_json(track_albums),
                "metadata": serialize_to_json(metadata),
                "provider_mappings": serialize_to_json(provider_mappings),
                "isrc": ";".join(cur_item.isrc),
                "timestamp_modified": int(utc_timestamp()),
            },
        )
        # update/set provider_mappings table
        await self._set_provider_mappings(item_id, provider_mappings)
        self.logger.debug("updated %s in database: %s", item.name, item_id)
        return await self.get_db_item(item_id)

    async def _get_track_artists(
        self,
        base_track: Track,
        upd_track: Track | None = None,
    ) -> list[ItemMapping]:
        """Extract all (unique) artists of track as ItemMapping."""
        track_artists = upd_track.artists if upd_track and upd_track.artists else base_track.artists
        # use intermediate set to clear out duplicates
        return list({await self._get_artist_mapping(x) for x in track_artists})

    async def _get_track_albums(
        self,
        base_track: Track,
        upd_track: Track | None = None,
    ) -> list[TrackAlbumMapping]:
        """Extract all (unique) albums of track as TrackAlbumMapping."""
        track_albums: list[TrackAlbumMapping] = []
        # existing TrackAlbumMappings are starting point
        if base_track.albums:
            track_albums = base_track.albums
        elif upd_track and upd_track.albums:
            track_albums = upd_track.albums
        # append update item album if needed
        if upd_track and upd_track.album:
            mapping = await self._get_album_mapping(upd_track.album)
            mapping = TrackAlbumMapping.from_dict(
                {
                    **mapping.to_dict(),
                    "disc_number": upd_track.disc_number,
                    "track_number": upd_track.track_number,
                }
            )
            if mapping not in track_albums:
                track_albums.append(mapping)
        # append base item album if needed
        elif base_track and base_track.album:
            mapping = await self._get_album_mapping(base_track.album)
            mapping = TrackAlbumMapping.from_dict(
                {
                    **mapping.to_dict(),
                    "disc_number": base_track.disc_number,
                    "track_number": base_track.track_number,
                }
            )
            if mapping not in track_albums:
                track_albums.append(mapping)

        return track_albums

    async def _get_album_mapping(
        self,
        album: Album | ItemMapping,
    ) -> ItemMapping:
        """Extract (database) album as ItemMapping."""
        if album.provider == "database":
            if isinstance(album, ItemMapping):
                return album
            return ItemMapping.from_item(album)

        if db_album := await self.mass.music.albums.get_db_item_by_prov_id(
            album.item_id, provider_domain=album.provider
        ):
            return ItemMapping.from_item(db_album)

        db_album = await self.mass.music.albums.add_db_item(album)
        return ItemMapping.from_item(db_album)

    async def _get_artist_mapping(self, artist: Artist | ItemMapping) -> ItemMapping:
        """Extract (database) track artist as ItemMapping."""
        if artist.provider == "database":
            if isinstance(artist, ItemMapping):
                return artist
            return ItemMapping.from_item(artist)

        if db_artist := await self.mass.music.artists.get_db_item_by_prov_id(
            artist.item_id, provider_domain=artist.provider
        ):
            return ItemMapping.from_item(db_artist)

        db_artist = await self.mass.music.artists.add_db_item(artist)
        return ItemMapping.from_item(db_artist)
