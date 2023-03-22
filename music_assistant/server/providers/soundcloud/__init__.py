"""Soundcloud support for MusicAssistant."""
import asyncio
from typing import AsyncGenerator, Callable  # noqa: UP035
from urllib.parse import unquote

from music_assistant.common.helpers.util import parse_title_and_version
from music_assistant.common.models.enums import ContentType, ImageType, MediaType, ProviderFeature
from music_assistant.common.models.errors import InvalidDataError, LoginFailed
from music_assistant.common.models.media_items import (  # ContentType,; ImageType,; MediaType,
    Album,
    AlbumType,
    Artist,
    MediaItemImage,
    MediaItemType,
    Playlist,
    ProviderMapping,
    StreamDetails,
    Track,
)
from music_assistant.server.models.music_provider import MusicProvider

from .soundcloudpy.asyncsoundcloudpy import SoundcloudAsync

CONF_CLIENT_ID = "client_id"
CONF_AUTHORIZATION = "authorization"

SUPPORTED_FEATURES = (
    ProviderFeature.LIBRARY_ARTISTS,
    # ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.SIMILAR_TRACKS,
)


class SoundcloudMusicProvider(MusicProvider):
    """Provider for Soundcloud."""

    _headers = None
    _context = None
    _cookies = None
    _signature_timestamp = 0
    _cipher = None
    _user_id = None
    _soundcloud = None
    _me = None

    async def setup(self) -> None:
        """Set up the Soundcloud provider."""
        if not self.config.get_value(CONF_CLIENT_ID) or not self.config.get_value(
            CONF_AUTHORIZATION
        ):
            raise LoginFailed("Invalid login credentials")

        client_id = self.config.get_value(CONF_CLIENT_ID)
        auth_token = self.config.get_value(CONF_AUTHORIZATION)

        async with SoundcloudAsync(auth_token, client_id) as account:
            await account.login()

        self._soundcloud = account
        self._me = await account.get_account_details()
        self._user_id = self._me["id"]

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return SUPPORTED_FEATURES

    @classmethod
    async def _run_async(cls, call: Callable, *args, **kwargs):
        return await asyncio.to_thread(call, *args, **kwargs)

    async def search(
        self, search_query: str, media_types=list[MediaType] | None, limit: int = 5
    ) -> list[MediaItemType]:
        """Perform search on musicprovider.

        :param search_query: Search query.
        :param media_types: A list of media_types to include. All types if None.
        :param limit: Number of items to return in the search (per type).
        """
        if not media_types:
            media_types = [MediaType.TRACK]

        tasks = [
            self._soundcloud.search_tracks(search_query, limit)
            for media_type in media_types
            if media_type == MediaType.TRACK
        ]
        search_results = await asyncio.gather(*tasks)

        results = []
        for item in search_results[0]["collection"]:
            track = await self._soundcloud.get_track_details(item["id"])
            # track = await self.get_track(item["id"])
            try:
                await self._parse_track(track[0])
            except (KeyError, TypeError, InvalidDataError, IndexError) as error:
                self.logger.debug("Search-parse_track")
                self.logger.debug(search_query)
                self.logger.debug(search_results)
                self.logger.debug(track)
                self.logger.debug(error)
                continue
            results.append(track)

        return results

    # async def get_library_albums(self) -> AsyncGenerator[Album, None]:
    #     """Retrieve library albums from Soundcloud."""
    #     albums =
    #     self.logger.debug(albums["collection"])
    #     for item in albums["collection"]:
    #         # album = await self._soundcloud.get_track_details(item)
    #         album =
    #         self.logger.debug(type(album))
    #         try:
    #             yield await self._parse_album(album[0])
    #         except (KeyError, TypeError, InvalidDataError, IndexError) as error:
    #             self.logger.debug("get_library_albums")
    #             self.logger.debug(album)
    #             self.logger.debug(error)
    #             continue

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve all library artists from Soundcloud."""
        following = await self._soundcloud.get_following(self._user_id)
        for artist in following["collection"]:
            try:
                yield await self._parse_artist(artist)
            except (KeyError, TypeError, InvalidDataError, IndexError) as error:
                self.logger.debug("get_library_artists")
                self.logger.debug(artist)
                self.logger.debug(error)
                continue

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve all library playlists from Soundcloud."""
        playlists = await self._soundcloud.get_account_playlists()
        for item in playlists["collection"]:
            if "playlist" in item:
                if item and (item["playlist"]["title"] or item["system_playlist"]["title"]):
                    try:
                        yield await self._parse_playlist(item)
                    except (KeyError, TypeError, InvalidDataError, IndexError) as error:
                        self.logger.debug("get_library_playlists-playlist")
                        self.logger.debug(item)
                        self.logger.debug(error)
                        continue
            elif "system_playlist" in item and item and item["system_playlist"]["title"]:
                try:
                    yield await self._parse_playlist(item)
                except (KeyError, TypeError, InvalidDataError, IndexError) as error:
                    self.logger.debug("get_library_playlists-system_playlist")
                    self.logger.debug(item)
                    self.logger.debug(error)
                    continue

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from Soundcloud."""
        tracks = await self._soundcloud.get_tracks_liked()
        # self.logger.debug(tracks["collection"])
        for item in tracks["collection"]:
            track = await self._soundcloud.get_track_details(item)
            # track = await self.get_track(item)
            # self.logger.debug(type(track))
            try:
                yield await self._parse_track(track[0])
            except IndexError:
                continue
            except (KeyError, TypeError, InvalidDataError) as error:
                self.logger.debug("get_library_tracks")
                self.logger.debug(track)
                self.logger.debug(error)
                continue

    async def get_album(self, prov_album_id) -> Album:
        """Get full album details by id."""
        album_obj = await get_album(prov_album_id=prov_album_id)
        return (
            await self._parse_album(album_obj=album_obj, album_id=prov_album_id)
            if album_obj
            else None
        )

    # async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
    #     """Get album tracks for given album id."""
    #     album_obj = await get_album(prov_album_id=prov_album_id)
    #     if not album_obj.get("tracks"):
    #         return []
    #     tracks = []
    #     for idx, track_obj in enumerate(album_obj["tracks"], 1):
    #         track = await self._parse_track(track_obj=track_obj)
    #         track.di_soundcloud_number = 0
    #         track.track_number = idx
    #         tracks.append(track)
    #     return tracks

    async def get_artist(self, prov_artist_id) -> Artist:
        """Get full artist details by id."""
        artist_obj = await self._soundcloud.get_user_details(user_id=prov_artist_id)
        try:
            artist = await self._parse_artist(artist_obj=artist_obj) if artist_obj else None
        except (KeyError, TypeError, InvalidDataError, IndexError) as error:
            self.logger.debug("get_artist")
            self.logger.debug(prov_artist_id)
            self.logger.debug(artist_obj)
            self.logger.debug(error)
        return artist

    async def get_track(self, prov_track_id) -> Track:
        """Get full track details by id."""
        track_obj = await self._soundcloud.get_track_details(track_id=prov_track_id)
        try:
            track = await self._parse_track(track_obj[0])
        except (KeyError, TypeError, InvalidDataError, IndexError) as error:
            self.logger.debug("get_track")
            self.logger.debug(prov_track_id)
            self.logger.debug(track_obj)
            self.logger.debug(error)
        return track

    async def get_playlist(self, prov_playlist_id) -> Playlist:
        """Get full playlist details by id."""
        playlist_obj = await self._soundcloud.get_playlist_details(playlist_id=prov_playlist_id)
        try:
            playlist = await self._parse_playlist(playlist_obj)
        except (KeyError, TypeError, InvalidDataError, IndexError) as error:
            self.logger.debug("get_playlist")
            self.logger.debug(prov_playlist_id)
            self.logger.debug(playlist_obj)
            self.logger.debug(error)
        return playlist

    async def get_playlist_tracks(self, prov_playlist_id) -> list[Track]:
        """Get all playlist tracks for given playlist id."""
        playlist_obj = await self._soundcloud.get_playlist_details(playlist_id=prov_playlist_id)
        if "tracks" not in playlist_obj:
            return []
        tracks = []
        for index, track in enumerate(playlist_obj["tracks"]):
            song = await self._soundcloud.get_track_details(track["id"])
            # song = await self.get_track(track["id"])
            try:
                track = await self._parse_track(song[0])
                if track:
                    track.position = index
                    tracks.append(track)
            except (KeyError, TypeError, InvalidDataError, IndexError) as error:
                self.logger.debug("get_playlist_tracks")
                self.logger.debug(track)
                self.logger.debug(error)
                continue
        return tracks

    async def get_artist_albums(self, prov_artist_id) -> list[Album]:
        """Get a list of albums for the given artist."""
        album_obj = await self._soundcloud.get_album_from_user(user_id=prov_artist_id, limit=25)
        albums = []
        for album in album_obj["collection"]:
            try:
                _album = await self._parse_album(album[0])
                albums.append(_album)
            except (KeyError, TypeError, InvalidDataError, IndexError) as error:
                self.logger.debug("get_artist_albums")
                self.logger.debug(album)
                self.logger.debug(error)
                continue
        return albums

    async def get_artist_toptracks(self, prov_artist_id) -> list[Track]:
        """Get a list of 25 most popular tracks for the given artist."""
        tracks_obj = await self._soundcloud.get_popular_tracks_user(
            user_id=prov_artist_id, limit=25
        )
        tracks = []
        for track in tracks_obj["collection"]:
            song = await self._soundcloud.get_track_details(track["id"])
            # song = await self.get_track(track["id"])
            try:
                track = await self._parse_track(song[0])
                tracks.append(track)
            except (KeyError, TypeError, InvalidDataError, IndexError) as error:
                self.logger.debug("get_artist_toptracks")
                self.logger.debug(track)
                self.logger.debug(error)
                continue
        return tracks

    # async def library_add(self, prov_item_id, media_type: MediaType) -> None:
    #     """Add an item to the library."""

    # async def library_remove(self, prov_item_id, media_type: MediaType):
    #     """Remove an item from the library."""

    # async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
    #     """Add track(s) to playlist."""

    # async def remove_playlist_tracks(
    #     self, prov_playlist_id: str, positions_to_remove: tuple[int]
    # ) -> None:
    #     """Remove track(s) from playlist."""

    async def get_similar_tracks(self, prov_track_id, limit=25) -> list[Track]:
        """Retrieve a dynamic list of tracks based on the provided item."""
        tracks_obj = await self._soundcloud.get_recommended(track_id=prov_track_id, limit=25)
        tracks = []
        for track in tracks_obj["collection"]:
            song = await self._soundcloud.get_track_details(track["id"])
            # song = await self.get_track(track["id"])
            try:
                track = await self._parse_track(song[0])
                tracks.append(track)
            except (KeyError, TypeError, InvalidDataError, IndexError) as error:
                self.logger.debug("get_similar_tracks")
                self.logger.debug(track)
                self.logger.debug(error)
                continue

        return tracks

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        track_details = await self._soundcloud.get_track_details(track_id=item_id)
        # track_details = await self.get_track(item_id)
        stream_format = track_details[0]["media"]["transcodings"][0]["format"]["mime_type"]
        url = await self._soundcloud.get_stream_url(track_id=item_id)
        return StreamDetails(
            provider=self.domain,
            item_id=item_id,
            content_type=ContentType.try_parse(stream_format),
            direct=url,
        )

    async def _parse_album(self, album_obj: dict, album_id: str = None) -> Album:
        """Parse a Soundcloud Album response to an Album model object."""
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
        # if "thumbnails" in album_obj:
        #     album.metadata.images = await self._parse_thumbnails(album_obj["thumbnails"])
        if "description" in album_obj:
            album.metadata.description = unquote(album_obj["description"])
        if "artists" in album_obj:
            album.artists = [
                await self._parse_artist(artist)
                for artist in album_obj["artists"]
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
        """Parse a Soundcloud user response to Artist model object."""
        artist_id = None
        permalink = artist_obj["permalink"]
        if "id" in artist_obj and artist_obj["id"]:
            artist_id = artist_obj["id"]
        if not artist_id:
            raise InvalidDataError("Artist does not have a valid ID")
        artist = Artist(item_id=artist_id, name=artist_obj["username"], provider=self.domain)
        if "description" in artist_obj:
            artist.metadata.description = artist_obj["description"]
        if artist_obj.get("avatar_url"):
            img_url = artist_obj["avatar_url"]
            if "2a96cbd8b46e442fc41c2b86b821562f" not in img_url:
                artist.metadata.images = [MediaItemImage(ImageType.THUMB, img_url)]
        # if "avatar_url" in artist_obj and artist_obj["avatar_url"]:
        #     artist.metadata.images = await self._parse_thumbnails(artist_obj["avatar_url"])
        artist.add_provider_mapping(
            ProviderMapping(
                item_id=str(artist_id),
                provider_domain=self.domain,
                provider_instance=self.instance_id,
                url=f"https://soundcloud.com/{permalink}",
            )
        )
        return artist

    async def _parse_playlist(self, playlist_obj: dict) -> Playlist:
        """Parse a Soundcloud Playlist response to a Playlist object."""
        # if "avatar_url" in playlist_obj and playlist_obj["artwork_url"]:
        #     playlist.metadata.images = await self._parse_thumbnails(playlist_obj["artwork_url"])
        is_editable = False
        if "playlist" not in playlist_obj:
            playlist = Playlist(
                item_id=playlist_obj["id"],
                provider=self.domain,
                name=playlist_obj["title"],
            )
            if "description" in playlist_obj:
                playlist.metadata.description = playlist_obj["description"]
            # if playlist_obj.get("sharing") and playlist_obj.get("sharing") == "private":
            #     is_editable = True
            playlist.is_editable = is_editable
            playlist.add_provider_mapping(
                ProviderMapping(
                    item_id=playlist_obj["id"],
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            )
        else:
            playlist = Playlist(
                item_id=playlist_obj["playlist"]["id"],
                provider=self.domain,
                name=playlist_obj["playlist"]["title"],
            )
            if "description" in playlist_obj["playlist"]:
                playlist.metadata.description = playlist_obj["playlist"]["description"]
            # if playlist_obj.get("sharing") and playlist_obj.get("sharing") == "private":
            #     is_editable = True
            playlist.is_editable = is_editable
            playlist.add_provider_mapping(
                ProviderMapping(
                    item_id=playlist_obj["playlist"]["id"],
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            )
        playlist.metadata.checksum = playlist_obj.get("checksum")
        return playlist

    async def _parse_track(self, track_obj: dict) -> Track:
        """Parse a Soundcloud Track response to a Track model object."""
        name, version = parse_title_and_version(track_obj["title"])
        track = Track(
            item_id=track_obj["id"],
            provider=self.domain,
            name=name,
            version=version,
            duration=track_obj["duration"] / 1000,
        )
        user_id = track_obj["user"]["id"]
        user = await self._soundcloud.get_user_details(user_id)
        artist = await self._parse_artist(user)
        if artist and artist.item_id not in {x.item_id for x in track.artists}:
            track.artists.append(artist)
        # track = Track(item_id=track_obj["id"], provider=self.domain, name=track_obj["title"])

        # track.metadata.explicit = track_obj["explicit"]
        if "artwork_url" in track_obj:
            track.metadata.preview = track_obj["artwork_url"]
        if "album" in track_obj:
            track.album = await self._parse_album(track_obj["album"])
            if track_obj["album"].get("images"):
                track.metadata.images = [
                    MediaItemImage(ImageType.THUMB, track_obj["album"]["images"][0]["url"])
                ]
        if track_obj.get("copyright"):
            track.metadata.copyright = track_obj["copyright"]
        if track_obj.get("explicit"):
            track.metadata.explicit = True
        if track_obj.get("popularity"):
            track.metadata.popularity = track_obj["popularity"]
        track.add_provider_mapping(
            ProviderMapping(
                item_id=track_obj["id"],
                provider_domain=self.domain,
                provider_instance=self.instance_id,
                content_type=ContentType.MP3,
                # bit_rate=320,
                url=track_obj["permalink_url"],
            )
        )
        return track
