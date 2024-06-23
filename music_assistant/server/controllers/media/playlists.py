"""Manage MediaItems of type Playlist."""

from __future__ import annotations

import random
from typing import Any

from music_assistant.common.helpers.json import serialize_to_json
from music_assistant.common.helpers.uri import create_uri, parse_uri
from music_assistant.common.models.enums import MediaType, ProviderFeature, ProviderType
from music_assistant.common.models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    ProviderUnavailableError,
    UnsupportedFeaturedException,
)
from music_assistant.common.models.media_items import Playlist, PlaylistTrack, Track
from music_assistant.constants import DB_TABLE_PLAYLISTS
from music_assistant.server.models.music_provider import MusicProvider

from .base import MediaControllerBase


class PlaylistController(MediaControllerBase[Playlist]):
    """Controller managing MediaItems of type Playlist."""

    db_table = DB_TABLE_PLAYLISTS
    media_type = MediaType.PLAYLIST
    item_cls = Playlist

    def __init__(self, *args, **kwargs) -> None:
        """Initialize class."""
        super().__init__(*args, **kwargs)
        # register (extra) api handlers
        api_base = self.api_base
        self.mass.register_api_command(f"music/{api_base}/create_playlist", self.create_playlist)
        self.mass.register_api_command("music/playlists/playlist_tracks", self.tracks)
        self.mass.register_api_command(
            "music/playlists/add_playlist_tracks", self.add_playlist_tracks
        )
        self.mass.register_api_command(
            "music/playlists/remove_playlist_tracks", self.remove_playlist_tracks
        )

    async def tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        force_refresh: bool = False,
        offset: int = 0,
        limit: int = 50,
        prefer_library_items: bool = True,
    ) -> list[PlaylistTrack]:
        """Return playlist tracks for the given provider playlist id."""
        playlist = await self.get(
            item_id,
            provider_instance_id_or_domain,
            force_refresh=force_refresh,
            lazy=not force_refresh,
        )
        prov_map = next(x for x in playlist.provider_mappings)
        cache_checksum = playlist.metadata.cache_checksum
        tracks = await self._get_provider_playlist_tracks(
            prov_map.item_id,
            prov_map.provider_instance,
            cache_checksum=cache_checksum,
            offset=offset,
            limit=limit,
        )
        if prefer_library_items:
            final_tracks = []
            for track in tracks:
                # prefer library_item
                # TODO: we could speedup this call by requesting all tracks at once
                # but so far this doesn't seem to be that slow due to the paging
                if track.provider == "library":
                    final_tracks.append(track)
                elif db_item := await self.mass.music.tracks.get_library_item_by_prov_id(
                    track.item_id, track.provider
                ):
                    db_item.position = track.position
                    final_tracks.append(db_item)
                else:
                    # fall back to the original playlist item if we do not know it in the db
                    final_tracks.append(track)
        else:
            final_tracks = tracks
        return final_tracks

    async def create_playlist(
        self, name: str, provider_instance_or_domain: str | None = None
    ) -> Playlist:
        """Create new playlist."""
        # if provider is omitted, just pick builtin provider
        if provider_instance_or_domain:
            provider = self.mass.get_provider(provider_instance_or_domain)
            if provider is None:
                raise ProviderUnavailableError
        else:
            provider = self.mass.get_provider("builtin")

        # create playlist on the provider
        playlist = await provider.create_playlist(name)
        # add the new playlist to the library
        return await self.add_item_to_library(playlist, False)

    async def add_playlist_tracks(self, db_playlist_id: str | int, uris: list[str]) -> None:  # noqa: PLR0915
        """Add tracks to playlist."""
        db_id = int(db_playlist_id)  # ensure integer
        playlist = await self.get_library_item(db_id)
        if not playlist:
            msg = f"Playlist with id {db_id} not found"
            raise MediaNotFoundError(msg)
        if not playlist.is_editable:
            msg = f"Playlist {playlist.name} is not editable"
            raise InvalidDataError(msg)

        # grab all existing track ids in the playlist so we can check for duplicates
        playlist_prov_map = next(iter(playlist.provider_mappings))
        playlist_prov = self.mass.get_provider(playlist_prov_map.provider_instance)
        if not playlist_prov or not playlist_prov.available:
            msg = f"Provider {playlist_prov_map.provider_instance} is not available"
            raise ProviderUnavailableError(msg)
        cur_playlist_track_ids = set()
        cur_playlist_track_uris = set()
        for item in await self.get_all_playlist_tracks(playlist):
            cur_playlist_track_uris.add(item.item_id)
            cur_playlist_track_uris.add(item.uri)

        # work out the track id's that need to be added
        # filter out duplicates and items that not exist on the provider.
        ids_to_add: set[str] = set()
        for uri in uris:
            # skip if item already in the playlist
            if uri in cur_playlist_track_uris:
                self.logger.info(
                    "Not adding %s to playlist %s - it already exists", uri, playlist.name
                )
                continue

            # parse uri for further processing
            media_type, provider_instance_id_or_domain, item_id = await parse_uri(uri)

            # skip if item already in the playlist
            if item_id in cur_playlist_track_ids:
                self.logger.warning(
                    "Not adding %s to playlist %s - it already exists", uri, playlist.name
                )
                continue

            # skip non-track items
            # TODO: revisit this once we support audiobooks and podcasts ?
            if media_type != MediaType.TRACK:
                self.logger.warning(
                    "Not adding %s to playlist %s - not a track", uri, playlist.name
                )
                continue

            # special: the builtin provider can handle uri's from all providers (with uri as id)
            if provider_instance_id_or_domain != "library" and playlist_prov.domain == "builtin":
                # note: we try not to add library uri's to the builtin playlists
                # so we can survive db rebuilds
                ids_to_add.add(uri)
                self.logger.info(
                    "Adding %s to playlist %s",
                    uri,
                    playlist.name,
                )
                continue

            # if target playlist is an exact provider match, we can add it
            if provider_instance_id_or_domain != "library":
                item_prov = self.mass.get_provider(provider_instance_id_or_domain)
                if not item_prov or not item_prov.available:
                    self.logger.warning(
                        "Skip adding %s to playlist: Provider %s is not available",
                        uri,
                        provider_instance_id_or_domain,
                    )
                    continue
                if item_prov.lookup_key == playlist_prov.lookup_key:
                    ids_to_add.add(item_id)
                    continue

            # ensure we have a full library track
            db_track = await self.mass.music.tracks.get(
                item_id, provider_instance_id_or_domain, lazy=False, add_to_library=True
            )
            # a track can contain multiple versions on the same provider
            # simply sort by quality and just add the first available version
            for track_version in sorted(
                db_track.provider_mappings, key=lambda x: x.quality, reverse=True
            ):
                if not track_version.available:
                    continue
                if track_version.item_id in cur_playlist_track_ids:
                    break  # already existing in the playlist
                item_prov = self.mass.get_provider(track_version.provider_instance)
                if not item_prov:
                    continue
                track_version_uri = create_uri(
                    MediaType.TRACK,
                    item_prov.lookup_key,
                    track_version.item_id,
                )
                if track_version_uri in cur_playlist_track_uris:
                    self.logger.warning(
                        "Not adding %s to playlist %s - it already exists",
                        db_track.name,
                        playlist.name,
                    )
                    break  # already existing in the playlist
                if playlist_prov.domain == "builtin":
                    # the builtin provider can handle uri's from all providers (with uri as id)
                    ids_to_add.add(track_version_uri)
                    self.logger.info(
                        "Adding %s to playlist %s",
                        db_track.name,
                        playlist.name,
                    )
                    break
                if item_prov.lookup_key == playlist_prov.lookup_key:
                    ids_to_add.add(track_version.item_id)
                    self.logger.info(
                        "Adding %s to playlist %s",
                        db_track.name,
                        playlist.name,
                    )
                    break
            else:
                self.logger.warning(
                    "Can't add %s to playlist %s - it is not available provider %s",
                    db_track.name,
                    playlist.name,
                    playlist_prov.name,
                )

        if not ids_to_add:
            return

        # actually add the tracks to the playlist on the provider
        await playlist_prov.add_playlist_tracks(playlist_prov_map.item_id, list(ids_to_add))
        # invalidate cache so tracks get refreshed
        await self.get(
            playlist.item_id,
            playlist.provider,
            force_refresh=True,
        )

    async def add_playlist_track(self, db_playlist_id: str | int, track_uri: str) -> None:
        """Add (single) track to playlist."""
        await self.add_playlist_tracks(db_playlist_id, [track_uri])

    async def remove_playlist_tracks(
        self, db_playlist_id: str | int, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove multiple tracks from playlist."""
        db_id = int(db_playlist_id)  # ensure integer
        playlist = await self.get_library_item(db_id)
        if not playlist:
            msg = f"Playlist with id {db_id} not found"
            raise MediaNotFoundError(msg)
        if not playlist.is_editable:
            msg = f"Playlist {playlist.name} is not editable"
            raise InvalidDataError(msg)
        for prov_mapping in playlist.provider_mappings:
            provider = self.mass.get_provider(prov_mapping.provider_instance)
            if ProviderFeature.PLAYLIST_TRACKS_EDIT not in provider.supported_features:
                self.logger.warning(
                    "Provider %s does not support editing playlists",
                    prov_mapping.provider_domain,
                )
                continue
            await provider.remove_playlist_tracks(prov_mapping.item_id, positions_to_remove)
        # invalidate cache so tracks get refreshed
        await self.get(
            playlist.item_id,
            playlist.provider,
            force_refresh=True,
        )

    async def get_all_playlist_tracks(
        self, playlist: Playlist, prefer_library_items: bool = False
    ) -> list[PlaylistTrack]:
        """Return all tracks for given playlist (by unwrapping the paged listing)."""
        result: list[PlaylistTrack] = []
        offset = 0
        limit = 50
        self.logger.debug(
            "Fetching all tracks for playlist %s",
            playlist.name,
        )
        while True:
            paged_items = await self.tracks(
                item_id=playlist.item_id,
                provider_instance_id_or_domain=playlist.provider,
                offset=offset,
                limit=limit,
                prefer_library_items=prefer_library_items,
            )
            result += paged_items
            if len(paged_items) > limit:
                # this happens if the provider doesn't support paging
                # and it does simply return all items in one call
                break
            if len(paged_items) == 0:
                break
            if len(paged_items) < (limit - 20):
                # if get get less than 30 items, we assume this is the end
                # note that we account for the fact that the provider might
                # return less than the limit (e.g. 20 items) due to track unavailability
                break

            offset += limit
        return result

    async def _add_library_item(self, item: Playlist) -> int:
        """Add a new record to the database."""
        new_item = await self.mass.music.database.insert(
            self.db_table,
            {
                "name": item.name,
                "sort_name": item.sort_name,
                "owner": item.owner,
                "is_editable": item.is_editable,
                "favorite": item.favorite,
                "metadata": serialize_to_json(item.metadata),
                "external_ids": serialize_to_json(item.external_ids),
            },
        )
        db_id = new_item["item_id"]
        # update/set provider_mappings table
        await self._set_provider_mappings(db_id, item.provider_mappings)
        self.logger.debug("added %s to database (id: %s)", item.name, db_id)
        return db_id

    async def _update_library_item(
        self, item_id: int, update: Playlist, overwrite: bool = False
    ) -> None:
        """Update existing record in the database."""
        db_id = int(item_id)  # ensure integer
        cur_item = await self.get_library_item(db_id)
        metadata = update.metadata if overwrite else cur_item.metadata.update(update.metadata)
        cur_item.external_ids.update(update.external_ids)
        await self.mass.music.database.update(
            self.db_table,
            {"item_id": db_id},
            {
                # always prefer name/owner from updated item here
                "name": update.name if overwrite else cur_item.name,
                "sort_name": update.sort_name
                if overwrite
                else cur_item.sort_name or update.sort_name,
                "owner": update.owner or cur_item.owner,
                "is_editable": update.is_editable,
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
        self.logger.debug("updated %s in database: (id %s)", update.name, db_id)

    async def _get_provider_playlist_tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        cache_checksum: Any = None,
        offset: int = 0,
        limit: int = 50,
    ) -> list[PlaylistTrack]:
        """Return playlist tracks for the given provider playlist id."""
        assert provider_instance_id_or_domain != "library"
        provider: MusicProvider = self.mass.get_provider(provider_instance_id_or_domain)
        if not provider:
            return []
        # prefer cache items (if any)
        cache_key = f"{provider.lookup_key}.playlist.{item_id}.tracks.{offset}.{limit}"
        if (cache := await self.mass.cache.get(cache_key, checksum=cache_checksum)) is not None:
            return [PlaylistTrack.from_dict(x) for x in cache]
        # no items in cache - get listing from provider
        result: list[Track] = []
        for item in await provider.get_playlist_tracks(item_id, offset=offset, limit=limit):
            # double check if position set
            assert item.position is not None, "Playlist items require position to be set"
            result.append(item)
            # if this is a complete track object, pre-cache it as
            # that will save us an (expensive) lookup later
            if item.image and item.artist_str and item.album and provider.domain != "builtin":
                await self.mass.cache.set(
                    f"provider_item.track.{provider.lookup_key}.{item_id}", item.to_dict()
                )
        # store (serializable items) in cache
        self.mass.create_task(
            self.mass.cache.set(cache_key, [x.to_dict() for x in result], checksum=cache_checksum)
        )
        return result

    async def _get_provider_dynamic_tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        limit: int = 25,
    ):
        """Generate a dynamic list of tracks based on the playlist content."""
        assert provider_instance_id_or_domain != "library"
        provider = self.mass.get_provider(provider_instance_id_or_domain)
        if not provider or ProviderFeature.SIMILAR_TRACKS not in provider.supported_features:
            return []
        playlist = await self.get(item_id, provider_instance_id_or_domain)
        playlist_tracks = [
            x
            for x in await self.get_all_playlist_tracks(playlist)
            # filter out unavailable tracks
            if x.available
        ]
        limit = min(limit, len(playlist_tracks))
        # use set to prevent duplicates
        final_items: list[Track] = []
        # to account for playlists with mixed content we grab suggestions from a few
        # random playlist tracks to prevent getting too many tracks of one of the
        # source playlist's genres.
        sample_size = min(len(playlist_tracks), 5)
        while len(final_items) < limit:
            # grab 5 random tracks from the playlist
            base_tracks = random.sample(playlist_tracks, sample_size)
            # add the source/base playlist tracks to the final list...
            final_items.extend(base_tracks)
            # get 5 suggestions for one of the base tracks
            base_track = next(x for x in base_tracks if x.available)
            similar_tracks = await provider.get_similar_tracks(
                prov_track_id=base_track.item_id, limit=5
            )
            final_items.extend(x for x in similar_tracks if x.available)
        # Remove duplicate tracks
        radio_items = {track.sort_name: track for track in final_items}.values()
        # NOTE: In theory we can return a few more items than limit here
        # Shuffle the final items list
        return random.sample(list(radio_items), len(radio_items))

    async def _get_dynamic_tracks(
        self,
        media_item: Playlist,
        limit: int = 25,
    ) -> list[Track]:
        """Get dynamic list of tracks for given item, fallback/default implementation."""
        # check if we have any provider that supports dynamic tracks
        # TODO: query metadata provider(s) (such as lastfm?)
        # to get similar tracks (or tracks from similar artists)
        for prov in self.mass.get_providers(ProviderType.MUSIC):
            if ProviderFeature.SIMILAR_TRACKS in prov.supported_features:
                break
        else:
            msg = "No Music Provider found that supports requesting similar tracks."
            raise UnsupportedFeaturedException(msg)

        radio_items: list[Track] = []
        radio_item_titles: set[str] = set()
        playlist_tracks = await self.get_all_playlist_tracks(media_item, prefer_library_items=True)
        random.shuffle(playlist_tracks)
        for playlist_track in playlist_tracks:
            if not playlist_track.available:
                continue
            # include base item in the list
            radio_items.append(playlist_track)
            radio_item_titles.add(playlist_track.name)
            # now try to find similar tracks
            for item_prov_mapping in playlist_track.provider_mappings:
                if not (prov := self.mass.get_provider(item_prov_mapping.provider_instance)):
                    continue
                if ProviderFeature.SIMILAR_TRACKS not in prov.supported_features:
                    continue
                # fetch some similar tracks on this provider
                for similar_track in await prov.get_similar_tracks(
                    prov_track_id=item_prov_mapping.item_id, limit=5
                ):
                    if similar_track.name not in radio_item_titles:
                        radio_items.append(similar_track)
                        radio_item_titles.add(similar_track.name)
                continue
            if len(radio_items) >= limit:
                break
        # Shuffle the final items list
        random.shuffle(radio_items)
        return radio_items
