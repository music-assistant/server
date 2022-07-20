"""Youtube Music support for MusicAssistant."""
import re
from operator import itemgetter
from time import time
from typing import AsyncGenerator, Dict, List, Optional, Tuple
from urllib.parse import unquote

import pytube
import ytmusicapi

from music_assistant.models.enums import MusicProviderFeature, ProviderType
from music_assistant.models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
)
from music_assistant.models.media_items import (
    Album,
    AlbumType,
    Artist,
    ContentType,
    ImageType,
    MediaItemImage,
    MediaItemProviderId,
    MediaItemType,
    MediaType,
    Playlist,
    StreamDetails,
    Track,
)
from music_assistant.models.music_provider import MusicProvider
from music_assistant.music_providers.ytmusic.helpers import (
    add_remove_playlist_tracks,
    get_album,
    get_artist,
    get_library_albums,
    get_library_artists,
    get_library_playlists,
    get_library_tracks,
    get_playlist,
    get_track,
    library_add_remove_album,
    library_add_remove_artist,
    library_add_remove_playlist,
    search,
)

YTM_DOMAIN = "https://music.youtube.com"
YTM_BASE_URL = f"{YTM_DOMAIN}/youtubei/v1/"


class YoutubeMusicProvider(MusicProvider):
    """Provider for Youtube Music."""

    _attr_type = ProviderType.YTMUSIC
    _attr_name = "Youtube Music"
    _headers = None
    _context = None
    _cookies = None
    _signature_timestamp = 0
    _cipher = None

    @property
    def supported_features(self) -> Tuple[MusicProviderFeature]:
        """Return the features supported by this MusicProvider."""
        return (
            MusicProviderFeature.LIBRARY_ARTISTS,
            MusicProviderFeature.LIBRARY_ALBUMS,
            MusicProviderFeature.LIBRARY_TRACKS,
            MusicProviderFeature.LIBRARY_PLAYLISTS,
            MusicProviderFeature.BROWSE,
            MusicProviderFeature.SEARCH,
            MusicProviderFeature.ARTIST_ALBUMS,
            MusicProviderFeature.ARTIST_TOPTRACKS,
        )

    async def setup(self) -> bool:
        """Set up the YTMusic provider."""
        if not self.config.enabled:
            return False
        if not self.config.username or not self.config.password:
            raise LoginFailed("Invalid login credentials")
        await self._initialize_headers(cookie=self.config.password)
        await self._initialize_context()
        self._cookies = {"CONSENT": "YES+1"}
        self._signature_timestamp = await self._get_signature_timestamp()
        return True

    async def search(
        self, search_query: str, media_types=Optional[List[MediaType]], limit: int = 5
    ) -> List[MediaItemType]:
        """
        Perform search on musicprovider.

            :param search_query: Search query.
            :param media_types: A list of media_types to include. All types if None.
            :param limit: Number of items to return in the search (per type).
        """
        ytm_filter = None
        if len(media_types) == 1:
            # YTM does not support multiple searchtypes, falls back to all if no type given
            if media_types[0] == MediaType.ARTIST:
                ytm_filter = "artists"
            if media_types[0] == MediaType.ALBUM:
                ytm_filter = "albums"
            if media_types[0] == MediaType.TRACK:
                ytm_filter = "songs"
            if media_types[0] == MediaType.PLAYLIST:
                ytm_filter = "playlists"
        results = await search(query=search_query, ytm_filter=ytm_filter, limit=limit)
        parsed_results = []
        for result in results:
            if result["resultType"] == "artist":
                parsed_results.append(await self._parse_artist(result))
            elif result["resultType"] == "album":
                parsed_results.append(
                    # Search result for albums contain invalid artists
                    # Use a get_album to get full details
                    await self.get_album(result["browseId"])
                )
            elif result["resultType"] == "playlist":
                parsed_results.append(await self._parse_playlist(result))
            elif result["resultType"] == "song":
                # Tracks from search results sometimes do not have a valid artist id
                # In that case, call the API for track details based on track id
                try:
                    track = await self._parse_track(result)
                    if track:
                        parsed_results.append(track)
                except InvalidDataError:
                    track = await self.get_track(result["videoId"])
                    if track:
                        parsed_results.append(track)
        return parsed_results

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve all library artists from Youtube Music."""
        artists_obj = await get_library_artists(
            headers=self._headers, username=self.config.username
        )
        for artist in artists_obj:
            yield await self._parse_artist(artist)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve all library albums from Youtube Music."""
        albums_obj = await get_library_albums(
            headers=self._headers, username=self.config.username
        )
        for album in albums_obj:
            yield await self._parse_album(album, album["browseId"])

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve all library playlists from the provider."""
        playlists_obj = await get_library_playlists(
            headers=self._headers, username=self.config.username
        )
        for playlist in playlists_obj:
            yield await self._parse_playlist(playlist)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from Youtube Music."""
        tracks_obj = await get_library_tracks(
            headers=self._headers, username=self.config.username
        )
        for track in tracks_obj:
            # Library tracks sometimes do not have a valid artist id
            # In that case, call the API for track details based on track id
            try:
                yield await self._parse_track(track)
            except InvalidDataError:
                track = await self.get_track(track["videoId"])
                yield track

    async def get_album(self, prov_album_id) -> Album:
        """Get full album details by id."""
        album_obj = await get_album(prov_album_id=prov_album_id)
        return (
            await self._parse_album(album_obj=album_obj, album_id=prov_album_id)
            if album_obj
            else None
        )

    async def get_album_tracks(self, prov_album_id: str) -> List[Track]:
        """Get album tracks for given album id."""
        album_obj = await get_album(prov_album_id=prov_album_id)
        return [
            await self._parse_track(track)
            for track in album_obj["tracks"]
            if "tracks" in album_obj
        ]

    async def get_artist(self, prov_artist_id) -> Artist:
        """Get full artist details by id."""
        artist_obj = await get_artist(prov_artist_id=prov_artist_id)
        return await self._parse_artist(artist_obj=artist_obj) if artist_obj else None

    async def get_track(self, prov_track_id) -> Track:
        """Get full track details by id."""
        track_obj = await get_track(prov_track_id=prov_track_id)
        return await self._parse_track(track_obj)

    async def get_playlist(self, prov_playlist_id) -> Playlist:
        """Get full playlist details by id."""
        playlist_obj = await get_playlist(
            prov_playlist_id=prov_playlist_id,
            headers=self._headers,
            username=self.config.username,
        )
        return await self._parse_playlist(playlist_obj)

    async def get_playlist_tracks(self, prov_playlist_id) -> List[Track]:
        """Get all playlist tracks for given playlist id."""
        playlist_obj = await get_playlist(
            prov_playlist_id=prov_playlist_id,
            headers=self._headers,
            username=self.config.username,
        )
        if "tracks" not in playlist_obj:
            return []
        tracks = []
        for index, track in enumerate(playlist_obj["tracks"]):
            if track["isAvailable"]:
                # Playlist tracks sometimes do not have a valid artist id
                # In that case, call the API for track details based on track id
                try:
                    track = await self._parse_track(track)
                    if track:
                        track.position = index
                        tracks.append(track)
                except InvalidDataError:
                    track = await self.get_track(track["videoId"])
                    if track:
                        track.position = index
                        tracks.append(track)
        return tracks

    async def get_artist_albums(self, prov_artist_id) -> List[Album]:
        """Get a list of albums for the given artist."""
        artist_obj = await get_artist(prov_artist_id=prov_artist_id)
        if "albums" in artist_obj and "results" in artist_obj["albums"]:
            albums = []
            for album_obj in artist_obj["albums"]["results"]:
                if "artists" not in album_obj:
                    album_obj["artists"] = [
                        {"id": artist_obj["channelId"], "name": artist_obj["name"]}
                    ]
                albums.append(await self._parse_album(album_obj, album_obj["browseId"]))
            return albums
        return []

    async def get_artist_toptracks(self, prov_artist_id) -> List[Track]:
        """Get a list of 5 most popular tracks for the given artist."""
        artist_obj = await get_artist(prov_artist_id=prov_artist_id)
        if "songs" in artist_obj and "results" in artist_obj["songs"]:
            return [
                await self.get_track(track["videoId"])
                for track in artist_obj["songs"]["results"]
                if track.get("videoId")
            ]
        return []

    async def library_add(self, prov_item_id, media_type: MediaType) -> None:
        """Add an item to the library."""
        result = False
        if media_type == MediaType.ARTIST:
            result = await library_add_remove_artist(
                headers=self._headers,
                prov_artist_id=prov_item_id,
                add=True,
                username=self.config.username,
            )
        elif media_type == MediaType.ALBUM:
            result = await library_add_remove_album(
                headers=self._headers,
                prov_item_id=prov_item_id,
                add=True,
                username=self.config.username,
            )
        elif media_type == MediaType.PLAYLIST:
            result = await library_add_remove_playlist(
                headers=self._headers,
                prov_item_id=prov_item_id,
                add=True,
                username=self.config.username,
            )
        elif media_type == MediaType.TRACK:
            raise NotImplementedError
        return result

    async def library_remove(self, prov_item_id, media_type: MediaType):
        """Remove an item from the library."""
        result = False
        if media_type == MediaType.ARTIST:
            result = await library_add_remove_artist(
                headers=self._headers,
                prov_artist_id=prov_item_id,
                add=False,
                username=self.config.username,
            )
        elif media_type == MediaType.ALBUM:
            result = await library_add_remove_album(
                headers=self._headers,
                prov_item_id=prov_item_id,
                add=False,
                username=self.config.username,
            )
        elif media_type == MediaType.PLAYLIST:
            result = await library_add_remove_playlist(
                headers=self._headers,
                prov_item_id=prov_item_id,
                add=False,
                username=self.config.username,
            )
        elif media_type == MediaType.TRACK:
            raise NotImplementedError
        return result

    async def add_playlist_tracks(
        self, prov_playlist_id: str, prov_track_ids: List[str]
    ) -> None:
        """Add track(s) to playlist."""
        return await add_remove_playlist_tracks(
            headers=self._headers,
            prov_playlist_id=prov_playlist_id,
            prov_track_ids=prov_track_ids,
            add=True,
            username=self.config.username,
        )

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: Tuple[int]
    ) -> None:
        """Remove track(s) from playlist."""
        playlist_obj = await get_playlist(
            prov_playlist_id=prov_playlist_id,
            headers=self._headers,
            username=self.config.username,
        )
        if "tracks" not in playlist_obj:
            return
        tracks_to_delete = []
        for index, track in enumerate(playlist_obj["tracks"]):
            if index in positions_to_remove:
                # YT needs both the videoId and the setVideoId in order to remove
                # the track. Thus, we need to obtain the playlist details and
                # grab the info from there.
                tracks_to_delete.append(
                    {"videoId": track["videoId"], "setVideoId": track["setVideoId"]}
                )

        return await add_remove_playlist_tracks(
            headers=self._headers,
            prov_playlist_id=prov_playlist_id,
            prov_track_ids=tracks_to_delete,
            add=False,
            username=self.config.username,
        )

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        data = {
            "playbackContext": {
                "contentPlaybackContext": {
                    "signatureTimestamp": self._signature_timestamp
                }
            },
            "video_id": item_id,
        }
        track_obj = await self._post_data("player", data=data)
        stream_format = await self._parse_stream_format(track_obj)
        url = await self._parse_stream_url(stream_format=stream_format, item_id=item_id)
        stream_details = StreamDetails(
            provider=self.type,
            item_id=item_id,
            content_type=ContentType.try_parse(stream_format["mimeType"]),
            direct=url,
        )
        if (
            track_obj["streamingData"].get("expiresInSeconds")
            and track_obj["streamingData"].get("expiresInSeconds").isdigit()
        ):
            stream_details.expires = time() + int(
                track_obj["streamingData"].get("expiresInSeconds")
            )
        if (
            stream_format.get("audioChannels")
            and str(stream_format.get("audioChannels")).isdigit()
        ):
            stream_details.channels = int(stream_format.get("audioChannels"))
        if (
            stream_format.get("audioSampleRate")
            and stream_format.get("audioSampleRate").isdigit()
        ):
            stream_details.sample_rate = int(stream_format.get("audioSampleRate"))
        return stream_details

    async def _post_data(self, endpoint: str, data: Dict[str, str], **kwargs):
        url = f"{YTM_BASE_URL}{endpoint}"
        data.update(self._context)
        async with self.mass.http_session.post(
            url,
            headers=self._headers,
            json=data,
            verify_ssl=False,
            cookies=self._cookies,
        ) as response:
            return await response.json()

    async def _get_data(self, url: str, params: Dict = None):
        async with self.mass.http_session.get(
            url, headers=self._headers, params=params, cookies=self._cookies
        ) as response:
            return await response.text()

    async def _initialize_headers(self, cookie: str) -> Dict[str, str]:
        """Return headers to include in the requests."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:72.0) Gecko/20100101 Firefox/72.0",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.5",
            "Content-Type": "application/json",
            "X-Goog-AuthUser": "0",
            "x-origin": "https://music.youtube.com",
            "Cookie": cookie,
        }
        sapisid = ytmusicapi.helpers.sapisid_from_cookie(cookie)
        origin = headers.get("origin", headers.get("x-origin"))
        headers["Authorization"] = ytmusicapi.helpers.get_authorization(
            sapisid + " " + origin
        )
        self._headers = headers

    async def _initialize_context(self) -> Dict[str, str]:
        """Return a dict to use as a context in requests."""
        self._context = {
            "context": {
                "client": {"clientName": "WEB_REMIX", "clientVersion": "0.1"},
                "user": {},
            }
        }

    async def _parse_album(self, album_obj: dict, album_id: str) -> Album:
        """Parse a YT Album response to an Album model object."""
        if "title" in album_obj:
            name = album_obj["title"]
        elif "name" in album_obj:
            name = album_obj["name"]
        album = Album(
            item_id=album_id,
            name=name,
            provider=self.type,
        )
        if "year" in album_obj and album_obj["year"].isdigit():
            album.year = album_obj["year"]
        if "thumbnails" in album_obj:
            album.metadata.images = await self._parse_thumbnails(
                album_obj["thumbnails"]
            )
        if "description" in album_obj:
            album.metadata.description = unquote(album_obj["description"])
        if "artists" in album_obj:
            album.artists = [
                await self._parse_artist(artist)
                for artist in album_obj["artists"]
                # artist object may be missing an id
                # in that case its either a performer (like the composer) OR this
                # is a Various artists compilation album...
                if (artist.get("id") or artist["name"] == "Various Artists")
            ]
        if "type" in album_obj:
            if album_obj["type"] == "Single":
                album_type = AlbumType.SINGLE
            elif album_obj["type"] == "EP":
                album_type = AlbumType.EP
            elif album_obj["type"] == "Album":
                album_type = AlbumType.ALBUM
            else:
                album_type = AlbumType.UNKNOWN
            album.album_type = album_type
        album.add_provider_id(
            MediaItemProviderId(
                item_id=str(album_id), prov_type=self.type, prov_id=self.id
            )
        )
        return album

    async def _parse_artist(self, artist_obj: dict) -> Artist:
        """Parse a YT Artist response to Artist model object."""
        artist_id = None
        if "channelId" in artist_obj:
            artist_id = artist_obj["channelId"]
        elif "id" in artist_obj and artist_obj["id"]:
            artist_id = artist_obj["id"]
        elif artist_obj["name"] == "Various Artists":
            artist_id = "UCUTXlgdcKU5vfzFqHOWIvkA"
        if not artist_id:
            raise InvalidDataError("Artist does not have a valid ID")
        artist = Artist(item_id=artist_id, name=artist_obj["name"], provider=self.type)
        if "description" in artist_obj:
            artist.metadata.description = artist_obj["description"]
        if "thumbnails" in artist_obj and artist_obj["thumbnails"]:
            artist.metadata.images = await self._parse_thumbnails(
                artist_obj["thumbnails"]
            )
        artist.add_provider_id(
            MediaItemProviderId(
                item_id=str(artist_id),
                prov_type=self.type,
                prov_id=self.id,
                url=f"https://music.youtube.com/channel/{artist_id}",
            )
        )
        return artist

    async def _parse_playlist(self, playlist_obj: dict) -> Playlist:
        """Parse a YT Playlist response to a Playlist object."""
        playlist = Playlist(
            item_id=playlist_obj["id"], provider=self.type, name=playlist_obj["title"]
        )
        if "description" in playlist_obj:
            playlist.metadata.description = playlist_obj["description"]
        if "thumbnails" in playlist_obj and playlist_obj["thumbnails"]:
            playlist.metadata.images = await self._parse_thumbnails(
                playlist_obj["thumbnails"]
            )
        is_editable = False
        if playlist_obj.get("privacy") and playlist_obj.get("privacy") == "PRIVATE":
            is_editable = True
        playlist.is_editable = is_editable
        playlist.add_provider_id(
            MediaItemProviderId(
                item_id=playlist_obj["id"], prov_type=self.type, prov_id=self.id
            )
        )
        playlist.metadata.checksum = playlist_obj["checksum"]
        return playlist

    async def _parse_track(self, track_obj: dict) -> Track:
        """Parse a YT Track response to a Track model object."""
        track = Track(
            item_id=track_obj["videoId"], provider=self.type, name=track_obj["title"]
        )
        if "artists" in track_obj:
            track.artists = [
                await self._parse_artist(artist) for artist in track_obj["artists"]
            ]
        if "thumbnails" in track_obj and track_obj["thumbnails"]:
            track.metadata.images = await self._parse_thumbnails(
                track_obj["thumbnails"]
            )
        if (
            track_obj.get("album")
            and track_obj.get("artists")
            and isinstance(track_obj.get("album"), dict)
            and track_obj["album"].get("id")
        ):
            album = track_obj["album"]
            album["artists"] = track_obj["artists"]
            track.album = await self._parse_album(album, album["id"])
        if "isExplicit" in track_obj:
            track.metadata.explicit = track_obj["isExplicit"]
        if "duration" in track_obj and track_obj["duration"].isdigit():
            track.duration = track_obj["duration"]
        elif (
            "duration_seconds" in track_obj
            and str(track_obj["duration_seconds"]).isdigit()
        ):
            track.duration = track_obj["duration_seconds"]
        available = True
        if "isAvailable" in track_obj:
            available = track_obj["isAvailable"]
        track.add_provider_id(
            MediaItemProviderId(
                item_id=str(track_obj["videoId"]),
                prov_type=self.type,
                prov_id=self.id,
                available=available,
            )
        )
        return track

    async def _get_signature_timestamp(self):
        """Get a signature timestamp required to generate valid stream URLs."""
        response = await self._get_data(url=YTM_DOMAIN)
        match = re.search(r'jsUrl"\s*:\s*"([^"]+)"', response)
        if match is None:
            raise Exception("Could not identify the URL for base.js player.")
        url = YTM_DOMAIN + match.group(1)
        response = await self._get_data(url=url)
        match = re.search(r"signatureTimestamp[:=](\d+)", response)
        if match is None:
            raise Exception("Unable to identify the signatureTimestamp.")
        return int(match.group(1))

    async def _parse_stream_url(self, stream_format: dict, item_id: str) -> str:
        """Figure out the stream URL to use based on the YT track object."""
        url = None
        if stream_format.get("signatureCipher"):
            # Secured URL
            cipher_parts = {}
            for part in stream_format["signatureCipher"].split("&"):
                key, val = part.split("=", maxsplit=1)
                cipher_parts[key] = unquote(val)
            signature = await self._decipher_signature(
                ciphered_signature=cipher_parts["s"], item_id=item_id
            )
            url = cipher_parts["url"] + "&sig=" + signature
        elif stream_format.get("url"):
            # Non secured URL
            url = stream_format.get("url")
        return url

    @classmethod
    async def _parse_thumbnails(cls, thumbnails_obj: dict) -> List[MediaItemImage]:
        """Parse and sort a list of thumbnails and return the highest quality."""
        thumb = sorted(thumbnails_obj, key=itemgetter("width"), reverse=True)[0]
        return [MediaItemImage(ImageType.THUMB, thumb["url"])]

    @classmethod
    async def _parse_stream_format(cls, track_obj: dict) -> dict:
        """Grab the highest available audio stream from available streams."""
        stream_format = {}
        quality_mapper = {
            "AUDIO_QUALITY_LOW": 1,
            "AUDIO_QUALITY_MEDIUM": 2,
            "AUDIO_QUALITY_HIGH": 3,
        }
        for adaptive_format in track_obj["streamingData"]["adaptiveFormats"]:
            if adaptive_format["mimeType"].startswith("audio") and (
                not stream_format
                or quality_mapper.get(adaptive_format["audioQuality"], 0)
                > quality_mapper.get(stream_format["audioQuality"], 0)
            ):
                stream_format = adaptive_format
        if stream_format is None:
            raise MediaNotFoundError("No stream found for this track")
        return stream_format

    async def _decipher_signature(self, ciphered_signature: str, item_id: str):
        """Decipher the signature, required to build the Stream URL."""

        def _decipher():
            embed_url = f"https://www.youtube.com/embed/{item_id}"
            embed_html = pytube.request.get(embed_url)
            js_url = pytube.extract.js_url(embed_html)
            ytm_js = pytube.request.get(js_url)
            cipher = pytube.cipher.Cipher(js=ytm_js)
            return cipher

        if not self._cipher:
            self._cipher = await self.mass.loop.run_in_executor(None, _decipher)
        return self._cipher.get_signature(ciphered_signature)
