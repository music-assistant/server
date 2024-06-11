"""Spotify musicprovider support for MusicAssistant."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import platform
import time
from json.decoder import JSONDecodeError
from tempfile import gettempdir
from typing import TYPE_CHECKING, Any

from music_assistant.common.helpers.json import json_loads
from music_assistant.common.helpers.util import parse_title_and_version
from music_assistant.common.models.config_entries import ConfigEntry, ConfigValueType
from music_assistant.common.models.enums import (
    ConfigEntryType,
    ExternalID,
    ProviderFeature,
    StreamType,
)
from music_assistant.common.models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ResourceTemporarilyUnavailable,
)
from music_assistant.common.models.media_items import (
    Album,
    AlbumType,
    Artist,
    AudioFormat,
    ContentType,
    ImageType,
    MediaItemImage,
    MediaItemType,
    MediaType,
    Playlist,
    ProviderMapping,
    SearchResults,
    Track,
)
from music_assistant.common.models.streamdetails import StreamDetails
from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME

# pylint: disable=no-name-in-module
from music_assistant.server.helpers.app_vars import app_var

# pylint: enable=no-name-in-module
from music_assistant.server.helpers.audio import get_chunksize
from music_assistant.server.helpers.process import AsyncProcess, check_output
from music_assistant.server.helpers.throttle_retry import ThrottlerManager, throttle_with_retries
from music_assistant.server.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType


CACHE_DIR = gettempdir()
LIKED_SONGS_FAKE_PLAYLIST_ID_PREFIX = "liked_songs"
SUPPORTED_FEATURES = (
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.LIBRARY_ARTISTS_EDIT,
    ProviderFeature.LIBRARY_ALBUMS_EDIT,
    ProviderFeature.LIBRARY_PLAYLISTS_EDIT,
    ProviderFeature.LIBRARY_TRACKS_EDIT,
    ProviderFeature.PLAYLIST_TRACKS_EDIT,
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.SIMILAR_TRACKS,
)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = SpotifyProvider(mass, manifest, config)
    await prov.handle_async_init()
    return prov


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # ruff: noqa: ARG001
    return (
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            label="Username",
            required=True,
        ),
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Password",
            required=True,
        ),
    )


class SpotifyProvider(MusicProvider):
    """Implementation of a Spotify MusicProvider."""

    _auth_token: str | None = None
    _sp_user: dict[str, Any] | None = None
    _librespot_bin: str | None = None
    # rate limiter needs to be specified on provider-level,
    # so make it an instance attribute
    throttler = ThrottlerManager(rate_limit=1, period=2)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._cache_dir = CACHE_DIR
        self._ap_workaround = False
        # try to get a token, raise if that fails
        self._cache_dir = os.path.join(CACHE_DIR, self.instance_id)
        # try login which will raise if it fails
        await self.login()

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return (
            ProviderFeature.LIBRARY_ARTISTS,
            ProviderFeature.LIBRARY_ALBUMS,
            ProviderFeature.LIBRARY_TRACKS,
            ProviderFeature.LIBRARY_PLAYLISTS,
            ProviderFeature.LIBRARY_ARTISTS_EDIT,
            ProviderFeature.LIBRARY_ALBUMS_EDIT,
            ProviderFeature.LIBRARY_PLAYLISTS_EDIT,
            ProviderFeature.LIBRARY_TRACKS_EDIT,
            ProviderFeature.PLAYLIST_TRACKS_EDIT,
            ProviderFeature.PLAYLIST_CREATE,
            ProviderFeature.BROWSE,
            ProviderFeature.SEARCH,
            ProviderFeature.ARTIST_ALBUMS,
            ProviderFeature.ARTIST_TOPTRACKS,
            ProviderFeature.SIMILAR_TRACKS,
        )

    async def search(
        self, search_query: str, media_types=list[MediaType], limit: int = 5
    ) -> SearchResults:
        """Perform search on musicprovider.

        :param search_query: Search query.
        :param media_types: A list of media_types to include.
        :param limit: Number of items to return in the search (per type).
        """
        searchresult = SearchResults()
        searchtypes = []
        if MediaType.ARTIST in media_types:
            searchtypes.append("artist")
        if MediaType.ALBUM in media_types:
            searchtypes.append("album")
        if MediaType.TRACK in media_types:
            searchtypes.append("track")
        if MediaType.PLAYLIST in media_types:
            searchtypes.append("playlist")
        if not searchtypes:
            return searchresult
        searchtype = ",".join(searchtypes)
        search_query = search_query.replace("'", "")
        api_result = await self._get_data("search", q=search_query, type=searchtype, limit=limit)
        if "artists" in api_result:
            searchresult.artists += [
                self._parse_artist(item)
                for item in api_result["artists"]["items"]
                if (item and item["id"] and item["name"])
            ]
        if "albums" in api_result:
            searchresult.albums += [
                self._parse_album(item)
                for item in api_result["albums"]["items"]
                if (item and item["id"])
            ]
        if "tracks" in api_result:
            searchresult.tracks += [
                self._parse_track(item)
                for item in api_result["tracks"]["items"]
                if (item and item["id"])
            ]
        if "playlists" in api_result:
            searchresult.playlists += [
                self._parse_playlist(item)
                for item in api_result["playlists"]["items"]
                if (item and item["id"])
            ]
        return searchresult

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve library artists from spotify."""
        endpoint = "me/following"
        while True:
            spotify_artists = await self._get_data(
                endpoint,
                type="artist",
                limit=50,
            )
            for item in spotify_artists["artists"]["items"]:
                if item and item["id"]:
                    yield self._parse_artist(item)
            if spotify_artists["artists"]["next"]:
                endpoint = spotify_artists["artists"]["next"]
                endpoint = endpoint.replace("https://api.spotify.com/v1/", "")
            else:
                break

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve library albums from the provider."""
        for item in await self._get_all_items("me/albums"):
            if item["album"] and item["album"]["id"]:
                yield self._parse_album(item["album"])

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from the provider."""
        for item in await self._get_all_items("me/tracks"):
            if item and item["track"]["id"]:
                yield self._parse_track(item["track"])

    def _get_liked_songs_playlist_id(self) -> str:
        return f"{LIKED_SONGS_FAKE_PLAYLIST_ID_PREFIX}-{self.instance_id}"

    async def _get_liked_songs_playlist(self) -> Playlist:
        liked_songs = Playlist(
            item_id=self._get_liked_songs_playlist_id(),
            provider=self.domain,
            name=f'Liked Songs {self._sp_user["display_name"]}',  # TODO to be translated
            owner=self._sp_user["display_name"],
            provider_mappings={
                ProviderMapping(
                    item_id=self._get_liked_songs_playlist_id(),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url="https://open.spotify.com/collection/tracks",
                )
            },
        )

        liked_songs.is_editable = False  # TODO Editing requires special endpoints

        liked_songs.metadata.images = [
            MediaItemImage(
                type=ImageType.THUMB,
                path="https://misc.scdn.co/liked-songs/liked-songs-64.png",
                provider=self.domain,
                remotely_accessible=True,
            )
        ]

        liked_songs.metadata.cache_checksum = str(time.time())

        return liked_songs

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve playlists from the provider."""
        yield await self._get_liked_songs_playlist()
        for item in await self._get_all_items("me/playlists"):
            if item and item["id"]:
                yield self._parse_playlist(item)

    async def get_artist(self, prov_artist_id) -> Artist:
        """Get full artist details by id."""
        artist_obj = await self._get_data(f"artists/{prov_artist_id}")
        return self._parse_artist(artist_obj)

    async def get_album(self, prov_album_id) -> Album:
        """Get full album details by id."""
        album_obj = await self._get_data(f"albums/{prov_album_id}")
        return self._parse_album(album_obj)

    async def get_track(self, prov_track_id) -> Track:
        """Get full track details by id."""
        track_obj = await self._get_data(f"tracks/{prov_track_id}")
        return self._parse_track(track_obj)

    async def get_playlist(self, prov_playlist_id) -> Playlist:
        """Get full playlist details by id."""
        if prov_playlist_id == self._get_liked_songs_playlist_id():
            return await self._get_liked_songs_playlist()

        playlist_obj = await self._get_data(f"playlists/{prov_playlist_id}")
        return self._parse_playlist(playlist_obj)

    async def get_album_tracks(self, prov_album_id) -> list[Track]:
        """Get all album tracks for given album id."""
        return [
            self._parse_track(item)
            for item in await self._get_all_items(f"albums/{prov_album_id}/tracks")
            if item["id"]
        ]

    async def get_playlist_tracks(
        self, prov_playlist_id: str, offset: int, limit: int
    ) -> list[Track]:
        """Get playlist tracks."""
        result: list[Track] = []
        uri = (
            "me/tracks"
            if prov_playlist_id == self._get_liked_songs_playlist_id()
            else f"playlists/{prov_playlist_id}/tracks"
        )
        spotify_result = await self._get_data(uri, limit=limit, offset=offset)
        for index, item in enumerate(spotify_result["items"], 1):
            if not (item and item["track"] and item["track"]["id"]):
                continue
            # use count as position
            track = self._parse_track(item["track"])
            track.position = offset + index
            result.append(track)
        return result

    async def get_artist_albums(self, prov_artist_id) -> list[Album]:
        """Get a list of all albums for the given artist."""
        return [
            self._parse_album(item)
            for item in await self._get_all_items(
                f"artists/{prov_artist_id}/albums?include_groups=album,single,compilation"
            )
            if (item and item["id"])
        ]

    async def get_artist_toptracks(self, prov_artist_id) -> list[Track]:
        """Get a list of 10 most popular tracks for the given artist."""
        artist = await self.get_artist(prov_artist_id)
        endpoint = f"artists/{prov_artist_id}/top-tracks"
        items = await self._get_data(endpoint)
        return [
            self._parse_track(item, artist=artist)
            for item in items["tracks"]
            if (item and item["id"])
        ]

    async def library_add(self, item: MediaItemType):
        """Add item to library."""
        if item.media_type == MediaType.ARTIST:
            await self._put_data("me/following", {"ids": [item.item_id]}, type="artist")
        elif item.media_type == MediaType.ALBUM:
            await self._put_data("me/albums", {"ids": [item.item_id]})
        elif item.media_type == MediaType.TRACK:
            await self._put_data("me/tracks", {"ids": [item.item_id]})
        elif item.media_type == MediaType.PLAYLIST:
            await self._put_data(f"playlists/{item.item_id}/followers", data={"public": False})
        return True

    async def library_remove(self, prov_item_id, media_type: MediaType):
        """Remove item from library."""
        if media_type == MediaType.ARTIST:
            await self._delete_data("me/following", {"ids": [prov_item_id]}, type="artist")
        elif media_type == MediaType.ALBUM:
            await self._delete_data("me/albums", {"ids": [prov_item_id]})
        elif media_type == MediaType.TRACK:
            await self._delete_data("me/tracks", {"ids": [prov_item_id]})
        elif media_type == MediaType.PLAYLIST:
            await self._delete_data(f"playlists/{prov_item_id}/followers")
        return True

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]):
        """Add track(s) to playlist."""
        track_uris = [f"spotify:track:{track_id}" for track_id in prov_track_ids]
        data = {"uris": track_uris}
        await self._post_data(f"playlists/{prov_playlist_id}/tracks", data=data)

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist."""
        track_uris = []
        for pos in positions_to_remove:
            for track in await self.get_playlist_tracks(prov_playlist_id, pos, pos):
                track_uris.append({"uri": f"spotify:track:{track.item_id}"})
        data = {"tracks": track_uris}
        await self._delete_data(f"playlists/{prov_playlist_id}/tracks", data=data)

    async def create_playlist(self, name: str) -> Playlist:
        """Create a new playlist on provider with given name."""
        data = {"name": name, "public": False}
        new_playlist = await self._post_data(f"users/{self._sp_user['id']}/playlists", data=data)
        self._fix_create_playlist_api_bug(new_playlist)
        return self._parse_playlist(new_playlist)

    async def get_similar_tracks(self, prov_track_id, limit=25) -> list[Track]:
        """Retrieve a dynamic list of tracks based on the provided item."""
        endpoint = "recommendations"
        items = await self._get_data(endpoint, seed_tracks=prov_track_id, limit=limit)
        return [self._parse_track(item) for item in items["tracks"] if (item and item["id"])]

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        return StreamDetails(
            item_id=item_id,
            provider=self.instance_id,
            audio_format=AudioFormat(
                content_type=ContentType.OGG,
            ),
            stream_type=StreamType.CUSTOM,
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Return the audio stream for the provider item."""
        # make sure that the token is still valid by just requesting it
        await self.login()
        librespot = await self.get_librespot_binary()
        args = [
            librespot,
            "-c",
            self._cache_dir,
            "--pass-through",
            "-b",
            "320",
            "--single-track",
            f"spotify://track:{streamdetails.item_id}",
        ]
        if seek_position:
            args += ["--start-position", str(int(seek_position))]
        if self._ap_workaround:
            args += ["--ap-port", "12345"]
        chunk_size = get_chunksize(streamdetails.audio_format)
        async with AsyncProcess(args, stdout=True, name="librespot") as librespot_proc:
            async for chunk in librespot_proc.iter_any(chunk_size):
                yield chunk

    def _parse_artist(self, artist_obj):
        """Parse spotify artist object to generic layout."""
        artist = Artist(
            item_id=artist_obj["id"],
            provider=self.domain,
            name=artist_obj["name"] or artist_obj["id"],
            provider_mappings={
                ProviderMapping(
                    item_id=artist_obj["id"],
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=artist_obj["external_urls"]["spotify"],
                )
            },
        )
        if "genres" in artist_obj:
            artist.metadata.genres = set(artist_obj["genres"])
        if artist_obj.get("images"):
            for img in artist_obj["images"]:
                img_url = img["url"]
                if "2a96cbd8b46e442fc41c2b86b821562f" not in img_url:
                    artist.metadata.images = [
                        MediaItemImage(
                            type=ImageType.THUMB,
                            path=img_url,
                            provider=self.instance_id,
                            remotely_accessible=True,
                        )
                    ]
                    break
        return artist

    def _parse_album(self, album_obj: dict):
        """Parse spotify album object to generic layout."""
        name, version = parse_title_and_version(album_obj["name"])
        album = Album(
            item_id=album_obj["id"],
            provider=self.domain,
            name=name,
            version=version,
            provider_mappings={
                ProviderMapping(
                    item_id=album_obj["id"],
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=AudioFormat(content_type=ContentType.OGG, bit_rate=320),
                    url=album_obj["external_urls"]["spotify"],
                )
            },
        )
        if "external_ids" in album_obj and album_obj["external_ids"].get("upc"):
            album.external_ids.add((ExternalID.BARCODE, "0" + album_obj["external_ids"]["upc"]))
        if "external_ids" in album_obj and album_obj["external_ids"].get("ean"):
            album.external_ids.add((ExternalID.BARCODE, album_obj["external_ids"]["ean"]))

        for artist_obj in album_obj["artists"]:
            if not artist_obj.get("name") or not artist_obj.get("id"):
                continue
            album.artists.append(self._parse_artist(artist_obj))

        with contextlib.suppress(ValueError):
            album.album_type = AlbumType(album_obj["album_type"])

        if "genres" in album_obj:
            album.metadata.genre = set(album_obj["genres"])
        if album_obj.get("images"):
            album.metadata.images = [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=album_obj["images"][0]["url"],
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            ]
        if "label" in album_obj:
            album.metadata.label = album_obj["label"]
        if album_obj.get("release_date"):
            album.year = int(album_obj["release_date"].split("-")[0])
        if album_obj.get("copyrights"):
            album.metadata.copyright = album_obj["copyrights"][0]["text"]
        if album_obj.get("explicit"):
            album.metadata.explicit = album_obj["explicit"]
        return album

    def _parse_track(
        self,
        track_obj: dict[str, Any],
        artist=None,
    ) -> Track:
        """Parse spotify track object to generic layout."""
        name, version = parse_title_and_version(track_obj["name"])
        track = Track(
            item_id=track_obj["id"],
            provider=self.domain,
            name=name,
            version=version,
            duration=track_obj["duration_ms"] / 1000,
            provider_mappings={
                ProviderMapping(
                    item_id=track_obj["id"],
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=AudioFormat(
                        content_type=ContentType.OGG,
                        bit_rate=320,
                    ),
                    url=track_obj["external_urls"]["spotify"],
                    available=not track_obj["is_local"] and track_obj["is_playable"],
                )
            },
            disc_number=track_obj.get("disc_number"),
            track_number=track_obj.get("track_number"),
        )
        if isrc := track_obj.get("external_ids", {}).get("isrc"):
            track.external_ids.add((ExternalID.ISRC, isrc))

        if artist:
            track.artists.append(artist)
        for track_artist in track_obj.get("artists", []):
            if not track_artist.get("name") or not track_artist.get("id"):
                continue
            artist = self._parse_artist(track_artist)
            if artist and artist.item_id not in {x.item_id for x in track.artists}:
                track.artists.append(artist)

        track.metadata.explicit = track_obj["explicit"]
        if "preview_url" in track_obj:
            track.metadata.preview = track_obj["preview_url"]
        if "album" in track_obj:
            track.album = self._parse_album(track_obj["album"])
            if track_obj["album"].get("images"):
                track.metadata.images = [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=track_obj["album"]["images"][0]["url"],
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                ]
        if track_obj.get("copyright"):
            track.metadata.copyright = track_obj["copyright"]
        if track_obj.get("explicit"):
            track.metadata.explicit = True
        if track_obj.get("popularity"):
            track.metadata.popularity = track_obj["popularity"]
        return track

    def _parse_playlist(self, playlist_obj):
        """Parse spotify playlist object to generic layout."""
        playlist = Playlist(
            item_id=playlist_obj["id"],
            provider=self.domain,
            name=playlist_obj["name"],
            owner=playlist_obj["owner"]["display_name"],
            provider_mappings={
                ProviderMapping(
                    item_id=playlist_obj["id"],
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=playlist_obj["external_urls"]["spotify"],
                )
            },
        )
        playlist.is_editable = (
            playlist_obj["owner"]["id"] == self._sp_user["id"] or playlist_obj["collaborative"]
        )
        if playlist_obj.get("images"):
            playlist.metadata.images = [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=playlist_obj["images"][0]["url"],
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            ]
        if playlist.owner is None:
            playlist.owner = self._sp_user["display_name"]
        playlist.metadata.cache_checksum = str(playlist_obj["snapshot_id"])
        return playlist

    async def login(self) -> dict:
        """Log-in Spotify and return tokeninfo."""
        # return existing token if we have one in memory
        if (
            self._auth_token
            and await asyncio.to_thread(os.path.isdir, self._cache_dir)
            and (self._auth_token["expiresAt"] > int(time.time()) + 600)
        ):
            return self._auth_token
        tokeninfo, userinfo = None, self._sp_user
        if not self.config.get_value(CONF_USERNAME) or not self.config.get_value(CONF_PASSWORD):
            msg = "Invalid login credentials"
            raise LoginFailed(msg)
        # retrieve token with librespot
        retries = 0
        while retries < 5:
            try:
                retries += 1
                if not tokeninfo:
                    async with asyncio.timeout(10):
                        tokeninfo = await self._get_token()
                if tokeninfo and not userinfo:
                    async with asyncio.timeout(10):
                        userinfo = await self._get_data("me", tokeninfo=tokeninfo)
                if tokeninfo and userinfo:
                    # we have all info we need!
                    break
                if retries > 2:
                    # switch to ap workaround after 2 retries
                    self._ap_workaround = True
            except TimeoutError:
                await asyncio.sleep(2)
        if tokeninfo and userinfo:
            self._auth_token = tokeninfo
            self._sp_user = userinfo
            self.mass.metadata.set_default_preferred_language(userinfo["country"])
            self.logger.info("Successfully logged in to Spotify as %s", userinfo["id"])
            self._auth_token = tokeninfo
            return tokeninfo
        if tokeninfo and not userinfo:
            msg = (
                "Unable to retrieve userdetails from Spotify API - "
                "probably just a temporary error"
            )
            raise LoginFailed(msg)
        if self.config.get_value(CONF_USERNAME).isnumeric():
            # a spotify free/basic account can be recognized when
            # the username consists of numbers only - check that here
            # an integer can be parsed of the username, this is a free account
            msg = "Only Spotify Premium accounts are supported"
            raise LoginFailed(msg)
        msg = f"Login failed for user {self.config.get_value(CONF_USERNAME)}"
        raise LoginFailed(msg)

    async def _get_token(self):
        """Get spotify auth token with librespot bin."""
        time_start = time.time()
        # authorize with username and password (NOTE: this can also be Spotify Connect)
        args = [
            await self.get_librespot_binary(),
            "-O",
            "-c",
            self._cache_dir,
            "-a",
            "-u",
            self.config.get_value(CONF_USERNAME),
            "-p",
            self.config.get_value(CONF_PASSWORD),
        ]
        if self._ap_workaround:
            args += ["--ap-port", "12345"]
        _returncode, output = await check_output(args)
        if _returncode == 0 and output.decode().strip() != "authorized":
            raise LoginFailed(f"Login failed for username {self.config.get_value(CONF_USERNAME)}")
        # get token with (authorized) librespot
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
            await self.get_librespot_binary(),
            "-O",
            "-t",
            "--client-id",
            app_var(2),
            "--scope",
            scope,
            "-c",
            self._cache_dir,
        ]
        if self._ap_workaround:
            args += ["--ap-port", "12345"]
        _returncode, output = await check_output(args)
        duration = round(time.time() - time_start, 2)
        try:
            result = json.loads(output)
        except JSONDecodeError:
            self.logger.warning(
                "Error while retrieving Spotify token after %s seconds, details: %s",
                duration,
                output.decode(),
            )
            return None
        self.logger.debug(
            "Retrieved Spotify token using librespot in %s seconds",
            duration,
        )
        # transform token info to spotipy compatible format
        if result and "accessToken" in result:
            tokeninfo = result
            tokeninfo["expiresAt"] = tokeninfo["expiresIn"] + int(time.time())
            return tokeninfo
        return None

    async def _get_all_items(self, endpoint, key="items", **kwargs) -> list[dict]:
        """Get all items from a paged list."""
        limit = 50
        offset = 0
        all_items = []
        while True:
            kwargs["limit"] = limit
            kwargs["offset"] = offset
            result = await self._get_data(endpoint, **kwargs)
            offset += limit
            if not result or key not in result or not result[key]:
                break
            all_items += result[key]
            if len(result[key]) < limit:
                break
        return all_items

    @throttle_with_retries
    async def _get_data(self, endpoint, **kwargs) -> dict[str, Any]:
        """Get data from api."""
        url = f"https://api.spotify.com/v1/{endpoint}"
        kwargs["market"] = "from_token"
        kwargs["country"] = "from_token"
        tokeninfo = kwargs.pop("tokeninfo", None)
        if tokeninfo is None:
            tokeninfo = await self.login()
        headers = {"Authorization": f'Bearer {tokeninfo["accessToken"]}'}
        locale = self.mass.metadata.locale.replace("_", "-")
        language = locale.split("-")[0]
        headers["Accept-Language"] = f"{locale}, {language};q=0.9, *;q=0.5"
        async with (
            self.mass.http_session.get(
                url, headers=headers, params=kwargs, ssl=True, timeout=120
            ) as response,
        ):
            # handle spotify rate limiter
            if response.status == 429:
                backoff_time = int(response.headers["Retry-After"])
                raise ResourceTemporarilyUnavailable(
                    "Spotify Rate Limiter", backoff_time=backoff_time
                )
            # handle temporary server error
            if response.status in (502, 503):
                raise ResourceTemporarilyUnavailable(backoff_time=30)

            # handle 404 not found, convert to MediaNotFoundError
            if response.status == 404:
                raise MediaNotFoundError(f"{endpoint} not found")
            response.raise_for_status()
            return await response.json(loads=json_loads)

    @throttle_with_retries
    async def _delete_data(self, endpoint, data=None, **kwargs) -> None:
        """Delete data from api."""
        url = f"https://api.spotify.com/v1/{endpoint}"
        token = await self.login()
        headers = {"Authorization": f'Bearer {token["accessToken"]}'}
        async with self.mass.http_session.delete(
            url, headers=headers, params=kwargs, json=data, ssl=False
        ) as response:
            # handle spotify rate limiter
            if response.status == 429:
                backoff_time = int(response.headers["Retry-After"])
                raise ResourceTemporarilyUnavailable(
                    "Spotify Rate Limiter", backoff_time=backoff_time
                )
            # handle temporary server error
            if response.status in (502, 503):
                raise ResourceTemporarilyUnavailable(backoff_time=30)
            response.raise_for_status()

    @throttle_with_retries
    async def _put_data(self, endpoint, data=None, **kwargs) -> None:
        """Put data on api."""
        url = f"https://api.spotify.com/v1/{endpoint}"
        token = await self.login()
        headers = {"Authorization": f'Bearer {token["accessToken"]}'}
        async with self.mass.http_session.put(
            url, headers=headers, params=kwargs, json=data, ssl=False
        ) as response:
            # handle spotify rate limiter
            if response.status == 429:
                backoff_time = int(response.headers["Retry-After"])
                raise ResourceTemporarilyUnavailable(
                    "Spotify Rate Limiter", backoff_time=backoff_time
                )
            # handle temporary server error
            if response.status in (502, 503):
                raise ResourceTemporarilyUnavailable(backoff_time=30)
            response.raise_for_status()

    @throttle_with_retries
    async def _post_data(self, endpoint, data=None, **kwargs) -> dict[str, Any]:
        """Post data on api."""
        url = f"https://api.spotify.com/v1/{endpoint}"
        token = await self.login()
        headers = {"Authorization": f'Bearer {token["accessToken"]}'}
        async with self.mass.http_session.post(
            url, headers=headers, params=kwargs, json=data, ssl=False
        ) as response:
            # handle spotify rate limiter
            if response.status == 429:
                backoff_time = int(response.headers["Retry-After"])
                raise ResourceTemporarilyUnavailable(
                    "Spotify Rate Limiter", backoff_time=backoff_time
                )
            # handle temporary server error
            if response.status in (502, 503):
                raise ResourceTemporarilyUnavailable(backoff_time=30)
            response.raise_for_status()
            return await response.json(loads=json_loads)

    async def get_librespot_binary(self):
        """Find the correct librespot binary belonging to the platform."""
        # ruff: noqa: SIM102
        if self._librespot_bin is not None:
            return self._librespot_bin

        async def check_librespot(librespot_path: str) -> str | None:
            try:
                returncode, output = await check_output([librespot_path, "--check"])
                if returncode == 0 and b"ok spotty" in output and b"using librespot" in output:
                    self._librespot_bin = librespot_path
                    return librespot_path
            except OSError:
                return None

        base_path = os.path.join(os.path.dirname(__file__), "bin")
        system = platform.system().lower()
        architecture = platform.machine().lower()

        if bridge_binary := await check_librespot(
            os.path.join(base_path, f"librespot-{system}-{architecture}")
        ):
            return bridge_binary

        msg = f"Unable to locate Librespot for {system}/{architecture}"
        raise RuntimeError(msg)

    def _fix_create_playlist_api_bug(self, playlist_obj: dict[str, Any]) -> None:
        """Fix spotify API bug where incorrect owner id is returned from Create Playlist."""
        if playlist_obj["owner"]["id"] != self._sp_user["id"]:
            playlist_obj["owner"]["id"] = self._sp_user["id"]
            playlist_obj["owner"]["display_name"] = self._sp_user["display_name"]
        else:
            self.logger.warning(
                "FIXME: Spotify have fixed their Create Playlist API, this fix can be removed."
            )
