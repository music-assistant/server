"""Spotify musicprovider support for MusicAssistant."""
from __future__ import annotations

import asyncio
import json
import os
import platform
import time
from json.decoder import JSONDecodeError
from typing import List, Optional

import aiohttp
from asyncio_throttle import Throttler
from music_assistant.helpers.app_vars import (  # noqa # pylint: disable=no-name-in-module
    get_app_var,
)
from music_assistant.helpers.util import parse_title_and_version
from music_assistant.models.errors import LoginFailed
from music_assistant.models.media_items import (
    Album,
    AlbumType,
    Artist,
    ContentType,
    MediaItemProviderId,
    MediaItemType,
    MediaQuality,
    MediaType,
    Playlist,
    StreamDetails,
    StreamType,
    Track,
)
from music_assistant.models.provider import MusicProvider


class SpotifyProvider(MusicProvider):
    """Implementation of a Spotify MusicProvider."""

    def __init__(self, username: str, password: str) -> None:
        """Initialize the Spotify provider."""
        self._attr_id = "spotify"
        self._attr_name = "Spotify"
        self._attr_supported_mediatypes = [
            MediaType.ARTIST,
            MediaType.ALBUM,
            MediaType.TRACK,
            MediaType.PLAYLIST
            # TODO: Return spotify radio
        ]
        self._username = username
        self._password = password
        self._auth_token = None
        self._sp_user = None
        self._throttler = Throttler(rate_limit=4, period=1)

    async def setup(self) -> None:
        """Handle async initialization of the provider."""
        if not self._username or not self._password:
            raise LoginFailed("Invalid login credentials")
        # try to get a token, raise if that fails
        token = await self.get_token()
        if not token:
            raise LoginFailed(f"Login failed for user {self._username}")

    async def search(
        self, search_query: str, media_types=Optional[List[MediaType]], limit: int = 5
    ) -> List[MediaItemType]:
        """
        Perform search on musicprovider.

            :param search_query: Search query.
            :param media_types: A list of media_types to include. All types if None.
            :param limit: Number of items to return in the search (per type).
        """
        result = []
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
        if searchresult := await self._get_data("search", params=params):
            if "artists" in searchresult:
                result += [
                    await self._parse_artist(item)
                    for item in searchresult["artists"]["items"]
                    if (item and item["id"])
                ]
            if "albums" in searchresult:
                result += [
                    await self._parse_album(item)
                    for item in searchresult["albums"]["items"]
                    if (item and item["id"])
                ]
            if "tracks" in searchresult:
                result += [
                    await self._parse_track(item)
                    for item in searchresult["tracks"]["items"]
                    if (item and item["id"])
                ]
            if "playlists" in searchresult:
                result += [
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

    async def get_artist(self, prov_artist_id) -> Artist:
        """Get full artist details by id."""
        artist_obj = await self._get_data(f"artists/{prov_artist_id}")
        return await self._parse_artist(artist_obj) if artist_obj else None

    async def get_album(self, prov_album_id) -> Album:
        """Get full album details by id."""
        album_obj = await self._get_data(f"albums/{prov_album_id}")
        return await self._parse_album(album_obj) if album_obj else None

    async def get_track(self, prov_track_id) -> Track:
        """Get full track details by id."""
        track_obj = await self._get_data(f"tracks/{prov_track_id}")
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
            track_uris.append(f"spotify:track:{track_id}")
        data = {"uris": track_uris}
        return await self._post_data(f"playlists/{prov_playlist_id}/tracks", data=data)

    async def remove_playlist_tracks(self, prov_playlist_id, prov_track_ids):
        """Remove track(s) from playlist."""
        track_uris = []
        for track_id in prov_track_ids:
            track_uris.append({"uri": f"spotify:track:{track_id}"})
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
        spotty_exec = f'{spotty} -n temp -c "/tmp" -b 320 --pass-through --single-track spotify://track:{track.item_id}'
        return StreamDetails(
            type=StreamType.EXECUTABLE,
            item_id=track.item_id,
            provider=self.id,
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
        artist.provider_ids.append(
            MediaItemProviderId(provider=self.id, item_id=artist_obj["id"])
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
        name, version = parse_title_and_version(album_obj["name"])
        album = Album(
            item_id=album_obj["id"], provider=self.id, name=name, version=version
        )
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
        album.provider_ids.append(
            MediaItemProviderId(
                provider=self.id,
                item_id=album_obj["id"],
                quality=MediaQuality.LOSSY_OGG,
            )
        )
        return album

    async def _parse_track(self, track_obj, artist=None):
        """Parse spotify track object to generic layout."""
        name, version = parse_title_and_version(track_obj["name"])
        track = Track(
            item_id=track_obj["id"],
            provider=self.id,
            name=name,
            version=version,
            duration=track_obj["duration_ms"] / 1000,
            disc_number=track_obj["disc_number"],
            track_number=track_obj["track_number"],
        )
        if artist:
            track.artists.append(artist)
        for track_artist in track_obj.get("artists", []):
            artist = await self._parse_artist(track_artist)
            if artist and artist.item_id not in {x.item_id for x in track.artists}:
                track.artists.append(artist)

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
        if track_obj.get("popularity"):
            track.metadata["popularity"] = track_obj["popularity"]
        track.provider_ids.append(
            MediaItemProviderId(
                provider=self.id,
                item_id=track_obj["id"],
                quality=MediaQuality.LOSSY_OGG,
                available=not track_obj["is_local"] and track_obj["is_playable"],
            )
        )
        return track

    async def _parse_playlist(self, playlist_obj):
        """Parse spotify playlist object to generic layout."""
        playlist = Playlist(
            item_id=playlist_obj["id"],
            provider=self.id,
            name=playlist_obj["name"],
            owner=playlist_obj["owner"]["display_name"],
        )
        playlist.provider_ids.append(
            MediaItemProviderId(provider=self.id, item_id=playlist_obj["id"])
        )
        playlist.is_editable = (
            playlist_obj["owner"]["id"] == self._sp_user["id"]
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
        if self._auth_token and (self._auth_token["expiresAt"] > int(time.time()) + 20):
            return self._auth_token
        tokeninfo = {}
        if not self._username or not self._password:
            return tokeninfo
        # retrieve token with spotty
        tokeninfo = await self._get_token()
        if tokeninfo:
            self._auth_token = tokeninfo
            self._sp_user = await self._get_data("me")
            self.logger.info(
                "Succesfully logged in to Spotify as %s", self._sp_user["id"]
            )
            self._auth_token = tokeninfo
        else:
            self.logger.error("Login failed for user %s", self._username)
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
            "/tmp",
            "--disable-discovery",
        ]
        spotty = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        stdout, _ = await spotty.communicate()
        try:
            result = json.loads(stdout)
        except JSONDecodeError:
            self.logger.warning("Error while retrieving Spotify token!")
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
        url = f"https://api.spotify.com/v1/{endpoint}"
        params["market"] = "from_token"
        params["country"] = "from_token"
        token = await self.get_token()
        if not token:
            return None
        headers = {"Authorization": f'Bearer {token["accessToken"]}'}
        async with self._throttler:
            async with self.mass.http_session.get(
                url, headers=headers, params=params, verify_ssl=False
            ) as response:
                try:
                    result = await response.json()
                    if "error" in result or (
                        "status" in result and "error" in result["status"]
                    ):
                        self.logger.error("%s - %s", endpoint, result)
                        return None
                except (
                    aiohttp.ContentTypeError,
                    JSONDecodeError,
                ) as err:
                    self.logger.error("%s - %s", endpoint, str(err))
                    return None
                return result

    async def _delete_data(self, endpoint, params=None, data=None):
        """Delete data from api."""
        if not params:
            params = {}
        url = f"https://api.spotify.com/v1/{endpoint}"
        token = await self.get_token()
        if not token:
            return None
        headers = {"Authorization": f'Bearer {token["accessToken"]}'}
        async with self.mass.http_session.delete(
            url, headers=headers, params=params, json=data, verify_ssl=False
        ) as response:
            return await response.text()

    async def _put_data(self, endpoint, params=None, data=None):
        """Put data on api."""
        if not params:
            params = {}
        url = f"https://api.spotify.com/v1/{endpoint}"
        token = await self.get_token()
        if not token:
            return None
        headers = {"Authorization": f'Bearer {token["accessToken"]}'}
        async with self.mass.http_session.put(
            url, headers=headers, params=params, json=data, verify_ssl=False
        ) as response:
            return await response.text()

    async def _post_data(self, endpoint, params=None, data=None):
        """Post data on api."""
        if not params:
            params = {}
        url = f"https://api.spotify.com/v1/{endpoint}"
        token = await self.get_token()
        if not token:
            return None
        headers = {"Authorization": f'Bearer {token["accessToken"]}'}
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
