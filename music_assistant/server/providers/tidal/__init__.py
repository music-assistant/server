"""Tidal musicprovider support for MusicAssistant."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from tempfile import gettempdir
from typing import TYPE_CHECKING

from tidalapi import Session as TidalSession

from music_assistant.common.helpers.uri import create_uri
from music_assistant.common.helpers.util import create_sort_name
from music_assistant.common.models.config_entries import ConfigEntry
from music_assistant.common.models.enums import (
    AlbumType,
    ConfigEntryType,
    MediaType,
    ProviderFeature,
)
from music_assistant.common.models.errors import MediaNotFoundError
from music_assistant.common.models.media_items import (
    Album,
    Artist,
    ContentType,
    ImageType,
    ItemMapping,
    MediaItemImage,
    Playlist,
    ProviderMapping,
    SearchResults,
    StreamDetails,
    Track,
)
from music_assistant.constants import (
    CONF_ACCESS_TOKEN,
    CONF_EXPIRY_TIME,
    CONF_REFRESH_TOKEN,
    CONF_USERNAME,
)
from music_assistant.server.models.music_provider import MusicProvider

from .helpers import (
    add_remove_playlist_tracks,
    create_playlist,
    get_album,
    get_album_tracks,
    get_artist,
    get_artist_albums,
    get_artist_toptracks,
    get_library_albums,
    get_library_artists,
    get_library_playlists,
    get_library_tracks,
    get_playlist,
    get_playlist_tracks,
    get_similar_tracks,
    get_track,
    get_track_url,
    library_items_add_remove,
    search,
    tidal_session,
)

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType

CACHE_DIR = gettempdir()
TOKEN_TYPE = "Bearer"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = TidalProvider(mass, manifest, config)
    await prov.handle_setup()
    return prov


async def get_config_entries(
    mass: MusicAssistant, manifest: ProviderManifest  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    return (
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            label="User ID",
            required=False,
            hidden=True,
        ),
        ConfigEntry(
            key=CONF_ACCESS_TOKEN,
            type=ConfigEntryType.STRING,
            label="Access Token",
            required=False,
            hidden=True,
        ),
        ConfigEntry(
            key=CONF_REFRESH_TOKEN,
            type=ConfigEntryType.STRING,
            label="Refresh Token",
            required=False,
            hidden=True,
        ),
        ConfigEntry(
            key=CONF_EXPIRY_TIME,
            type=ConfigEntryType.STRING,
            label="Expiry Time",
            required=False,
            hidden=True,
        ),
    )


class TidalProvider(MusicProvider):
    """Implementation of a Tidal MusicProvider."""

    _token_type: str | None = None
    _access_token: str | None = None
    _refresh_token: str | None = None
    _expiry_time: datetime | None = None
    _tidal_user_id: str | None = None
    _tidal_session: TidalSession | None = None

    async def handle_setup(self) -> None:
        """Handle async initialization of the provider."""
        self._cache_dir = CACHE_DIR
        # try to get a token, raise if that fails
        self._cache_dir = os.path.join(CACHE_DIR, self.instance_id)
        # try login which will raise if it fails
        access_token = self.mass.config.get(
            f"providers/{self.instance_id}/values/{CONF_ACCESS_TOKEN}"
        )
        refresh_token = self.mass.config.get(
            f"providers/{self.instance_id}/values/{CONF_REFRESH_TOKEN}"
        )
        expiry_time = self.mass.config.get(
            f"providers/{self.instance_id}/values/{CONF_EXPIRY_TIME}"
        )
        if access_token is not None and access_token:
            self._access_token = access_token
        if refresh_token is not None and refresh_token:
            self._refresh_token = refresh_token
        if expiry_time is not None and expiry_time:
            self._expiry_time = datetime.fromisoformat(expiry_time)
        await self.login()

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return (
            ProviderFeature.LIBRARY_ARTISTS,
            ProviderFeature.LIBRARY_ALBUMS,
            ProviderFeature.LIBRARY_TRACKS,
            ProviderFeature.LIBRARY_PLAYLISTS,
            ProviderFeature.ARTIST_ALBUMS,
            ProviderFeature.ARTIST_TOPTRACKS,
            ProviderFeature.SEARCH,
            ProviderFeature.LIBRARY_ARTISTS_EDIT,
            ProviderFeature.LIBRARY_ALBUMS_EDIT,
            ProviderFeature.LIBRARY_TRACKS_EDIT,
            ProviderFeature.LIBRARY_PLAYLISTS_EDIT,
            ProviderFeature.PLAYLIST_CREATE,
            ProviderFeature.SIMILAR_TRACKS,
            ProviderFeature.BROWSE,
            ProviderFeature.PLAYLIST_TRACKS_EDIT,
        )

    async def search(
        self, search_query: str, media_types=list[MediaType] | None, limit: int = 5
    ) -> SearchResults:
        """Perform search on musicprovider.

        :param search_query: Search query.
        :param media_types: A list of media_types to include. All types if None.
        :param limit: Number of items to return in the search (per type).
        """
        search_query = search_query.replace("'", "")
        results = await search(self._tidal_session, search_query, media_types, limit)
        parsed_results = SearchResults()
        if results["artists"]:
            for artist in results["artists"]:
                parsed_results.artists.append(await self._parse_artist(artist))
        if results["albums"]:
            for album in results["albums"]:
                parsed_results.albums.append(await self._parse_album(album))
        if results["playlists"]:
            for playlist in results["playlists"]:
                parsed_results.playlists.append(await self._parse_playlist(playlist))
        if results["tracks"]:
            for track in results["tracks"]:
                parsed_results.tracks.append(await self._parse_track(track))
        return parsed_results

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve all library artists from Tidal."""
        artists_obj = await get_library_artists(self._tidal_session, self._tidal_user_id)
        for artist in artists_obj:
            yield await self._parse_artist(artist)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve all library albums from Tidal."""
        albums_obj = await get_library_albums(self._tidal_session, self._tidal_user_id)
        for album in albums_obj:
            yield await self._parse_album(album)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from Tidal."""
        tracks_obj = await get_library_tracks(self._tidal_session, self._tidal_user_id)
        for track in tracks_obj:
            if track.available:
                yield await self._parse_track(track)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve all library playlists from the provider."""
        playlists_obj = await get_library_playlists(self._tidal_session, self._tidal_user_id)
        for playlist in playlists_obj:
            yield await self._parse_playlist(playlist)

    async def get_album(self, prov_album_id) -> Album:
        """Get full album details by id."""
        if album_obj := await get_album(self._tidal_session, prov_album_id):
            return await self._parse_album(album_obj)
        raise MediaNotFoundError(f"Item {prov_album_id} not found")

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id."""
        result = []
        tracks = await get_album_tracks(self._tidal_session, prov_album_id)
        for index, track_obj in enumerate(tracks, 1):
            if track_obj.available:
                track = await self._parse_track(track_obj=track_obj)
                track.position = index
                result.append(track)
        return result

    async def get_artist(self, prov_artist_id) -> Artist:
        """Get full artist details by id."""
        if artist_obj := await get_artist(self._tidal_session, prov_artist_id):
            return await self._parse_artist(artist_obj)
        raise MediaNotFoundError(f"Item {artist_obj} not found")

    async def get_artist_albums(self, prov_artist_id) -> list[Album]:
        """Get a list of all albums for the given artist."""
        result = []
        albums = await get_artist_albums(self._tidal_session, prov_artist_id)
        for album_obj in albums:
            album = await self._parse_album(album_obj=album_obj)
            result.append(album)
        return result

    async def get_artist_toptracks(self, prov_artist_id) -> list[Track]:
        """Get a list of 10 most popular tracks for the given artist."""
        result = []
        tracks = await get_artist_toptracks(self._tidal_session, prov_artist_id)
        for index, track_obj in enumerate(tracks, 1):
            if track_obj.available:
                track = await self._parse_track(track_obj=track_obj)
                track.position = index
                result.append(track)
        return result

    async def get_track(self, prov_track_id) -> Track:
        """Get full track details by id."""
        if track_obj := await get_track(self._tidal_session, prov_track_id):
            return await self._parse_track(track_obj)
        raise MediaNotFoundError(f"Item {prov_track_id} not found")

    async def get_playlist(self, prov_playlist_id) -> Playlist:
        """Get full playlist details by id."""
        if playlist_obj := await get_playlist(self._tidal_session, prov_playlist_id):
            return await self._parse_playlist(playlist_obj)
        raise MediaNotFoundError(f"Item {prov_playlist_id} not found")

    async def get_playlist_tracks(self, prov_playlist_id) -> AsyncGenerator[Track, None]:
        """Get all playlist tracks for given playlist id."""
        tracks = await get_playlist_tracks(self._tidal_session, prov_playlist_id=prov_playlist_id)
        for index, track_obj in enumerate(tracks):
            if track_obj.available:
                track = await self._parse_track(track_obj=track_obj)
                if track:
                    track.position = index + 1
                    yield track

    async def get_similar_tracks(self, prov_track_id, limit=25) -> list[Track]:
        """Get similar tracks for given track id."""
        similar_tracks_obj = await get_similar_tracks(self._tidal_session, prov_track_id, limit)
        tracks = []
        for track_obj in similar_tracks_obj:
            if track_obj.available:
                track = await self._parse_track(track_obj=track_obj)
                if track:
                    tracks.append(track)
        return tracks

    async def library_add(self, prov_item_id, media_type: MediaType):
        """Add item to library."""
        return await library_items_add_remove(
            self._tidal_session, self._tidal_user_id, prov_item_id, media_type, add=True
        )

    async def library_remove(self, prov_item_id, media_type: MediaType):
        """Remove item from library."""
        return await library_items_add_remove(
            self._tidal_session, self._tidal_user_id, prov_item_id, media_type, add=False
        )

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]):
        """Add track(s) to playlist."""
        return await add_remove_playlist_tracks(
            self._tidal_session, prov_playlist_id, prov_track_ids, add=True
        )

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist."""
        prov_track_ids = []
        async for track in self.get_playlist_tracks(prov_playlist_id):
            if track.position in positions_to_remove:
                prov_track_ids.append(track.item_id)
            if len(prov_track_ids) == len(positions_to_remove):
                break
        return await add_remove_playlist_tracks(
            self._tidal_session, prov_playlist_id, prov_track_ids, add=False
        )

    async def create_playlist(self, name: str) -> Playlist:  # type: ignore[return]
        """Create a new playlist on provider with given name."""
        playlist_obj = await create_playlist(self._tidal_session, self._tidal_user_id, name)
        playlist = await self._parse_playlist(playlist_obj)
        db_playlist = await self.mass.music.playlists.add_db_item(playlist)
        return db_playlist

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        # make sure a valid track is requested.
        track = await get_track(self._tidal_session, item_id)
        url = await get_track_url(self._tidal_session, item_id)
        if not track:
            raise MediaNotFoundError(f"track {item_id} not found")
        # make sure that the token is still valid by just requesting it
        await self.login()
        return StreamDetails(
            item_id=track.id,
            provider=self.instance_id,
            content_type=ContentType.FLAC,
            duration=track.duration,
            direct=url,
        )

    async def _parse_artist(self, artist_obj) -> Artist:
        """Parse tidal artist object to generic layout."""
        artist_id = None
        artist_id = artist_obj.id
        artist = Artist(item_id=artist_id, provider=self.instance_id, name=artist_obj.name)
        artist.add_provider_mapping(
            ProviderMapping(
                item_id=str(artist_id),
                provider_domain=self.domain,
                provider_instance=self.instance_id,
                url=f"http://www.tidal.com/artist/{artist_id}",
            )
        )
        image_url = None
        if artist_obj.name != "Various Artists":
            try:
                image_url = artist_obj.image(750)
            except Exception:
                self.logger.info(f"Artist {artist_id} has no available picture")
        artist.metadata.images = [
            MediaItemImage(
                ImageType.THUMB,
                image_url,
            )
        ]
        #            break
        return artist

    async def _parse_album(self, album_obj: dict) -> Album:
        """Parse tidal album object to generic layout."""
        name = album_obj.name
        version = None
        if album_obj.version is not None:
            version = album_obj.version
        album_id = album_obj.id
        album = Album(item_id=album_id, provider=self.instance_id, name=name, version=version)
        for artist_obj in album_obj.artists:
            album.artists.append(await self._parse_artist(artist_obj))
        if album_obj.type == "SINGLE":
            album.album_type = AlbumType.SINGLE
        elif album_obj.type == "COMPILATION":
            album.album_type = AlbumType.COMPILATION
        elif album_obj.type == "ALBUM":
            album.album_type = AlbumType.ALBUM
        elif album_obj.type == "EP":
            album.album_type = AlbumType.EP
        image_url = None
        try:
            image_url = album_obj.image(1280)
        except Exception:
            self.logger.info(f"Album {album_id} has no available picture")
        album.metadata.images = [
            MediaItemImage(
                ImageType.THUMB,
                image_url,
            )
        ]
        album.upc = album_obj.universal_product_number
        album.year = int(album_obj.year)
        album.metadata.copyright = album_obj.copyright
        album.metadata.explicit = album_obj.explicit
        album.add_provider_mapping(
            ProviderMapping(
                item_id=album_id,
                provider_domain=self.domain,
                provider_instance=self.instance_id,
                content_type=ContentType.FLAC,
                bit_rate=1411,
                url=f"http://www.tidal.com/album/{album_id}",
            )
        )
        return album

    def _get_item_mapping(self, media_type: MediaType, key: str, name: str) -> ItemMapping:
        return ItemMapping(
            media_type,
            key,
            self.instance_id,
            name,
            create_uri(media_type, self.instance_id, key),
            create_sort_name(self.name),
        )

    async def _parse_track(self, track_obj) -> Track:
        """Parse tidal track object to generic layout."""
        version = None
        if track_obj.version is not None:
            version = track_obj.version
        track_id = str(track_obj.id)
        track = Track(
            item_id=track_id,
            provider=self.instance_id,
            name=track_obj.name,
            version=version,
            duration=track_obj.duration,
            disc_number=track_obj.volume_num,
            track_number=track_obj.track_num,
        )
        track.isrc.add(track_obj.isrc)
        track.album = self._get_item_mapping(
            MediaType.ALBUM, track_obj.album.id, track_obj.album.name
        )
        track.artists = []
        for track_artist in track_obj.artists:
            artist = await self._parse_artist(track_artist)
            track.artists.append(artist)

        track.metadata.explicit = track_obj.explicit
        track.metadata.popularity = track_obj.popularity
        track.metadata.copyright = track_obj.copyright
        available = track_obj.available
        track.add_provider_mapping(
            ProviderMapping(
                item_id=track_id,
                provider_domain=self.domain,
                provider_instance=self.instance_id,
                content_type=ContentType.FLAC,
                bit_rate=1411,
                url=f"http://www.tidal.com/tracks/{track_id}",
                available=available,
            )
        )
        return track

    async def _parse_playlist(self, playlist_obj) -> Playlist:
        """Parse tidal playlist object to generic layout."""
        playlist_id = playlist_obj.id
        creator_id = None
        if playlist_obj.creator:
            creator_id = playlist_obj.creator.id
        playlist = Playlist(
            item_id=playlist_id,
            provider=self.instance_id,
            name=playlist_obj.name,
            owner=playlist_obj.creator.name,
        )
        playlist.add_provider_mapping(
            ProviderMapping(
                item_id=playlist_id,
                provider_domain=self.domain,
                provider_instance=self.instance_id,
                url=f"http://www.tidal.com/playlists/{playlist_id}",
            )
        )
        is_editable = False
        if creator_id and creator_id == self._tidal_user_id:
            is_editable = True
        playlist.is_editable = is_editable
        image_url = None
        try:
            image_url = playlist_obj.image(1080)
        except Exception:
            self.logger.info(f"Playlist {playlist_id} has no available picture")
        playlist.metadata.images = [
            MediaItemImage(
                ImageType.THUMB,
                image_url,
            )
        ]
        playlist.metadata.checksum = str(playlist_obj.last_updated)
        return playlist

    async def login(self) -> TidalSession:
        """Log-in Tidal and return tokeninfo."""
        if (
            self._tidal_session
            and self._tidal_session.access_token == self._access_token
            and self._expiry_time > (datetime.now(UTC) + timedelta(minutes=30))
        ):
            return self._tidal_session
        session = await tidal_session(
            TOKEN_TYPE,
            self._access_token,
            self._refresh_token,
            self._expiry_time,
        )
        self.mass.config.set(
            f"providers/{self.instance_id}/values/{CONF_USERNAME}", str(session.user.id)
        )
        self.mass.config.set(
            f"providers/{self.instance_id}/values/{CONF_ACCESS_TOKEN}", session.access_token
        )
        self.mass.config.set(
            f"providers/{self.instance_id}/values/{CONF_REFRESH_TOKEN}", session.refresh_token
        )
        self.mass.config.set(
            f"providers/{self.instance_id}/values/{CONF_EXPIRY_TIME}",
            session.expiry_time.isoformat(),
        )
        self._access_token = session.access_token
        self._refresh_token = session.refresh_token
        self._expiry_time = session.expiry_time
        self._tidal_user_id = session.user.id
        self._tidal_session = session
        return self._tidal_session
