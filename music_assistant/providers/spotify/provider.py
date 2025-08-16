"""Main Spotify provider implementation."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import (
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    MediaItemImage,
    MediaItemType,
    Playlist,
    ProviderMapping,
    SearchResults,
    Track,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.helpers.app_vars import app_var  # type: ignore[attr-defined]
from music_assistant.helpers.json import json_loads
from music_assistant.helpers.process import check_output
from music_assistant.helpers.throttle_retry import ThrottlerManager, throttle_with_retries
from music_assistant.helpers.util import lock
from music_assistant.models.music_provider import MusicProvider

from .constants import (
    CONF_CLIENT_ID,
    CONF_REFRESH_TOKEN,
    LIKED_SONGS_FAKE_PLAYLIST_ID_PREFIX,
)
from .helpers import get_librespot_binary
from .parsers import parse_album, parse_artist, parse_playlist, parse_track
from .streaming import LibrespotStreamer

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


class SpotifyProvider(MusicProvider):
    """Implementation of a Spotify MusicProvider."""

    _auth_info: str | None = None
    _sp_user: dict[str, Any] | None = None
    _librespot_bin: str | None = None
    custom_client_id_active: bool = False
    throttler: ThrottlerManager

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.cache_dir = os.path.join(self.mass.cache_path, self.instance_id)
        self.throttler = ThrottlerManager(rate_limit=1, period=2)
        self.streamer = LibrespotStreamer(self)
        if self.config.get_value(CONF_CLIENT_ID):
            # loosen the throttler a bit when a custom client id is used
            self.throttler = ThrottlerManager(rate_limit=45, period=30)
            self.custom_client_id_active = True
        # check if we have a librespot binary for this arch
        self._librespot_bin = await get_librespot_binary()
        # try login which will raise if it fails
        await self.login()

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        base = {
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
        }
        if not self.custom_client_id_active:
            # Spotify has killed the similar tracks api for developers
            # https://developer.spotify.com/blog/2024-11-27-changes-to-the-web-api
            base.add(ProviderFeature.SIMILAR_TRACKS)
        return base

    @property
    def instance_name_postfix(self) -> str | None:
        """Return a (default) instance name postfix for this provider instance."""
        if self._sp_user:
            return str(self._sp_user["display_name"])
        return None

    async def search(
        self, search_query: str, media_types: list[MediaType] | None = None, limit: int = 5
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
        offset = 0
        page_limit = min(limit, 50)
        while True:
            items_received = 0
            api_result = await self._get_data(
                "search", q=search_query, type=searchtype, limit=page_limit, offset=offset
            )
            if "artists" in api_result:
                searchresult.artists += [
                    parse_artist(item, self)
                    for item in api_result["artists"]["items"]
                    if (item and item["id"] and item["name"])
                ]
                items_received += len(api_result["artists"]["items"])
            if "albums" in api_result:
                searchresult.albums += [
                    parse_album(item, self)
                    for item in api_result["albums"]["items"]
                    if (item and item["id"])
                ]
                items_received += len(api_result["albums"]["items"])
            if "tracks" in api_result:
                searchresult.tracks += [
                    parse_track(item, self)
                    for item in api_result["tracks"]["items"]
                    if (item and item["id"])
                ]
                items_received += len(api_result["tracks"]["items"])
            if "playlists" in api_result:
                searchresult.playlists += [
                    parse_playlist(item, self)
                    for item in api_result["playlists"]["items"]
                    if (item and item["id"])
                ]
                items_received += len(api_result["playlists"]["items"])
            offset += page_limit
            if offset >= limit:
                break
            if items_received < page_limit:
                break
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
                    yield parse_artist(item, self)
            if spotify_artists["artists"]["next"]:
                endpoint = spotify_artists["artists"]["next"]
                endpoint = endpoint.replace("https://api.spotify.com/v1/", "")
            else:
                break

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve library albums from the provider."""
        async for item in self._get_all_items("me/albums"):
            if item["album"] and item["album"]["id"]:
                yield parse_album(item["album"], self)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from the provider."""
        async for item in self._get_all_items("me/tracks"):
            if item and item["track"]["id"]:
                yield parse_track(item["track"], self)

    def _get_liked_songs_playlist_id(self) -> str:
        return f"{LIKED_SONGS_FAKE_PLAYLIST_ID_PREFIX}-{self.instance_id}"

    async def _get_liked_songs_playlist(self) -> Playlist:
        liked_songs = Playlist(
            item_id=self._get_liked_songs_playlist_id(),
            provider=self.lookup_key,
            name=f"Liked Songs {self._sp_user['display_name']}",  # TODO to be translated
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
                provider=self.lookup_key,
                remotely_accessible=True,
            )
        ]

        liked_songs.cache_checksum = str(time.time())

        return liked_songs

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve playlists from the provider."""
        yield await self._get_liked_songs_playlist()
        async for item in self._get_all_items("me/playlists"):
            if item and item["id"]:
                yield parse_playlist(item, self)

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        artist_obj = await self._get_data(f"artists/{prov_artist_id}")
        return parse_artist(artist_obj, self)

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        album_obj = await self._get_data(f"albums/{prov_album_id}")
        return parse_album(album_obj, self)

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        track_obj = await self._get_data(f"tracks/{prov_track_id}")
        return parse_track(track_obj, self)

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        if prov_playlist_id == self._get_liked_songs_playlist_id():
            return await self._get_liked_songs_playlist()

        playlist_obj = await self._get_data(f"playlists/{prov_playlist_id}")
        return parse_playlist(playlist_obj, self)

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get all album tracks for given album id."""
        return [
            parse_track(item, self)
            async for item in self._get_all_items(f"albums/{prov_album_id}/tracks")
            if item["id"]
        ]

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get playlist tracks."""
        result: list[Track] = []
        uri = (
            "me/tracks"
            if prov_playlist_id == self._get_liked_songs_playlist_id()
            else f"playlists/{prov_playlist_id}/tracks"
        )
        page_size = 50
        offset = page * page_size
        spotify_result = await self._get_data(uri, limit=page_size, offset=offset)
        for index, item in enumerate(spotify_result["items"], 1):
            if not (item and item["track"] and item["track"]["id"]):
                continue
            # use count as position
            track = parse_track(item["track"], self)
            track.position = offset + index
            result.append(track)
        return result

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get a list of all albums for the given artist."""
        return [
            parse_album(item, self)
            async for item in self._get_all_items(
                f"artists/{prov_artist_id}/albums?include_groups=album,single,compilation"
            )
            if (item and item["id"])
        ]

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get a list of 10 most popular tracks for the given artist."""
        artist = await self.get_artist(prov_artist_id)
        endpoint = f"artists/{prov_artist_id}/top-tracks"
        items = await self._get_data(endpoint)
        return [
            parse_track(item, self, artist=artist)
            for item in items["tracks"]
            if (item and item["id"])
        ]

    async def library_add(self, item: MediaItemType) -> bool:
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

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
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

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
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
            uri = f"playlists/{prov_playlist_id}/tracks"
            spotify_result = await self._get_data(uri, limit=1, offset=pos - 1)
            for item in spotify_result["items"]:
                if not (item and item["track"] and item["track"]["id"]):
                    continue
                track_uris.append({"uri": f"spotify:track:{item['track']['id']}"})
        data = {"tracks": track_uris}
        await self._delete_data(f"playlists/{prov_playlist_id}/tracks", data=data)

    async def create_playlist(self, name: str) -> Playlist:
        """Create a new playlist on provider with given name."""
        data = {"name": name, "public": False}
        new_playlist = await self._post_data(f"users/{self._sp_user['id']}/playlists", data=data)
        self._fix_create_playlist_api_bug(new_playlist)
        return parse_playlist(new_playlist, self)

    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Retrieve a dynamic list of tracks based on the provided item."""
        endpoint = "recommendations"
        items = await self._get_data(endpoint, seed_tracks=prov_track_id, limit=limit)
        return [parse_track(item, self) for item in items["tracks"] if (item and item["id"])]

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        return StreamDetails(
            item_id=item_id,
            provider=self.lookup_key,
            audio_format=AudioFormat(
                content_type=ContentType.OGG,
            ),
            stream_type=StreamType.CUSTOM,
            allow_seek=True,
            can_seek=True,
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Return the audio stream for the provider item."""
        async for chunk in self.streamer.get_audio_stream(streamdetails, seek_position):
            yield chunk

    @lock
    async def login(self, force_refresh: bool = False) -> dict:
        """Log-in Spotify and return Auth/token info."""
        # return existing token if we have one in memory
        if (
            not force_refresh
            and self._auth_info
            and (self._auth_info["expires_at"] > (time.time() - 600))
        ):
            return self._auth_info
        # request new access token using the refresh token
        if not (refresh_token := self.config.get_value(CONF_REFRESH_TOKEN)):
            raise LoginFailed("Authentication required")

        client_id = self.config.get_value(CONF_CLIENT_ID) or app_var(2)
        params = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        }
        for _ in range(2):
            async with self.mass.http_session.post(
                "https://accounts.spotify.com/api/token", data=params
            ) as response:
                if response.status != 200:
                    err = await response.text()
                    if "revoked" in err:
                        err_msg = f"Failed to refresh access token: {err}"
                        # clear refresh token if it's invalid
                        self.update_config_value(CONF_REFRESH_TOKEN, None)
                        if self.available:
                            # If we're already loaded, we need to unload and set an error
                            self.unload_with_error(err_msg)
                        raise LoginFailed(err_msg)
                    # the token failed to refresh, we allow one retry
                    await asyncio.sleep(2)
                    continue
                # if we reached this point, the token has been successfully refreshed
                auth_info = await response.json()
                auth_info["expires_at"] = int(auth_info["expires_in"] + time.time())
                self.logger.debug("Successfully refreshed access token")
                break
        else:
            if self.available:
                self.mass.create_task(self.mass.unload_provider_with_error(self.instance_id))
            raise LoginFailed(f"Failed to refresh access token: {err}")

        # make sure that our updated creds get stored in memory + config
        self._auth_info = auth_info
        self.update_config_value(CONF_REFRESH_TOKEN, auth_info["refresh_token"], encrypted=True)
        # check if librespot still has valid auth
        args = [
            self._librespot_bin,
            "--cache",
            self.cache_dir,
            "--check-auth",
        ]
        ret_code, stdout = await check_output(*args)
        if ret_code != 0:
            # cached librespot creds are invalid, re-authenticate
            # we can use the check-token option to send a new token to librespot
            # librespot will then get its own token from spotify (somehow) and cache that.
            args += [
                "--access-token",
                auth_info["access_token"],
            ]
            ret_code, stdout = await check_output(*args)
            if ret_code != 0:
                # this should not happen, but guard it just in case
                err = stdout.decode("utf-8").strip()
                raise LoginFailed(f"Failed to verify credentials on Librespot: {err}")

        # get logged-in user info
        if not self._sp_user:
            self._sp_user = userinfo = await self._get_data("me", auth_info=auth_info)
            self.mass.metadata.set_default_preferred_language(userinfo["country"])
            self.logger.info("Successfully logged in to Spotify as %s", userinfo["display_name"])
        return auth_info

    async def _get_all_items(
        self, endpoint: str, key: str = "items", **kwargs: Any
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Get all items from a paged list."""
        limit = 50
        offset = 0
        while True:
            kwargs["limit"] = limit
            kwargs["offset"] = offset
            result = await self._get_data(endpoint, **kwargs)
            offset += limit
            if not result or key not in result or not result[key]:
                break
            for item in result[key]:
                yield item
            if len(result[key]) < limit:
                break

    @throttle_with_retries
    async def _get_data(self, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        """Get data from api."""
        url = f"https://api.spotify.com/v1/{endpoint}"
        kwargs["market"] = "from_token"
        kwargs["country"] = "from_token"
        if not (auth_info := kwargs.pop("auth_info", None)):
            auth_info = await self.login()
        headers = {"Authorization": f"Bearer {auth_info['access_token']}"}
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

            # handle token expired, raise ResourceTemporarilyUnavailable
            # so it will be retried (and the token refreshed)
            if response.status == 401:
                self._auth_info = None
                raise ResourceTemporarilyUnavailable("Token expired", backoff_time=0.05)

            # handle 404 not found, convert to MediaNotFoundError
            if response.status == 404:
                raise MediaNotFoundError(f"{endpoint} not found")
            response.raise_for_status()
            return await response.json(loads=json_loads)

    @throttle_with_retries
    async def _delete_data(self, endpoint: str, data: Any = None, **kwargs: Any) -> None:
        """Delete data from api."""
        url = f"https://api.spotify.com/v1/{endpoint}"
        auth_info = kwargs.pop("auth_info", await self.login())
        headers = {"Authorization": f"Bearer {auth_info['access_token']}"}
        async with self.mass.http_session.delete(
            url, headers=headers, params=kwargs, json=data, ssl=False
        ) as response:
            # handle spotify rate limiter
            if response.status == 429:
                backoff_time = int(response.headers["Retry-After"])
                raise ResourceTemporarilyUnavailable(
                    "Spotify Rate Limiter", backoff_time=backoff_time
                )
            # handle token expired, raise ResourceTemporarilyUnavailable
            # so it will be retried (and the token refreshed)
            if response.status == 401:
                self._auth_info = None
                raise ResourceTemporarilyUnavailable("Token expired", backoff_time=0.05)
            # handle temporary server error
            if response.status in (502, 503):
                raise ResourceTemporarilyUnavailable(backoff_time=30)
            response.raise_for_status()

    @throttle_with_retries
    async def _put_data(self, endpoint: str, data: Any = None, **kwargs: Any) -> None:
        """Put data on api."""
        url = f"https://api.spotify.com/v1/{endpoint}"
        auth_info = kwargs.pop("auth_info", await self.login())
        headers = {"Authorization": f"Bearer {auth_info['access_token']}"}
        async with self.mass.http_session.put(
            url, headers=headers, params=kwargs, json=data, ssl=False
        ) as response:
            # handle spotify rate limiter
            if response.status == 429:
                backoff_time = int(response.headers["Retry-After"])
                raise ResourceTemporarilyUnavailable(
                    "Spotify Rate Limiter", backoff_time=backoff_time
                )
            # handle token expired, raise ResourceTemporarilyUnavailable
            # so it will be retried (and the token refreshed)
            if response.status == 401:
                self._auth_info = None
                raise ResourceTemporarilyUnavailable("Token expired", backoff_time=0.05)

            # handle temporary server error
            if response.status in (502, 503):
                raise ResourceTemporarilyUnavailable(backoff_time=30)
            response.raise_for_status()

    @throttle_with_retries
    async def _post_data(self, endpoint: str, data: Any = None, **kwargs: Any) -> dict[str, Any]:
        """Post data on api."""
        url = f"https://api.spotify.com/v1/{endpoint}"
        auth_info = kwargs.pop("auth_info", await self.login())
        headers = {"Authorization": f"Bearer {auth_info['access_token']}"}
        async with self.mass.http_session.post(
            url, headers=headers, params=kwargs, json=data, ssl=False
        ) as response:
            # handle spotify rate limiter
            if response.status == 429:
                backoff_time = int(response.headers["Retry-After"])
                raise ResourceTemporarilyUnavailable(
                    "Spotify Rate Limiter", backoff_time=backoff_time
                )
            # handle token expired, raise ResourceTemporarilyUnavailable
            # so it will be retried (and the token refreshed)
            if response.status == 401:
                self._auth_info = None
                raise ResourceTemporarilyUnavailable("Token expired", backoff_time=0.05)
            # handle temporary server error
            if response.status in (502, 503):
                raise ResourceTemporarilyUnavailable(backoff_time=30)
            response.raise_for_status()
            return await response.json(loads=json_loads)

    def _fix_create_playlist_api_bug(self, playlist_obj: dict[str, Any]) -> None:
        """Fix spotify API bug where incorrect owner id is returned from Create Playlist."""
        if playlist_obj["owner"]["id"] != self._sp_user["id"]:
            playlist_obj["owner"]["id"] = self._sp_user["id"]
            playlist_obj["owner"]["display_name"] = self._sp_user["display_name"]
        else:
            self.logger.warning(
                "FIXME: Spotify have fixed their Create Playlist API, this fix can be removed."
            )
