"""Deezer music provider support for MusicAssistant."""
import hashlib
from asyncio import TaskGroup
from collections.abc import AsyncGenerator
from math import ceil

import deezer
from aiohttp import ClientTimeout
from asyncio_throttle.throttler import Throttler
from Crypto.Cipher import Blowfish

from music_assistant.common.models.config_entries import (
    ConfigEntry,
    ConfigValueType,
    ProviderConfig,
)
from music_assistant.common.models.enums import (
    AlbumType,
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
)
from music_assistant.common.models.errors import LoginFailed
from music_assistant.common.models.media_items import (
    Album,
    Artist,
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemMetadata,
    Playlist,
    ProviderMapping,
    SearchResults,
    StreamDetails,
    Track,
)
from music_assistant.common.models.provider import ProviderManifest
from music_assistant.server.helpers.app_vars import app_var  # pylint: disable=no-name-in-module
from music_assistant.server.helpers.auth import AuthenticationHelper
from music_assistant.server.models import ProviderInstanceType
from music_assistant.server.models.music_provider import MusicProvider
from music_assistant.server.server import MusicAssistant

from .gw_client import GWClient
from .helpers import Credential, DeezerClient

SUPPORTED_FEATURES = (
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.LIBRARY_ALBUMS_EDIT,
    ProviderFeature.LIBRARY_TRACKS_EDIT,
    ProviderFeature.LIBRARY_ARTISTS_EDIT,
    ProviderFeature.LIBRARY_PLAYLISTS_EDIT,
    ProviderFeature.ALBUM_METADATA,
    ProviderFeature.TRACK_METADATA,
    ProviderFeature.ARTIST_METADATA,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.PLAYLIST_TRACKS_EDIT,
    ProviderFeature.PLAYLIST_CREATE,
    ProviderFeature.RECOMMENDATIONS,
)

CONF_ACCESS_TOKEN = "access_token"
CONF_ACTION_AUTH = "auth"
DEEZER_AUTH_URL = "https://connect.deezer.com/oauth/auth.php"
RELAY_URL = "https://deezer.oauth.jonathanbangert.com/"
DEEZER_PERMS = "basic_access,email,offline_access,manage_library,\
manage_community,delete_library,listening_history"
DEEZER_APP_ID = app_var(6)
DEEZER_APP_SECRET = app_var(7)


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
            values[CONF_ACCESS_TOKEN] = await DeezerProvider.update_access_token(  # type: ignore
                DeezerProvider, DEEZER_APP_ID, DEEZER_APP_SECRET, code, mass.http_session
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


class DeezerProvider(MusicProvider):  # pylint: disable=W0223
    """Deezer provider support."""

    client: DeezerClient
    gw_client: GWClient
    creds: Credential
    _throttler: Throttler

    @property
    def name(self) -> str:
        """Return (custom) friendly name for this provider instance."""
        if client := getattr(self, "client", None):
            return f"Deezer - {client.user.name}"
        return super().name

    async def handle_setup(self) -> None:
        """Set up the Deezer provider."""
        self._throttler = Throttler(rate_limit=50, period=5)
        self.creds = Credential(
            app_id=DEEZER_APP_ID,
            app_secret=DEEZER_APP_SECRET,
            access_token=self.config.get_value(CONF_ACCESS_TOKEN),  # type: ignore
        )
        try:
            deezer_client = await DeezerClient.get_deezer_client(self=None, creds=self.creds)
            self.client = DeezerClient(creds=self.creds, client=deezer_client)
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
                        self.search_and_parse_tracks(
                            query=search_query,
                            limit=limit,
                            user_country=self.gw_client.user_country,
                        )
                    )
                elif media_type == MediaType.ARTIST:
                    tasks[MediaType.ARTIST] = taskgroup.create_task(
                        self.search_and_parse_artists(query=search_query, limit=limit)
                    )
                elif media_type == MediaType.ALBUM:
                    tasks[MediaType.ALBUM] = taskgroup.create_task(
                        self.search_and_parse_albums(query=search_query, limit=limit)
                    )
                elif media_type == MediaType.PLAYLIST:
                    tasks[MediaType.PLAYLIST] = taskgroup.create_task(
                        self.search_and_parse_playlists(query=search_query, limit=limit)
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
        for artist in await self.client.get_user_artists():
            yield self.parse_artist(artist=artist)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve all library albums from Deezer."""
        for album in await self.client.get_user_albums():
            yield self.parse_album(album=album)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve all library playlists from Deezer."""
        for playlist in await self.client.get_user_playlists():
            yield self.parse_playlist(playlist=playlist)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve all library tracks from Deezer."""
        for track in await self.client.get_user_tracks():
            yield self.parse_track(track=track, user_country=self.gw_client.user_country)

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        return self.parse_artist(artist=await self.client.get_artist(artist_id=int(prov_artist_id)))

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        try:
            return self.parse_album(album=await self.client.get_album(album_id=int(prov_album_id)))
        except deezer.exceptions.DeezerErrorResponse as error:
            self.logger.warning("Failed getting album: %s", error)
            return Album(prov_album_id, self.instance_id, "Not Found")

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        return self.parse_playlist(
            playlist=await self.client.get_playlist(playlist_id=int(prov_playlist_id)),
        )

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        return self.parse_track(
            track=await self.client.get_track(track_id=int(prov_track_id)),
            user_country=self.gw_client.user_country,
        )

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get all albums in a playlist."""
        album = await self.client.get_album(album_id=int(prov_album_id))
        return [
            self.parse_track(track=track, user_country=self.gw_client.user_country)
            for track in album.tracks
        ]

    async def get_playlist_tracks(self, prov_playlist_id: str) -> AsyncGenerator[Track, None]:
        """Get all tracks in a playlist."""
        playlist = await self.client.get_playlist(playlist_id=prov_playlist_id)
        for count, track in enumerate(playlist.tracks, start=1):
            track_parsed = self.parse_track(track=track, user_country=self.gw_client.user_country)
            track_parsed.position = count
            track_parsed.id = track.id
            yield track_parsed

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get albums by an artist."""
        artist = await self.client.get_artist(artist_id=int(prov_artist_id))
        albums = []
        for album in await self.client.get_albums_by_artist(artist=artist):
            albums.append(self.parse_album(album=album))
        return albums

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get top 25 tracks of an artist."""
        artist = await self.client.get_artist(artist_id=int(prov_artist_id))
        top_tracks = await self.client.get_artist_top(artist=artist, limit=25)
        return [
            self.parse_track(track=track, user_country=self.gw_client.user_country)
            for track in top_tracks
        ]

    async def library_add(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Add an item to the library."""
        result = False
        if media_type == MediaType.ARTIST:
            result = await self.client.add_user_artists(
                artist_id=int(prov_item_id),
            )
        elif media_type == MediaType.ALBUM:
            result = await self.client.add_user_albums(
                album_id=int(prov_item_id),
            )
        elif media_type == MediaType.TRACK:
            result = await self.client.add_user_tracks(
                track_id=int(prov_item_id),
            )
        elif media_type == MediaType.PLAYLIST:
            result = await self.client.add_user_playlists(
                playlist_id=int(prov_item_id),
            )
        else:
            raise NotImplementedError
        return result

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove an item to the library."""
        result = False
        if media_type == MediaType.ARTIST:
            result = await self.client.remove_user_artists(
                artist_id=int(prov_item_id),
            )
        elif media_type == MediaType.ALBUM:
            result = await self.client.remove_user_albums(
                album_id=int(prov_item_id),
            )
        elif media_type == MediaType.TRACK:
            result = await self.client.remove_user_tracks(
                track_id=int(prov_item_id),
            )
        elif media_type == MediaType.PLAYLIST:
            result = await self.client.remove_user_playlists(
                playlist_id=int(prov_item_id),
            )
        else:
            raise NotImplementedError
        return result

    async def recommendations(self) -> list[BrowseFolder]:
        """Get deezer's recommendations."""
        browser_folder = BrowseFolder(
            item_id="recommendations",
            provider=self.domain,
            path="recommendations",
            name="Recommendations",
            label="recommendations",
            items=[
                self.parse_track(track=track, user_country=self.gw_client.user_country)
                for track in await self.client.get_recommended_tracks()
            ],
        )
        return [browser_folder]

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]):
        """Add tra ck(s) to playlist."""
        await self.client.add_playlist_tracks(
            playlist_id=prov_playlist_id, tracks=[int(i) for i in prov_track_ids]
        )

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ):
        """Remove track(s) to playlist."""
        playlist_track_ids = []
        async for track in self.get_playlist_tracks(prov_playlist_id):
            if track.position in positions_to_remove:
                playlist_track_ids.append(track.id)
            if len(playlist_track_ids) == len(positions_to_remove):
                break
        await self.client.remove_playlist_tracks(
            playlist_id=prov_playlist_id, tracks=list(playlist_track_ids)
        )

    async def create_playlist(self, name: str) -> Playlist:
        """Create a new playlist on provider with given name."""
        playlist = await self.client.create_playlist(playlist_name=name)
        return self.parse_playlist(playlist=playlist)

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
        blowfish_key = self.get_blowfish_key(streamdetails.item_id)
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
                        yield self.decrypt_chunk(bytes(buffer[:2048]), blowfish_key)
                    chunk_index += 1
                    del buffer[:2048]
        yield bytes(buffer)

    ### PARSING METADATA FUNCTIONS ###

    def parse_metadata_track(self, track: deezer.Track) -> MediaItemMetadata:
        """Parse the track metadata."""
        try:
            return MediaItemMetadata(
                preview=track.preview,
                images=[
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=track.album.cover_big,
                    )
                ],
            )
        except AttributeError:
            return MediaItemMetadata(
                preview=track.preview,
            )

    def parse_metadata_album(self, album: deezer.Album) -> MediaItemMetadata:
        """Parse the album metadata."""
        return MediaItemMetadata(
            images=[MediaItemImage(type=ImageType.THUMB, path=album.cover_big)],
        )

    def parse_metadata_artist(self, artist: deezer.Artist) -> MediaItemMetadata:
        """Parse the artist metadata."""
        return MediaItemMetadata(
            images=[MediaItemImage(type=ImageType.THUMB, path=artist.picture_big)],
        )

    ### PARSING FUNCTIONS ###
    def parse_artist(self, artist: deezer.Artist) -> Artist:
        """Parse the deezer-python artist to a MASS artist."""
        return Artist(
            item_id=str(artist.id),
            provider=self.domain,
            name=artist.name,
            media_type=MediaType.ARTIST,
            provider_mappings={
                ProviderMapping(
                    item_id=str(artist.id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            metadata=self.parse_metadata_artist(artist=artist),
        )

    def parse_album(self, album: deezer.Album) -> Album:
        """Parse the deezer-python album to a MASS album."""
        return Album(
            album_type=AlbumType(album.type),
            item_id=str(album.id),
            provider=self.domain,
            name=album.title,
            artists=[
                ItemMapping(
                    MediaType.ARTIST,
                    str(album.artist.id),
                    self.instance_id,
                    album.artist.name,
                )
            ],
            media_type=MediaType.ALBUM,
            provider_mappings={
                ProviderMapping(
                    item_id=str(album.id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            metadata=self.parse_metadata_album(album=album),
        )

    def parse_playlist(self, playlist: deezer.Playlist) -> Playlist:
        """Parse the deezer-python playlist to a MASS playlist."""
        return Playlist(
            item_id=str(playlist.id),
            provider=self.domain,
            name=playlist.title,
            media_type=MediaType.PLAYLIST,
            provider_mappings={
                ProviderMapping(
                    item_id=str(playlist.id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            metadata=MediaItemMetadata(
                images=[MediaItemImage(type=ImageType.THUMB, path=playlist.picture_big)],
            ),
            is_editable=playlist.creator.id == self.client.user.id,
        )

    def parse_track(self, track: deezer.Track, user_country: str) -> Track:
        """Parse the deezer-python track to a MASS track."""
        return Track(
            item_id=str(track.id),
            provider=self.domain,
            name=track.title,
            media_type=MediaType.TRACK,
            sort_name=track.title_short,
            position=track.track_position,
            duration=track.duration,
            artists=[
                ItemMapping(
                    MediaType.ARTIST,
                    str(track.artist.id),
                    self.instance_id,
                    track.artist.name,
                )
            ],
            album=ItemMapping(
                MediaType.ALBUM,
                str(track.album.id),
                self.instance_id,
                track.album.title,
            ),
            provider_mappings={
                ProviderMapping(
                    item_id=str(track.id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    available=self.track_available(track, user_country),
                )
            },
            metadata=self.parse_metadata_track(track=track),
        )

    ### SEARCH AND PARSE FUNCTIONS ###
    async def search_and_parse_tracks(
        self, query: str, user_country: str, limit: int = 5
    ) -> list[Track]:
        """Search for tracks and parse them."""
        deezer_tracks = await self.client.search_track(query=query, limit=limit)
        return [self.parse_track(track, user_country) for track in deezer_tracks]

    async def search_and_parse_artists(self, query: str, limit: int = 5) -> list[Artist]:
        """Search for artists and parse them."""
        deezer_artist = await self.client.search_artist(query=query, limit=limit)
        return [self.parse_artist(artist=artist) for artist in deezer_artist]

    async def search_and_parse_albums(self, query: str, limit: int = 5) -> list[Album]:
        """Search for album and parse them."""
        deezer_albums = await self.client.search_album(query=query, limit=limit)
        return [self.parse_album(album=album) for album in deezer_albums]

    async def search_and_parse_playlists(self, query: str, limit: int = 5) -> list[Playlist]:
        """Search for playlists and parse them."""
        deezer_playlists = await self.client.search_playlist(query=query, limit=limit)
        return [self.parse_playlist(playlist=playlist) for playlist in deezer_playlists]

    ### OTHER PARSING FUNCTIONS ###
    def _get_album(self, track: deezer.Track) -> Album | None:
        try:
            return self.parse_album(album=track.get_album())
        except AttributeError:
            return None

    ### OTHER FUNCTIONS ###
    async def update_access_token(self, app_id, app_secret, code, http_session=None) -> str:
        """Update the access_token."""
        if not http_session:
            http_session = self.mass.http_session
        response = await self._post_http(  # pylint: disable=E1124
            self=self,
            http_session=http_session,
            url="https://connect.deezer.com/oauth/access_token.php",
            data={
                "code": code,
                "app_id": app_id,
                "secret": app_secret,
            },
            params={
                "code": code,
                "app_id": app_id,
                "secret": app_secret,
            },
            headers=None,
        )
        try:
            return response.split("=")[1].split("&")[0]
        except Exception as error:
            raise LoginFailed("Invalid auth code") from error

    async def _post_http(self, http_session, url, data, params=None, headers=None) -> str:
        async with http_session.post(
            url, headers=headers, params=params, json=data, ssl=False
        ) as response:
            if response.status != 200:
                raise ConnectionError(f"HTTP Error {response.status}: {response.reason}")
            response_text = await response.text()
            return response_text

    async def get_track_content_type(self, gw_client: GWClient, track_id: int):
        """Get a tracks contentType."""
        song_data = await gw_client.get_song_data(track_id)
        if song_data["results"]["FILESIZE_FLAC"]:
            return ContentType.FLAC

        if song_data["results"]["FILESIZE_MP3_320"] or song_data["results"]["FILESIZE_MP3_128"]:
            return ContentType.MP3

        raise NotImplementedError("Unsupported contenttype")

    def track_available(self, track: deezer.Track, user_country: str) -> bool:
        """Check if a given track is available in the users country."""
        return user_country in track.available_countries

    def _md5(self, data, data_type="ascii"):
        md5sum = hashlib.md5()
        md5sum.update(data.encode(data_type))
        return md5sum.hexdigest()

    def get_blowfish_key(self, track_id):
        """Get blowfish key to decrypt a chunk of a track."""
        secret = "g4el58wc" + "0zvf9na1"
        id_md5 = self._md5(track_id)
        return "".join(
            chr(ord(id_md5[i]) ^ ord(id_md5[i + 16]) ^ ord(secret[i])) for i in range(16)
        )

    def decrypt_chunk(self, chunk, blowfish_key):
        """Decrypt a given chunk using the blow fish key."""
        cipher = Blowfish.new(
            blowfish_key.encode("ascii"), Blowfish.MODE_CBC, b"\x00\x01\x02\x03\x04\x05\x06\x07"
        )
        return cipher.decrypt(chunk)
