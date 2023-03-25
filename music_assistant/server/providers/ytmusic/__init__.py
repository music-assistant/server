"""Youtube Music support for MusicAssistant."""
from __future__ import annotations

import asyncio
import re
from operator import itemgetter
from time import time
from typing import TYPE_CHECKING, AsyncGenerator  # noqa: UP035
from urllib.parse import unquote

import pytube
import ytmusicapi

from music_assistant.common.models.config_entries import ConfigEntry
from music_assistant.common.models.enums import ConfigEntryType, ProviderFeature
from music_assistant.common.models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    UnplayableMediaError,
)
from music_assistant.common.models.media_items import (
    Album,
    AlbumType,
    Artist,
    ContentType,
    ImageType,
    MediaItemImage,
    MediaType,
    Playlist,
    ProviderMapping,
    SearchResults,
    StreamDetails,
    Track,
)
from music_assistant.constants import CONF_USERNAME
from music_assistant.server.models.music_provider import MusicProvider

from .helpers import (
    add_remove_playlist_tracks,
    get_album,
    get_artist,
    get_library_albums,
    get_library_artists,
    get_library_playlists,
    get_library_tracks,
    get_playlist,
    get_song_radio_tracks,
    get_track,
    library_add_remove_album,
    library_add_remove_artist,
    library_add_remove_playlist,
    search,
)

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType


CONF_COOKIE = "cookie"

YT_DOMAIN = "https://www.youtube.com"
YTM_DOMAIN = "https://music.youtube.com"
YTM_BASE_URL = f"{YTM_DOMAIN}/youtubei/v1/"

SUPPORTED_FEATURES = (
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.SIMILAR_TRACKS,
)

# TODO: fix disabled tests
# ruff: noqa: PLW2901, RET504


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = YoutubeMusicProvider(mass, manifest, config)
    await prov.handle_setup()
    return prov


async def get_config_entries(
    mass: MusicAssistant, manifest: ProviderManifest  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    return (
        ConfigEntry(
            key=CONF_USERNAME, type=ConfigEntryType.STRING, label="Username", required=True
        ),
        ConfigEntry(
            key=CONF_COOKIE,
            type=ConfigEntryType.SECURE_STRING,
            label="Login Cookie",
            required=True,
            description="The Login cookie you grabbed from an existing session, "
            "see the documentation.",
        ),
    )


class YoutubeMusicProvider(MusicProvider):
    """Provider for Youtube Music."""

    _headers = None
    _context = None
    _cookies = None
    _signature_timestamp = 0
    _cipher = None

    async def handle_setup(self) -> None:
        """Set up the YTMusic provider."""
        if not self.config.get_value(CONF_USERNAME) or not self.config.get_value(CONF_COOKIE):
            raise LoginFailed("Invalid login credentials")
        await self._initialize_headers(cookie=self.config.get_value(CONF_COOKIE))
        await self._initialize_context()
        self._cookies = {"CONSENT": "YES+1"}
        self._signature_timestamp = await self._get_signature_timestamp()

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return SUPPORTED_FEATURES

    async def search(
        self, search_query: str, media_types=list[MediaType] | None, limit: int = 5
    ) -> SearchResults:
        """Perform search on musicprovider.

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
        parsed_results = SearchResults()
        for result in results:
            try:
                if result["resultType"] == "artist":
                    parsed_results.artists.append(await self._parse_artist(result))
                elif result["resultType"] == "album":
                    parsed_results.albums.append(await self._parse_album(result))
                elif result["resultType"] == "playlist":
                    parsed_results.playlists.append(await self._parse_playlist(result))
                elif result["resultType"] == "song" and (track := await self._parse_track(result)):
                    parsed_results.tracks.append(track)
            except InvalidDataError:
                pass  # ignore invalid item
        return parsed_results

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve all library artists from Youtube Music."""
        artists_obj = await get_library_artists(
            headers=self._headers, username=self.config.get_value(CONF_USERNAME)
        )
        for artist in artists_obj:
            yield await self._parse_artist(artist)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve all library albums from Youtube Music."""
        albums_obj = await get_library_albums(
            headers=self._headers, username=self.config.get_value(CONF_USERNAME)
        )
        for album in albums_obj:
            yield await self._parse_album(album, album["browseId"])

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve all library playlists from the provider."""
        playlists_obj = await get_library_playlists(
            headers=self._headers, username=self.config.get_value(CONF_USERNAME)
        )
        for playlist in playlists_obj:
            yield await self._parse_playlist(playlist)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from Youtube Music."""
        tracks_obj = await get_library_tracks(
            headers=self._headers, username=self.config.get_value(CONF_USERNAME)
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

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id."""
        album_obj = await get_album(prov_album_id=prov_album_id)
        if not album_obj.get("tracks"):
            return []
        tracks = []
        for idx, track_obj in enumerate(album_obj["tracks"], 1):
            track = await self._parse_track(track_obj=track_obj)
            track.disc_number = 0
            track.track_number = idx
            tracks.append(track)
        return tracks

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
            username=self.config.get_value(CONF_USERNAME),
        )
        return await self._parse_playlist(playlist_obj)

    async def get_playlist_tracks(self, prov_playlist_id) -> list[Track]:
        """Get all playlist tracks for given playlist id."""
        playlist_obj = await get_playlist(
            prov_playlist_id=prov_playlist_id,
            headers=self._headers,
            username=self.config.get_value(CONF_USERNAME),
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

    async def get_artist_albums(self, prov_artist_id) -> list[Album]:
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

    async def get_artist_toptracks(self, prov_artist_id) -> list[Track]:
        """Get a list of 25 most popular tracks for the given artist."""
        artist_obj = await get_artist(prov_artist_id=prov_artist_id)
        if artist_obj.get("songs") and artist_obj["songs"].get("browseId"):
            prov_playlist_id = artist_obj["songs"]["browseId"]
            playlist_tracks = await self.get_playlist_tracks(prov_playlist_id=prov_playlist_id)
            return playlist_tracks[:25]
        return []

    async def library_add(self, prov_item_id, media_type: MediaType) -> None:
        """Add an item to the library."""
        result = False
        if media_type == MediaType.ARTIST:
            result = await library_add_remove_artist(
                headers=self._headers,
                prov_artist_id=prov_item_id,
                add=True,
                username=self.config.get_value(CONF_USERNAME),
            )
        elif media_type == MediaType.ALBUM:
            result = await library_add_remove_album(
                headers=self._headers,
                prov_item_id=prov_item_id,
                add=True,
                username=self.config.get_value(CONF_USERNAME),
            )
        elif media_type == MediaType.PLAYLIST:
            result = await library_add_remove_playlist(
                headers=self._headers,
                prov_item_id=prov_item_id,
                add=True,
                username=self.config.get_value(CONF_USERNAME),
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
                username=self.config.get_value(CONF_USERNAME),
            )
        elif media_type == MediaType.ALBUM:
            result = await library_add_remove_album(
                headers=self._headers,
                prov_item_id=prov_item_id,
                add=False,
                username=self.config.get_value(CONF_USERNAME),
            )
        elif media_type == MediaType.PLAYLIST:
            result = await library_add_remove_playlist(
                headers=self._headers,
                prov_item_id=prov_item_id,
                add=False,
                username=self.config.get_value(CONF_USERNAME),
            )
        elif media_type == MediaType.TRACK:
            raise NotImplementedError
        return result

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add track(s) to playlist."""
        return await add_remove_playlist_tracks(
            headers=self._headers,
            prov_playlist_id=prov_playlist_id,
            prov_track_ids=prov_track_ids,
            add=True,
            username=self.config.get_value(CONF_USERNAME),
        )

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist."""
        playlist_obj = await get_playlist(
            prov_playlist_id=prov_playlist_id,
            headers=self._headers,
            username=self.config.get_value(CONF_USERNAME),
        )
        if "tracks" not in playlist_obj:
            return None
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
            username=self.config.get_value(CONF_USERNAME),
        )

    async def get_similar_tracks(self, prov_track_id, limit=25) -> list[Track]:
        """Retrieve a dynamic list of tracks based on the provided item."""
        result = []
        result = await get_song_radio_tracks(
            headers=self._headers,
            username=self.config.get_value(CONF_USERNAME),
            prov_item_id=prov_track_id,
            limit=limit,
        )
        if "tracks" in result:
            tracks = []
            for track in result["tracks"]:
                # Playlist tracks sometimes do not have a valid artist id
                # In that case, call the API for track details based on track id
                try:
                    track = await self._parse_track(track)
                    if track:
                        tracks.append(track)
                except InvalidDataError:
                    track = await self.get_track(track["videoId"])
                    if track:
                        tracks.append(track)
            return tracks
        return []

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        data = {
            "playbackContext": {
                "contentPlaybackContext": {"signatureTimestamp": self._signature_timestamp}
            },
            "video_id": item_id,
        }
        track_obj = await self._post_data("player", data=data)
        stream_format = await self._parse_stream_format(track_obj)
        url = await self._parse_stream_url(stream_format=stream_format, item_id=item_id)
        stream_details = StreamDetails(
            provider=self.domain,
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
        if stream_format.get("audioChannels") and str(stream_format.get("audioChannels")).isdigit():
            stream_details.channels = int(stream_format.get("audioChannels"))
        if stream_format.get("audioSampleRate") and stream_format.get("audioSampleRate").isdigit():
            stream_details.sample_rate = int(stream_format.get("audioSampleRate"))
        return stream_details

    async def _post_data(self, endpoint: str, data: dict[str, str], **kwargs):  # noqa: ARG002
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

    async def _get_data(self, url: str, params: dict = None):
        async with self.mass.http_session.get(
            url, headers=self._headers, params=params, cookies=self._cookies
        ) as response:
            return await response.text()

    async def _initialize_headers(self, cookie: str) -> dict[str, str]:
        """Return headers to include in the requests."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:72.0) Gecko/20100101 Firefox/72.0",  # noqa: E501
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.5",
            "Content-Type": "application/json",
            "X-Goog-AuthUser": "0",
            "x-origin": "https://music.youtube.com",
            "Cookie": cookie,
        }
        sapisid = ytmusicapi.helpers.sapisid_from_cookie(cookie)
        origin = headers.get("origin", headers.get("x-origin"))
        headers["Authorization"] = ytmusicapi.helpers.get_authorization(sapisid + " " + origin)
        self._headers = headers

    async def _initialize_context(self) -> dict[str, str]:
        """Return a dict to use as a context in requests."""
        self._context = {
            "context": {
                "client": {"clientName": "WEB_REMIX", "clientVersion": "0.1"},
                "user": {},
            }
        }

    async def _parse_album(self, album_obj: dict, album_id: str = None) -> Album:
        """Parse a YT Album response to an Album model object."""
        album_id = album_id or album_obj.get("id") or album_obj.get("browseId")
        if "title" in album_obj:
            name = album_obj["title"]
        elif "name" in album_obj:
            name = album_obj["name"]
        album = Album(
            item_id=album_id,
            name=name,
            provider=self.domain,
        )
        if album_obj.get("year") and album_obj["year"].isdigit():
            album.year = album_obj["year"]
        if "thumbnails" in album_obj:
            album.metadata.images = await self._parse_thumbnails(album_obj["thumbnails"])
        if "description" in album_obj:
            album.metadata.description = unquote(album_obj["description"])
        if "isExplicit" in album_obj:
            album.metadata.explicit = album_obj["isExplicit"]
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
        album.add_provider_mapping(
            ProviderMapping(
                item_id=str(album_id),
                provider_domain=self.domain,
                provider_instance=self.instance_id,
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
        artist = Artist(item_id=artist_id, name=artist_obj["name"], provider=self.domain)
        if "description" in artist_obj:
            artist.metadata.description = artist_obj["description"]
        if "thumbnails" in artist_obj and artist_obj["thumbnails"]:
            artist.metadata.images = await self._parse_thumbnails(artist_obj["thumbnails"])
        artist.add_provider_mapping(
            ProviderMapping(
                item_id=str(artist_id),
                provider_domain=self.domain,
                provider_instance=self.instance_id,
                url=f"https://music.youtube.com/channel/{artist_id}",
            )
        )
        return artist

    async def _parse_playlist(self, playlist_obj: dict) -> Playlist:
        """Parse a YT Playlist response to a Playlist object."""
        playlist = Playlist(
            item_id=playlist_obj["id"], provider=self.domain, name=playlist_obj["title"]
        )
        if "description" in playlist_obj:
            playlist.metadata.description = playlist_obj["description"]
        if "thumbnails" in playlist_obj and playlist_obj["thumbnails"]:
            playlist.metadata.images = await self._parse_thumbnails(playlist_obj["thumbnails"])
        is_editable = False
        if playlist_obj.get("privacy") and playlist_obj.get("privacy") == "PRIVATE":
            is_editable = True
        playlist.is_editable = is_editable
        playlist.add_provider_mapping(
            ProviderMapping(
                item_id=playlist_obj["id"],
                provider_domain=self.domain,
                provider_instance=self.instance_id,
            )
        )
        if authors := playlist_obj.get("author"):
            if isinstance(authors, str):
                playlist.owner = authors
            elif isinstance(authors, list):
                playlist.owner = authors[0]["name"]
            else:
                playlist.owner = authors["name"]
        else:
            playlist.owner = self.instance_id
        playlist.metadata.checksum = playlist_obj.get("checksum")
        return playlist

    async def _parse_track(self, track_obj: dict) -> Track:
        """Parse a YT Track response to a Track model object."""
        track = Track(item_id=track_obj["videoId"], provider=self.domain, name=track_obj["title"])
        if "artists" in track_obj:
            track.artists = [
                await self._parse_artist(artist)
                for artist in track_obj["artists"]
                if artist.get("id")
                or artist.get("channelId")
                or artist.get("name") == "Various Artists"
            ]
        # guard that track has valid artists
        if not track.artists:
            raise InvalidDataError("Track is missing artists")
        if "thumbnails" in track_obj and track_obj["thumbnails"]:
            track.metadata.images = await self._parse_thumbnails(track_obj["thumbnails"])
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
        if "duration" in track_obj and str(track_obj["duration"]).isdigit():
            track.duration = int(track_obj["duration"])
        elif "duration_seconds" in track_obj and str(track_obj["duration_seconds"]).isdigit():
            track.duration = int(track_obj["duration_seconds"])
        available = True
        if "isAvailable" in track_obj:
            available = track_obj["isAvailable"]
        track.add_provider_mapping(
            ProviderMapping(
                item_id=str(track_obj["videoId"]),
                provider_domain=self.domain,
                provider_instance=self.instance_id,
                available=available,
                content_type=ContentType.M4A,
            )
        )
        return track

    async def _get_signature_timestamp(self):
        """Get a signature timestamp required to generate valid stream URLs."""
        response = await self._get_data(url=YTM_DOMAIN)
        match = re.search(r'jsUrl"\s*:\s*"([^"]+)"', response)
        if match is None:
            # retry with youtube domain
            response = await self._get_data(url=YT_DOMAIN)
            match = re.search(r'jsUrl"\s*:\s*"([^"]+)"', response)
        if match is None:
            raise Exception("Could not identify the URL for base.js player.")
        url = YTM_DOMAIN + match.group(1)
        response = await self._get_data(url=url)
        match = re.search(r"signatureTimestamp[:=](\d+)", response)
        if match is None:
            raise Exception("Unable to identify the signatureTimestamp.")
        return int(match.group(1))

    async def _parse_stream_url(self, stream_format: dict, item_id: str, retry: bool = True) -> str:
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
            # Verify if URL is playable. If not, obtain a new cipher and try again.
            if not await self._is_valid_deciphered_url(url=url):
                if not retry:
                    raise UnplayableMediaError(
                        f"Cannot obtain a valid URL for item '{item_id}' after renewing cipher."
                    )
                self.logger.debug("Cipher expired. Obtaining new Cipher.")
                self._cipher = None
                return self._parse_stream_url(
                    stream_format=stream_format, item_id=item_id, retry=False
                )
        elif stream_format.get("url"):
            # Non secured URL
            url = stream_format.get("url")
        return url

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
            self._cipher = await asyncio.to_thread(_decipher)
        return self._cipher.get_signature(ciphered_signature)

    async def _is_valid_deciphered_url(self, url: str) -> bool:
        """Verify whether the URL has been deciphered using a valid cipher."""
        async with self.mass.http_session.head(url) as response:
            return response.status == 200

    @classmethod
    async def _parse_thumbnails(cls, thumbnails_obj: dict) -> list[MediaItemImage]:
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
