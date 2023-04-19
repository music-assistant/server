"""Tidal music provider support for MusicAssistant."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from tidalapi import Album as TidalAlbum
from tidalapi import Artist as TidalArtist
from tidalapi import Config as TidalConfig
from tidalapi import Playlist as TidalPlaylist
from tidalapi import Quality as TidalQuality
from tidalapi import Session as TidalSession
from tidalapi import Track as TidalTrack

from music_assistant.common.helpers.uri import create_uri
from music_assistant.common.helpers.util import create_sort_name
from music_assistant.common.models.config_entries import ConfigEntry, ConfigValueType
from music_assistant.common.models.enums import (
    AlbumType,
    ConfigEntryType,
    ImageType,
    MediaType,
    ProviderFeature,
)
from music_assistant.common.models.errors import LoginFailed, MediaNotFoundError
from music_assistant.common.models.media_items import (
    Album,
    Artist,
    ContentType,
    ItemMapping,
    MediaItemImage,
    MediaItemMetadata,
    Playlist,
    ProviderMapping,
    SearchResults,
    StreamDetails,
    Track,
)
from music_assistant.server.helpers.auth import AuthenticationHelper
from music_assistant.server.models.music_provider import MusicProvider

from .helpers import (
    DEFAULT_LIMIT,
    add_remove_playlist_tracks,
    create_playlist,
    get_album,
    get_album_tracks,
    get_artist,
    get_artist_albums,
    get_artist_toptracks,
    get_library_albums,
    get_library_playlists,
    get_library_tracks,
    get_playlist,
    get_playlist_tracks,
    get_similar_tracks,
    get_track,
    get_track_url,
    library_items_add_remove,
    search,
)

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType

TOKEN_TYPE = "Bearer"
CONF_ACTION_AUTH = "auth"
CONF_AUTH_TOKEN = "auth_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_USER_ID = "user_id"
CONF_EXPIRY_TIME = "expiry_time"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = TidalProvider(mass, manifest, config)
    await prov.handle_setup()
    return prov


async def tidal_code_login(auth_helper: AuthenticationHelper) -> TidalSession:
    """Async wrapper around the tidalapi Session function."""

    def inner() -> TidalSession:
        config = TidalConfig(quality=TidalQuality.lossless, item_limit=10000, alac=False)
        session = TidalSession(config=config)
        login, future = session.login_oauth()
        auth_helper.send_url(f"https://{login.verification_uri_complete}")
        future.result()
        return session

    return await asyncio.to_thread(inner)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # config flow auth action/step (authenticate button clicked)
    if action == CONF_ACTION_AUTH:
        async with AuthenticationHelper(mass, values["session_id"]) as auth_helper:
            tidal_session = await tidal_code_login(auth_helper)
            if not tidal_session.check_login():
                raise LoginFailed("Authentication to Tidal failed")
            # set the retrieved token on the values object to pass along
            values[CONF_AUTH_TOKEN] = tidal_session.access_token
            values[CONF_REFRESH_TOKEN] = tidal_session.refresh_token
            values[CONF_EXPIRY_TIME] = tidal_session.expiry_time.isoformat()
            values[CONF_USER_ID] = str(tidal_session.user.id)

    # return the collected config entries
    return (
        ConfigEntry(
            key=CONF_AUTH_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Authentication token for Tidal",
            description="You need to link Music Assistant to your Tidal account.",
            action=CONF_ACTION_AUTH,
            action_label="Authenticate on Tidal.com",
            value=values.get(CONF_AUTH_TOKEN) if values else None,
        ),
        ConfigEntry(
            key=CONF_REFRESH_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Refresh token for Tidal",
            description="You need to link Music Assistant to your Tidal account.",
            hidden=True,
            value=values.get(CONF_REFRESH_TOKEN) if values else None,
        ),
        ConfigEntry(
            key=CONF_EXPIRY_TIME,
            type=ConfigEntryType.STRING,
            label="Expiry time of auth token for Tidal",
            hidden=True,
            value=values.get(CONF_EXPIRY_TIME) if values else None,
        ),
        ConfigEntry(
            key=CONF_USER_ID,
            type=ConfigEntryType.STRING,
            label="Your Tidal User ID",
            description="This is your unique Tidal user ID.",
            hidden=True,
            value=values.get(CONF_USER_ID) if values else None,
        ),
    )


async def iter_items(func: Awaitable | Callable, *args, **kwargs) -> AsyncGenerator[Any, None]:
    """Yield all items from a larger listing."""
    offset = 0
    while True:
        if asyncio.iscoroutinefunction(func):
            func = await func(*args, **kwargs, limit=DEFAULT_LIMIT, offset=offset)
        else:
            chunk = await asyncio.to_thread(
                func, *args, **kwargs, limit=DEFAULT_LIMIT, offset=offset
            )
        offset += len(chunk)
        for item in chunk:
            yield item
        if len(chunk) < DEFAULT_LIMIT:
            break


class TidalProvider(MusicProvider):
    """Implementation of a Tidal MusicProvider."""

    _tidal_session: TidalSession | None = None
    _tidal_user_id: str | None = None

    async def handle_setup(self) -> None:
        """Handle async initialization of the provider."""
        self._tidal_user_id = self.config.get_value(CONF_USER_ID)
        self._tidal_session = await self._get_tidal_session()

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
        results = await search(self, search_query, media_types, limit)
        parsed_results = SearchResults()
        if results["artists"]:
            for artist in results["artists"]:
                parsed_results.artists.append(self._parse_artist(artist_obj=artist))
        if results["albums"]:
            for album in results["albums"]:
                parsed_results.albums.append(self._parse_album(album_obj=album))
        if results["playlists"]:
            for playlist in results["playlists"]:
                parsed_results.playlists.append(self._parse_playlist(playlist_obj=playlist))
        if results["tracks"]:
            for track in results["tracks"]:
                parsed_results.tracks.append(self._parse_track(track_obj=track))
        return parsed_results

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve all library artists from Tidal."""
        offset = 0
        tidal_session = await self._get_tidal_session()
        artist: TidalArtist  # satisfy the type checker
        while True:
            chunk = await get_library_albums(
                tidal_session, self._tidal_user_id, limit=DEFAULT_LIMIT, offset=offset
            )
            for artist in chunk:
                yield self._parse_artist(artist_obj=artist)
            if len(chunk) < DEFAULT_LIMIT:
                break
        # async for artist in iter_items(get_library_artists, tidal_session, self._tidal_user_id):
        #    yield self._parse_artist(artist_obj=artist)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve all library albums from Tidal."""
        offset = 0
        tidal_session = await self._get_tidal_session()
        while True:
            chunk = await get_library_albums(
                tidal_session, self._tidal_user_id, limit=DEFAULT_LIMIT, offset=offset
            )
            for album in chunk:
                yield self._parse_album(album_obj=album)
            if len(chunk) < DEFAULT_LIMIT:
                break

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from Tidal."""
        offset = 0
        tidal_session = await self._get_tidal_session()
        while True:
            chunk = await get_library_tracks(
                tidal_session, self._tidal_user_id, limit=DEFAULT_LIMIT, offset=offset
            )
            for track in chunk:
                if track.available:
                    yield self._parse_track(track_obj=track)
            if len(chunk) < DEFAULT_LIMIT:
                break

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve all library playlists from the provider."""
        offset = 0
        tidal_session = await self._get_tidal_session()
        while True:
            chunk = await get_library_playlists(
                tidal_session, self._tidal_user_id, limit=DEFAULT_LIMIT, offset=offset
            )
            for playlist in chunk:
                yield self._parse_playlist(playlist_obj=playlist)
            if len(chunk) < DEFAULT_LIMIT:
                break

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id."""
        result = []
        tracks = await get_album_tracks(self, prov_album_id)
        for index, track_obj in enumerate(tracks, 1):
            if track_obj.available:
                track = self._parse_track(track_obj=track_obj)
                track.position = index
                result.append(track)
        return result

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get a list of all albums for the given artist."""
        result = []
        albums = await get_artist_albums(self, prov_artist_id)
        for album_obj in albums:
            album = self._parse_album(album_obj=album_obj)
            result.append(album)
        return result

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get a list of 10 most popular tracks for the given artist."""
        result = []
        tracks = await get_artist_toptracks(self, prov_artist_id)
        for index, track_obj in enumerate(tracks, 1):
            if track_obj.available:
                track = self._parse_track(track_obj=track_obj)
                track.position = index
                result.append(track)
        return result

    async def get_playlist_tracks(self, prov_playlist_id: str) -> AsyncGenerator[Track, None]:
        """Get all playlist tracks for given playlist id."""
        offset = 0
        total_playlist_tracks = 1
        tidal_session = await self._get_tidal_session()
        while True:
            chunk = await get_playlist_tracks(
                tidal_session,
                self._tidal_user_id,
                limit=DEFAULT_LIMIT,
                offset=offset,
                prov_playlist_id=prov_playlist_id,
            )
            for track_obj in chunk:
                if track_obj.available:
                    track = self._parse_track(track_obj=track_obj)
                    track.position = total_playlist_tracks
                    total_playlist_tracks += 1
                    yield track

    async def get_similar_tracks(self, prov_track_id: str, limit=25) -> list[Track]:
        """Get similar tracks for given track id."""
        similar_tracks_obj = await get_similar_tracks(self, prov_track_id, limit)
        tracks = []
        for track_obj in similar_tracks_obj:
            if track_obj.available:
                track = self._parse_track(track_obj=track_obj)
                tracks.append(track)
        return tracks

    async def library_add(self, prov_item_id: str, media_type: MediaType):
        """Add item to library."""
        return await library_items_add_remove(
            self,
            self._tidal_user_id,
            prov_item_id,
            media_type,
            add=True,
        )

    async def library_remove(self, prov_item_id: str, media_type: MediaType):
        """Remove item from library."""
        return await library_items_add_remove(
            self,
            self._tidal_user_id,
            prov_item_id,
            media_type,
            add=False,
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
        return await add_remove_playlist_tracks(self, prov_playlist_id, prov_track_ids, add=False)

    async def create_playlist(self, name: str) -> Playlist:
        """Create a new playlist on provider with given name."""
        playlist_obj = await create_playlist(self, self._tidal_user_id, name)
        playlist = self._parse_playlist(playlist_obj=playlist_obj)
        return await self.mass.music.playlists.add_db_item(playlist)

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        # make sure a valid track is requested.
        track = await get_track(self, item_id)
        url = await get_track_url(self, item_id)
        if not track:
            raise MediaNotFoundError(f"track {item_id} not found")
        return StreamDetails(
            item_id=track.id,
            provider=self.instance_id,
            content_type=ContentType.FLAC,
            sample_rate=44100,
            bit_depth=16,
            duration=track.duration,
            direct=url,
        )

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get artist details for given artist id."""
        try:
            artist = self._parse_artist(
                artist_obj=await get_artist(self, prov_artist_id),
            )
        except MediaNotFoundError as err:
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found") from err
        return artist

    async def get_album(self, prov_album_id: str) -> Album:
        """Get album details for given album id."""
        try:
            album = self._parse_album(
                album_obj=await get_album(self, prov_album_id),
            )
        except MediaNotFoundError as err:
            raise MediaNotFoundError(f"Album {prov_album_id} not found") from err
        return album

    async def get_track(self, prov_track_id: str) -> Track:
        """Get track details for given track id."""
        try:
            track = self._parse_track(
                track_obj=await get_track(self, prov_track_id),
            )
        except MediaNotFoundError as err:
            raise MediaNotFoundError(f"Track {prov_track_id} not found") from err
        return track

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get playlist details for given playlist id."""
        try:
            playlist = self._parse_playlist(
                playlist_obj=await get_playlist(self, prov_playlist_id),
            )
        except MediaNotFoundError as err:
            raise MediaNotFoundError(f"Playlist {prov_playlist_id} not found") from err
        return playlist

    def get_item_mapping(self, media_type: MediaType, key: str, name: str) -> ItemMapping:
        """Create a generic item mapping."""
        return ItemMapping(
            media_type,
            key,
            self.instance_id,
            name,
            create_uri(media_type, self.instance_id, key),
            create_sort_name(self.name),
        )

    async def _get_tidal_session(self) -> TidalSession:
        """Ensure the current token is valid and return a tidal session."""
        if (
            self._tidal_session
            and self._tidal_session.access_token
            and datetime.fromisoformat(self.config.get_value(CONF_EXPIRY_TIME))
            > (datetime.now() + timedelta(days=1))
        ):
            return self._tidal_session
        self._tidal_session = await self._load_tidal_session(
            token_type="Bearer",
            access_token=self.config.get_value(CONF_AUTH_TOKEN),
            refresh_token=self.config.get_value(CONF_REFRESH_TOKEN),
            expiry_time=datetime.fromisoformat(self.config.get_value(CONF_EXPIRY_TIME)),
        )
        await self.mass.config.set_provider_config_value(
            self.config.instance_id,
            CONF_AUTH_TOKEN,
            self._tidal_session.access_token,
        )
        await self.mass.config.set_provider_config_value(
            self.config.instance_id,
            CONF_REFRESH_TOKEN,
            self._tidal_session.refresh_token,
        )
        await self.mass.config.set_provider_config_value(
            self.config.instance_id,
            CONF_EXPIRY_TIME,
            self._tidal_session.expiry_time.isoformat(),
        )
        return self._tidal_session

    async def _load_tidal_session(
        self, token_type, access_token, refresh_token=None, expiry_time=None
    ) -> TidalSession:
        """Load the tidalapi Session."""

        def inner() -> TidalSession:
            config = TidalConfig(quality=TidalQuality.lossless, item_limit=10000, alac=False)
            session = TidalSession(config=config)
            session.load_oauth_session(token_type, access_token, refresh_token, expiry_time)
            return session

        return await asyncio.to_thread(inner)

    # Parsers

    def _parse_artist(self, artist_obj: TidalArtist) -> Artist:
        """Parse tidal artist object to generic layout."""
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
        artist.metadata = self._parse_artist_metadata(artist_obj)
        return artist

    def _parse_artist_metadata(self, artist_obj: TidalArtist) -> MediaItemMetadata:
        """Parse tidal artist object to MA metadata."""
        metadata = MediaItemMetadata()
        image_url = None
        if artist_obj.name != "Various Artists":
            try:
                image_url = artist_obj.image(750)
            except Exception:
                self.logger.info(f"Artist {artist_obj.id} has no available picture")
        metadata.images = [
            MediaItemImage(
                ImageType.THUMB,
                image_url,
            )
        ]
        return metadata

    def _parse_album(self, album_obj: TidalAlbum) -> Album:
        """Parse tidal album object to generic layout."""
        name = album_obj.name
        version = album_obj.version if album_obj.version is not None else None
        album_id = album_obj.id
        album = Album(item_id=album_id, provider=self.instance_id, name=name, version=version)
        for artist_obj in album_obj.artists:
            album.artists.append(self._parse_artist(artist_obj=artist_obj))
        if album_obj.type == "ALBUM":
            album.album_type = AlbumType.ALBUM
        elif album_obj.type == "COMPILATION":
            album.album_type = AlbumType.COMPILATION
        elif album_obj.type == "EP":
            album.album_type = AlbumType.EP
        elif album_obj.type == "SINGLE":
            album.album_type = AlbumType.SINGLE

        album.upc = album_obj.universal_product_number
        album.year = int(album_obj.year)
        album.add_provider_mapping(
            ProviderMapping(
                item_id=album_id,
                provider_domain=self.domain,
                provider_instance=self.instance_id,
                content_type=ContentType.FLAC,
                url=f"http://www.tidal.com/album/{album_id}",
            )
        )
        album.metadata = self._parse_album_metadata(album_obj)
        return album

    def _parse_album_metadata(self, album_obj: TidalAlbum) -> MediaItemMetadata:
        """Parse tidal album object to MA metadata."""
        metadata = MediaItemMetadata()
        image_url = None
        try:
            image_url = album_obj.image(1280)
        except Exception:
            self.logger.info(f"Album {album_obj.id} has no available picture")
        metadata.images = [
            MediaItemImage(
                ImageType.THUMB,
                image_url,
            )
        ]
        metadata.copyright = album_obj.copyright
        metadata.explicit = album_obj.explicit
        metadata.popularity = album_obj.popularity
        return metadata

    def _parse_track(self, track_obj: TidalTrack) -> Track:
        """Parse tidal track object to generic layout."""
        version = track_obj.version if track_obj.version is not None else None
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
        track.album = self.get_item_mapping(
            media_type=MediaType.ALBUM,
            key=track_obj.album.id,
            name=track_obj.album.name,
        )
        track.artists = []
        for track_artist in track_obj.artists:
            artist = self._parse_artist(artist_obj=track_artist)
            track.artists.append(artist)
        available = track_obj.available
        track.add_provider_mapping(
            ProviderMapping(
                item_id=track_id,
                provider_domain=self.domain,
                provider_instance=self.instance_id,
                content_type=ContentType.FLAC,
                sample_rate=44100,
                bit_depth=16,
                url=f"http://www.tidal.com/tracks/{track_id}",
                available=available,
            )
        )
        track.metadata = self._parse_track_metadata(track_obj)
        return track

    def _parse_track_metadata(self, track_obj: TidalTrack) -> MediaItemMetadata:
        """Parse tidal track object to MA metadata."""
        metadata = MediaItemMetadata()
        try:
            metadata.lyrics = track_obj.lyrics().text
        except Exception:
            self.logger.info(f"Track {track_obj.id} has no available lyrics")
        metadata.explicit = track_obj.explicit
        metadata.popularity = track_obj.popularity
        metadata.copyright = track_obj.copyright
        return metadata

    def _parse_playlist(self, playlist_obj: TidalPlaylist) -> Playlist:
        """Parse tidal playlist object to generic layout."""
        playlist_id = playlist_obj.id
        creator_id = playlist_obj.creator.id if playlist_obj.creator else None
        creator_name = playlist_obj.creator.name if playlist_obj.creator else "Tidal"
        playlist = Playlist(
            item_id=playlist_id,
            provider=self.instance_id,
            name=playlist_obj.name,
            owner=creator_name,
        )
        playlist.add_provider_mapping(
            ProviderMapping(
                item_id=playlist_id,
                provider_domain=self.domain,
                provider_instance=self.instance_id,
                url=f"http://www.tidal.com/playlists/{playlist_id}",
            )
        )
        is_editable = bool(creator_id and creator_id == self._tidal_user_id)
        playlist.is_editable = is_editable
        playlist.metadata = self._parse_playlist_metadata(playlist_obj)
        return playlist

    def _parse_playlist_metadata(self, playlist_obj: TidalPlaylist) -> MediaItemMetadata:
        """Parse tidal playlist object to MA metadata."""
        metadata = MediaItemMetadata()
        image_url = None
        try:
            image_url = playlist_obj.image(1080)
        except Exception:
            self.logger.info(f"Playlist {playlist_obj.id} has no available picture")
        metadata.images = [
            MediaItemImage(
                ImageType.THUMB,
                image_url,
            )
        ]
        metadata.checksum = str(playlist_obj.last_updated)
        metadata.popularity = playlist_obj.popularity
        return metadata
