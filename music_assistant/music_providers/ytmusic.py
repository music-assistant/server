"""Youtube Music support for MusicAssistant."""
import re
from datetime import date
from typing import AsyncGenerator, Dict, List, Optional
from urllib.parse import unquote

import pytube
import ytmusicapi

from music_assistant.helpers.audio import get_http_stream
from music_assistant.models.enums import ProviderType
from music_assistant.models.errors import LoginFailed, MediaNotFoundError
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

YTM_DOMAIN = "https://music.youtube.com"
YTM_BASE_URL = f"{YTM_DOMAIN}/youtubei/v1/"


class YoutubeMusicProvider(MusicProvider):
    """Provider for Youtube Music."""

    _attr_type = ProviderType.YTMUSIC
    _attr_name = "Youtube Music"
    _attr_supported_mediatypes = [
        MediaType.ARTIST,
        MediaType.ALBUM,
        MediaType.TRACK,
        MediaType.PLAYLIST,
    ]
    _headers = None
    _context = None
    _cookies = None

    async def setup(self) -> bool:
        """Set up the YTMusic provider."""
        if not self.config.enabled:
            return False
        if not self.config.username or not self.config.password:
            raise LoginFailed("Invalid login credentials")
        await self._initialize_headers(cookie=self.config.password)
        await self._initialize_context()
        self._cookies = {"CONSENT": "YES+1"}
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
        data = {"query": search_query}
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
        params = ytmusicapi.parsers.search_params.get_search_params(
            filter=ytm_filter, scope=None, ignore_spelling=False
        )
        data["params"] = params
        search_results = await self._post_data(endpoint="search", data=data)
        if "tabbedSearchResultsRenderer" in search_results["contents"]:
            results = search_results["contents"]["tabbedSearchResultsRenderer"]["tabs"][
                0
            ]["tabRenderer"]["content"]
        else:
            results = search_results["contents"]
        parsed_results = []
        for result in results["sectionListRenderer"]["contents"]:
            if "musicShelfRenderer" in result:
                category = result["musicShelfRenderer"]["title"]["runs"][0]["text"]
                if category == "Artists":
                    for artist in result["musicShelfRenderer"]["contents"]:
                        artist_id = artist["musicResponsiveListItemRenderer"][
                            "navigationEndpoint"
                        ]["browseEndpoint"]["browseId"]
                        parsed_results.append(await self.get_artist(artist_id))
                elif category == "Albums":
                    for album in result["musicShelfRenderer"]["contents"]:
                        album_id = album["musicResponsiveListItemRenderer"][
                            "navigationEndpoint"
                        ]["browseEndpoint"]["browseId"]
                        parsed_results.append(await self.get_album(album_id))
                elif category == "Songs":
                    for song in result["musicShelfRenderer"]["contents"]:
                        song_id = song["musicResponsiveListItemRenderer"][
                            "playlistItemData"
                        ]["videoId"]
                        parsed_results.append(await self.get_track(song_id))
                else:
                    print(category)
        return parsed_results

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve all library artists from Youtube Music."""
        data = {"browseId": "FEmusic_library_corpus_track_artists"}
        response = await self._post_data(endpoint="browse", data=data)
        response_artist = response["contents"]["singleColumnBrowseResultsRenderer"][
            "tabs"
        ][0]["tabRenderer"]["content"]["sectionListRenderer"]["contents"][1][
            "itemSectionRenderer"
        ][
            "contents"
        ][
            0
        ][
            "musicShelfRenderer"
        ][
            "contents"
        ]
        parsed_artists = ytmusicapi.parsers.library.parse_artists(response_artist)
        for item in parsed_artists:
            artist = Artist(
                item_id=item["browseId"], provider=self.type, name=item["artist"]
            )
            artist.metadata.images = [
                MediaItemImage(ImageType.THUMB, thumb["url"])
                for thumb in item["thumbnails"]
            ]
            artist.add_provider_id(
                MediaItemProviderId(
                    item_id=str(item["browseId"]), prov_type=self.type, prov_id=self.id
                )
            )
            yield artist

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve all library albums from Youtube Music."""
        data = {"browseId": "FEmusic_liked_albums"}
        response = await self._post_data(endpoint="browse", data=data)
        response_albums = response["contents"]["singleColumnBrowseResultsRenderer"][
            "tabs"
        ][0]["tabRenderer"]["content"]["sectionListRenderer"]["contents"][1][
            "itemSectionRenderer"
        ][
            "contents"
        ][
            0
        ][
            "gridRenderer"
        ][
            "items"
        ]
        parsed_albums = ytmusicapi.parsers.library.parse_albums(response_albums)
        for item in parsed_albums:
            if item["type"] == "Single":
                album_type = AlbumType.SINGLE
            elif item["type"] == "EP":
                album_type = AlbumType.EP
            elif item["type"] == "Album":
                album_type = AlbumType.ALBUM
            else:
                album_type = AlbumType.UNKNOWN
            album = Album(
                item_id=item["browseId"],
                name=item["title"],
                album_type=album_type,
                provider=self.type,
            )
            if item["year"].isdigit():
                album.year = item["year"]
            artists = []
            for artist in item["artists"]:
                artist_id = artist["id"]
                if not artist_id:
                    artist_id = "ytm_va"
                album_artist = Artist(
                    item_id=artist_id, name=artist["name"], provider=self.type
                )
                album_artist.add_provider_id(
                    MediaItemProviderId(
                        item_id=str(artist["id"]), prov_type=self.type, prov_id=self.id
                    )
                )
                artists.append(album_artist)
            album.artists = artists
            album.metadata.images = [
                MediaItemImage(ImageType.THUMB, thumb["url"])
                for thumb in item["thumbnails"]
            ]
            album.add_provider_id(
                MediaItemProviderId(
                    item_id=str(item["browseId"]), prov_type=self.type, prov_id=self.id
                )
            )
            yield album

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve all library playlists from the provider."""
        data = {"browseId": "FEmusic_liked_playlists"}
        response = await self._post_data(endpoint="browse", data=data)
        response_playlists = response["contents"]["singleColumnBrowseResultsRenderer"][
            "tabs"
        ][0]["tabRenderer"]["content"]["sectionListRenderer"]["contents"][1][
            "itemSectionRenderer"
        ][
            "contents"
        ][
            0
        ][
            "gridRenderer"
        ]
        playlists = ytmusicapi.parsers.browsing.parse_content_list(
            response_playlists["items"][1:], ytmusicapi.parsers.browsing.parse_playlist
        )
        for item in playlists:
            playlist = Playlist(
                item_id=str(item["playlistId"]), provider=self.type, name=item["title"]
            )
            playlist.metadata.description = item["description"]
            playlist.metadata.images = [
                MediaItemImage(ImageType.THUMB, thumb["url"])
                for thumb in item["thumbnails"]
            ]
            playlist.add_provider_id(
                MediaItemProviderId(
                    item_id=str(item["playlistId"]),
                    prov_type=self.type,
                    prov_id=self.id,
                )
            )
            yield playlist

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from Youtube Music."""
        data = {"browseId": "FEmusic_liked_videos"}
        response = await self._post_data(endpoint="browse", data=data)
        parsed_tracks = ytmusicapi.parsers.library.parse_library_songs(response)[
            "parsed"
        ]
        for item in parsed_tracks:
            track = await self.get_track(item["videoId"])
            album = await self.get_album(item["album"]["id"])
            track.album = album
            track.metadata.explicit = item["isExplicit"]
            track.add_provider_id(
                MediaItemProviderId(
                    item_id=str(item["videoId"]),
                    prov_type=self.type,
                    prov_id=self.id,
                )
            )
            yield track

    async def get_album(self, prov_album_id) -> Album:
        """Get full album details by id."""
        data = {"browseId": prov_album_id}
        album_obj = await self._post_data(endpoint="browse", data=data)
        return (
            await self._parse_album(album_obj=album_obj, album_id=prov_album_id)
            if album_obj
            else None
        )

    async def get_album_tracks(self, prov_album_id: str) -> List[Track]:
        """Get album tracks for given album id."""
        data = {"browseId": prov_album_id}
        album_obj = await self._post_data(endpoint="browse", data=data)
        parsed_album = ytmusicapi.parsers.albums.parse_album_header(album_obj)
        album_playlist_id = parsed_album["audioPlaylistId"]
        return await self.get_playlist_tracks(album_playlist_id)

    async def get_artist(self, prov_artist_id) -> Artist:
        """Get full artist details by id."""
        data = {"browseId": prov_artist_id}
        artist_obj = await self._post_data(endpoint="browse", data=data)
        return (
            await self._parse_artist(artist_obj=artist_obj, artist_id=prov_artist_id)
            if artist_obj
            else None
        )

    async def get_track(self, prov_track_id) -> Track:
        """Get full track details by id."""
        signature_timestamp = (date.today() - date.fromtimestamp(0)).days - 1
        data = {
            "playbackContext": {
                "contentPlaybackContext": {"signatureTimestamp": signature_timestamp}
            },
            "video_id": prov_track_id,
        }
        track_obj = await self._post_data("player", data=data)
        return await self._parse_track(track_obj) if track_obj else None

    async def get_playlist(self, prov_playlist_id) -> Playlist:
        """Get full playlist details by id."""
        browse_id = (
            "VL" + prov_playlist_id
            if not prov_playlist_id.startswith("VL")
            else prov_playlist_id
        )
        data = {"browseId": browse_id}
        playlist_response = await self._post_data("browse", data=data)
        return await self._parse_playlist(playlist_response)

    async def get_playlist_tracks(self, prov_playlist_id) -> List[Track]:
        """Get all playlist tracks for given playlist id."""
        browse_id = (
            "VL" + prov_playlist_id
            if not prov_playlist_id.startswith("VL")
            else prov_playlist_id
        )
        data = {"browseId": browse_id}
        playlist_obj = await self._post_data("browse", data=data)
        tracks = playlist_obj["contents"]["singleColumnBrowseResultsRenderer"]["tabs"][
            0
        ]["tabRenderer"]["content"]["sectionListRenderer"]["contents"][0][
            "musicPlaylistShelfRenderer"
        ][
            "contents"
        ]
        return [
            await self.get_track(
                track["musicResponsiveListItemRenderer"]["playlistItemData"]["videoId"]
            )
            for track in tracks
            if "playlistItemData" in track["musicResponsiveListItemRenderer"]
        ]

    async def get_artist_albums(self, prov_artist_id) -> List[Album]:
        """Get a list of albums for the given artist."""
        data = {"browseId": prov_artist_id}
        response = await self._post_data("browse", data=data)
        album_response = response["contents"]["singleColumnBrowseResultsRenderer"][
            "tabs"
        ][0]["tabRenderer"]["content"]["sectionListRenderer"]["contents"][1][
            "musicCarouselShelfRenderer"
        ][
            "contents"
        ]
        albums = []
        for item in album_response:
            album_id = item["musicTwoRowItemRenderer"]["navigationEndpoint"][
                "browseEndpoint"
            ]["browseId"]
            album_name = item["musicTwoRowItemRenderer"]["title"]["runs"][0]["text"]
            thumbnails = item["musicTwoRowItemRenderer"]["thumbnailRenderer"][
                "musicThumbnailRenderer"
            ]["thumbnail"]["thumbnails"]
            album = album = Album(
                item_id=album_id,
                name=album_name,
                provider=self.type,
            )
            album.metadata.images = [
                MediaItemImage(ImageType.THUMB, thumb["url"]) for thumb in thumbnails
            ]
            album.add_provider_id(
                MediaItemProviderId(
                    item_id=str(album_id), prov_type=self.type, prov_id=self.id
                )
            )
            albums.append(album)
        return albums

    async def get_artist_toptracks(self, prov_artist_id) -> List[Track]:
        """Get a list of 5 most popular tracks for the given artist."""
        data = {"browseId": prov_artist_id}
        response = await self._post_data("browse", data=data)
        # Check if we are dealing with an actual artist with songs, rather than a user
        if (
            "musicShelfRenderer"
            in response["contents"]["singleColumnBrowseResultsRenderer"]["tabs"][0][
                "tabRenderer"
            ]["content"]["sectionListRenderer"]["contents"][0]
        ):
            songs_response = response["contents"]["singleColumnBrowseResultsRenderer"][
                "tabs"
            ][0]["tabRenderer"]["content"]["sectionListRenderer"]["contents"][0][
                "musicShelfRenderer"
            ][
                "contents"
            ]
            return [
                await self.get_track(
                    prov_track_id=song["musicResponsiveListItemRenderer"]["overlay"][
                        "musicItemThumbnailOverlayRenderer"
                    ]["content"]["musicPlayButtonRenderer"]["playNavigationEndpoint"][
                        "watchEndpoint"
                    ][
                        "videoId"
                    ]
                )
                for song in songs_response
            ]
        return []

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        signature_timestamp = await self._get_signature_timestamp()
        data = {
            "playbackContext": {
                "contentPlaybackContext": {"signatureTimestamp": signature_timestamp}
            },
            "video_id": item_id,
        }
        track_obj = await self._post_data("player", data=data)
        stream_format = await self._parse_stream_format(track_obj)
        url = await self._parse_stream_url(stream_format=stream_format, item_id=item_id)
        return StreamDetails(
            provider=self.type,
            item_id=item_id,
            data=url,
            content_type=ContentType.try_parse(stream_format["mimeType"]),
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Return the audio stream for the provider item."""
        async for chunk in get_http_stream(
            self.mass, streamdetails.data, streamdetails, seek_position
        ):
            yield chunk

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
        parsed_album = ytmusicapi.parsers.albums.parse_album_header(album_obj)
        album = Album(
            item_id=album_id,
            name=parsed_album["title"],
            album_type=AlbumType.ALBUM,
            provider=self.type,
        )
        if parsed_album["year"].isdigit():
            album.year = parsed_album["year"]
        images = []
        for thumb in parsed_album["thumbnails"]:
            images.append(MediaItemImage(ImageType.THUMB, thumb["url"]))
        album.metadata.images = images
        if "description" in parsed_album:
            album.metadata.description = unquote(parsed_album["description"])
        artists = []
        for parsed_artist in parsed_album["artists"]:
            if parsed_artist["id"]:
                artist_id = parsed_artist["id"]
                if not artist_id:
                    artist_id = "ytm_va"
                artist = Artist(
                    item_id=artist_id,
                    provider=self.type,
                    name=parsed_artist["name"],
                )
                artist.add_provider_id(
                    MediaItemProviderId(
                        item_id=str(parsed_artist["id"]),
                        prov_type=self.type,
                        prov_id=self.id,
                    )
                )
                artists.append(artist)
        album.artists = artists
        album.add_provider_id(
            MediaItemProviderId(
                item_id=str(album_id), prov_type=self.type, prov_id=self.id
            )
        )
        return album

    async def _parse_artist(self, artist_obj: dict, artist_id: str) -> Artist:
        """Parse a YT Artist response to Artist model object."""
        if "musicImmersiveHeaderRenderer" in artist_obj["header"]:
            # Actual artist
            name = artist_obj["header"]["musicImmersiveHeaderRenderer"]["title"][
                "runs"
            ][0]["text"]
            artist_id = artist_obj["header"]["musicImmersiveHeaderRenderer"][
                "subscriptionButton"
            ]["subscribeButtonRenderer"]["channelId"]
            artist = Artist(item_id=str(artist_id), provider=self.type, name=name)
            if "description" in artist_obj["header"]["musicImmersiveHeaderRenderer"]:
                artist.metadata.description = unquote(
                    artist_obj["header"]["musicImmersiveHeaderRenderer"]["description"][
                        "runs"
                    ][0]["text"]
                )
            images = []
            if "thumbnail" in artist_obj["header"]["musicImmersiveHeaderRenderer"]:
                for thumb in artist_obj["header"]["musicImmersiveHeaderRenderer"][
                    "thumbnail"
                ]["musicThumbnailRenderer"]["thumbnail"]["thumbnails"]:
                    images.append(MediaItemImage(ImageType.THUMB, thumb["url"]))
                artist.metadata.images = images
            artist.add_provider_id(
                MediaItemProviderId(
                    item_id=str(artist_id), prov_type=self.type, prov_id=self.id
                )
            )
            return artist
        if "musicVisualHeaderRenderer" in artist_obj["header"]:
            # Artist that is actually a user
            name = artist_obj["header"]["musicVisualHeaderRenderer"]["title"]["runs"][
                0
            ]["text"]
            artist = Artist(item_id=str(artist_id), name=name, provider=self.type)
            if "thumbnail" in artist_obj["header"]["musicVisualHeaderRenderer"]:
                thumbnails = artist_obj["header"]["musicVisualHeaderRenderer"][
                    "thumbnail"
                ]["musicThumbnailRenderer"]["thumbnail"]["thumbnails"]
            elif (
                "foregroundThumbnail"
                in artist_obj["header"]["musicVisualHeaderRenderer"]
            ):
                thumbnails = artist_obj["header"]["musicVisualHeaderRenderer"][
                    "foregroundThumbnail"
                ]["musicThumbnailRenderer"]["thumbnail"]["thumbnails"]
            artist.metadata.images = [
                MediaItemImage(ImageType.THUMB, thumb["url"]) for thumb in thumbnails
            ]
            artist.add_provider_id(
                MediaItemProviderId(
                    item_id=str(artist_id), prov_type=self.type, prov_id=self.id
                )
            )
            return artist

    async def _parse_playlist(self, playlist_response: dict) -> Playlist:
        """Parse a YT Playlist response to a Playlist object."""
        playlist_id = playlist_response["contents"][
            "singleColumnBrowseResultsRenderer"
        ]["tabs"][0]["tabRenderer"]["content"]["sectionListRenderer"]["contents"][0][
            "musicPlaylistShelfRenderer"
        ][
            "playlistId"
        ]
        own_playlist = (
            "musicEditablePlaylistDetailHeaderRenderer" in playlist_response["header"]
        )
        if own_playlist:
            playlist_response = playlist_response["header"][
                "musicEditablePlaylistDetailHeaderRenderer"
            ]
        title = playlist_response["header"]["musicDetailHeaderRenderer"]["title"][
            "runs"
        ][0]["text"]
        thumbnails = playlist_response["header"]["musicDetailHeaderRenderer"][
            "thumbnail"
        ]["croppedSquareThumbnailRenderer"]["thumbnail"]["thumbnails"]
        playlist = Playlist(item_id=str(playlist_id), provider=self.type, name=title)
        if "description" in playlist_response["header"]["musicDetailHeaderRenderer"]:
            playlist.metadata.description = playlist_response["header"][
                "musicDetailHeaderRenderer"
            ]["description"]["runs"][0]["text"]
        playlist.metadata.images = [
            MediaItemImage(ImageType.THUMB, thumb["url"]) for thumb in thumbnails
        ]
        playlist.add_provider_id(
            MediaItemProviderId(
                item_id=str(playlist_id), prov_type=self.type, prov_id=self.id
            )
        )
        return playlist

    async def _parse_track(self, track_obj: dict) -> Track:
        """Parse a YT Track response to a Track model object."""
        track = Track(
            item_id=track_obj["videoDetails"]["videoId"],
            provider=self.type,
            name=track_obj["videoDetails"]["title"],
            duration=track_obj["videoDetails"]["lengthSeconds"],
        )
        artist_id = track_obj["microformat"]["microformatDataRenderer"][
            "pageOwnerDetails"
        ]["externalChannelId"]
        if artist_id == "UCUTXlgdcKU5vfzFqHOWIvkA":
            artist = Artist(
                item_id=artist_id, name="Various Artists", provider=self.type
            )
            artist.add_provider_id(
                MediaItemProviderId(
                    item_id=str(artist_id), prov_type=self.type, prov_id=self.id
                )
            )
        else:
            artist = await self.get_artist(artist_id)
        track.artists = [artist]
        track.metadata.images = [
            MediaItemImage(ImageType.THUMB, thumb["url"])
            for thumb in track_obj["microformat"]["microformatDataRenderer"][
                "thumbnail"
            ]["thumbnails"]
        ]
        available = False
        if track_obj["playabilityStatus"]["status"] == "OK":
            available = True
        track.add_provider_id(
            MediaItemProviderId(
                item_id=str(track_obj["videoDetails"]["videoId"]),
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
        cipher_parts = {}
        for part in stream_format["signatureCipher"].split("&"):
            key, val = part.split("=", maxsplit=1)
            cipher_parts[key] = unquote(val)
        signature = await self._decipher_signature(
            ciphered_signature=cipher_parts["s"], item_id=item_id
        )
        url = cipher_parts["url"] + "&sig=" + signature
        return url

    @classmethod
    async def _parse_stream_format(cls, track_obj: dict) -> dict:
        """Grab the highes available audio stream from available streams."""
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
            return cipher.get_signature(ciphered_signature)

        return await self.mass.loop.run_in_executor(None, _decipher)
