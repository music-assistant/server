"""Qobuz musicprovider support for MusicAssistant."""
from __future__ import annotations

import datetime
import hashlib
import time
from json import JSONDecodeError
from typing import AsyncGenerator, List, Optional, Tuple

import aiohttp
from asyncio_throttle import Throttler

from music_assistant.helpers.app_vars import (  # pylint: disable=no-name-in-module
    app_var,
)
from music_assistant.helpers.util import parse_title_and_version, try_parse_int
from music_assistant.models.enums import MusicProviderFeature, ProviderType
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
    MediaQuality,
    MediaType,
    Playlist,
    StreamDetails,
    Track,
)
from music_assistant.models.music_provider import MusicProvider


class QobuzProvider(MusicProvider):
    """Provider for the Qobux music service."""

    _attr_type = ProviderType.QOBUZ
    _attr_name = "Qobuz"
    _user_auth_info = None
    _throttler = Throttler(rate_limit=4, period=1)

    @property
    def supported_features(self) -> Tuple[MusicProviderFeature]:
        """Return the features supported by this MusicProvider."""
        return (
            MusicProviderFeature.LIBRARY_ARTISTS,
            MusicProviderFeature.LIBRARY_ALBUMS,
            MusicProviderFeature.LIBRARY_TRACKS,
            MusicProviderFeature.LIBRARY_PLAYLISTS,
            MusicProviderFeature.LIBRARY_ARTISTS_EDIT,
            MusicProviderFeature.LIBRARY_ALBUMS_EDIT,
            MusicProviderFeature.LIBRARY_PLAYLISTS_EDIT,
            MusicProviderFeature.LIBRARY_TRACKS_EDIT,
            MusicProviderFeature.PLAYLIST_TRACKS_EDIT,
            MusicProviderFeature.BROWSE,
            MusicProviderFeature.SEARCH,
            MusicProviderFeature.ARTIST_ALBUMS,
            MusicProviderFeature.ARTIST_TOPTRACKS,
        )

    async def setup(self) -> bool:
        """Handle async initialization of the provider."""
        if not self.config.enabled:
            return False
        if not self.config.username or not self.config.password:
            raise LoginFailed("Invalid login credentials")
        # try to get a token, raise if that fails
        token = await self._auth_token()
        if not token:
            raise LoginFailed(f"Login failed for user {self.config.username}")
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
        result = []
        params = {"query": search_query, "limit": limit}
        if len(media_types) == 1:
            # qobuz does not support multiple searchtypes, falls back to all if no type given
            if media_types[0] == MediaType.ARTIST:
                params["type"] = "artists"
            if media_types[0] == MediaType.ALBUM:
                params["type"] = "albums"
            if media_types[0] == MediaType.TRACK:
                params["type"] = "tracks"
            if media_types[0] == MediaType.PLAYLIST:
                params["type"] = "playlists"
        if searchresult := await self._get_data("catalog/search", **params):
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

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve all library artists from Qobuz."""
        endpoint = "favorite/getUserFavorites"
        for item in await self._get_all_items(endpoint, key="artists", type="artists"):
            if item and item["id"]:
                yield await self._parse_artist(item)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve all library albums from Qobuz."""
        endpoint = "favorite/getUserFavorites"
        for item in await self._get_all_items(endpoint, key="albums", type="albums"):
            if item and item["id"]:
                yield await self._parse_album(item)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from Qobuz."""
        endpoint = "favorite/getUserFavorites"
        for item in await self._get_all_items(endpoint, key="tracks", type="tracks"):
            if item and item["id"]:
                yield await self._parse_track(item)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve all library playlists from the provider."""
        endpoint = "playlist/getUserPlaylists"
        for item in await self._get_all_items(endpoint, key="playlists"):
            if item and item["id"]:
                yield await self._parse_playlist(item)

    async def get_artist(self, prov_artist_id) -> Artist:
        """Get full artist details by id."""
        params = {"artist_id": prov_artist_id}
        artist_obj = await self._get_data("artist/get", **params)
        return (
            await self._parse_artist(artist_obj)
            if artist_obj and artist_obj["id"]
            else None
        )

    async def get_album(self, prov_album_id) -> Album:
        """Get full album details by id."""
        params = {"album_id": prov_album_id}
        album_obj = await self._get_data("album/get", **params)
        return (
            await self._parse_album(album_obj)
            if album_obj and album_obj["id"]
            else None
        )

    async def get_track(self, prov_track_id) -> Track:
        """Get full track details by id."""
        params = {"track_id": prov_track_id}
        track_obj = await self._get_data("track/get", **params)
        return (
            await self._parse_track(track_obj)
            if track_obj and track_obj["id"]
            else None
        )

    async def get_playlist(self, prov_playlist_id) -> Playlist:
        """Get full playlist details by id."""
        params = {"playlist_id": prov_playlist_id}
        playlist_obj = await self._get_data("playlist/get", **params)
        return (
            await self._parse_playlist(playlist_obj)
            if playlist_obj and playlist_obj["id"]
            else None
        )

    async def get_album_tracks(self, prov_album_id) -> List[Track]:
        """Get all album tracks for given album id."""
        params = {"album_id": prov_album_id}
        return [
            await self._parse_track(item)
            for item in await self._get_all_items("album/get", **params, key="tracks")
            if (item and item["id"])
        ]

    async def get_playlist_tracks(self, prov_playlist_id) -> List[Track]:
        """Get all playlist tracks for given playlist id."""
        count = 0
        result = []
        for item in await self._get_all_items(
            "playlist/get",
            key="tracks",
            playlist_id=prov_playlist_id,
            extra="tracks",
        ):
            if not (item and item["id"]):
                continue
            track = await self._parse_track(item)
            # use count as position
            track.position = count
            result.append(track)
            count += 1
        return result

    async def get_artist_albums(self, prov_artist_id) -> List[Album]:
        """Get a list of albums for the given artist."""
        endpoint = "artist/get"
        return [
            await self._parse_album(item)
            for item in await self._get_all_items(
                endpoint, key="albums", artist_id=prov_artist_id, extra="albums"
            )
            if (item and item["id"] and str(item["artist"]["id"]) == prov_artist_id)
        ]

    async def get_artist_toptracks(self, prov_artist_id) -> List[Track]:
        """Get a list of most popular tracks for the given artist."""
        result = await self._get_data(
            "artist/get",
            artist_id=prov_artist_id,
            extra="playlists",
            offset=0,
            limit=25,
        )
        if result and result["playlists"]:
            return [
                await self._parse_track(item)
                for item in result["playlists"][0]["tracks"]["items"]
                if (item and item["id"])
            ]
        # fallback to search
        artist = await self.get_artist(prov_artist_id)
        searchresult = await self._get_data(
            "catalog/search", query=artist.name, limit=25, type="tracks"
        )
        return [
            await self._parse_track(item)
            for item in searchresult["tracks"]["items"]
            if (
                item
                and item["id"]
                and "performer" in item
                and str(item["performer"]["id"]) == str(prov_artist_id)
            )
        ]

    async def get_similar_artists(self, prov_artist_id):
        """Get similar artists for given artist."""
        # https://www.qobuz.com/api.json/0.2/artist/getSimilarArtists?artist_id=220020&offset=0&limit=3

    async def library_add(self, prov_item_id, media_type: MediaType):
        """Add item to library."""
        result = None
        if media_type == MediaType.ARTIST:
            result = await self._get_data("favorite/create", artist_id=prov_item_id)
        elif media_type == MediaType.ALBUM:
            result = await self._get_data("favorite/create", album_ids=prov_item_id)
        elif media_type == MediaType.TRACK:
            result = await self._get_data("favorite/create", track_ids=prov_item_id)
        elif media_type == MediaType.PLAYLIST:
            result = await self._get_data(
                "playlist/subscribe", playlist_id=prov_item_id
            )
        return result

    async def library_remove(self, prov_item_id, media_type: MediaType):
        """Remove item from library."""
        result = None
        if media_type == MediaType.ARTIST:
            result = await self._get_data("favorite/delete", artist_ids=prov_item_id)
        elif media_type == MediaType.ALBUM:
            result = await self._get_data("favorite/delete", album_ids=prov_item_id)
        elif media_type == MediaType.TRACK:
            result = await self._get_data("favorite/delete", track_ids=prov_item_id)
        elif media_type == MediaType.PLAYLIST:
            playlist = await self.get_playlist(prov_item_id)
            if playlist.is_editable:
                result = await self._get_data(
                    "playlist/delete", playlist_id=prov_item_id
                )
            else:
                result = await self._get_data(
                    "playlist/unsubscribe", playlist_id=prov_item_id
                )
        return result

    async def add_playlist_tracks(
        self, prov_playlist_id: str, prov_track_ids: List[str]
    ) -> None:
        """Add track(s) to playlist."""
        return await self._get_data(
            "playlist/addTracks",
            playlist_id=prov_playlist_id,
            track_ids=",".join(prov_track_ids),
            playlist_track_ids=",".join(prov_track_ids),
        )

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: Tuple[int]
    ) -> None:
        """Remove track(s) from playlist."""
        playlist_track_ids = set()
        for track in await self.get_playlist_tracks(prov_playlist_id):
            if track.position in positions_to_remove:
                playlist_track_ids.add(str(track["playlist_track_id"]))
            if len(playlist_track_ids) == positions_to_remove:
                break
        return await self._get_data(
            "playlist/deleteTracks",
            playlist_id=prov_playlist_id,
            playlist_track_ids=",".join(playlist_track_ids),
        )

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        streamdata = None
        for format_id in [27, 7, 6, 5]:
            # it seems that simply requesting for highest available quality does not work
            # from time to time the api response is empty for this request ?!
            result = await self._get_data(
                "track/getFileUrl",
                sign_request=True,
                format_id=format_id,
                track_id=item_id,
                intent="stream",
            )
            if result and result.get("url"):
                streamdata = result
                break
        if not streamdata:
            raise MediaNotFoundError(f"Unable to retrieve stream details for {item_id}")
        if streamdata["mime_type"] == "audio/mpeg":
            content_type = ContentType.MPEG
        elif streamdata["mime_type"] == "audio/flac":
            content_type = ContentType.FLAC
        else:
            raise MediaNotFoundError(f"Unsupported mime type for {item_id}")
        # report playback started as soon as the streamdetails are requested
        self.mass.create_task(self._report_playback_started(streamdata))
        return StreamDetails(
            item_id=str(item_id),
            provider=self.type,
            content_type=content_type,
            duration=streamdata["duration"],
            sample_rate=int(streamdata["sampling_rate"] * 1000),
            bit_depth=streamdata["bit_depth"],
            data=streamdata,  # we need these details for reporting playback
            expires=time.time() + 1800,  # not sure about the real allowed value
            direct=streamdata["url"],
            callback=self._report_playback_stopped,
        )

    async def _report_playback_started(self, streamdata: dict) -> None:
        """Report playback start to qobuz."""
        # TODO: need to figure out if the streamed track is purchased by user
        # https://www.qobuz.com/api.json/0.2/purchase/getUserPurchasesIds?limit=5000&user_id=xxxxxxx
        # {"albums":{"total":0,"items":[]},"tracks":{"total":0,"items":[]},"user":{"id":xxxx,"login":"xxxxx"}}
        device_id = self._user_auth_info["user"]["device"]["id"]
        credential_id = self._user_auth_info["user"]["credential"]["id"]
        user_id = self._user_auth_info["user"]["id"]
        format_id = streamdata["format_id"]
        timestamp = int(time.time())
        events = [
            {
                "online": True,
                "sample": False,
                "intent": "stream",
                "device_id": device_id,
                "track_id": streamdata["track_id"],
                "purchase": False,
                "date": timestamp,
                "credential_id": credential_id,
                "user_id": user_id,
                "local": False,
                "format_id": format_id,
            }
        ]
        await self._post_data("track/reportStreamingStart", data=events)

    async def _report_playback_stopped(self, streamdetails: StreamDetails) -> None:
        """Report playback stop to qobuz."""
        user_id = self._user_auth_info["user"]["id"]
        await self._get_data(
            "/track/reportStreamingEnd",
            user_id=user_id,
            track_id=str(streamdetails.item_id),
            duration=try_parse_int(streamdetails.seconds_streamed),
        )

    async def _parse_artist(self, artist_obj: dict):
        """Parse qobuz artist object to generic layout."""
        artist = Artist(
            item_id=str(artist_obj["id"]), provider=self.type, name=artist_obj["name"]
        )
        artist.add_provider_id(
            MediaItemProviderId(
                item_id=str(artist_obj["id"]),
                prov_type=self.type,
                prov_id=self.id,
                url=artist_obj.get(
                    "url", f'https://open.qobuz.com/artist/{artist_obj["id"]}'
                ),
            )
        )
        if img := self.__get_image(artist_obj):
            artist.metadata.images = [MediaItemImage(ImageType.THUMB, img)]
        if artist_obj.get("biography"):
            artist.metadata.description = artist_obj["biography"].get("content")
        return artist

    async def _parse_album(self, album_obj: dict, artist_obj: dict = None):
        """Parse qobuz album object to generic layout."""
        if not artist_obj and "artist" not in album_obj:
            # artist missing in album info, return full abum instead
            return await self.get_album(album_obj["id"])
        name, version = parse_title_and_version(
            album_obj["title"], album_obj.get("version")
        )
        album = Album(
            item_id=str(album_obj["id"]), provider=self.type, name=name, version=version
        )
        if album_obj["maximum_sampling_rate"] > 192:
            quality = MediaQuality.LOSSLESS_HI_RES_4
        elif album_obj["maximum_sampling_rate"] > 96:
            quality = MediaQuality.LOSSLESS_HI_RES_3
        elif album_obj["maximum_sampling_rate"] > 48:
            quality = MediaQuality.LOSSLESS_HI_RES_2
        elif album_obj["maximum_bit_depth"] > 16:
            quality = MediaQuality.LOSSLESS_HI_RES_1
        elif album_obj.get("format_id", 0) == 5:
            quality = MediaQuality.LOSSY_AAC
        else:
            quality = MediaQuality.LOSSLESS
        album.add_provider_id(
            MediaItemProviderId(
                item_id=str(album_obj["id"]),
                prov_type=self.type,
                prov_id=self.id,
                quality=quality,
                url=album_obj.get(
                    "url", f'https://open.qobuz.com/album/{album_obj["id"]}'
                ),
                details=f'{album_obj["maximum_sampling_rate"]}kHz {album_obj["maximum_bit_depth"]}bit',
                available=album_obj["streamable"] and album_obj["displayable"],
            )
        )

        album.artist = await self._parse_artist(artist_obj or album_obj["artist"])
        if (
            album_obj.get("product_type", "") == "single"
            or album_obj.get("release_type", "") == "single"
        ):
            album.album_type = AlbumType.SINGLE
        elif (
            album_obj.get("product_type", "") == "compilation"
            or "Various" in album.artist.name
        ):
            album.album_type = AlbumType.COMPILATION
        elif (
            album_obj.get("product_type", "") == "album"
            or album_obj.get("release_type", "") == "album"
        ):
            album.album_type = AlbumType.ALBUM
        if "genre" in album_obj:
            album.metadata.genres = {album_obj["genre"]["name"]}
        if img := self.__get_image(album_obj):
            album.metadata.images = [MediaItemImage(ImageType.THUMB, img)]
        if len(album_obj["upc"]) == 13:
            # qobuz writes ean as upc ?!
            album.upc = album_obj["upc"][1:]
        else:
            album.upc = album_obj["upc"]
        if "label" in album_obj:
            album.metadata.label = album_obj["label"]["name"]
        if album_obj.get("released_at"):
            album.year = datetime.datetime.fromtimestamp(album_obj["released_at"]).year
        if album_obj.get("copyright"):
            album.metadata.copyright = album_obj["copyright"]
        if album_obj.get("description"):
            album.metadata.description = album_obj["description"]
        return album

    async def _parse_track(self, track_obj: dict):
        """Parse qobuz track object to generic layout."""
        name, version = parse_title_and_version(
            track_obj["title"], track_obj.get("version")
        )
        track = Track(
            item_id=str(track_obj["id"]),
            provider=self.type,
            name=name,
            version=version,
            disc_number=track_obj["media_number"],
            track_number=track_obj["track_number"],
            duration=track_obj["duration"],
            position=track_obj.get("position"),
        )
        if track_obj.get("performer") and "Various " not in track_obj["performer"]:
            artist = await self._parse_artist(track_obj["performer"])
            if artist:
                track.artists.append(artist)
        if not track.artists:
            # try to grab artist from album
            if (
                track_obj.get("album")
                and track_obj["album"].get("artist")
                and "Various " not in track_obj["album"]["artist"]
            ):
                artist = await self._parse_artist(track_obj["album"]["artist"])
                if artist:
                    track.artists.append(artist)
        if not track.artists:
            # last resort: parse from performers string
            for performer_str in track_obj["performers"].split(" - "):
                role = performer_str.split(", ")[1]
                name = performer_str.split(", ")[0]
                if "artist" in role.lower():
                    artist = Artist(name, self.type, name)
                track.artists.append(artist)
        # TODO: fix grabbing composer from details

        if "album" in track_obj:
            album = await self._parse_album(track_obj["album"])
            if album:
                track.album = album
        if track_obj.get("isrc"):
            track.isrc = track_obj["isrc"]
        if track_obj.get("performers"):
            track.metadata.performers = {
                x.strip() for x in track_obj["performers"].split("-")
            }
        if track_obj.get("copyright"):
            track.metadata.copyright = track_obj["copyright"]
        if track_obj.get("audio_info"):
            track.metadata.replaygain = track_obj["audio_info"]["replaygain_track_gain"]
        if track_obj.get("parental_warning"):
            track.metadata.explicit = True
        if img := self.__get_image(track_obj):
            track.metadata.images = [MediaItemImage(ImageType.THUMB, img)]
        # get track quality
        if track_obj["maximum_sampling_rate"] > 192:
            quality = MediaQuality.LOSSLESS_HI_RES_4
        elif track_obj["maximum_sampling_rate"] > 96:
            quality = MediaQuality.LOSSLESS_HI_RES_3
        elif track_obj["maximum_sampling_rate"] > 48:
            quality = MediaQuality.LOSSLESS_HI_RES_2
        elif track_obj["maximum_bit_depth"] > 16:
            quality = MediaQuality.LOSSLESS_HI_RES_1
        elif track_obj.get("format_id", 0) == 5:
            quality = MediaQuality.LOSSY_AAC
        else:
            quality = MediaQuality.LOSSLESS
        track.add_provider_id(
            MediaItemProviderId(
                item_id=str(track_obj["id"]),
                prov_type=self.type,
                prov_id=self.id,
                quality=quality,
                url=track_obj.get(
                    "url", f'https://open.qobuz.com/track/{track_obj["id"]}'
                ),
                details=f'{track_obj["maximum_sampling_rate"]}kHz {track_obj["maximum_bit_depth"]}bit',
                available=track_obj["streamable"] and track_obj["displayable"],
            )
        )
        return track

    async def _parse_playlist(self, playlist_obj):
        """Parse qobuz playlist object to generic layout."""
        playlist = Playlist(
            item_id=str(playlist_obj["id"]),
            provider=self.type,
            name=playlist_obj["name"],
            owner=playlist_obj["owner"]["name"],
        )
        playlist.add_provider_id(
            MediaItemProviderId(
                item_id=str(playlist_obj["id"]),
                prov_type=self.type,
                prov_id=self.id,
                url=playlist_obj.get(
                    "url", f'https://open.qobuz.com/playlist/{playlist_obj["id"]}'
                ),
            )
        )
        playlist.is_editable = (
            playlist_obj["owner"]["id"] == self._user_auth_info["user"]["id"]
            or playlist_obj["is_collaborative"]
        )
        if img := self.__get_image(playlist_obj):
            playlist.metadata.images = [MediaItemImage(ImageType.THUMB, img)]
        playlist.metadata.checksum = str(playlist_obj["updated_at"])
        return playlist

    async def _auth_token(self):
        """Login to qobuz and store the token."""
        if self._user_auth_info:
            return self._user_auth_info["user_auth_token"]
        params = {
            "username": self.config.username,
            "password": self.config.password,
            "device_manufacturer_id": "music_assistant",
        }
        details = await self._get_data("user/login", **params)
        if details and "user" in details:
            self._user_auth_info = details
            self.logger.info(
                "Succesfully logged in to Qobuz as %s", details["user"]["display_name"]
            )
            self.mass.metadata.preferred_language = details["user"]["country_code"]
            return details["user_auth_token"]

    async def _get_all_items(self, endpoint, key="tracks", **kwargs):
        """Get all items from a paged list."""
        limit = 50
        offset = 0
        all_items = []
        while True:
            kwargs["limit"] = limit
            kwargs["offset"] = offset
            result = await self._get_data(endpoint, **kwargs)
            offset += limit
            if not result:
                break
            if not result.get(key) or not result[key].get("items"):
                break
            for item in result[key]["items"]:
                item["position"] = len(all_items) + 1
                all_items.append(item)
            if len(result[key]["items"]) < limit:
                break
        return all_items

    async def _get_data(self, endpoint, sign_request=False, **kwargs):
        """Get data from api."""
        url = f"http://www.qobuz.com/api.json/0.2/{endpoint}"
        headers = {"X-App-Id": app_var(0)}
        if endpoint != "user/login":
            auth_token = await self._auth_token()
            if not auth_token:
                self.logger.debug("Not logged in")
                return None
            headers["X-User-Auth-Token"] = auth_token
        if sign_request:
            signing_data = "".join(endpoint.split("/"))
            keys = list(kwargs.keys())
            keys.sort()
            for key in keys:
                signing_data += f"{key}{kwargs[key]}"
            request_ts = str(time.time())
            request_sig = signing_data + request_ts + app_var(1)
            request_sig = str(hashlib.md5(request_sig.encode()).hexdigest())
            kwargs["request_ts"] = request_ts
            kwargs["request_sig"] = request_sig
            kwargs["app_id"] = app_var(0)
            kwargs["user_auth_token"] = await self._auth_token()
        async with self._throttler:
            async with self.mass.http_session.get(
                url, headers=headers, params=kwargs, verify_ssl=False
            ) as response:
                try:
                    # make sure status is 200
                    assert response.status == 200
                    result = await response.json()
                    # check for error in json
                    if error := result.get("error"):
                        raise ValueError(error)
                    if result.get("status") and "error" in result["status"]:
                        raise ValueError(result["status"])
                except (
                    aiohttp.ContentTypeError,
                    JSONDecodeError,
                    AssertionError,
                    ValueError,
                ) as err:
                    text = await response.text()
                    self.logger.exception(
                        "Error while processing %s: %s", endpoint, text, exc_info=err
                    )
                    return None
                return result

    async def _post_data(self, endpoint, params=None, data=None):
        """Post data to api."""
        if not params:
            params = {}
        if not data:
            data = {}
        url = f"http://www.qobuz.com/api.json/0.2/{endpoint}"
        params["app_id"] = app_var(0)
        params["user_auth_token"] = await self._auth_token()
        async with self.mass.http_session.post(
            url, params=params, json=data, verify_ssl=False
        ) as response:
            try:
                result = await response.json()
                # check for error in json
                if error := result.get("error"):
                    raise ValueError(error)
                if result.get("status") and "error" in result["status"]:
                    raise ValueError(result["status"])
            except (
                aiohttp.ContentTypeError,
                JSONDecodeError,
                AssertionError,
                ValueError,
            ) as err:
                text = await response.text()
                self.logger.exception(
                    "Error while processing %s: %s", endpoint, text, exc_info=err
                )
                return None
            return result

    def __get_image(self, obj: dict) -> Optional[str]:
        """Try to parse image from Qobuz media object."""
        if obj.get("image"):
            for key in ["extralarge", "large", "medium", "small"]:
                if obj["image"].get(key):
                    if "2a96cbd8b46e442fc41c2b86b821562f" in obj["image"][key]:
                        continue
                    return obj["image"][key]
        if obj.get("images300"):
            # playlists seem to use this strange format
            return obj["images300"][0]
        if obj.get("album"):
            return self.__get_image(obj["album"])
        if obj.get("artist"):
            return self.__get_image(obj["artist"])
        return None
