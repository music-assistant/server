"""Tidal music provider support for MusicAssistant."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from asyncio_throttle import Throttler
from tidalapi import Album as TidalAlbum
from tidalapi import Artist as TidalArtist
from tidalapi import Config as TidalConfig
from tidalapi import Playlist as TidalPlaylist
from tidalapi import Quality as TidalQuality
from tidalapi import Session as TidalSession
from tidalapi import Track as TidalTrack

from music_assistant.common.models.config_entries import (
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
)
from music_assistant.common.models.enums import (
    AlbumType,
    ConfigEntryType,
    ExternalID,
    ImageType,
    MediaType,
    ProviderFeature,
)
from music_assistant.common.models.errors import LoginFailed, MediaNotFoundError
from music_assistant.common.models.media_items import (
    Album,
    AlbumTrack,
    Artist,
    AudioFormat,
    ContentType,
    ItemMapping,
    MediaItemImage,
    Playlist,
    PlaylistTrack,
    ProviderMapping,
    SearchResults,
    StreamDetails,
    Track,
)
from music_assistant.server.helpers.auth import AuthenticationHelper
from music_assistant.server.helpers.tags import AudioTags, parse_tags
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
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from tidalapi.media import Lyrics as TidalLyrics

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
CONF_QUALITY = "quality"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = TidalProvider(mass, manifest, config)
    await prov.handle_async_init()
    return prov


async def tidal_code_login(auth_helper: AuthenticationHelper, quality: str) -> TidalSession:
    """Async wrapper around the tidalapi Session function."""

    def inner() -> TidalSession:
        config = TidalConfig(quality=TidalQuality[quality], item_limit=10000, alac=False)
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
        async with AuthenticationHelper(mass, cast(str, values["session_id"])) as auth_helper:
            quality: str | int | float | list[str] | list[int] | None = (
                values.get(CONF_QUALITY) if values else None
            )
            tidal_session = await tidal_code_login(auth_helper, cast(str, quality))
            if not tidal_session.check_login():
                msg = "Authentication to Tidal failed"
                raise LoginFailed(msg)
            # set the retrieved token on the values object to pass along
            values[CONF_AUTH_TOKEN] = tidal_session.access_token
            values[CONF_REFRESH_TOKEN] = tidal_session.refresh_token
            values[CONF_EXPIRY_TIME] = tidal_session.expiry_time.isoformat()
            values[CONF_USER_ID] = str(tidal_session.user.id)

    # config flow auth action/step to pick the library to use
    # because this call is very slow, we only show/calculate the dropdown if we do
    # not yet have this info or we/user invalidated it.

    # return the collected config entries
    return (
        ConfigEntry(
            key=CONF_QUALITY,
            type=ConfigEntryType.STRING,
            label="Quality",
            required=True,
            description="The Tidal Quality you wish to use",
            options=(
                ConfigValueOption(
                    title=TidalQuality.low_96k.value, value=TidalQuality.low_96k.name
                ),
                ConfigValueOption(
                    title=TidalQuality.low_320k.value, value=TidalQuality.low_320k.name
                ),
                ConfigValueOption(
                    title=TidalQuality.high_lossless.value,
                    value=TidalQuality.high_lossless.name,
                ),
                ConfigValueOption(title=TidalQuality.hi_res.value, value=TidalQuality.hi_res.name),
            ),
            default_value=TidalQuality.high_lossless.name,
            value=values.get(CONF_QUALITY) if values else None,
        ),
        ConfigEntry(
            key=CONF_AUTH_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Authentication token for Tidal",
            description="You need to link Music Assistant to your Tidal account.",
            action=CONF_ACTION_AUTH,
            depends_on=CONF_QUALITY,
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


class TidalProvider(MusicProvider):
    """Implementation of a Tidal MusicProvider."""

    _tidal_session: TidalSession | None = None
    _tidal_user_id: str | None = None

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._tidal_user_id: str = self.config.get_value(CONF_USER_ID)
        self._tidal_session = await self._get_tidal_session()
        self._throttler = Throttler(rate_limit=1, period=0.1)

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
        self,
        search_query: str,
        media_types: list[MediaType] | None = None,
        limit: int = 5,
    ) -> SearchResults:
        """Perform search on musicprovider.

        :param search_query: Search query.
        :param media_types: A list of media_types to include. All types if None.
        :param limit: Number of items to return in the search (per type).
        """
        tidal_session = await self._get_tidal_session()
        search_query = search_query.replace("'", "")
        results = await search(tidal_session, search_query, media_types, limit)
        parsed_results = SearchResults()
        if results["artists"]:
            for artist in results["artists"]:
                parsed_results.artists.append(await self._parse_artist(artist_obj=artist))
        if results["albums"]:
            for album in results["albums"]:
                parsed_results.albums.append(await self._parse_album(album_obj=album))
        if results["playlists"]:
            for playlist in results["playlists"]:
                parsed_results.playlists.append(await self._parse_playlist(playlist_obj=playlist))
        if results["tracks"]:
            for track in results["tracks"]:
                parsed_results.tracks.append(await self._parse_track(track_obj=track))
        return parsed_results

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve all library artists from Tidal."""
        tidal_session = await self._get_tidal_session()
        artist: TidalArtist  # satisfy the type checker
        async for artist in self._iter_items(
            get_library_artists, tidal_session, self._tidal_user_id, limit=DEFAULT_LIMIT
        ):
            yield await self._parse_artist(artist_obj=artist)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve all library albums from Tidal."""
        tidal_session = await self._get_tidal_session()
        album: TidalAlbum  # satisfy the type checker
        async for album in self._iter_items(
            get_library_albums, tidal_session, self._tidal_user_id, limit=DEFAULT_LIMIT
        ):
            yield await self._parse_album(album_obj=album)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from Tidal."""
        tidal_session = await self._get_tidal_session()
        track: TidalTrack  # satisfy the type checker
        async for track in self._iter_items(
            get_library_tracks, tidal_session, self._tidal_user_id, limit=DEFAULT_LIMIT
        ):
            yield await self._parse_track(track_obj=track)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve all library playlists from the provider."""
        tidal_session = await self._get_tidal_session()
        playlist: TidalPlaylist  # satisfy the type checker
        async for playlist in self._iter_items(
            get_library_playlists, tidal_session, self._tidal_user_id
        ):
            yield await self._parse_playlist(playlist_obj=playlist)

    async def get_album_tracks(self, prov_album_id: str) -> list[AlbumTrack]:
        """Get album tracks for given album id."""
        tidal_session = await self._get_tidal_session()
        async with self._throttler:
            return cast(
                list[AlbumTrack],
                [
                    await self._parse_track(
                        track_obj=track_obj,
                        extra_init_kwargs={
                            "disc_number": track_obj.volume_num,
                            "track_number": track_obj.track_num,
                        },
                    )
                    for track_obj in await get_album_tracks(tidal_session, prov_album_id)
                ],
            )

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get a list of all albums for the given artist."""
        tidal_session = await self._get_tidal_session()
        async with self._throttler:
            return [
                await self._parse_album(album_obj=album)
                for album in await get_artist_albums(tidal_session, prov_artist_id)
            ]

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get a list of 10 most popular tracks for the given artist."""
        tidal_session = await self._get_tidal_session()
        async with self._throttler:
            return [
                await self._parse_track(track_obj=track)
                for track in await get_artist_toptracks(tidal_session, prov_artist_id)
            ]

    async def get_playlist_tracks(
        self, prov_playlist_id: str
    ) -> AsyncGenerator[PlaylistTrack, None]:
        """Get all playlist tracks for given playlist id."""
        tidal_session = await self._get_tidal_session()
        total_playlist_tracks = 0
        track: TidalTrack  # satisfy the type checker
        async for track_obj in self._iter_items(
            get_playlist_tracks, tidal_session, prov_playlist_id, limit=DEFAULT_LIMIT
        ):
            total_playlist_tracks += 1
            track = await self._parse_track(
                track_obj=track_obj,
                extra_init_kwargs={"position": total_playlist_tracks},
            )
            yield track

    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Get similar tracks for given track id."""
        tidal_session = await self._get_tidal_session()
        async with self._throttler:
            return [
                await self._parse_track(track_obj=track)
                for track in await get_similar_tracks(tidal_session, prov_track_id, limit)
            ]

    async def library_add(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Add item to library."""
        tidal_session = await self._get_tidal_session()
        return await library_items_add_remove(
            tidal_session,
            str(self._tidal_user_id),
            prov_item_id,
            media_type,
            add=True,
        )

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove item from library."""
        tidal_session = await self._get_tidal_session()
        return await library_items_add_remove(
            tidal_session,
            str(self._tidal_user_id),
            prov_item_id,
            media_type,
            add=False,
        )

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add track(s) to playlist."""
        tidal_session = await self._get_tidal_session()
        return await add_remove_playlist_tracks(
            tidal_session, prov_playlist_id, prov_track_ids, add=True
        )

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist."""
        prov_track_ids = []
        tidal_session = await self._get_tidal_session()
        async for track in self.get_playlist_tracks(prov_playlist_id):
            if track.position in positions_to_remove:
                prov_track_ids.append(track.item_id)
            if len(prov_track_ids) == len(positions_to_remove):
                break
        return await add_remove_playlist_tracks(
            tidal_session, prov_playlist_id, prov_track_ids, add=False
        )

    async def create_playlist(self, name: str) -> Playlist:
        """Create a new playlist on provider with given name."""
        tidal_session = await self._get_tidal_session()
        playlist_obj = await create_playlist(
            session=tidal_session,
            user_id=str(self._tidal_user_id),
            title=name,
            description="",
        )
        return await self._parse_playlist(playlist_obj=playlist_obj)

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        # make sure a valid track is requested.
        tidal_session = await self._get_tidal_session()
        track = await get_track(tidal_session, item_id)
        url = await get_track_url(tidal_session, item_id)
        media_info = await self._get_media_info(item_id=item_id, url=url)
        if not track:
            msg = f"track {item_id} not found"
            raise MediaNotFoundError(msg)
        return StreamDetails(
            item_id=track.id,
            provider=self.instance_id,
            audio_format=AudioFormat(
                content_type=ContentType.try_parse(media_info.format),
                sample_rate=media_info.sample_rate,
                bit_depth=media_info.bits_per_sample,
                channels=media_info.channels,
            ),
            duration=track.duration,
            direct=url,
        )

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get artist details for given artist id."""
        tidal_session = await self._get_tidal_session()
        async with self._throttler:
            return await self._parse_artist(
                artist_obj=await get_artist(tidal_session, prov_artist_id),
                full_details=True,
            )

    async def get_album(self, prov_album_id: str) -> Album:
        """Get album details for given album id."""
        tidal_session = await self._get_tidal_session()
        async with self._throttler:
            return await self._parse_album(
                album_obj=await get_album(tidal_session, prov_album_id),
                full_details=True,
            )

    async def get_track(self, prov_track_id: str) -> Track:
        """Get track details for given track id."""
        tidal_session = await self._get_tidal_session()
        async with self._throttler:
            return await self._parse_track(
                track_obj=await get_track(tidal_session, prov_track_id),
                full_details=True,
            )

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get playlist details for given playlist id."""
        tidal_session = await self._get_tidal_session()
        async with self._throttler:
            return await self._parse_playlist(
                playlist_obj=await get_playlist(tidal_session, prov_playlist_id),
                full_details=True,
            )

    def get_item_mapping(self, media_type: MediaType, key: str, name: str) -> ItemMapping:
        """Create a generic item mapping."""
        return ItemMapping(
            media_type=media_type,
            item_id=key,
            provider=self.instance_id,
            name=name,
        )

    async def _get_tidal_session(self) -> TidalSession:
        """Ensure the current token is valid and return a tidal session."""
        if (
            self._tidal_session
            and self._tidal_session.access_token
            and datetime.fromisoformat(str(self.config.get_value(CONF_EXPIRY_TIME)))
            > (datetime.now() + timedelta(days=1))
        ):
            return self._tidal_session
        self._tidal_session = await self._load_tidal_session(
            token_type="Bearer",
            quality=self.config.get_value(CONF_QUALITY),
            access_token=str(self.config.get_value(CONF_AUTH_TOKEN)),
            refresh_token=str(self.config.get_value(CONF_REFRESH_TOKEN)),
            expiry_time=datetime.fromisoformat(str(self.config.get_value(CONF_EXPIRY_TIME))),
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
        self,
        token_type: str,
        quality: TidalQuality,
        access_token: str,
        refresh_token: str,
        expiry_time: datetime | None = None,
    ) -> TidalSession:
        """Load the tidalapi Session."""

        def inner() -> TidalSession:
            config = TidalConfig(quality=TidalQuality[quality], item_limit=10000, alac=False)
            session = TidalSession(config=config)
            session.load_oauth_session(token_type, access_token, refresh_token, expiry_time)
            return session

        return await asyncio.to_thread(inner)

    # Parsers

    async def _parse_artist(self, artist_obj: TidalArtist, full_details: bool = False) -> Artist:
        """Parse tidal artist object to generic layout."""
        artist_id = artist_obj.id
        artist = Artist(
            item_id=str(artist_id),
            provider=self.instance_id,
            name=artist_obj.name,
            provider_mappings={
                ProviderMapping(
                    item_id=str(artist_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=f"http://www.tidal.com/artist/{artist_id}",
                )
            },
        )
        # metadata
        if full_details and artist_obj.name != "Various Artists":
            try:
                image_url = await self._get_image_url(artist_obj, 750)
                artist.metadata.images = [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=image_url,
                    )
                ]
            except Exception:
                self.logger.info(f"Artist {artist_obj.id} has no available picture")

        return artist

    async def _parse_album(self, album_obj: TidalAlbum, full_details: bool = False) -> Album:
        """Parse tidal album object to generic layout."""
        name = album_obj.name
        version = album_obj.version or ""
        album_id = album_obj.id
        album = Album(
            item_id=str(album_id),
            provider=self.instance_id,
            name=name,
            version=version,
            provider_mappings={
                ProviderMapping(
                    item_id=str(album_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=AudioFormat(
                        content_type=ContentType.FLAC,
                    ),
                    url=f"http://www.tidal.com/album/{album_id}",
                    available=album_obj.available,
                )
            },
        )
        for artist_obj in album_obj.artists:
            album.artists.append(await self._parse_artist(artist_obj=artist_obj))
        if album_obj.type == "ALBUM":
            album.album_type = AlbumType.ALBUM
        elif album_obj.type == "COMPILATION":
            album.album_type = AlbumType.COMPILATION
        elif album_obj.type == "EP":
            album.album_type = AlbumType.EP
        elif album_obj.type == "SINGLE":
            album.album_type = AlbumType.SINGLE

        # album.upc = album_obj.universal_product_number
        album.year = int(album_obj.year)
        # metadata
        album.metadata.copyright = album_obj.copyright
        album.metadata.explicit = album_obj.explicit
        album.metadata.popularity = album_obj.popularity
        if full_details:
            try:
                image_url = await self._get_image_url(album_obj, 1280)
                album.metadata.images = [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=image_url,
                    )
                ]
            except Exception:
                self.logger.info(f"Album {album_obj.id} has no available picture")

        return album

    async def _parse_track(
        self,
        track_obj: TidalTrack,
        full_details: bool = False,
        extra_init_kwargs: dict[str, Any] | None = None,
    ) -> Track | AlbumTrack | PlaylistTrack:
        """Parse tidal track object to generic layout."""
        version = track_obj.version or ""
        track_id = str(track_obj.id)
        if extra_init_kwargs is None:
            extra_init_kwargs = {}
        if "position" in extra_init_kwargs:
            track_class = PlaylistTrack
        elif "disc_number" in extra_init_kwargs and "track_number" in extra_init_kwargs:
            track_class = AlbumTrack
        else:
            track_class = Track
        track = track_class(
            item_id=str(track_id),
            provider=self.instance_id,
            name=track_obj.name,
            version=version,
            duration=track_obj.duration,
            provider_mappings={
                ProviderMapping(
                    item_id=str(track_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=AudioFormat(
                        content_type=ContentType.FLAC,
                        bit_depth=24 if self._is_hi_res(track_obj=track_obj) else 16,
                    ),
                    url=f"http://www.tidal.com/tracks/{track_id}",
                    available=track_obj.available,
                )
            },
            **extra_init_kwargs,
        )
        if track_obj.isrc:
            track.external_ids.add((ExternalID.ISRC, track_obj.isrc))
        # Here we use an ItemMapping as Tidal return minimal data when getting an Album from a Track
        track.album = self.get_item_mapping(
            media_type=MediaType.ALBUM,
            key=str(track_obj.album.id),
            name=track_obj.album.name,
        )
        track.artists = []
        for track_artist in track_obj.artists:
            artist = await self._parse_artist(artist_obj=track_artist)
            track.artists.append(artist)
        # metadata
        track.metadata.explicit = track_obj.explicit
        track.metadata.popularity = track_obj.popularity
        track.metadata.copyright = track_obj.copyright
        if full_details:
            try:
                if lyrics_obj := await self._get_lyrics(track_obj):
                    track.metadata.lyrics = lyrics_obj.text
            except Exception:
                self.logger.info(f"Track {track_obj.id} has no available lyrics")
        if not track.image and track_obj.album and (image_url := track_obj.album.image(640, None)):
            track.metadata.images = [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=image_url,
                )
            ]

        return track

    async def _parse_playlist(
        self, playlist_obj: TidalPlaylist, full_details: bool = False
    ) -> Playlist:
        """Parse tidal playlist object to generic layout."""
        playlist_id = playlist_obj.id
        creator_id = playlist_obj.creator.id if playlist_obj.creator else None
        creator_name = playlist_obj.creator.name if playlist_obj.creator else "Tidal"
        playlist = Playlist(
            item_id=str(playlist_id),
            provider=self.instance_id,
            name=playlist_obj.name,
            owner=creator_name,
            provider_mappings={
                ProviderMapping(
                    item_id=str(playlist_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=f"http://www.tidal.com/playlists/{playlist_id}",
                )
            },
        )
        is_editable = bool(creator_id and str(creator_id) == self._tidal_user_id)
        playlist.is_editable = is_editable
        # metadata
        playlist.metadata.checksum = str(playlist_obj.last_updated)
        playlist.metadata.popularity = playlist_obj.popularity
        if full_details:
            try:
                image_url = await self._get_image_url(playlist_obj, 1080)
                playlist.metadata.images = [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=image_url,
                    )
                ]
            except Exception:
                self.logger.info(f"Playlist {playlist_obj.id} has no available picture")

        return playlist

    async def _get_image_url(
        self, item: TidalArtist | TidalAlbum | TidalPlaylist, size: int
    ) -> str:
        def inner() -> str:
            image_url: str = item.image(size)
            return image_url

        return await asyncio.to_thread(inner)

    async def _get_lyrics(self, item: TidalTrack) -> TidalLyrics:
        def inner() -> TidalLyrics:
            return item.lyrics

        return await asyncio.to_thread(inner)

    async def _iter_items(
        self, func: Awaitable | Callable, *args, **kwargs
    ) -> AsyncGenerator[Any, None]:
        """Yield all items from a larger listing."""
        offset = 0
        async with self._throttler:
            while True:
                if asyncio.iscoroutinefunction(func):
                    chunk = await func(*args, **kwargs, offset=offset)
                else:
                    chunk = await asyncio.to_thread(func, *args, **kwargs, offset=offset)
                offset += len(chunk)
                for item in chunk:
                    yield item
                if len(chunk) < DEFAULT_LIMIT:
                    break

    async def _get_media_info(
        self, item_id: str, url: str, force_refresh: bool = False
    ) -> AudioTags:
        """Retrieve (cached) mediainfo for track."""
        cache_key = f"{self.instance_id}.media_info.{item_id}"
        # do we have some cached info for this url ?
        cached_info = await self.mass.cache.get(cache_key)
        if cached_info and not force_refresh:
            media_info = AudioTags.parse(cached_info)
        else:
            # parse info with ffprobe (and store in cache)
            media_info = await parse_tags(url)
            await self.mass.cache.set(cache_key, media_info.raw)
        return media_info

    def _is_hi_res(self, track_obj: TidalTrack) -> bool:
        """Check if track is hi-res."""
        hi_res: bool = track_obj.audio_quality.value == "HI_RES"
        return hi_res
