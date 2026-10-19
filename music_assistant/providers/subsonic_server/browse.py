"""Subsonic endpoints that browse and search the Music Assistant library."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast

from aiohttp import web
from libopensonic.media import MusicFolder
from music_assistant_models.enums import MediaType, SortDirection, SortField
from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError

from music_assistant.helpers.datetime import utc
from music_assistant.helpers.provider_access import visible_music_sources

from . import mappers
from .constants import ErrorCode
from .ids import ItemRef, parse_subsonic_id
from .library import album_track_stats, artist_album_counts, collect_pages
from .response import SubsonicError

if TYPE_CHECKING:
    from music_assistant_models.media_items import Album, Artist, MediaItemType, Track

    from music_assistant.mass import MusicAssistant

    from .request import EndpointHandler, SubsonicRequest

MUSIC_FOLDER_ID = 1
IGNORED_ARTICLES = "The El La Los Las Le Les"
THUMB_SIZES = (80, 160, 256, 512, 1024)
MAX_LIST_SIZE = 500
DEFAULT_SEARCH_COUNT = 20

ALBUM_LIST_SORTS: dict[str, tuple[SortField, SortDirection | None]] = {
    "random": (SortField.RANDOM, None),
    "newest": (SortField.TIMESTAMP_ADDED, SortDirection.DESC),
    "frequent": (SortField.PLAY_COUNT, SortDirection.DESC),
    "highest": (SortField.PLAY_COUNT, SortDirection.DESC),
    "recent": (SortField.LAST_PLAYED, SortDirection.DESC),
    "alphabeticalByName": (SortField.SORT_NAME, SortDirection.ASC),
    "alphabeticalByArtist": (SortField.ARTIST_NAME, SortDirection.ASC),
    "starred": (SortField.FAVORITE_TIMESTAMP, SortDirection.DESC),
    "byGenre": (SortField.SORT_NAME, SortDirection.ASC),
    "byYear": (SortField.YEAR, SortDirection.ASC),
}


class BrowseEndpoints:
    """Library browsing, searching and cover art."""

    def __init__(self, mass: MusicAssistant) -> None:
        """
        Initialize the endpoints.

        :param mass: The Music Assistant instance.
        """
        self.mass = mass

    def endpoints(self) -> dict[str, EndpointHandler]:
        """Return the handlers of this group, keyed by endpoint name."""
        return {
            "getMusicFolders": self._get_music_folders,
            "getArtists": self._get_artists,
            "getArtist": self._get_artist,
            "getAlbum": self._get_album,
            "getSong": self._get_song,
            "getAlbumList2": self._get_album_list2,
            "search3": self._search3,
            "getCoverArt": self._get_cover_art,
            "getGenres": self._get_genres,
            "getRandomSongs": self._get_random_songs,
            "getSongsByGenre": self._get_songs_by_genre,
            "getStarred": self._get_starred,
            "getStarred2": self._get_starred2,
            "getTopSongs": self._get_top_songs,
            "getSimilarSongs": self._get_similar_songs,
            "getSimilarSongs2": self._get_similar_songs2,
            "getArtistInfo2": self._get_artist_info2,
        }

    async def get_item(self, ref: ItemRef) -> MediaItemType:
        """
        Return the media item a Subsonic id refers to.

        :param ref: The parsed Subsonic id.
        """
        controller = self.mass.music.get_controller(ref.media_type)
        try:
            # get() also applies the access checks, such as the visibility of a playlist
            item = await controller.get(ref.item_id, ref.provider, allow_update_metadata=False)
        except (MediaNotFoundError, ProviderUnavailableError) as err:
            raise SubsonicError(ErrorCode.NOT_FOUND, str(err)) from err
        return cast("MediaItemType", item)

    async def _get_music_folders(self, request: SubsonicRequest) -> dict[str, Any]:
        folder = MusicFolder(id=MUSIC_FOLDER_ID, name="Music Assistant")
        return {"musicFolders": {"musicFolder": [folder.to_dict()]}}

    async def _get_artists(self, request: SubsonicRequest) -> dict[str, Any]:
        artists = await collect_pages(
            lambda limit, offset: self.mass.music.artists.library_items(
                limit=limit, offset=offset, sort_field=SortField.SORT_NAME
            )
        )
        counts = await artist_album_counts(self.mass, [artist.item_id for artist in artists])
        index: dict[str, list[dict[str, Any]]] = {}
        for artist in artists:
            album_count = counts.get(str(artist.item_id), 0)
            if not album_count:
                continue
            index.setdefault(_index_letter(artist), []).append(mappers.artist(artist, album_count))
        return {
            "artists": {
                "ignoredArticles": IGNORED_ARTICLES,
                "index": [{"name": letter, "artist": entries} for letter, entries in index.items()],
            }
        }

    async def _get_artist(self, request: SubsonicRequest) -> dict[str, Any]:
        ref = parse_subsonic_id(request.require("id"), MediaType.ARTIST)
        artist = cast("Artist", await self.get_item(ref))
        if ref.is_library:
            albums = await self.mass.music.artists.get_library_artist_albums(ref.item_id)
        else:
            albums = await self.mass.music.artists.albums(ref.item_id, ref.provider, limit=0)
        data = mappers.artist(artist, len(albums))
        data["album"] = await self._albums(albums)
        return {"artist": data}

    async def _get_album(self, request: SubsonicRequest) -> dict[str, Any]:
        ref = parse_subsonic_id(request.require("id"), MediaType.ALBUM)
        album = cast("Album", await self.get_item(ref))
        tracks = await self.mass.music.albums.tracks(ref.item_id, ref.provider, limit=0)
        songs = [mappers.song(track) for track in tracks]
        duration = sum(track.duration or 0 for track in tracks)
        return {"album": mappers.album(album, len(songs), duration, songs)}

    async def _get_song(self, request: SubsonicRequest) -> dict[str, Any]:
        ref = parse_subsonic_id(request.require("id"), MediaType.TRACK)
        track = cast("Track", await self.get_item(ref))
        return {"song": mappers.song(track)}

    async def _get_album_list2(self, request: SubsonicRequest) -> dict[str, Any]:
        list_type = request.require("type")
        if list_type not in ALBUM_LIST_SORTS:
            raise SubsonicError(ErrorCode.GENERIC, f"Unknown album list type: {list_type}")
        sort_field, sort_direction = ALBUM_LIST_SORTS[list_type]
        size = request.get_int("size", 10, MAX_LIST_SIZE)
        offset = request.get_int("offset", 0)
        albums: Sequence[Album]
        if list_type == "byYear":
            albums = await self._albums_by_year(request, size, offset)
        else:
            genre_ids: list[int] | None = None
            if list_type == "byGenre":
                genre_ids = await self._genre_ids(request.require("genre"))
                if not genre_ids:
                    return {"albumList2": {"album": []}}
            albums = await self.mass.music.albums.library_items(
                favorite=True if list_type == "starred" else None,
                genre=genre_ids,
                played_only=list_type == "recent",
                limit=size,
                offset=offset,
                sort_field=sort_field,
                sort_direction=sort_direction,
                summary=False,
            )
        return {"albumList2": {"album": await self._albums(albums)}}

    async def _search3(self, request: SubsonicRequest) -> dict[str, Any]:
        query = (request.get("query") or "").strip().strip('"')
        search = query or None
        artist_count = request.get_int("artistCount", DEFAULT_SEARCH_COUNT, MAX_LIST_SIZE)
        album_count = request.get_int("albumCount", DEFAULT_SEARCH_COUNT, MAX_LIST_SIZE)
        song_count = request.get_int("songCount", DEFAULT_SEARCH_COUNT, MAX_LIST_SIZE)
        artists: Sequence[Artist] = []
        albums: Sequence[Album] = []
        tracks: Sequence[Track] = []
        if artist_count:
            artists = await self.mass.music.artists.library_items(
                search=search, limit=artist_count, offset=request.get_int("artistOffset", 0)
            )
        if album_count:
            albums = await self.mass.music.albums.library_items(
                search=search,
                limit=album_count,
                offset=request.get_int("albumOffset", 0),
                summary=False,
            )
        if song_count:
            tracks = await self.mass.music.tracks.library_items(
                search=search,
                limit=song_count,
                offset=request.get_int("songOffset", 0),
                summary=False,
            )
        counts = await artist_album_counts(self.mass, [artist.item_id for artist in artists])
        return {
            "searchResult3": {
                "artist": [
                    mappers.artist(artist, counts.get(str(artist.item_id), 0)) for artist in artists
                ],
                "album": await self._albums(albums),
                "song": [mappers.song(track) for track in tracks],
            }
        }

    async def _get_cover_art(self, request: SubsonicRequest) -> web.StreamResponse:
        item = await self.get_item(parse_subsonic_id(request.require("id")))
        data = await self.mass.metadata.get_image_data_for_item(
            item, size=_thumb_size(request.get_int("size", 0))
        )
        if not data:
            raise SubsonicError(ErrorCode.NOT_FOUND, f"No cover art for {request.require('id')}")
        return web.Response(
            body=data,
            content_type=_image_content_type(data),
            headers={"Cache-Control": "max-age=86400"},
        )

    async def _get_genres(self, request: SubsonicRequest) -> dict[str, Any]:
        genres = []
        offset = 0
        while page := await self.mass.music.genres.library_items(
            hide_empty=True, limit=MAX_LIST_SIZE, offset=offset
        ):
            genres.extend(page)
            offset += len(page)
        counts = await self.mass.music.genres.get_genre_media_counts(
            [str(genre.item_id) for genre in genres]
        )
        return {
            "genres": {
                "genre": [
                    {
                        "value": genre.name,
                        "songCount": counts.get(str(genre.item_id), {}).get(MediaType.TRACK, 0),
                        "albumCount": counts.get(str(genre.item_id), {}).get(MediaType.ALBUM, 0),
                    }
                    for genre in genres
                ]
            }
        }

    async def _get_random_songs(self, request: SubsonicRequest) -> dict[str, Any]:
        genre_ids: list[int] | None = None
        if genre := request.get("genre"):
            genre_ids = await self._genre_ids(genre)
            if not genre_ids:
                return {"randomSongs": {"song": []}}
        tracks = await self.mass.music.tracks.library_items(
            genre=genre_ids,
            limit=request.get_int("size", 10, MAX_LIST_SIZE),
            sort_field=SortField.RANDOM,
            summary=False,
        )
        return {"randomSongs": {"song": [mappers.song(track) for track in tracks]}}

    async def _get_songs_by_genre(self, request: SubsonicRequest) -> dict[str, Any]:
        genre_ids = await self._genre_ids(request.require("genre"))
        tracks: Sequence[Track] = []
        if genre_ids:
            tracks = await self.mass.music.tracks.library_items(
                genre=genre_ids,
                limit=request.get_int("count", 10, MAX_LIST_SIZE),
                offset=request.get_int("offset", 0),
                summary=False,
            )
        return {"songsByGenre": {"song": [mappers.song(track) for track in tracks]}}

    async def _get_starred(self, request: SubsonicRequest) -> dict[str, Any]:
        return {"starred": await self._starred()}

    async def _get_starred2(self, request: SubsonicRequest) -> dict[str, Any]:
        return {"starred2": await self._starred()}

    async def _get_top_songs(self, request: SubsonicRequest) -> dict[str, Any]:
        count = request.get_int("count", 50, MAX_LIST_SIZE)
        ref: ItemRef | None
        if (subsonic_id := request.get("id")) is not None:
            ref = parse_subsonic_id(subsonic_id, MediaType.ARTIST)
        else:
            ref = await self._artist_by_name(request.require("artist"))
        tracks: Sequence[Track] = []
        if ref is not None:
            tracks = await self.mass.music.artists.top_tracks(
                ref.item_id, ref.provider, limit=count
            )
        return {"topSongs": {"song": [mappers.song(track) for track in tracks]}}

    async def _get_similar_songs(self, request: SubsonicRequest) -> dict[str, Any]:
        return {"similarSongs": {"song": await self._similar_songs(request)}}

    async def _get_similar_songs2(self, request: SubsonicRequest) -> dict[str, Any]:
        return {"similarSongs2": {"song": await self._similar_songs(request)}}

    async def _get_artist_info2(self, request: SubsonicRequest) -> dict[str, Any]:
        ref = parse_subsonic_id(request.require("id"), MediaType.ARTIST)
        artist = await self.get_item(ref)
        info: dict[str, Any] = {"similarArtist": []}
        if artist.metadata and artist.metadata.description:
            info["biography"] = artist.metadata.description
        if artist.mbid:
            info["musicBrainzId"] = artist.mbid
        return {"artistInfo2": info}

    async def _albums(self, albums: Sequence[Album]) -> list[dict[str, Any]]:
        library_ids = [album.item_id for album in albums if album.provider == "library"]
        stats = await album_track_stats(self.mass, library_ids)
        return [mappers.album(album, *stats.get(str(album.item_id), (0, 0))) for album in albums]

    async def _albums_by_year(
        self, request: SubsonicRequest, size: int, offset: int
    ) -> Sequence[Album]:
        from_year = request.get_int("fromYear", 0)
        to_year = request.get_int("toYear", utc().year)
        direction = SortDirection.ASC if from_year <= to_year else SortDirection.DESC
        controller = self.mass.music.albums
        params: dict[str, Any] = {
            "year_low": min(from_year, to_year),
            "year_high": max(from_year, to_year),
        }
        # the same restrictions library_items applies: the caller's sources and listing filter
        visible_sources = visible_music_sources(self.mass, request.user)
        albums = await controller.get_library_items_by_query(
            limit=size,
            offset=offset,
            sort_field=SortField.YEAR,
            sort_direction=direction,
            provider_filter=sorted(visible_sources) if visible_sources is not None else None,
            extra_query_parts=[
                *controller.listing_filter(params),
                "albums.year BETWEEN :year_low AND :year_high",
            ],
            extra_query_params=params,
            in_library_only=True,
        )
        return list(albums)

    async def _starred(self) -> dict[str, Any]:
        music = self.mass.music
        artists = await collect_pages(
            lambda limit, offset: music.artists.library_items(
                favorite=True, limit=limit, offset=offset
            )
        )
        albums = await collect_pages(
            lambda limit, offset: music.albums.library_items(
                favorite=True, limit=limit, offset=offset, summary=False
            )
        )
        tracks = await collect_pages(
            lambda limit, offset: music.tracks.library_items(
                favorite=True, limit=limit, offset=offset, summary=False
            )
        )
        counts = await artist_album_counts(self.mass, [artist.item_id for artist in artists])
        return {
            "artist": [
                mappers.artist(artist, counts.get(str(artist.item_id), 0)) for artist in artists
            ],
            "album": await self._albums(albums),
            "song": [mappers.song(track) for track in tracks],
        }

    async def _similar_songs(self, request: SubsonicRequest) -> list[dict[str, Any]]:
        ref = parse_subsonic_id(
            request.require("id"), MediaType.TRACK, MediaType.ARTIST, MediaType.ALBUM
        )
        count = request.get_int("count", 50, MAX_LIST_SIZE)
        tracks: Sequence[Track]
        if ref.media_type == MediaType.TRACK:
            tracks = await self.mass.music.tracks.similar_tracks(
                ref.item_id, ref.provider, limit=count
            )
        elif ref.media_type == MediaType.ARTIST:
            tracks = await self.mass.music.artists.top_tracks(
                ref.item_id, ref.provider, limit=count
            )
        else:
            album_tracks = await self.mass.music.albums.tracks(ref.item_id, ref.provider, limit=1)
            if not album_tracks:
                return []
            seed = album_tracks[0]
            tracks = await self.mass.music.tracks.similar_tracks(
                seed.item_id, seed.provider, limit=count
            )
        return [mappers.song(track) for track in tracks]

    async def _genre_ids(self, name: str) -> list[int]:
        genres = await self.mass.music.genres.library_items(search=name, hide_empty=True, limit=50)
        return [int(genre.item_id) for genre in genres if genre.name.lower() == name.lower()]

    async def _artist_by_name(self, name: str) -> ItemRef | None:
        artists = await self.mass.music.artists.library_items(search=name, limit=25)
        for artist in artists:
            if artist.name.lower() == name.lower():
                return ItemRef(MediaType.ARTIST, str(artist.item_id))
        return None


def _index_letter(artist: Artist) -> str:
    first = (artist.sort_name or artist.name)[:1].upper()
    return first if first.isalpha() else "#"


def _thumb_size(requested: int) -> int:
    if not requested:
        return 0
    return next((size for size in THUMB_SIZES if size >= requested), THUMB_SIZES[-1])


def _image_content_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:3] == b"GIF":
        return "image/gif"
    return "image/jpeg"
