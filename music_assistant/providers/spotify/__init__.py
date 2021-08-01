"""Spotify musicprovider support for MusicAssistant."""
import asyncio
import json
import logging
import os
import platform
import time
from json.decoder import JSONDecodeError
from typing import List, Optional

from asyncio_throttle import Throttler
from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.helpers.app_vars import get_app_var  # noqa # pylint: disable=all
from music_assistant.helpers.util import parse_title_and_version
from music_assistant.models.config_entry import ConfigEntry, ConfigEntryType
from music_assistant.models.media_types import (
    Album,
    AlbumType,
    Artist,
    MediaItemProviderId,
    MediaType,
    Playlist,
    Radio,
    SearchResult,
    Track,
    TrackQuality,
)
from music_assistant.models.provider import MusicProvider
from music_assistant.models.streamdetails import ContentType, StreamDetails, StreamType

PROV_ID = "spotify"
PROV_NAME = "Spotify"

LOGGER = logging.getLogger(PROV_ID)

CONFIG_ENTRIES = [
    ConfigEntry(
        entry_key=CONF_USERNAME,
        entry_type=ConfigEntryType.STRING,
        label=CONF_USERNAME,
        description="desc_spotify_username",
    ),
    ConfigEntry(
        entry_key=CONF_PASSWORD,
        entry_type=ConfigEntryType.PASSWORD,
        label=CONF_PASSWORD,
        description="desc_spotify_password",
    ),
]


async def setup(mass):
    """Perform async setup of this Plugin/Provider."""
    prov = SpotifyProvider()
    await mass.register_provider(prov)


class SpotifyProvider(MusicProvider):
    """Implementation for the Spotify MusicProvider."""

    # pylint: disable=abstract-method

    __auth_token = None
    sp_user = None
    _username = None
    _password = None

    @property
    def id(self) -> str:
        """Return provider ID for this provider."""
        return PROV_ID

    @property
    def name(self) -> str:
        """Return provider Name for this provider."""
        return PROV_NAME

    @property
    def config_entries(self) -> List[ConfigEntry]:
        """Return Config Entries for this provider."""
        return CONFIG_ENTRIES

    @property
    def supported_mediatypes(self) -> List[MediaType]:
        """Return MediaTypes the provider supports."""
        return [
            MediaType.ALBUM,
            MediaType.ARTIST,
            MediaType.PLAYLIST,
            # MediaType.RADIO, # TODO!
            MediaType.TRACK,
        ]

    async def on_start(self) -> bool:
        """Handle initialization of the provider based on config."""
        config = self.mass.config.get_provider_config(self.id)
        # pylint: disable=attribute-defined-outside-init
        self._cur_user = None
        self.sp_user = None
        if not config[CONF_USERNAME] or not config[CONF_PASSWORD]:
            LOGGER.debug("Username and password not set. Abort load of provider.")
            return False
        self._username = config[CONF_USERNAME]
        self._password = config[CONF_PASSWORD]
        self.__auth_token = {}
        self._throttler = Throttler(rate_limit=4, period=1)
        token = await self.get_token()

        return token is not None

    async def search(
        self, search_query: str, media_types=Optional[List[MediaType]], limit: int = 5
    ) -> SearchResult:
        """
        Perform search on musicprovider.

            :param search_query: Search query.
            :param media_types: A list of media_types to include. All types if None.
            :param limit: Number of items to return in the search (per type).
        """
        result = SearchResult()
        searchtypes = []
        if MediaType.ARTIST in media_types:
            searchtypes.append("artist")
        if MediaType.ALBUM in media_types:
            searchtypes.append("album")
        if MediaType.TRACK in media_types:
            searchtypes.append("track")
        if MediaType.PLAYLIST in media_types:
            searchtypes.append("playlist")
        searchtype = ",".join(searchtypes)
        params = {"q": search_query, "type": searchtype, "limit": limit}
        searchresult = await self._get_data("search", params=params)
        if searchresult:
            if "artists" in searchresult:
                result.artists = [
                    await self._parse_artist(item)
                    for item in searchresult["artists"]["items"]
                    if (item and item["id"])
                ]
            if "albums" in searchresult:
                result.albums = [
                    await self._parse_album(item)
                    for item in searchresult["albums"]["items"]
                    if (item and item["id"])
                ]
            if "tracks" in searchresult:
                result.tracks = [
                    await self._parse_track(item)
                    for item in searchresult["tracks"]["items"]
                    if (item and item["id"])
                ]
            if "playlists" in searchresult:
                result.playlists = [
                    await self._parse_playlist(item)
                    for item in searchresult["playlists"]["items"]
                    if (item and item["id"])
                ]
        return result

    async def get_library_artists(self) -> List[Artist]:
        """Retrieve library artists from spotify."""
        spotify_artists = await self._get_data("me/following?type=artist&limit=50")
        return [
            await self._parse_artist(item)
            for item in spotify_artists["artists"]["items"]
            if (item and item["id"])
        ]

    async def get_library_albums(self) -> List[Album]:
        """Retrieve library albums from the provider."""
        return [
            await self._parse_album(item["album"])
            for item in await self._get_all_items("me/albums")
            if (item["album"] and item["album"]["id"])
        ]

    async def get_library_tracks(self) -> List[Track]:
        """Retrieve library tracks from the provider."""
        return [
            await self._parse_track(item["track"])
            for item in await self._get_all_items("me/tracks")
            if (item and item["track"]["id"])
        ]

    async def get_library_playlists(self) -> List[Playlist]:
        """Retrieve playlists from the provider."""
        return [
            await self._parse_playlist(item)
            for item in await self._get_all_items("me/playlists")
            if (item and item["id"])
        ]

    async def get_radios(self) -> List[Radio]:
        """Retrieve library/subscribed radio stations from the provider."""
        return []  # TODO: Return spotify radio

    async def get_artist(self, prov_artist_id) -> Artist:
        """Get full artist details by id."""
        artist_obj = await self._get_data("artists/%s" % prov_artist_id)
        return await self._parse_artist(artist_obj) if artist_obj else None

    async def get_album(self, prov_album_id) -> Album:
        """Get full album details by id."""
        album_obj = await self._get_data("albums/%s" % prov_album_id)
        return await self._parse_album(album_obj) if album_obj else None

    async def get_track(self, prov_track_id) -> Track:
        """Get full track details by id."""
        track_obj = await self._get_data("tracks/%s" % prov_track_id)
        return await self._parse_track(track_obj) if track_obj else None

    async def get_playlist(self, prov_playlist_id) -> Playlist:
        """Get full playlist details by id."""
        playlist_obj = await self._get_data(f"playlists/{prov_playlist_id}")
        return await self._parse_playlist(playlist_obj) if playlist_obj else None

    async def get_album_tracks(self, prov_album_id) -> List[Track]:
        """Get all album tracks for given album id."""
        return [
            await self._parse_track(item)
            for item in await self._get_all_items(f"albums/{prov_album_id}/tracks")
            if (item and item["id"])
        ]

    async def get_playlist_tracks(self, prov_playlist_id) -> List[Track]:
        """Get all playlist tracks for given playlist id."""
        return [
            await self._parse_track(item["track"])
            for item in await self._get_all_items(
                f"playlists/{prov_playlist_id}/tracks"
            )
            if (item and item["track"] and item["track"]["id"])
        ]

    async def get_artist_albums(self, prov_artist_id) -> List[Album]:
        """Get a list of all albums for the given artist."""
        return [
            await self._parse_album(item)
            for item in await self._get_all_items(
                f"artists/{prov_artist_id}/albums?include_groups=album,single,compilation"
            )
            if (item and item["id"])
        ]

    async def get_artist_toptracks(self, prov_artist_id) -> List[Track]:
        """Get a list of 10 most popular tracks for the given artist."""
        artist = await self.get_artist(prov_artist_id)
        endpoint = f"artists/{prov_artist_id}/top-tracks"
        items = await self._get_data(endpoint)
        return [
            await self._parse_track(item, artist=artist)
            for item in items["tracks"]
            if (item and item["id"])
        ]

    async def library_add(self, prov_item_id, media_type: MediaType):
        """Add item to library."""
        result = False
        if media_type == MediaType.ARTIST:
            result = await self._put_data(
                "me/following", {"ids": prov_item_id, "type": "artist"}
            )
        elif media_type == MediaType.ALBUM:
            result = await self._put_data("me/albums", {"ids": prov_item_id})
        elif media_type == MediaType.TRACK:
            result = await self._put_data("me/tracks", {"ids": prov_item_id})
        elif media_type == MediaType.PLAYLIST:
            result = await self._put_data(
                f"playlists/{prov_item_id}/followers", data={"public": False}
            )
        return result

    async def library_remove(self, prov_item_id, media_type: MediaType):
        """Remove item from library."""
        result = False
        if media_type == MediaType.ARTIST:
            result = await self._delete_data(
                "me/following", {"ids": prov_item_id, "type": "artist"}
            )
        elif media_type == MediaType.ALBUM:
            result = await self._delete_data("me/albums", {"ids": prov_item_id})
        elif media_type == MediaType.TRACK:
            result = await self._delete_data("me/tracks", {"ids": prov_item_id})
        elif media_type == MediaType.PLAYLIST:
            result = await self._delete_data(f"playlists/{prov_item_id}/followers")
        return result

    async def add_playlist_tracks(self, prov_playlist_id, prov_track_ids):
        """Add track(s) to playlist."""
        track_uris = []
        for track_id in prov_track_ids:
            track_uris.append("spotify:track:%s" % track_id)
        data = {"uris": track_uris}
        return await self._post_data(f"playlists/{prov_playlist_id}/tracks", data=data)

    async def remove_playlist_tracks(self, prov_playlist_id, prov_track_ids):
        """Remove track(s) from playlist."""
        track_uris = []
        for track_id in prov_track_ids:
            track_uris.append({"uri": "spotify:track:%s" % track_id})
        data = {"tracks": track_uris}
        return await self._delete_data(
            f"playlists/{prov_playlist_id}/tracks", data=data
        )

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        # make sure a valid track is requested.
        track = await self.get_track(item_id)
        if not track:
            return None
        # make sure that the token is still valid by just requesting it
        await self.get_token()
        spotty = self.get_spotty_binary()
        spotty_exec = (
            '%s -n temp -c "%s" -b 320 --pass-through --single-track spotify://track:%s'
            % (
                spotty,
                self.mass.config.data_path,
                track.item_id,
            )
        )
        return StreamDetails(
            type=StreamType.EXECUTABLE,
            item_id=track.item_id,
            provider=PROV_ID,
            path=spotty_exec,
            content_type=ContentType.OGG,
            sample_rate=44100,
            bit_depth=16,
        )

    async def _parse_artist(self, artist_obj):
        """Parse spotify artist object to generic layout."""
        artist = Artist(
            item_id=artist_obj["id"], provider=self.id, name=artist_obj["name"]
        )
        artist.provider_ids.add(
            MediaItemProviderId(provider=PROV_ID, item_id=artist_obj["id"])
        )
        if "genres" in artist_obj:
            artist.metadata["genres"] = artist_obj["genres"]
        if artist_obj.get("images"):
            for img in artist_obj["images"]:
                img_url = img["url"]
                if "2a96cbd8b46e442fc41c2b86b821562f" not in img_url:
                    artist.metadata["image"] = img_url
                    break
        if artist_obj.get("external_urls"):
            artist.metadata["spotify_url"] = artist_obj["external_urls"]["spotify"]
        return artist

    async def _parse_album(self, album_obj):
        """Parse spotify album object to generic layout."""
        album = Album(item_id=album_obj["id"], provider=self.id)
        album.name, album.version = parse_title_and_version(album_obj["name"])
        for artist in album_obj["artists"]:
            album.artist = await self._parse_artist(artist)
            if album.artist:
                break
        if album_obj["album_type"] == "single":
            album.album_type = AlbumType.SINGLE
        elif album_obj["album_type"] == "compilation":
            album.album_type = AlbumType.COMPILATION
        elif album_obj["album_type"] == "album":
            album.album_type = AlbumType.ALBUM
        if "genres" in album_obj:
            album.metadata["genres"] = album_obj["genres"]
        if album_obj.get("images"):
            album.metadata["image"] = album_obj["images"][0]["url"]
        if "external_ids" in album_obj and album_obj["external_ids"].get("upc"):
            album.upc = album_obj["external_ids"]["upc"]
        if "label" in album_obj:
            album.metadata["label"] = album_obj["label"]
        if album_obj.get("release_date"):
            album.year = int(album_obj["release_date"].split("-")[0])
        if album_obj.get("copyrights"):
            album.metadata["copyright"] = album_obj["copyrights"][0]["text"]
        if album_obj.get("external_urls"):
            album.metadata["spotify_url"] = album_obj["external_urls"]["spotify"]
        if album_obj.get("explicit"):
            album.metadata["explicit"] = str(album_obj["explicit"]).lower()
        album.provider_ids.add(
            MediaItemProviderId(
                provider=PROV_ID,
                item_id=album_obj["id"],
                quality=TrackQuality.LOSSY_OGG,
            )
        )
        return album

    async def _parse_track(self, track_obj, artist=None):
        """Parse spotify track object to generic layout."""
        track = Track(
            item_id=track_obj["id"],
            provider=self.id,
            duration=track_obj["duration_ms"] / 1000,
            disc_number=track_obj["disc_number"],
            track_number=track_obj["track_number"],
        )
        if artist:
            track.artists.add(artist)
        for track_artist in track_obj.get("artists", []):
            artist = await self._parse_artist(track_artist)
            if artist and artist.item_id not in {x.item_id for x in track.artists}:
                track.artists.add(artist)
        track.name, track.version = parse_title_and_version(track_obj["name"])
        track.metadata["explicit"] = str(track_obj["explicit"]).lower()
        if "external_ids" in track_obj and "isrc" in track_obj["external_ids"]:
            track.isrc = track_obj["external_ids"]["isrc"]
        if "album" in track_obj:
            track.album = await self._parse_album(track_obj["album"])
            if track_obj["album"].get("images"):
                track.metadata["image"] = track_obj["album"]["images"][0]["url"]
        if track_obj.get("copyright"):
            track.metadata["copyright"] = track_obj["copyright"]
        if track_obj.get("explicit"):
            track.metadata["explicit"] = True
        if track_obj.get("external_urls"):
            track.metadata["spotify_url"] = track_obj["external_urls"]["spotify"]
        track.provider_ids.add(
            MediaItemProviderId(
                provider=PROV_ID,
                item_id=track_obj["id"],
                quality=TrackQuality.LOSSY_OGG,
                available=not track_obj["is_local"] and track_obj["is_playable"],
            )
        )
        return track

    async def _parse_playlist(self, playlist_obj):
        """Parse spotify playlist object to generic layout."""
        playlist = Playlist(item_id=playlist_obj["id"], provider=self.id)
        playlist.provider_ids.add(
            MediaItemProviderId(provider=PROV_ID, item_id=playlist_obj["id"])
        )
        playlist.name = playlist_obj["name"]
        playlist.owner = playlist_obj["owner"]["display_name"]
        playlist.is_editable = (
            playlist_obj["owner"]["id"] == self.sp_user["id"]
            or playlist_obj["collaborative"]
        )
        if playlist_obj.get("images"):
            playlist.metadata["image"] = playlist_obj["images"][0]["url"]
        if playlist_obj.get("external_urls"):
            playlist.metadata["spotify_url"] = playlist_obj["external_urls"]["spotify"]
        playlist.checksum = playlist_obj["snapshot_id"]
        return playlist

    async def get_token(self):
        """Get auth token on spotify."""
        # return existing token if we have one in memory
        if self.__auth_token and (
            self.__auth_token["expiresAt"] > int(time.time()) + 20
        ):
            return self.__auth_token
        tokeninfo = {}
        if not self._username or not self._password:
            return tokeninfo
        # retrieve token with spotty
        tokeninfo = await self._get_token()
        if tokeninfo:
            self.__auth_token = tokeninfo
            self.sp_user = await self._get_data("me")
            LOGGER.info("Succesfully logged in to Spotify as %s", self.sp_user["id"])
            self.__auth_token = tokeninfo
        else:
            LOGGER.error("Login failed for user %s", self._username)
        return tokeninfo

    async def _get_token(self):
        """Get spotify auth token with spotty bin."""
        # get token with spotty
        scopes = [
            "user-read-playback-state",
            "user-read-currently-playing",
            "user-modify-playback-state",
            "playlist-read-private",
            "playlist-read-collaborative",
            "playlist-modify-public",
            "playlist-modify-private",
            "user-follow-modify",
            "user-follow-read",
            "user-library-read",
            "user-library-modify",
            "user-read-private",
            "user-read-email",
            "user-read-birthdate",
            "user-top-read",
        ]
        scope = ",".join(scopes)
        args = [
            self.get_spotty_binary(),
            "-t",
            "--client-id",
            get_app_var(2),
            "--scope",
            scope,
            "-n",
            "temp-spotty",
            "-u",
            self._username,
            "-p",
            self._password,
            "-c",
            self.mass.config.data_path,
            "--disable-discovery",
        ]
        spotty = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        stdout, _ = await spotty.communicate()
        try:
            result = json.loads(stdout)
        except JSONDecodeError:
            LOGGER.warning("Error while retrieving Spotify token!")
            return None
        # transform token info to spotipy compatible format
        if result and "accessToken" in result:
            tokeninfo = result
            tokeninfo["expiresAt"] = tokeninfo["expiresIn"] + int(time.time())
            return tokeninfo
        return None

    async def _get_all_items(self, endpoint, params=None, key="items"):
        """Get all items from a paged list."""
        if not params:
            params = {}
        limit = 50
        offset = 0
        all_items = []
        while True:
            params["limit"] = limit
            params["offset"] = offset
            result = await self._get_data(endpoint, params=params)
            offset += limit
            if not result or key not in result or not result[key]:
                break
            all_items += result[key]
            if len(result[key]) < limit:
                break
        return all_items

    async def _get_data(self, endpoint, params=None):
        """Get data from api."""
        if not params:
            params = {}
        url = "https://api.spotify.com/v1/%s" % endpoint
        params["market"] = "from_token"
        params["country"] = "from_token"
        token = await self.get_token()
        if not token:
            return None
        headers = {"Authorization": "Bearer %s" % token["accessToken"]}
        async with self._throttler:
            async with self.mass.http_session.get(
                url, headers=headers, params=params, verify_ssl=False
            ) as response:
                result = await response.json()
                if not result or "error" in result:
                    LOGGER.error("%s - %s", endpoint, result)
                    result = None
                return result

    async def _delete_data(self, endpoint, params=None, data=None):
        """Delete data from api."""
        if not params:
            params = {}
        url = "https://api.spotify.com/v1/%s" % endpoint
        token = await self.get_token()
        if not token:
            return None
        headers = {"Authorization": "Bearer %s" % token["accessToken"]}
        async with self.mass.http_session.delete(
            url, headers=headers, params=params, json=data, verify_ssl=False
        ) as response:
            return await response.text()

    async def _put_data(self, endpoint, params=None, data=None):
        """Put data on api."""
        if not params:
            params = {}
        url = "https://api.spotify.com/v1/%s" % endpoint
        token = await self.get_token()
        if not token:
            return None
        headers = {"Authorization": "Bearer %s" % token["accessToken"]}
        async with self.mass.http_session.put(
            url, headers=headers, params=params, json=data, verify_ssl=False
        ) as response:
            return await response.text()

    async def _post_data(self, endpoint, params=None, data=None):
        """Post data on api."""
        if not params:
            params = {}
        url = "https://api.spotify.com/v1/%s" % endpoint
        token = await self.get_token()
        if not token:
            return None
        headers = {"Authorization": "Bearer %s" % token["accessToken"]}
        async with self.mass.http_session.post(
            url, headers=headers, params=params, json=data, verify_ssl=False
        ) as response:
            return await response.text()

    @staticmethod
    def get_spotty_binary():
        """Find the correct spotty binary belonging to the platform."""
        if platform.system() == "Windows":
            return os.path.join(
                os.path.dirname(__file__), "spotty", "windows", "spotty.exe"
            )
        if platform.system() == "Darwin":
            # macos binary is x86_64 intel
            return os.path.join(os.path.dirname(__file__), "spotty", "osx", "spotty")
        if platform.system() == "Linux":
            architecture = platform.machine()
            if architecture in ["AMD64", "x86_64"]:
                # generic linux x86_64 binary
                return os.path.join(
                    os.path.dirname(__file__), "spotty", "linux", "spotty-x86_64"
                )
            if "i386" in architecture:
                # i386 linux binary
                return os.path.join(
                    os.path.dirname(__file__), "spotty", "linux", "spotty-i386"
                )
            if "aarch64" in architecture or "armv8" in architecture:
                # arm64 linux binary
                return os.path.join(
                    os.path.dirname(__file__), "spotty", "linux", "spotty-aarch64"
                )
            # assume armv7
            return os.path.join(
                os.path.dirname(__file__), "spotty", "linux", "spotty-armhf"
            )
        return None
