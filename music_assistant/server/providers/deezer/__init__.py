"""Deezer music provider support for MusicAssistant."""
from asyncio import TaskGroup
from collections.abc import AsyncGenerator
from math import ceil

import deezer
from aiohttp import ClientTimeout
from asyncio_throttle.throttler import Throttler

from music_assistant.common.models.config_entries import (
    ConfigEntry,
    ConfigValueType,
    ProviderConfig,
)
from music_assistant.common.models.enums import (
    ConfigEntryType,
    ContentType,
    MediaType,
    ProviderFeature,
)
from music_assistant.common.models.errors import LoginFailed
from music_assistant.common.models.media_items import (
    Album,
    Artist,
    Playlist,
    SearchResults,
    StreamDetails,
    Track,
)
from music_assistant.common.models.provider import ProviderManifest
from music_assistant.server.helpers.auth import AuthenticationHelper
from music_assistant.server.models import ProviderInstanceType
from music_assistant.server.models.music_provider import MusicProvider
from music_assistant.server.server import MusicAssistant

from .gw_client import GWClient
from .helpers import (
    Credential,
    add_user_albums,
    add_user_artists,
    add_user_tracks,
    decrypt_chunk,
    get_album,
    get_albums_by_artist,
    get_artist,
    get_artist_top,
    get_blowfish_key,
    get_deezer_client,
    get_playlist,
    get_track,
    get_user_albums,
    get_user_artists,
    get_user_playlists,
    get_user_tracks,
    parse_album,
    parse_artist,
    parse_playlist,
    parse_track,
    remove_user_albums,
    remove_user_artists,
    remove_user_tracks,
    search_and_parse_albums,
    search_and_parse_artists,
    search_and_parse_playlists,
    search_and_parse_tracks,
    track_available,
    update_access_token,
)

SUPPORTED_FEATURES = (
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.ALBUM_METADATA,
    ProviderFeature.TRACK_METADATA,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_METADATA,
    ProviderFeature.LIBRARY_ALBUMS_EDIT,
    ProviderFeature.LIBRARY_TRACKS_EDIT,
    ProviderFeature.BROWSE,
)

CONF_ACCESS_TOKEN = "access_token"
CONF_ACTION_AUTH = "auth"
DEEZER_AUTH_URL = "https://connect.deezer.com/oauth/auth.php"
RELAY_URL = "https://deezer.oauth.jonathanbangert.com/"
DEEZER_PERMS = "basic_access,email,offline_access,manage_library,\
manage_community,delete_library,listening_history"
DEEZER_APP_ID = 596944
DEEZER_APP_SECRET = "6d15ff599e70a706db68ec83698bd885"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = DeezerProvider(mass, manifest, config)
    await prov.handle_setup()
    return prov


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001 pylint: disable=W0613
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    # If the action is to launch oauth flow
    if action == CONF_ACTION_AUTH:
        # We use the AuthenticationHelper to authenticate
        async with AuthenticationHelper(mass, values["session_id"]) as auth_helper:  # type: ignore
            callback_url = auth_helper.callback_url
            url = f"{DEEZER_AUTH_URL}?app_id={DEEZER_APP_ID}&redirect_uri={RELAY_URL}\
&perms={DEEZER_PERMS}&state={callback_url}"
            code = (await auth_helper.authenticate(url))["code"]
            values[CONF_ACCESS_TOKEN] = await update_access_token(  # type: ignore
                mass, DEEZER_APP_ID, DEEZER_APP_SECRET, code
            )

    return (
        ConfigEntry(
            key=CONF_ACCESS_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Access token",
            required=True,
            action=CONF_ACTION_AUTH,
            description="You need to authenticate on Deezer.",
            action_label="Authenticate with Deezer",
            value=values.get(CONF_ACCESS_TOKEN) if values else None,
        ),
    )


class DeezerProvider(MusicProvider):
    """Deezer provider support."""

    client: deezer.Client
    gw_client: GWClient
    creds: Credential
    _throttler: Throttler

    async def handle_setup(self) -> None:
        """Set up the Deezer provider."""
        self._throttler = Throttler(rate_limit=4, period=1)
        self.creds = Credential(
            app_id=DEEZER_APP_ID,
            app_secret=DEEZER_APP_SECRET,
            access_token=self.config.get_value(CONF_ACCESS_TOKEN),  # type: ignore
        )
        try:
            self.client = await get_deezer_client(creds=self.creds)
        except Exception as error:
            raise LoginFailed("Invalid login credentials") from error

        self.gw_client = GWClient(self.mass.http_session, self.config.get_value(CONF_ACCESS_TOKEN))
        await self.gw_client.setup()

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return SUPPORTED_FEATURES

    async def search(
        self, search_query: str, media_types=list[MediaType] | None, limit: int = 5
    ) -> SearchResults:
        """Perform search on music provider.

        :param search_query: Search query.
        :param media_types: A list of media_types to include. All types if None.
        """
        if not media_types:
            media_types = [MediaType.ARTIST, MediaType.ALBUM, MediaType.TRACK, MediaType.PLAYLIST]

        tasks = {}

        async with TaskGroup() as taskgroup:
            for media_type in media_types:
                if media_type == MediaType.TRACK:
                    tasks[MediaType.TRACK] = taskgroup.create_task(
                        search_and_parse_tracks(
                            mass=self,
                            client=self.client,
                            query=search_query,
                            limit=limit,
                            user_country=self.gw_client.user_country,
                        )
                    )
                elif media_type == MediaType.ARTIST:
                    tasks[MediaType.ARTIST] = taskgroup.create_task(
                        search_and_parse_artists(
                            mass=self, client=self.client, query=search_query, limit=limit
                        )
                    )
                elif media_type == MediaType.ALBUM:
                    tasks[MediaType.ALBUM] = taskgroup.create_task(
                        search_and_parse_albums(
                            mass=self, client=self.client, query=search_query, limit=limit
                        )
                    )
                elif media_type == MediaType.PLAYLIST:
                    tasks[MediaType.PLAYLIST] = taskgroup.create_task(
                        search_and_parse_playlists(
                            mass=self, client=self.client, query=search_query, limit=limit
                        )
                    )

        results = SearchResults()

        for media_type, task in tasks.items():
            if media_type == MediaType.ARTIST:
                results.artists = task.result()
            elif media_type == MediaType.ALBUM:
                results.albums = task.result()
            elif media_type == MediaType.TRACK:
                results.tracks = task.result()
            elif media_type == MediaType.PLAYLIST:
                results.playlists = task.result()

        return results

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve all library artists from Deezer."""
        for artist in await get_user_artists(client=self.client):
            yield parse_artist(mass=self, artist=artist)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve all library albums from Deezer."""
        for album in await get_user_albums(client=self.client):
            yield parse_album(mass=self, album=album)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve all library playlists from Deezer."""
        for playlist in await get_user_playlists(client=self.client):
            yield parse_playlist(mass=self, playlist=playlist)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve all library tracks from Deezer."""
        for track in await get_user_tracks(client=self.client):
            if track_available(track, self.gw_client.user_country):
                yield parse_track(mass=self, track=track, user_country=self.gw_client.user_country)

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        return parse_artist(
            mass=self, artist=await get_artist(client=self.client, artist_id=int(prov_artist_id))
        )

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        return parse_album(
            mass=self, album=await get_album(client=self.client, album_id=int(prov_album_id))
        )

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        return parse_playlist(
            mass=self,
            playlist=await get_playlist(client=self.client, playlist_id=int(prov_playlist_id)),
        )

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        return parse_track(
            mass=self,
            track=await get_track(client=self.client, track_id=int(prov_track_id)),
            user_country=self.gw_client.user_country,
        )

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get all albums in a playlist."""
        album = await get_album(client=self.client, album_id=int(prov_album_id))
        return [
            parse_track(mass=self, track=track, user_country=self.gw_client.user_country)
            for track in album.tracks
            if track_available(track, self.gw_client.user_country)
        ]

    async def get_playlist_tracks(self, prov_playlist_id: str) -> AsyncGenerator[Track, None]:
        """Get all tracks in a playlist."""
        playlist = await get_playlist(client=self.client, playlist_id=prov_playlist_id)
        for track in playlist.tracks:
            if track_available(track, self.gw_client.user_country):
                yield parse_track(mass=self, track=track, user_country=self.gw_client.user_country)

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get albums by an artist."""
        artist = await get_artist(client=self.client, artist_id=int(prov_artist_id))
        albums = []
        for album in await get_albums_by_artist(artist=artist):
            albums.append(parse_album(mass=self, album=album))
        return albums

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get top 25 tracks of an artist."""
        artist = await get_artist(client=self.client, artist_id=int(prov_artist_id))
        top_tracks = (await get_artist_top(artist=artist))[:25]
        return [
            parse_track(mass=self, track=track, user_country=self.gw_client.user_country)
            for track in top_tracks
            if track_available(track, self.gw_client.user_country)
        ]

    async def library_add(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Add an item to the library."""
        result = False
        if media_type == MediaType.ARTIST:
            result = await add_user_artists(
                artist_id=int(prov_item_id),
                client=self.client,
            )
        elif media_type == MediaType.ALBUM:
            result = await add_user_albums(
                album_id=int(prov_item_id),
                client=self.client,
            )
        elif media_type == MediaType.TRACK:
            result = await add_user_tracks(
                track_id=int(prov_item_id),
                client=self.client,
            )
        else:
            raise NotImplementedError
        return result

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove an item to the library."""
        result = False
        if media_type == MediaType.ARTIST:
            result = await remove_user_artists(
                artist_id=int(prov_item_id),
                client=self.client,
            )
        elif media_type == MediaType.ALBUM:
            result = await remove_user_albums(
                album_id=int(prov_item_id),
                client=self.client,
            )
        elif media_type == MediaType.TRACK:
            result = await remove_user_tracks(
                track_id=int(prov_item_id),
                client=self.client,
            )
        else:
            raise NotImplementedError
        return result

    async def get_stream_details(self, item_id: str) -> StreamDetails | None:
        """Return the content details for the given track when it will be streamed."""
        url_details, song_data = await self.gw_client.get_deezer_track_urls(item_id)
        url = url_details["sources"][0]["url"]
        return StreamDetails(
            item_id=item_id,
            provider=self.instance_id,
            content_type=ContentType.try_parse(url_details["format"].split("_")[0]),
            duration=int(song_data["DURATION"]),
            data=url,
            expires=url_details["exp"],
            size=int(song_data[f"FILESIZE_{url_details['format']}"]),
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Return the audio stream for the provider item."""
        blowfish_key = get_blowfish_key(streamdetails.item_id)
        chunk_index = 0
        timeout = ClientTimeout(total=0, connect=30, sock_read=600)
        headers = {}
        if seek_position and streamdetails.size:
            chunk_count = ceil(streamdetails.size / 2048)
            chunk_index = int(chunk_count / streamdetails.duration) * seek_position
            skip_bytes = chunk_index * 2048
            headers["Range"] = f"bytes={skip_bytes}-"

        buffer = bytearray()
        async with self.mass.http_session.get(
            streamdetails.data, headers=headers, timeout=timeout
        ) as resp:
            async for chunk in resp.content.iter_chunked(2048):
                buffer += chunk
                if len(buffer) >= 2048:
                    if chunk_index % 3 > 0:
                        yield bytes(buffer[:2048])
                    else:
                        yield decrypt_chunk(bytes(buffer[:2048]), blowfish_key)
                    chunk_index += 1
                    del buffer[:2048]
        yield bytes(buffer)
