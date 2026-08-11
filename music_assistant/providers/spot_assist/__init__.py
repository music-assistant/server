"""Spotify and YTmusic for MusicAssistant."""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

import yt_dlp
from music_assistant_models.enums import (
    AlbumType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    Playlist,
    ProviderMapping,
    RecommendationFolder,
    SearchResults,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails
from spotdl import Spotdl
from spotifyfreenew import Spotify  # pyright: ignore[reportMissingImports]
from ytmusicapi import YTMusic

from music_assistant.controllers.cache import use_cache
from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.recommendation_payload import RecommendationPayloadMixin

CONF_COOKIES = "CONF_SPOTIFY_COOKIES"
CONF_EMAIL = "CONF_SPOTIFY_EMAIL"


SUPPORTED_FEATURES = {
    # ProviderFeature.LIBRARY_ARTISTS,
    # ProviderFeature.LIBRARY_ALBUMS,
    # ProviderFeature.LIBRARY_TRACKS,
    # ProviderFeature.LIBRARY_PLAYLISTS,
    # ProviderFeature.PLAYLIST_CREATE,
    # ProviderFeature.PLAYLIST_TRACKS_EDIT,
    # ProviderFeature.BROWSE,
    # Upcoming maybe...
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.SIMILAR_TRACKS,
    ProviderFeature.RECOMMENDATIONS,
}

_MEDIA_TYPE_MAP = {
    "artist": MediaType.ARTIST,
    "album": MediaType.ALBUM,
    "playlist": MediaType.PLAYLIST,
}


if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.media_items import BrowseFolder, MediaItemType
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Set up the provider."""
    return SpotProvider(mass, manifest, config, SUPPORTED_FEATURES)


class SpotProvider(MusicProvider, RecommendationPayloadMixin):
    """Provider for Spotify and YTmusic."""

    recommendation_payload_ttl = 3600 * 3

    _user_id: str = ""
    _spotdl: Spotdl | None = None
    _yt_dlp: yt_dlp = None
    _yt_music: YTMusic = None
    _spotify: Spotify | None = None
    _me: dict[str, Any]

    async def handle_async_init(self) -> None:
        """Set up the YTMusic and Spotify source."""
        self._yt_music = YTMusic()
        self._spotdl = Spotdl(
            client_id="5f573c9620494bae87890c0f08a60293",
            client_secret="212476d9b0f3472eaa762d90b19b0ba8",
        )
        cookies: str | None = self.get_setup_value(CONF_COOKIES)
        email: str | None = self.get_setup_value(CONF_EMAIL)

        if not cookies:
            self.logger.error("Spotify cookies are missing! Please paste them in the settings.")
            self.logger.warning("Spotify cookies: %s", cookies)
            return

        self._spotify = await asyncio.to_thread(Spotify, cookies=json.loads(cookies), email=email)

        self._me = await asyncio.to_thread(self._spotify.me)
        self._user_id = self._me.get("display_name") or ""

        self.logger.debug("Logged in as: %s", self._user_id)

        # fetching the recommendations in advance as this takes some time
        self.mass.create_task(self._fetch_recommendation_payload())

    @use_cache(3600 * 24, allow_expired_cache=False)  # Cache for 24 hours
    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 10
    ) -> SearchResults:
        """
        Perform search on musicprovider.

        :param search_query: Search query.
        :param media_types: A list of media_types to include.
        :param limit: Number of items to return in the search (per type).
        """
        result = SearchResults()

        media_types = [
            x
            for x in media_types
            if x in (MediaType.ARTIST, MediaType.TRACK, MediaType.PLAYLIST, MediaType.ALBUM)
        ]
        if not media_types:
            return result
        # searchresult = await self._yt_music.search(search_query, filter="songs", limit=20)
        # searchresult = await asyncio.to_thread(self._yt_music.search, search_query, filter="songs", limit=limit)

        raw_songs, raw_albums, raw_artists = await asyncio.gather(
            asyncio.to_thread(self._yt_music.search, search_query, filter="songs", limit=limit),
            asyncio.to_thread(self._yt_music.search, search_query, filter="albums", limit=limit),
            asyncio.to_thread(self._yt_music.search, search_query, filter="artists", limit=limit),
            return_exceptions=True,
        )
        # songs.extend(videos)
        songs = raw_songs[:limit] if isinstance(raw_songs, list) else []
        albums = raw_albums[:limit] if isinstance(raw_albums, list) else []
        artists = raw_artists[:limit] if isinstance(raw_artists, list) else []
        # searchresult = await asyncio.to_thread(self._spotdl.search, [search_query])
        # self.logger.debug(songs["tracks"]["items"][:limit])

        # with Spotify search
        for song in songs:
            result.tracks = [*result.tracks, await self._parse_track(song, source="yt-music")]

        for album in albums:
            result.albums = [*result.albums, await self._parse_album(album)]

        for artist in artists:
            result.artists = [*result.artists, await self._parse_artist(artist)]

        return result

        # --- CACHING ---

    # --- PARSERS ---

    async def _parse_track(
        self, song, source: str = "yt-music", playlist_position: int | None = None
    ) -> Track:
        # Build base Track

        # SPOTIFY
        if source == "spotify":
            track = Track(
                item_id=song["id"],
                provider=self.domain,
                name=song["name"],
                duration=round(song["duration_ms"] / 1000),
                provider_mappings={
                    ProviderMapping(
                        item_id=song["id"],
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                        available=True,
                        audio_format=AudioFormat(
                            content_type=ContentType.UNKNOWN,  # yt-dlp gives various formats
                        ),
                        url=song["external_urls"]["spotify"],  # spotify id
                    )
                },
                position=playlist_position,
            )

            if song["album"]["coverArt"]["sources"]:
                track.metadata.images = UniqueList(
                    [
                        MediaItemImage(
                            type=ImageType.THUMB,
                            path=song["album"]["coverArt"]["sources"][-1]["url"],
                            provider=self.instance_id,
                            remotely_accessible=True,
                        )
                    ]
                )
            if song.get("artists"):
                artist = await self._parse_artist(song["artists"][0], source="spotify")
                track.artists.append(artist)
            return track
            """ else:
                root = song["itemV3"]["data"]
                song_id = root["identityTrait"]["contentHierarchyParent"]["uri"].split(":")[2]
                track = Track(
                    item_id=song_id,
                    provider=self.domain,
                    name=root["identityTrait"]["contentHierarchyParent"]["identityTrait"]["name"],
                    duration=root["consumptionExperienceTrait"]["duration"]["seconds"],
                    provider_mappings={
                        ProviderMapping(
                            item_id=song_id,
                            provider_domain=self.domain,
                            provider_instance=self.instance_id,
                            available=True,
                            audio_format=AudioFormat(
                                content_type=ContentType.UNKNOWN,  # yt-dlp gives various formats
                            ),
                            url=f"https://open.spotify.com/track/{song_id}",  # spotify id
                        )
                    },
                    position=playlist_position,
                )

                if root["visualIdentityTrait"]["squareCoverImage"]["image"]["data"]["sources"]:
                    track.metadata.images = UniqueList(
                        [
                            MediaItemImage(
                                type=ImageType.THUMB,
                                path=root["visualIdentityTrait"]["squareCoverImage"]["image"][
                                    "data"
                                ]["sources"][-1]["url"],
                                provider=self.instance_id,
                                remotely_accessible=True,
                            )
                        ]
                    )
                return track """

        # YT-MUSIC
        if "videoDetails" not in song:
            track = Track(
                item_id=song["videoId"],
                provider=self.domain,
                name=song["title"],
                provider_mappings={
                    ProviderMapping(
                        item_id=song["videoId"],
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                        available=True,
                        audio_format=AudioFormat(
                            content_type=ContentType.UNKNOWN,  # yt-dlp gives various formats
                        ),
                        url=f"https://music.youtube.com/watch?v={song['videoId']}",  # yt id
                    )
                },
                position=playlist_position,
            )
            if song.get("duration_seconds"):
                track.duration = song["duration_seconds"]
            if song["thumbnails"]:
                track.metadata.images = UniqueList(
                    [
                        MediaItemImage(
                            type=ImageType.THUMB,
                            path=song["thumbnails"][-1]["url"],
                            provider=self.instance_id,
                            remotely_accessible=True,
                        )
                    ]
                )

            if song.get("artists"):
                artist = await self._parse_artist(song["artists"][0], source="yt")
                if artist:
                    track.artists.append(artist)
                else:
                    raise MediaNotFoundError(
                        "Artist couldbn't be fetched with for song: %s", song["title"]
                    )

            """ for artist in song["artists"]:
                            track.artists.append(artist["name"]) """
            return track
        else:  # noqa: RET505
            track = Track(
                item_id=song["videoDetails"]["videoId"],
                provider=self.domain,
                name=song["videoDetails"]["title"],
                duration=int(song["videoDetails"]["lengthSeconds"]),
                provider_mappings={
                    ProviderMapping(
                        item_id=song["videoDetails"]["videoId"],
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                        available=True,
                        audio_format=AudioFormat(
                            content_type=ContentType.UNKNOWN,  # yt-dlp gives various formats
                        ),
                        url=f"https://music.youtube.com/watch?v={song['videoDetails']['videoId']}",  # yt id
                    )
                },
                position=playlist_position,
            )
            if song["videoDetails"]["thumbnail"]["thumbnails"]:
                track.metadata.images = UniqueList(
                    [
                        MediaItemImage(
                            type=ImageType.THUMB,
                            path=song["videoDetails"]["thumbnail"]["thumbnails"][-1]["url"],
                            provider=self.instance_id,
                            remotely_accessible=True,
                        )
                    ]
                )

            """ track.artists.append(song["videoDetails"]["author"]) """
            if song.get("artists"):
                artist_data = await asyncio.to_thread(
                    self._yt_music.get_artist,
                    song["microformat"]["pageOwnerDetails"]["externalChannelId"],
                )
                artist = await self._parse_artist(artist_data, source="yt")
                if not artist:
                    raise MediaNotFoundError(
                        "Artist couldbn't be fetched for song with id: %s",
                        song["videoDetails"]["videoId"],
                    )
                track.artists.append(artist)
            return track

    async def _parse_artist(self, artist_obj: dict[str, Any], source: str = "yt") -> Artist:
        """Parse a Ytmusic user response to Artist model object."""
        if source == "yt":
            if artist_obj.get("channelId"):  # fetched from .get_artist()
                artist = Artist(
                    item_id=artist_obj["channelId"],
                    name=artist_obj["name"],
                    provider=self.domain,
                    provider_mappings={
                        ProviderMapping(
                            item_id=artist_obj["channelId"],
                            provider_domain=self.domain,
                            provider_instance=self.instance_id,
                            url=f"https://music.youtube.com/@{artist_obj['name']}",
                        )
                    },
                )
                if artist_obj["thumbnails"]:
                    artist.metadata.images = UniqueList(
                        [
                            MediaItemImage(
                                type=ImageType.THUMB,
                                path=artist_obj["thumbnails"][-1]["url"],
                                provider=self.instance_id,
                                remotely_accessible=True,
                            )
                        ]
                    )
                if artist_obj.get("description"):
                    artist.metadata.description = artist_obj["description"]
                return artist

            if artist_obj.get("id"):
                artist = Artist(
                    item_id=artist_obj["id"],
                    name=artist_obj["name"],
                    provider=self.domain,
                    provider_mappings={
                        ProviderMapping(
                            item_id=artist_obj["id"],
                            provider_domain=self.domain,
                            provider_instance=self.instance_id,
                            url=f"https://music.youtube.com/@{artist_obj['name']}",
                        )
                    },
                )
                if artist_obj.get("thumbnails"):
                    artist.metadata.images = UniqueList(
                        [
                            MediaItemImage(
                                type=ImageType.THUMB,
                                path=artist_obj["thumbnails"][-1]["url"],
                                provider=self.instance_id,
                                remotely_accessible=True,
                            )
                        ]
                    )
                return artist

            if artist_obj.get("browseId"):  # fetched when searching for artists
                artist = Artist(
                    item_id=artist_obj["browseId"],
                    name=artist_obj["artist"],
                    provider=self.domain,
                    provider_mappings={
                        ProviderMapping(
                            item_id=artist_obj["browseId"],
                            provider_domain=self.domain,
                            provider_instance=self.instance_id,
                            url=f"https://music.youtube.com/@{artist_obj['artist']}",
                        )
                    },
                )
                if artist_obj["thumbnails"]:
                    artist.metadata.images = UniqueList(
                        [
                            MediaItemImage(
                                type=ImageType.THUMB,
                                path=artist_obj["thumbnails"][-1]["url"],
                                provider=self.instance_id,
                                remotely_accessible=True,
                            )
                        ]
                    )
                return artist

            self.logger.warning("Returned None on artist: %s", artist_obj)
            return None

        # SPOTIFY
        artist_id = artist_obj["id"] if artist_obj.get("id") else artist_obj["uri"].split(":")[2]
        artist = Artist(
            item_id=artist_id,
            name=artist_obj["name"],
            provider=self.domain,
            provider_mappings={
                ProviderMapping(
                    item_id=artist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=artist_obj["external_urls"]["spotify"],
                )
            },
        )
        return artist  # noqa: RET504

    async def _parse_album(self, album_data: dict[str, Any]) -> Album | None:
        """Parse YT Music album to MA Album."""
        # self.logger.debug(f"Parsing: {album_data['title']}")
        album_id = album_data.get("browseId")
        if not album_id:
            if album_data.get("audioPlaylistId"):
                album_id = await asyncio.to_thread(
                    self._yt_music.get_album_browse_id, album_data["audioPlaylistId"]
                )
            else:
                self.logger.warning("Album has no browseId: %s", album_data.get("title"))
                return None

        album_id = str(album_id)

        title = album_data.get("title", "Unknown Album")

        album = Album(
            item_id=album_id,
            provider=self.domain,
            name=title,
            provider_mappings={
                ProviderMapping(
                    item_id=album_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    available=True,
                    url=f"https://music.youtube.com/browse/{album_id}",
                )
            },
        )

        # Album type
        type_str = str(album_data.get("type", "album")).lower()
        if type_str == "single":
            album.album_type = AlbumType.SINGLE
        elif type_str == "ep":
            album.album_type = AlbumType.EP
        elif type_str == "compilation":
            album.album_type = AlbumType.COMPILATION
        else:
            album.album_type = AlbumType.ALBUM

        # Year (safely convert)
        year_raw = album_data.get("year")
        if year_raw:
            try:
                year = int(str(year_raw)[:4])
                if 1900 <= year <= 2100:
                    album.year = year
            except ValueError, TypeError:
                pass

        artists_data = album_data.get("artists", [])
        if artists_data:
            album.artists = UniqueList()
            for artist_data in artists_data:
                if not isinstance(artist_data, dict):
                    continue

                artist_name = artist_data.get("name")
                if not artist_name:
                    continue

                artist_id = artist_data.get("id") or artist_name

                album.artists.append(
                    ItemMapping(
                        media_type=MediaType.ARTIST,
                        item_id=str(artist_id),
                        provider=self.instance_id,
                        name=artist_name,
                    )
                )

        thumbnails = album_data.get("thumbnails", [])
        if thumbnails:
            thumb_url = thumbnails[-1].get("url", "")

            album.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=thumb_url,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                ]
            )

        if album_data.get("description"):
            album.metadata.description = album_data["description"]
        # Explicit
        if album_data.get("isExplicit") is not None:
            album.metadata.explicit = album_data["isExplicit"]

        return album

    async def _parse_playlist(self, playlist_data: dict[str, Any]) -> Playlist:
        """Parse a spotify playlist to a Playlist object."""
        playlist_id = playlist_data["id"]

        playlist = Playlist(
            item_id=playlist_id,
            provider=self.domain,
            name=playlist_data["name"],
            provider_mappings={
                ProviderMapping(
                    item_id=playlist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        playlist.is_editable = False
        if playlist_data.get("images"):
            playlist.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=playlist_data["images"][-1]["url"],
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                ]
            )
        if playlist_data.get("description"):
            playlist.metadata.description = playlist_data["description"]
        return playlist

    # --- RECOMMENDATIONS ---

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Get this provider's available recommendation rows, without items."""
        rows = await self._recommendation_rows_from_payload()
        rows.append(
            RecommendationFolder(
                name="Your Playlists",
                translation_key="your_playlists",
                item_id=f"{self.instance_id}_spc_own_playlists",
                provider=self.instance_id,
                icon="mdi-playlist-music",
                items=UniqueList(),
            )
        )
        rows.append(
            RecommendationFolder(
                name="Made For You",
                translation_key="made_for_you",
                item_id=f"{self.instance_id}_spc_playlists",
                provider=self.instance_id,
                icon="mdi-playlist-music",
                items=UniqueList(),
            ),
        )
        return rows

    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """Get the items for a single recommendation row."""
        return await self._recommendation_items_from_payload(item_id)

    async def _fetch_recommendation_payload(self) -> list[RecommendationFolder]:
        """Fetch this provider's recommendation folders, fully populated with items."""
        folders = []
        self.logger.debug("Seeking recommendations")
        sp_playlists = await asyncio.to_thread(self._spotify.current_user_playlists)

        personnal_folder = RecommendationFolder(
            name="Your Playlists",
            translation_key="your_playlists",
            item_id=f"{self.instance_id}_playlists",
            provider=self.instance_id,
            icon="mdi-playlist-music",
            items=UniqueList(),
        )
        spotify_folder = RecommendationFolder(
            name="Made For You",
            translation_key="made_for_you",
            item_id=f"{self.instance_id}_sp_playlists",
            provider=self.instance_id,
            icon="mdi-playlist-music",
            items=UniqueList(),
        )

        for sp_playlist in sp_playlists["items"]:
            if sp_playlist["owner"]["name"] == "Spotify":
                spotify_folder.items.append(await self._parse_playlist(sp_playlist))
            else:
                personnal_folder.items.append(await self._parse_playlist(sp_playlist))

        folders.append(await self._build_recent_folder())
        folders.append(personnal_folder)
        folders.append(spotify_folder)

        return folders

    async def get_playlist(self, prov_playlist_id):
        """Fetch spotify playlist."""
        playlist_data = await asyncio.to_thread(self._spotify.playlist, prov_playlist_id)
        if not playlist_data:
            raise MediaNotFoundError(f"Playlist {prov_playlist_id} failed to load")

        playlist = await self._parse_playlist(playlist_data)

        image_path = playlist.metadata.images[0].path if playlist.metadata.images else None
        await self._track_recently_viewed(
            "playlist", prov_playlist_id, playlist_data["name"], image_path
        )
        return playlist

    @use_cache(3600 * 24, allow_expired_cache=True)
    async def get_playlist_tracks(
        self,
        prov_playlist_id: str,
        page: int = 0,
    ) -> list[Track]:
        """Get all playlist tracks for given playlist id."""
        # Get all tracks for a given playlist.
        # Mandatory only if you reported LIBRARY_PLAYLISTS in the supported_features.
        # NOTE: It is advised to apply caching here (if possible)
        # to avoid too many calls to the provider's API.
        # You can use the @use_cache decorator from music_assistant.controllers.cache
        # to easily apply caching to this method.
        # As this returns a collection that also serves as good fallback data, decorate it with
        # allow_expired_cache=True, e.g. @use_cache(3600 * 3, allow_expired_cache=True).
        # That serves the stale result instantly while refreshing it in the background.
        if page > 0:
            # we already returned everything on page 0
            return []
        playlist = await asyncio.to_thread(self._spotify.playlist_items, prov_playlist_id)
        sem = asyncio.Semaphore(25)

        async def fetch(raw_song: dict[str, Any]):
            async with sem:
                full_song = await asyncio.to_thread(self._spotify.track, raw_song["track"]["id"])
                return await self._parse_track(full_song, source="spotify")

        return await asyncio.gather(*(fetch(item) for item in playlist["items"]))

    # --- SEARCHERS ---

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by yt music id."""
        if len(prov_track_id) <= 12:
            # self.logger.warning("Fetching using yt")
            track = await asyncio.to_thread(self._yt_music.get_song, prov_track_id)
            if not track:
                raise MediaNotFoundError(f"Yt-music track {prov_track_id} not found")

            return await self._parse_track(track, source="yt-music")
        # self.logger.warning("Fetching using spotify")
        track = await asyncio.to_thread(self._spotify.track, prov_track_id)
        if not track:
            raise MediaNotFoundError(f"Spotify track {prov_track_id} not found")

        return await self._parse_track(track, source="spotify")

    async def get_album(self, prov_album_id: str) -> Album:
        """Fetch full album details."""
        self.logger.debug("Fetching album: %s", prov_album_id)

        try:
            album_data = await asyncio.to_thread(self._yt_music.get_album, prov_album_id)
        except Exception as err:
            self.logger.error("Failed to fetch album %s: %s", prov_album_id, err)
            raise MediaNotFoundError(f"Album {prov_album_id} not found")

        # Reuse parser
        album_data["browseId"] = prov_album_id  # ensure ID is set
        album = await self._parse_album(album_data)

        if not album:
            raise MediaNotFoundError(f"Failed to parse album {prov_album_id}")

        image_path = album.metadata.images[0].path if album.metadata.images else None
        await self._track_recently_viewed("album", prov_album_id, album_data["title"], image_path)

        return album

    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Return tracks similar to the given track, used to auto-fill the queue."""
        # self.logger.warning("Called similar tracks")
        songs = await self._get_related_songs_for(prov_track_id)
        return [await self._parse_track(song) for song in songs[:limit]]

    # --- ARTIST ---

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        self.logger.debug("artist id: %s", prov_artist_id)
        if len(prov_artist_id) > 12:  # spotify
            artist_spot = await asyncio.to_thread(self._spotify.artist, prov_artist_id)
            if artist_spot:
                artist_searched = await asyncio.to_thread(
                    self._yt_music.search, artist_spot["name"], filter="artists", limit=1
                )
                artist_obj = await asyncio.to_thread(
                    self._yt_music.get_artist, artist_searched[0]["browseId"]
                )
            else:
                raise MediaNotFoundError("Artist %s couldn't be fetched", prov_artist_id)
        else:
            artist_obj = await asyncio.to_thread(self._yt_music.get_artist, prov_artist_id)

        if artist_obj:
            artist = await self._parse_artist(artist_obj)
            if not artist:
                raise MediaNotFoundError("Artist couldbn't be fetched with id: %s", prov_artist_id)

        image_path = artist.metadata.images[0].path if artist.metadata.images else None
        await self._track_recently_viewed("artist", prov_artist_id, artist_obj["name"], image_path)
        return artist

    @use_cache(3600 * 24 * 7, allow_expired_cache=True)
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:  # type: ignore[empty-body]
        """Get a list of all albums for the given artist."""
        artist_obj = await asyncio.to_thread(self._yt_music.get_artist, prov_artist_id)
        if not artist_obj:
            raise MediaNotFoundError(f"Failed to get albums for {prov_artist_id}")

        artist_albums = []
        if artist_obj.get("albums"):
            artist_albums = artist_obj["albums"]["results"]
        parsed_albums = []
        for song in artist_albums:
            fetched = await asyncio.to_thread(self._yt_music.get_album, song["browseId"])
            parsed = await self._parse_album(fetched)
            parsed_albums.append(parsed)

        return parsed_albums

    @use_cache(3600 * 24 * 7, allow_expired_cache=True)
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:  # type: ignore[empty-body]
        """Get a list of most popular tracks for the given artist."""
        artist = await asyncio.to_thread(self._yt_music.get_artist, prov_artist_id)
        top_songs = artist["songs"]["results"]
        parsed_songs = []
        for song in top_songs:
            fetched = await asyncio.to_thread(self._yt_music.get_song, song["videoId"])
            parsed = await self._parse_track(fetched)
            parsed_songs.append(parsed)

        return parsed_songs

    @use_cache(3600 * 24 * 7, allow_expired_cache=True)
    async def get_album_tracks(  # type: ignore[empty-body]
        self,
        prov_album_id: str,
    ) -> list[Track]:
        """Get album tracks for given album id."""
        album_data = await asyncio.to_thread(self._yt_music.get_album, prov_album_id)
        album_songs = album_data["tracks"]
        parsed_songs = []
        for song in album_songs:
            fetched = await asyncio.to_thread(self._yt_music.get_song, song["videoId"])
            parsed = await self._parse_track(fetched)
            parsed_songs.append(parsed)

        return parsed_songs

    # --- STREAMING ---

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        self.logger.debug("Getting stream for: %s", item_id)
        url = await self._get_stream_url(item_id)
        self.logger.debug("Getting stream bis for: %s", item_id)

        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            # let ffmpeg work out the details itself
            audio_format=AudioFormat(
                content_type=ContentType.UNKNOWN,
            ),
            stream_type=StreamType.HTTP,
            path=url,
            can_seek=True,
            allow_seek=True,
        )

    async def _get_stream_url(self, item_id: str) -> str | None:
        """Extract direct audio stream URL via yt-dlp."""
        if len(item_id) <= 12:
            yt_url = f"https://music.youtube.com/watch?v={item_id}"
        else:
            spotify_url = f"https://open.spotify.com/track/{item_id}"

            songs = await asyncio.to_thread(self._spotdl.search, [spotify_url])
            if not songs:
                self.logger.error("Song not found on Spotify: %s", item_id)
                return None

            yt_urls = await asyncio.to_thread(self._spotdl.get_download_urls, songs)
            if not yt_urls or not yt_urls[0]:
                self.logger.error("No YouTube match for: %s", item_id)
                return None

            yt_url = yt_urls[0]

        self.logger.debug("YouTube URL: %s", yt_url)

        try:
            stream_url = await asyncio.to_thread(self._extract_audio_url, yt_url)
            self.logger.debug("Stream URL: %s", stream_url[:80])
            return stream_url
        except Exception as err:
            self.logger.error("yt-dlp extraction failed: %s", err)
            return None

    def _extract_audio_url(self, youtube_url: str) -> str:
        """Blocking: extract direct audio URL from YouTube."""
        ydl_opts = {
            "format": "bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(youtube_url, download=False)
            return info["url"]

    # -- CACHING ---

    async def _track_recently_viewed(
        self, kind: str, item_id: str, name: str, thumbnail: str | None
    ) -> None:
        """Push an item onto the front of a recently-viewed cache list."""
        cache_key = f"recently_viewed_{kind}"
        history: list[dict[str, Any]] = await self.mass.cache.get(cache_key, default=[]) or []

        # drop any existing entry for this id so it moves to the front, not duplicates
        history = [entry for entry in history if entry["item_id"] != item_id]

        history.insert(
            0,
            {
                "item_id": item_id,
                "name": name,
                "media_type": kind,
                "viewed_at": time.time(),
                "thumbnail": thumbnail,
            },
        )
        history = history[:5]

        await self.mass.cache.set(cache_key, history, expiration=3600 * 24 * 30)  # 30 days

    async def _get_recently_viewed(self, kind: str) -> list[dict[str, Any]]:
        """Retrieve the recently-viewed cache list ('artists' or 'playlists')."""
        return await self.mass.cache.get(f"recently_viewed_{kind}", default=[]) or []

    async def _build_recent_folder(self) -> RecommendationFolder:
        recent_folder = RecommendationFolder(
            name="Recently Played",
            translation_key="recently_played_spc",
            item_id=f"{self.instance_id}_recent",
            provider=self.instance_id,
            icon="mdi-playlist-music",
            items=UniqueList(),
        )

        rec_playlists = await self._get_recently_viewed("playlist")
        rec_artists = await self._get_recently_viewed("artist")
        rec_albums = await self._get_recently_viewed("album")

        rec = rec_playlists + rec_artists + rec_albums
        rec = sorted(rec, key=lambda entry: entry["viewed_at"], reverse=True)

        for entry in rec:
            image = None
            if entry.get("thumbnail"):
                image = MediaItemImage(
                    type=ImageType.THUMB,
                    path=entry["thumbnail"],
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            recent_folder.items.append(
                ItemMapping(
                    media_type=_MEDIA_TYPE_MAP[entry["media_type"]],
                    item_id=entry["item_id"],
                    provider=self.instance_id,
                    name=entry["name"],
                    image=image,
                )
            )

        return recent_folder

    # --- OTHERS ---

    async def _get_related_songs_for(self, item_id: str) -> list[dict]:
        """Fetch related song dicts for a video/track id, resolving spotify ids to yt-music first."""
        yt_video_id: str

        if len(item_id) > 12:
            # spotify id — resolve to a yt-music video first via your existing stream lookup path
            # or fall back to searching by the track's name/artist if you don't already have a mapping
            spotify_full_song = await asyncio.to_thread(self._spotify.track, item_id)
            search_query = f"{spotify_full_song['name']} {spotify_full_song['artists'][0]['name']}"
            yt_search = await asyncio.to_thread(self._yt_music.search, search_query)

            if not yt_search:
                return []

            found_id = yt_search[0]["videoId"]
            if not found_id:
                return []

            yt_video_id = str(found_id)
            # self.logger.debug("Can't find next tracks for spotify issued tracks")
        else:
            yt_video_id = item_id

        watch_playlist = await asyncio.to_thread(
            self._yt_music.get_watch_playlist, videoId=yt_video_id
        )
        related_browse_id = watch_playlist.get("related")
        if not related_browse_id:
            return []

        related = await asyncio.to_thread(self._yt_music.get_song_related, related_browse_id)

        songs = []
        for section in related:
            for item in section.get("contents", []):
                if "videoId" in item:
                    songs.append(item)
        return songs

    # --- NOT IMPLEMENTED RN ---
