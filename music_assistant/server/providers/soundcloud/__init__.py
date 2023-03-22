"""Soundcloud support for MusicAssistant."""
import asyncio

# import re
# from operator import itemgetter
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

# from music_assistant.server.helpers.app_vars import app_var
# from music_assistant.server.helpers.process import AsyncProcess
from music_assistant.server.models.music_provider import MusicProvider

from .soundcloudpy.asyncsoundcloudpy import SoundcloudAsync

# from music_assistant.constants import CONF_USERNAME


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
        if (
            not self.config.get_value(CONF_CLIENT_ID)
            or not self.config.get_value(CONF_AUTHORIZATION)
            # or not self.config.get_value(CONF_USERNAME)
        ):
            raise LoginFailed("Invalid login credentials")

        # def connect():
        #     print("connect")
        #     soundcloud_account = Soundcloud(
        #         o_auth=self.config.get_value(CONF_AUTHORIZATION),
        #         client_id=self.config.get_value(CONF_CLIENT_ID),
        #     )
        #     return soundcloud_account.get_account_details()

        # self._soundcloud = await self._run_async(connect)
        # username = self.config.get_value(CONF_USERNAME)
        client_id = self.config.get_value(CONF_CLIENT_ID)
        auth_token = self.config.get_value(CONF_AUTHORIZATION)

        async with SoundcloudAsync(auth_token, client_id) as account:
            # print(account)
            await account.login()
            # print(status)

        self._soundcloud = account
        self._me = await account.get_account_details()
        self._user_id = self._me["id"]
        # assert me.permalink == username
        # print(self.me["id"])
        # print(type(self.me))
        # return None

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
            print(item)
            track = await self._soundcloud.get_track_details(item["id"])
            print(track)
            try:
                await self._parse_track(track)
            except IndexError:
                continue
            results.append(track)

        return results

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve all library artists from Soundcloud."""
        following = await self._soundcloud.get_following(self._user_id)
        for artist in following["collection"]:
            # print(item)
            # if artist and artist["id"]:
            # print(item)
            yield await self._parse_artist(artist)

        # artists_obj = await get_library_artists(
        #     headers=self._headers, username=self.config.get_value(CONF_USERNAME)
        # )
        # for artist in artists_obj:
        #     yield await self._parse_artist(artist)

    # async def get_library_albums(self) -> AsyncGenerator[Album, None]:
    #     """Retrieve all library albums from Youtube Music."""
    #     albums_obj = await get_library_albums(
    #         headers=self._headers, username=self.config.get_value(CONF_USERNAME)
    #     )
    #     for album in albums_obj:
    #         yield await self._parse_album(album, album["browseId"])

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve all library playlists from the provider."""
        playlists = await self._soundcloud.get_account_playlists()
        # print(playlists)
        for item in playlists["collection"]:
            # print(item)
            if "playlist" in item:
                if item and (item["playlist"]["title"] or item["system_playlist"]["title"]):
                    yield await self._parse_playlist(item)
            elif "system_playlist" in item and item and item["system_playlist"]["title"]:
                yield await self._parse_playlist(item)
            # elif item and item["system_playlist"]["title"]:
            #     # print(item)
            #     yield await self._parse_playlist(item)
        # for playlist in playlists_obj:
        #     yield await self._parse_playlist(playlist)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from Youtube Music."""
        tracks = await self._soundcloud.get_tracks_liked()
        # print("get_library_tracks")
        # print("get_library_tracks", tracks["collection"])
        # tracks_obj = await get_library_tracks(
        #     headers=self._headers, username=self.config.get_value(CONF_USERNAME)
        # )
        for item in tracks["collection"]:
            # print("item")
            # print(item)
            track = await self._soundcloud.get_track_details(item)
            # print("track")
            # try:
            #     print(track[0]["id"])
            # except IndexError:
            #     print("IndexError", track)
            # Library tracks sometimes do not have a valid artist id
            # In that case, call the API for track details based on track id
            try:
                yield await self._parse_track(track[0])
            # except InvalidDataError:
            # print(track)
            except IndexError:
                continue
            #     track = await self.get_track(track["videoId"])
            #     yield track

    # async def get_album(self, prov_album_id) -> Album:
    #     """Get full album details by id."""
    #     album_obj = await get_album(prov_album_id=prov_album_id)
    #     return (
    #         await self._parse_album(album_obj=album_obj, album_id=prov_album_id)
    #         if album_obj
    #         else None
    #     )

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
        # print(prov_artist_id)
        artist_obj = await self._soundcloud.get_user_details(user_id=prov_artist_id)

        return await self._parse_artist(artist_obj=artist_obj) if artist_obj else None

    async def get_track(self, prov_track_id) -> Track:
        """Get full track details by id."""
        track_obj = await self._soundcloud.get_track_details(track_id=prov_track_id)
        return await self._parse_track(track_obj[0])

    async def get_playlist(self, prov_playlist_id) -> Playlist:
        """Get full playlist details by id."""
        playlist_obj = await self._soundcloud.get_playlist_details(playlist_id=prov_playlist_id)
        # print(playlist_obj)
        return await self._parse_playlist(playlist_obj)

    async def get_playlist_tracks(self, prov_playlist_id) -> list[Track]:
        """Get all playlist tracks for given playlist id."""
        playlist_obj = await self._soundcloud.get_playlist_details(playlist_id=prov_playlist_id)
        if "tracks" not in playlist_obj:
            return []
        tracks = []
        # print(playlist_obj)
        for index, track in enumerate(playlist_obj["tracks"]):
            # if track["isAvailable"]:
            # Playlist tracks sometimes do not have a valid artist id
            # In that case, call the API for track details based on track id
            try:
                # print(track["id"])
                song = await self._soundcloud.get_track_details(track["id"])
                # print(song[0])
                track = await self._parse_track(song[0])
                if track:
                    track.position = index
                    tracks.append(track)
            except KeyError:
                print(track)
            except InvalidDataError:
                track = await self.get_track(track["id"])
                if track:
                    track.position = index
                    tracks.append(track)
        return tracks

    async def get_artist_albums(self, prov_artist_id) -> list[Album]:
        """Get a list of albums for the given artist."""
        album_obj = await self._soundcloud.get_album_from_user(user_id=prov_artist_id, limit=25)
        # print(tracks_obj)
        albums = []
        for album in album_obj["collection"]:
            try:
                # print(album)
                # item = await self._soundcloud.get_track_details(album["id"])
                # print(song[0])
                _album = await self._parse_album(album[0])
                # if track:
                #     track.position = index
                albums.append(_album)
            except KeyError:
                print(album)
            # except InvalidDataError:
            #     track = await self.get_track(track["id"])
            #     if track:
            #         track.position = index
            #         tracks.append(track)
            print(albums)
        return albums

    async def get_artist_toptracks(self, prov_artist_id) -> list[Track]:
        """Get a list of 25 most popular tracks for the given artist."""
        tracks_obj = await self._soundcloud.get_popular_tracks_user(
            user_id=prov_artist_id, limit=25
        )
        # print(tracks_obj)
        tracks = []
        for track in tracks_obj["collection"]:
            try:
                # print(track["id"])
                song = await self._soundcloud.get_track_details(track["id"])
                # print(song[0])
                track = await self._parse_track(song[0])
                # if track:
                #     track.position = index
                tracks.append(track)
            except KeyError:
                print(track)
            # except InvalidDataError:
            #     track = await self.get_track(track["id"])
            #     if track:
            #         track.position = index
            #         tracks.append(track)
        return tracks

    # async def library_add(self, prov_item_id, media_type: MediaType) -> None:
    #     """Add an item to the library."""
    #     result = False
    #     if media_type == MediaType.ARTIST:
    #         result = await library_add_remove_artist(
    #             headers=self._headers,
    #             prov_artist_id=prov_item_id,
    #             add=True,
    #             username=self.config.get_value(CONF_USERNAME),
    #         )
    #     elif media_type == MediaType.ALBUM:
    #         result = await library_add_remove_album(
    #             headers=self._headers,
    #             prov_item_id=prov_item_id,
    #             add=True,
    #             username=self.config.get_value(CONF_USERNAME),
    #         )
    #     elif media_type == MediaType.PLAYLIST:
    #         result = await library_add_remove_playlist(
    #             headers=self._headers,
    #             prov_item_id=prov_item_id,
    #             add=True,
    #             username=self.config.get_value(CONF_USERNAME),
    #         )
    #     elif media_type == MediaType.TRACK:
    #         raise NotImplementedError
    #     return result

    # async def library_remove(self, prov_item_id, media_type: MediaType):
    #     """Remove an item from the library."""
    #     result = False
    #     if media_type == MediaType.ARTIST:
    #         result = await library_add_remove_artist(
    #             headers=self._headers,
    #             prov_artist_id=prov_item_id,
    #             add=False,
    #             username=self.config.get_value(CONF_USERNAME),
    #         )
    #     elif media_type == MediaType.ALBUM:
    #         result = await library_add_remove_album(
    #             headers=self._headers,
    #             prov_item_id=prov_item_id,
    #             add=False,
    #             username=self.config.get_value(CONF_USERNAME),
    #         )
    #     elif media_type == MediaType.PLAYLIST:
    #         result = await library_add_remove_playlist(
    #             headers=self._headers,
    #             prov_item_id=prov_item_id,
    #             add=False,
    #             username=self.config.get_value(CONF_USERNAME),
    #         )
    #     elif media_type == MediaType.TRACK:
    #         raise NotImplementedError
    #     return result

    # async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
    #     """Add track(s) to playlist."""
    #     return await add_remove_playlist_tracks(
    #         headers=self._headers,
    #         prov_playlist_id=prov_playlist_id,
    #         prov_track_ids=prov_track_ids,
    #         add=True,
    #         username=self.config.get_value(CONF_USERNAME),
    #     )

    # async def remove_playlist_tracks(
    #     self, prov_playlist_id: str, positions_to_remove: tuple[int]
    # ) -> None:
    #     """Remove track(s) from playlist."""
    #     playlist_obj = await get_playlist(
    #         prov_playlist_id=prov_playlist_id,
    #         headers=self._headers,
    #         username=self.config.get_value(CONF_USERNAME),
    #     )
    #     if "tracks" not in playlist_obj:
    #         return None
    #     tracks_to_delete = []
    #     for index, track in enumerate(playlist_obj["tracks"]):
    #         if index in positions_to_remove:
    #             # YT needs both the videoId and the setVideoId in order to remove
    #             # the track. Thus, we need to obtain the playlist details and
    #             # grab the info from there.
    #             tracks_to_delete.append(
    #                 {"videoId": track["videoId"], "setVideoId": track["setVideoId"]}
    #             )

    #     return await add_remove_playlist_tracks(
    #         headers=self._headers,
    #         prov_playlist_id=prov_playlist_id,
    #         prov_track_ids=tracks_to_delete,
    #         add=False,
    #         username=self.config.get_value(CONF_USERNAME),
    #     )

    async def get_similar_tracks(self, prov_track_id, limit=25) -> list[Track]:
        """Retrieve a dynamic list of tracks based on the provided item."""
        tracks_obj = await self._soundcloud.get_recommended(track_id=prov_track_id, limit=25)
        # print(tracks_obj)
        tracks = []
        for track in tracks_obj["collection"]:
            try:
                # print(track["id"])
                song = await self._soundcloud.get_track_details(track["id"])
                # print(song[0])
                track = await self._parse_track(song[0])
                # if track:
                #     track.position = index
                tracks.append(track)
            except KeyError:
                print(track)
            # except InvalidDataError:
            #     track = await self.get_track(track["id"])
            #     if track:
            #         track.position = index
            #         tracks.append(track)
        return tracks

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        track_details = await self._soundcloud.get_track_details(track_id=item_id)
        # media_url = track_details[0]["media"]["transcodings"][0]["url"]
        stream_format = track_details[0]["media"]["transcodings"][0]["format"]["mime_type"]
        # print(format)
        # track_auth = track_details[0]["track_authorization"]
        # stream_url = f"{media_url}?client_id={self.client_id}&track_authorization={track_auth}"
        # print(stream_url)
        url = await self._soundcloud.get_stream_url(track_id=item_id)
        return StreamDetails(
            provider=self.domain,
            item_id=item_id,
            content_type=ContentType.try_parse(stream_format),
            direct=url,
        )

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
        # if "thumbnails" in album_obj:
        #     album.metadata.images = await self._parse_thumbnails(album_obj["thumbnails"])
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
        # print(artist_obj)
        permalink = artist_obj["permalink"]
        if "id" in artist_obj and artist_obj["id"]:
            artist_id = artist_obj["id"]
        if not artist_id:
            raise InvalidDataError("Artist does not have a valid ID")
        artist = Artist(item_id=artist_id, name=artist_obj["username"], provider=self.domain)
        if "description" in artist_obj:
            artist.metadata.description = artist_obj["description"]
        # print(artist_obj)
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
        """Parse a YT Playlist response to a Playlist object."""
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
        # print(playlist.metadata.checksum)
        return playlist

    async def _parse_track(self, track_obj: dict) -> Track:
        """Parse a YT Track response to a Track model object."""
        # print(track_obj)
        # print("Parse track", track_obj["id"])
        name, version = parse_title_and_version(track_obj["title"])
        track = Track(
            item_id=track_obj["id"],
            provider=self.domain,
            name=name,
            version=version,
            duration=track_obj["duration"] / 1000,
        )
        user_id = track_obj["user"]["id"]
        # print(user_id)
        user = await self._soundcloud.get_user_details(user_id)
        # print(user)
        artist = await self._parse_artist(user)
        if artist and artist.item_id not in {x.item_id for x in track.artists}:
            # print("if artist and artist.item_id not in ")
            # print(artist)
            # print("if artist and artist.item_id not in ")
            track.artists.append(artist)
            # print("if artist and artist.item_id not in ")
        # track = Track(item_id=track_obj["id"], provider=self.domain, name=track_obj["title"])

        # track.metadata.explicit = track_obj["explicit"]
        if "artwork_url" in track_obj:
            # print(track_obj["artwork_url"])
            track.metadata.preview = track_obj["artwork_url"]
        # else:
        #     print("no artwork_url")
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
