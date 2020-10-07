"""Spotify musicprovider support for MusicAssistant."""
import asyncio
import logging
import os
import platform
import time
from typing import List, Optional

import orjson
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


async def async_setup(mass):
    """Perform async setup of this Plugin/Provider."""
    prov = SpotifyProvider()
    await mass.async_register_provider(prov)


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
            MediaType.Album,
            MediaType.Artist,
            MediaType.Playlist,
            # MediaType.Radio, # TODO!
            MediaType.Track,
        ]

    async def async_on_start(self) -> bool:
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
        token = await self.async_get_token()

        return token is not None

    async def async_search(
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
        if MediaType.Artist in media_types:
            searchtypes.append("artist")
        if MediaType.Album in media_types:
            searchtypes.append("album")
        if MediaType.Track in media_types:
            searchtypes.append("track")
        if MediaType.Playlist in media_types:
            searchtypes.append("playlist")
        searchtype = ",".join(searchtypes)
        params = {"q": search_query, "type": searchtype, "limit": limit}
        searchresult = await self.__async_get_data("search", params=params)
        if searchresult:
            if "artists" in searchresult:
                for item in searchresult["artists"]["items"]:
                    artist = await self.__async_parse_artist(item)
                    if artist:
                        result.artists.append(artist)
            if "albums" in searchresult:
                for item in searchresult["albums"]["items"]:
                    album = await self.__async_parse_album(item)
                    if album:
                        result.albums.append(album)
            if "tracks" in searchresult:
                for item in searchresult["tracks"]["items"]:
                    track = await self.__async_parse_track(item)
                    if track:
                        result.tracks.append(track)
            if "playlists" in searchresult:
                for item in searchresult["playlists"]["items"]:
                    playlist = await self.__async_parse_playlist(item)
                    if playlist:
                        result.playlists.append(playlist)
        return result

    async def async_get_library_artists(self) -> List[Artist]:
        """Retrieve library artists from spotify."""
        spotify_artists = await self.__async_get_data(
            "me/following?type=artist&limit=50"
        )
        if spotify_artists:
            # TODO: use cursor method to retrieve more than 50 artists
            for artist_obj in spotify_artists["artists"]["items"]:
                prov_artist = await self.__async_parse_artist(artist_obj)
                yield prov_artist

    async def async_get_library_albums(self) -> List[Album]:
        """Retrieve library albums from the provider."""
        async for item in self.__async_get_all_items("me/albums"):
            album = await self.__async_parse_album(item)
            if album:
                yield album

    async def async_get_library_tracks(self) -> List[Track]:
        """Retrieve library tracks from the provider."""
        async for item in self.__async_get_all_items("me/tracks"):
            track = await self.__async_parse_track(item)
            if track:
                yield track

    async def async_get_library_playlists(self) -> List[Playlist]:
        """Retrieve playlists from the provider."""
        async for item in self.__async_get_all_items("me/playlists"):
            playlist = await self.__async_parse_playlist(item)
            if playlist:
                yield playlist

    async def async_get_radios(self) -> List[Radio]:
        """Retrieve library/subscribed radio stations from the provider."""
        yield None  # TODO: Return spotify radio

    async def async_get_artist(self, prov_artist_id) -> Artist:
        """Get full artist details by id."""
        artist_obj = await self.__async_get_data("artists/%s" % prov_artist_id)
        return await self.__async_parse_artist(artist_obj)

    async def async_get_album(self, prov_album_id) -> Album:
        """Get full album details by id."""
        album_obj = await self.__async_get_data("albums/%s" % prov_album_id)
        return await self.__async_parse_album(album_obj)

    async def async_get_track(self, prov_track_id) -> Track:
        """Get full track details by id."""
        track_obj = await self.__async_get_data("tracks/%s" % prov_track_id)
        return await self.__async_parse_track(track_obj)

    async def async_get_playlist(self, prov_playlist_id) -> Playlist:
        """Get full playlist details by id."""
        playlist_obj = await self.__async_get_data(f"playlists/{prov_playlist_id}")
        return await self.__async_parse_playlist(playlist_obj)

    async def async_get_album_tracks(self, prov_album_id) -> List[Track]:
        """Get all album tracks for given album id."""
        endpoint = f"albums/{prov_album_id}/tracks"
        async for track_obj in self.__async_get_all_items(endpoint):
            track = await self.__async_parse_track(track_obj)
            if track:
                yield track

    async def async_get_playlist_tracks(self, prov_playlist_id) -> List[Track]:
        """Get all playlist tracks for given playlist id."""
        endpoint = f"playlists/{prov_playlist_id}/tracks"
        async for track_obj in self.__async_get_all_items(endpoint):
            playlist_track = await self.__async_parse_track(track_obj)
            if playlist_track:
                yield playlist_track
            else:
                LOGGER.warning(
                    "Unavailable track found in playlist %s: %s",
                    prov_playlist_id,
                    track_obj["track"]["name"],
                )

    async def async_get_artist_albums(self, prov_artist_id) -> List[Album]:
        """Get a list of all albums for the given artist."""
        params = {"include_groups": "album,single,compilation"}
        endpoint = f"artists/{prov_artist_id}/albums"
        async for item in self.__async_get_all_items(endpoint, params):
            album = await self.__async_parse_album(item)
            if album:
                yield album

    async def async_get_artist_toptracks(self, prov_artist_id) -> List[Track]:
        """Get a list of 10 most popular tracks for the given artist."""
        artist = await self.async_get_artist(prov_artist_id)
        endpoint = f"artists/{prov_artist_id}/top-tracks"
        items = await self.__async_get_data(endpoint)
        for item in items["tracks"]:
            track = await self.__async_parse_track(item)
            if track:
                track.artists = [artist]
                yield track

    async def async_library_add(self, prov_item_id, media_type: MediaType):
        """Add item to library."""
        result = False
        if media_type == MediaType.Artist:
            result = await self.__async_put_data(
                "me/following", {"ids": prov_item_id, "type": "artist"}
            )
        elif media_type == MediaType.Album:
            result = await self.__async_put_data("me/albums", {"ids": prov_item_id})
        elif media_type == MediaType.Track:
            result = await self.__async_put_data("me/tracks", {"ids": prov_item_id})
        elif media_type == MediaType.Playlist:
            result = await self.__async_put_data(
                f"playlists/{prov_item_id}/followers", data={"public": False}
            )
        return result

    async def async_library_remove(self, prov_item_id, media_type: MediaType):
        """Remove item from library."""
        result = False
        if media_type == MediaType.Artist:
            result = await self.__async_delete_data(
                "me/following", {"ids": prov_item_id, "type": "artist"}
            )
        elif media_type == MediaType.Album:
            result = await self.__async_delete_data("me/albums", {"ids": prov_item_id})
        elif media_type == MediaType.Track:
            result = await self.__async_delete_data("me/tracks", {"ids": prov_item_id})
        elif media_type == MediaType.Playlist:
            result = await self.__async_delete_data(
                f"playlists/{prov_item_id}/followers"
            )
        return result

    async def async_add_playlist_tracks(self, prov_playlist_id, prov_track_ids):
        """Add track(s) to playlist."""
        track_uris = []
        for track_id in prov_track_ids:
            track_uris.append("spotify:track:%s" % track_id)
        data = {"uris": track_uris}
        return await self.__async_post_data(
            f"playlists/{prov_playlist_id}/tracks", data=data
        )

    async def async_remove_playlist_tracks(self, prov_playlist_id, prov_track_ids):
        """Remove track(s) from playlist."""
        track_uris = []
        for track_id in prov_track_ids:
            track_uris.append({"uri": "spotify:track:%s" % track_id})
        data = {"tracks": track_uris}
        return await self.__async_delete_data(
            f"playlists/{prov_playlist_id}/tracks", data=data
        )

    async def async_get_stream_details(self, item_id: str) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        # make sure a valid track is requested.
        track = await self.async_get_track(item_id)
        if not track:
            return None
        # make sure that the token is still valid by just requesting it
        await self.async_get_token()
        spotty = self.get_spotty_binary()
        spotty_exec = '%s -n temp -c "%s" --pass-through --single-track %s' % (
            spotty,
            self.mass.config.data_path,
            track.item_id,
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

    async def __async_parse_artist(self, artist_obj):
        """Parse spotify artist object to generic layout."""
        if not artist_obj:
            return None
        artist = Artist()
        artist.item_id = artist_obj["id"]
        artist.provider = self.id
        artist.provider_ids.append(
            MediaItemProviderId(provider=PROV_ID, item_id=artist_obj["id"])
        )
        artist.name = artist_obj["name"]
        if "genres" in artist_obj:
            artist.tags = artist_obj["genres"]
        if artist_obj.get("images"):
            for img in artist_obj["images"]:
                img_url = img["url"]
                if "2a96cbd8b46e442fc41c2b86b821562f" not in img_url:
                    artist.metadata["image"] = img_url
                    break
        if artist_obj.get("external_urls"):
            artist.metadata["spotify_url"] = artist_obj["external_urls"]["spotify"]
        return artist

    async def __async_parse_album(self, album_obj):
        """Parse spotify album object to generic layout."""
        if not album_obj:
            return None
        if "album" in album_obj:
            album_obj = album_obj["album"]
        if not album_obj["id"] or not album_obj.get("is_playable", True):
            return None
        album = Album()
        album.item_id = album_obj["id"]
        album.provider = self.id
        album.name, album.version = parse_title_and_version(album_obj["name"])
        for artist in album_obj["artists"]:
            album.artist = await self.__async_parse_artist(artist)
            if album.artist:
                break
        if album_obj["album_type"] == "single":
            album.albumtype = AlbumType.Single
        elif album_obj["album_type"] == "compilation":
            album.albumtype = AlbumType.Compilation
        else:
            album.albumtype = AlbumType.Album
        if "genres" in album_obj:
            album.tags = album_obj["genres"]
        if album_obj.get("images"):
            album.metadata["image"] = album_obj["images"][0]["url"]
        if "external_ids" in album_obj:
            album.external_ids = album_obj["external_ids"]
        if "label" in album_obj:
            album.labels = album_obj["label"].split("/")
        if album_obj.get("release_date"):
            album.year = int(album_obj["release_date"].split("-")[0])
        if album_obj.get("copyrights"):
            album.metadata["copyright"] = album_obj["copyrights"][0]["text"]
        if album_obj.get("external_urls"):
            album.metadata["spotify_url"] = album_obj["external_urls"]["spotify"]
        if album_obj.get("explicit"):
            album.metadata["explicit"] = str(album_obj["explicit"]).lower()
        album.provider_ids.append(
            MediaItemProviderId(
                provider=PROV_ID,
                item_id=album_obj["id"],
                quality=TrackQuality.LOSSY_OGG,
            )
        )
        return album

    async def __async_parse_track(self, track_obj):
        """Parse spotify track object to generic layout."""
        if not track_obj:
            return None
        if "track" in track_obj:
            track_obj = track_obj["track"]
        if track_obj["is_local"] or not track_obj["id"] or not track_obj["is_playable"]:
            # do not return unavailable items
            return None
        track = Track()
        track.item_id = track_obj["id"]
        track.provider = self.id
        for track_artist in track_obj["artists"]:
            artist = await self.__async_parse_artist(track_artist)
            if artist:
                track.artists.append(artist)
        track.name, track.version = parse_title_and_version(track_obj["name"])
        track.duration = track_obj["duration_ms"] / 1000
        track.metadata["explicit"] = str(track_obj["explicit"]).lower()
        if "external_ids" in track_obj:
            track.external_ids = track_obj["external_ids"]
        if "album" in track_obj:
            track.album = await self.__async_parse_album(track_obj["album"])
        if track_obj.get("copyright"):
            track.metadata["copyright"] = track_obj["copyright"]
        if track_obj.get("explicit"):
            track.metadata["explicit"] = True
        track.disc_number = track_obj["disc_number"]
        track.track_number = track_obj["track_number"]
        if track_obj.get("external_urls"):
            track.metadata["spotify_url"] = track_obj["external_urls"]["spotify"]
        track.provider_ids.append(
            MediaItemProviderId(
                provider=PROV_ID,
                item_id=track_obj["id"],
                quality=TrackQuality.LOSSY_OGG,
            )
        )
        return track

    async def __async_parse_playlist(self, playlist_obj):
        """Parse spotify playlist object to generic layout."""

        if not playlist_obj.get("id"):
            return None
        playlist = Playlist()
        playlist.item_id = playlist_obj["id"]
        playlist.provider = self.id
        playlist.provider_ids.append(
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

    async def async_get_token(self):
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
        tokeninfo = await self.__async_get_token()
        if tokeninfo:
            self.__auth_token = tokeninfo
            self.sp_user = await self.__async_get_data("me")
            LOGGER.info("Succesfully logged in to Spotify as %s", self.sp_user["id"])
            self.__auth_token = tokeninfo
        else:
            raise Exception("Can't get Spotify token for user %s" % self._username)
        return tokeninfo

    async def __async_get_token(self):
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
            result = orjson.loads(stdout)
        except orjson.JSONDecodeError:
            LOGGER.warning("Error while retrieving Spotify token!")
            return None
        # transform token info to spotipy compatible format
        if result and "accessToken" in result:
            tokeninfo = result
            tokeninfo["expiresAt"] = tokeninfo["expiresIn"] + int(time.time())
            return tokeninfo
        return None

    async def __async_get_all_items(self, endpoint, params=None, key="items"):
        """Get all items from a paged list."""
        if not params:
            params = {}
        limit = 50
        offset = 0
        while True:
            params["limit"] = limit
            params["offset"] = offset
            result = await self.__async_get_data(endpoint, params=params)
            offset += limit
            if not result or key not in result or not result[key]:
                break
            for item in result[key]:
                yield item
            if len(result[key]) < limit:
                break

    async def __async_get_data(self, endpoint, params=None):
        """Get data from api."""
        if not params:
            params = {}
        url = "https://api.spotify.com/v1/%s" % endpoint
        params["market"] = "from_token"
        params["country"] = "from_token"
        token = await self.async_get_token()
        headers = {"Authorization": "Bearer %s" % token["accessToken"]}
        async with self._throttler:
            async with self.mass.http_session.get(
                url, headers=headers, params=params, verify_ssl=False
            ) as response:
                result = await response.json(loads=orjson.loads)
                if not result or "error" in result:
                    LOGGER.error("%s - %s", endpoint, result)
                    result = None
                return result

    async def __async_delete_data(self, endpoint, params=None, data=None):
        """Delete data from api."""
        if not params:
            params = {}
        url = "https://api.spotify.com/v1/%s" % endpoint
        token = await self.async_get_token()
        headers = {"Authorization": "Bearer %s" % token["accessToken"]}
        async with self.mass.http_session.delete(
            url, headers=headers, params=params, json=data, verify_ssl=False
        ) as response:
            return await response.text()

    async def __async_put_data(self, endpoint, params=None, data=None):
        """Put data on api."""
        if not params:
            params = {}
        url = "https://api.spotify.com/v1/%s" % endpoint
        token = await self.async_get_token()
        headers = {"Authorization": "Bearer %s" % token["accessToken"]}
        async with self.mass.http_session.put(
            url, headers=headers, params=params, json=data, verify_ssl=False
        ) as response:
            return await response.text()

    async def __async_post_data(self, endpoint, params=None, data=None):
        """Post data on api."""
        if not params:
            params = {}
        url = "https://api.spotify.com/v1/%s" % endpoint
        token = await self.async_get_token()
        headers = {"Authorization": "Bearer %s" % token["accessToken"]}
        async with self.mass.http_session.post(
            url, headers=headers, params=params, json=data, verify_ssl=False
        ) as response:
            return await response.text()

    @staticmethod
    def get_spotty_binary():
        """Find the correct spotty binary belonging to the platform."""
        sp_binary = None
        if platform.system() == "Windows":
            sp_binary = os.path.join(
                os.path.dirname(__file__), "spotty", "windows", "spotty.exe"
            )
        elif platform.system() == "Darwin":
            # macos binary is x86_64 intel
            sp_binary = os.path.join(
                os.path.dirname(__file__), "spotty", "darwin", "spotty"
            )
        elif platform.system() == "Linux":
            # try to find out the correct architecture by trial and error
            architecture = platform.machine()
            if architecture.startswith("AMD64") or architecture.startswith("x86_64"):
                # generic linux x86_64 binary
                sp_binary = os.path.join(
                    os.path.dirname(__file__), "spotty", "x86-linux", "spotty-x86_64"
                )
            else:
                sp_binary = os.path.join(
                    os.path.dirname(__file__), "spotty", "arm-linux", "spotty-muslhf"
                )
        return sp_binary
