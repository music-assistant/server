"""Tidal music provider support for MusicAssistant."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from music_assistant.common.models.config_entries import ConfigEntry, ConfigValueType
from music_assistant.common.models.enums import ConfigEntryType, MediaType, ProviderFeature
from music_assistant.common.models.errors import LoginFailed, MediaNotFoundError
from music_assistant.common.models.media_items import (
    Album,
    Artist,
    ContentType,
    Playlist,
    SearchResults,
    StreamDetails,
    Track,
)
from music_assistant.server.helpers.auth import AuthenticationHelper
from music_assistant.server.models.music_provider import MusicProvider

from .helpers import (
    TidalSessionManager,
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
    parse_album,
    parse_artist,
    parse_playlist,
    parse_track,
    search,
    tidal_code_login,
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


class TidalProvider(MusicProvider):
    """Implementation of a Tidal MusicProvider."""

    _tidal_session_manager: TidalSessionManager | None = None

    async def handle_setup(self) -> None:
        """Handle async initialization of the provider."""
        async with TidalSessionManager(
            self.instance_id, self.config, self.mass
        ) as tidal_session_manager:
            self._tidal_session_manager = tidal_session_manager
        self._tidal_user_id = self.config.get_value(CONF_USER_ID)
        # check token which will raise if it fails
        await self._tidal_session_manager.validate_token_and_refresh()

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
                parsed_results.artists.append(await parse_artist(artist))
        if results["albums"]:
            for album in results["albums"]:
                parsed_results.albums.append(await parse_album(album))
        if results["playlists"]:
            for playlist in results["playlists"]:
                parsed_results.playlists.append(await parse_playlist(playlist))
        if results["tracks"]:
            for track in results["tracks"]:
                parsed_results.tracks.append(await parse_track(track))
        return parsed_results

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve all library artists from Tidal."""
        artists_obj = await get_library_artists(
            self._tidal_session_manager.session, self._tidal_user_id
        )
        for artist in artists_obj:
            yield parse_artist(tidal_provider=self, artist_obj=artist)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve all library albums from Tidal."""
        albums_obj = await get_library_albums(
            self._tidal_session_manager.session, self._tidal_user_id
        )
        for album in albums_obj:
            yield parse_album(tidal_provider=self, album_obj=album)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from Tidal."""
        tracks_obj = await get_library_tracks(
            self._tidal_session_manager.session, self._tidal_user_id
        )
        for track in tracks_obj:
            if track.available:
                yield parse_track(tidal_provider=self, track_obj=track)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve all library playlists from the provider."""
        playlists_obj = await get_library_playlists(
            self._tidal_session_manager.session, self._tidal_user_id
        )
        for playlist in playlists_obj:
            yield parse_playlist(tidal_provider=self, playlist_obj=playlist)

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id."""
        result = []
        tracks = await get_album_tracks(self._tidal_session_manager.session, prov_album_id)
        for index, track_obj in enumerate(tracks, 1):
            if track_obj.available:
                track = parse_track(tidal_provider=self, track_obj=track_obj)
                track.position = index
                result.append(track)
        return result

    async def get_artist_albums(self, prov_artist_id) -> list[Album]:
        """Get a list of all albums for the given artist."""
        result = []
        albums = await get_artist_albums(self._tidal_session_manager.session, prov_artist_id)
        for album_obj in albums:
            album = parse_album(tidal_provider=self, album_obj=album_obj)
            result.append(album)
        return result

    async def get_artist_toptracks(self, prov_artist_id) -> list[Track]:
        """Get a list of 10 most popular tracks for the given artist."""
        result = []
        tracks = await get_artist_toptracks(self._tidal_session_manager.session, prov_artist_id)
        for index, track_obj in enumerate(tracks, 1):
            if track_obj.available:
                track = parse_track(tidal_provider=self, track_obj=track_obj)
                track.position = index
                result.append(track)
        return result

    async def get_playlist_tracks(self, prov_playlist_id) -> AsyncGenerator[Track, None]:
        """Get all playlist tracks for given playlist id."""
        tracks = await get_playlist_tracks(
            self._tidal_session_manager.session, prov_playlist_id=prov_playlist_id
        )
        for index, track_obj in enumerate(tracks):
            if track_obj.available:
                track = parse_track(tidal_provider=self, track_obj=track_obj)
                track.position = index + 1
                yield track

    async def get_similar_tracks(self, prov_track_id, limit=25) -> list[Track]:
        """Get similar tracks for given track id."""
        similar_tracks_obj = await get_similar_tracks(
            self._tidal_session_manager.session, prov_track_id, limit
        )
        tracks = []
        for track_obj in similar_tracks_obj:
            if track_obj.available:
                track = parse_track(tidal_provider=self, track_obj=track_obj)
                tracks.append(track)
        return tracks

    async def library_add(self, prov_item_id, media_type: MediaType):
        """Add item to library."""
        return await library_items_add_remove(
            self._tidal_session_manager.session,
            self._tidal_user_id,
            prov_item_id,
            media_type,
            add=True,
        )

    async def library_remove(self, prov_item_id, media_type: MediaType):
        """Remove item from library."""
        return await library_items_add_remove(
            self._tidal_session_manager.session,
            self._tidal_user_id,
            prov_item_id,
            media_type,
            add=False,
        )

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]):
        """Add track(s) to playlist."""
        return await add_remove_playlist_tracks(
            self._tidal_session_manager.session, prov_playlist_id, prov_track_ids, add=True
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
            self._tidal_session_manager.session, prov_playlist_id, prov_track_ids, add=False
        )

    async def create_playlist(self, name: str) -> Playlist:  # type: ignore[return]
        """Create a new playlist on provider with given name."""
        playlist_obj = await create_playlist(
            self._tidal_session_manager.session, self._tidal_user_id, name
        )
        playlist = parse_playlist(tidal_provider=self, playlist_obj=playlist_obj)
        return await self.mass.music.playlists.add_db_item(playlist)

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        # make sure a valid track is requested.
        track = await get_track(self._tidal_session_manager.session, item_id)
        url = await get_track_url(self._tidal_session_manager.session, item_id)
        if not track:
            raise MediaNotFoundError(f"track {item_id} not found")
        # make sure that the token is still valid by just requesting it
        await self._tidal_session_manager.validate_token_and_refresh()
        return StreamDetails(
            item_id=track.id,
            provider=self.instance_id,
            content_type=ContentType.FLAC,
            duration=track.duration,
            direct=url,
        )

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get artist details for given artist id."""
        try:
            artist = parse_artist(
                tidal_provider=self,
                artist_obj=await get_artist(self._tidal_session_manager.session, prov_artist_id),
            )
        except MediaNotFoundError as err:
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found") from err
        return artist

    async def get_album(self, prov_album_id: str) -> Album:
        """Get album details for given album id."""
        try:
            album = parse_album(
                tidal_provider=self,
                album_obj=await get_album(self._tidal_session_manager.session, prov_album_id),
            )
        except MediaNotFoundError as err:
            raise MediaNotFoundError(f"Album {prov_album_id} not found") from err
        return album

    async def get_track(self, prov_track_id: str) -> Track:
        """Get track details for given track id."""
        try:
            track = parse_track(
                tidal_provider=self,
                track_obj=await get_track(self._tidal_session_manager.session, prov_track_id),
            )
        except MediaNotFoundError as err:
            raise MediaNotFoundError(f"Track {prov_track_id} not found") from err
        return track

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get playlist details for given playlist id."""
        try:
            playlist = parse_playlist(
                tidal_provider=self,
                playlist_obj=await get_playlist(
                    self._tidal_session_manager.session, prov_playlist_id
                ),
            )
        except MediaNotFoundError as err:
            raise MediaNotFoundError(f"Playlist {prov_playlist_id} not found") from err
        return playlist
