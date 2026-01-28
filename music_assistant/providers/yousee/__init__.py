"""YouSee Musik musicprovider support for MusicAssistant."""

from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import (
    AlbumType,
    ConfigEntryType,
    ContentType,
    ExternalID,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    InvalidDataError,
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
    RecommendationFolder,
    SearchResults,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import (
    CONF_PASSWORD,
    CONF_USERNAME,
    VARIOUS_ARTISTS_MBID,
    VARIOUS_ARTISTS_NAME,
)
from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.datetime import iso_from_utc_timestamp, utc_timestamp
from music_assistant.helpers.throttle_retry import ThrottlerManager, throttle_with_retries
from music_assistant.helpers.util import (
    infer_album_type,
    lock,
    parse_title_and_version,
    try_parse_int,
)
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.RECOMMENDATIONS,
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.LIBRARY_ARTISTS_EDIT,
    ProviderFeature.LIBRARY_ALBUMS_EDIT,
    ProviderFeature.LIBRARY_TRACKS_EDIT,
    ProviderFeature.LIBRARY_PLAYLISTS_EDIT,
    ProviderFeature.PLAYLIST_TRACKS_EDIT,
    ProviderFeature.PLAYLIST_CREATE,
    ProviderFeature.SIMILAR_TRACKS,
}

VARIOUS_ARTISTS_ID = 1776

PAGE_SIZE = 50
# to avoid infinite loops, this effectively limits any album/playlist to
# PAGE_SIZE * MAX_PAGES_PAGINATED items (1000 items with the current settings)
MAX_PAGES_PAGINATED = 20
GET_POPULAR_TRACKS_LIMIT = 25

PLAYBACK_QUALITY = "KBPS_320"

JsonLike = dict[str, Any]


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    # setup is called when the user wants to setup a new provider instance.
    # you are free to do any preflight checks here and but you must return
    #  an instance of the provider.
    return YouSeeMusikProvider(mass, manifest, config, SUPPORTED_FEATURES)


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


YOUSEE_GRAPHQL_ENDPOINT = "https://graphql-1458.api.247e.com/graphql"


class YouSeeAccessToken:
    """YouSee Musik access token wrapper."""

    def __init__(self, access_token: str) -> None:
        """Initialize YouSeeAccessToken."""
        self._access_token = access_token
        self._token_parts = self._parse_access_token(access_token)

    def is_expired(self) -> bool:
        """Return True if token is expired."""
        expires_at = try_parse_int(self._token_parts.get("ExpiresOn", 0))
        return not expires_at or expires_at <= time.time()

    def _parse_access_token(self, token: str) -> JsonLike:
        return dict(part.split("=", 1) for part in token.split("&") if "=" in part)

    def __str__(self) -> str:
        """Return string representation of the access token."""
        return self._access_token


class YouSeeGraphQLError(Exception):
    """YouSee Musik GraphQL error."""

    def __init__(self, data: JsonLike) -> None:
        """Initialize YouSeeGraphQLError."""
        super().__init__(json.dumps(data))


class YouSeeMusikProvider(MusicProvider):
    """Provider implementation for YouSee Musik."""

    _access_token: YouSeeAccessToken | None = None
    _refresh_token: str | None = None

    # rate limiter needs to be specified on provider-level,
    # so make it an instance attribute
    # Unsure if yousee enforces rate limiting, this is just a sane precaution
    throttler = ThrottlerManager(rate_limit=4, period=1)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        if not self.config.get_value(CONF_USERNAME) or not self.config.get_value(CONF_PASSWORD):
            msg = "Invalid login credentials"
            raise LoginFailed(msg)
        # try to get a token, raise if that fails
        token = await self._auth_token()
        if not token:
            msg = f"Login failed for user {self.config.get_value(CONF_USERNAME)}"
            raise LoginFailed(msg)

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """Perform search on musicprovider.

        :param search_query: Search query.
        :param media_types: A list of media_types to include.
        :param limit: Number of items to return in the search (per type).
        """
        sections = {
            MediaType.TRACK: """
                tracks(first: $first) {
                        totalCount
                        pageInfo {
                            endCursor
                            hasNextPage
                        }
                        items {
                            id
                            title
                            availableToStream
                            album {
                                id
                                title
                            }
                            artist {
                                id
                                title
                                cover(size: $imageSize)
                            }
                            cover(size: $imageSize)
                            duration
                            share
                            genre
                            isrc
                            playbackContext
                            featuredArtists {
                                items {
                                    id
                                    title
                                    cover(size: $imageSize)
                                }
                            }
                        }
                    }
                """,
            MediaType.ALBUM: """
                albums(first: $first) {
                    totalCount
                    pageInfo {
                        endCursor
                        hasNextPage
                    }
                    items {
                        id
                        title
                        cover(size: $imageSize)
                        artist {
                            id
                            title
                            cover(size: $imageSize)
                        }
                    }
                }
            """,
            MediaType.ARTIST: """
                artists(first: $first) {
                    totalCount
                    pageInfo {
                        endCursor
                        hasNextPage
                    }
                    items {
                        id
                        title
                        cover(size: $imageSize)
                        share
                    }
                }
            """,
            MediaType.PLAYLIST: """
                playlists(first: $first) {
                    totalCount
                    pageInfo {
                        hasNextPage
                        endCursor
                    }
                    items {
                        id
                        title
                        isOwned
                        share
                        cover(size: $imageSize)
                        description
                    }
                }
            """,
        }

        search_result = SearchResults()

        media_types = [x for x in media_types if x in (sections)]

        if not media_types:
            return search_result

        query = """
        query searchMixedSections($criterion: String!, $imageSize: Int = 512, $first: Int = 5) {
            search(criterion: $criterion) {
                TRACK_SECTION
                ALBUM_SECTION
                PLAYLIST_SECTION
                ARTIST_SECTION
            }
        }
        """
        for media_type, section in sections.items():
            if media_type in media_types:
                query = query.replace(f"{media_type.name}_SECTION", section)
            else:
                query = query.replace(f"{media_type.name}_SECTION", "")

        variables = {
            "criterion": search_query,
            "imageSize": 512,
            "first": limit,
        }

        result = await self._post_graphql(query, variables)

        result = result.get("data", {}).get("search", {})

        if not result:
            return search_result

        if "artists" in result:
            search_result.artists = [
                self._parse_artist(item) for item in result["artists"].get("items", [])
            ]
        if "albums" in result:
            search_result.albums = [
                await self._parse_album(item) for item in result["albums"].get("items", [])
            ]
        if "tracks" in result:
            search_result.tracks = [
                await self._parse_track(item) for item in result["tracks"].get("items", [])
            ]
        if "playlists" in result:
            search_result.playlists = [
                await self._parse_playlist(item) for item in result["playlists"].get("items", [])
            ]

        return search_result

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve library artists from the provider."""
        query = """
        query favoriteArtists($first: Int!, $after: String, $imageSize: Int = 512) {
            me {
                favorites {
                    artists(first: $first, after: $after) {
                        totalCount,
                        pageInfo {
                            endCursor
                            hasNextPage
                        }
                        items {
                            id
                            title
                            cover(size: $imageSize)
                            share
                        }
                    }
                }
            }
        }
        """
        variables = {"imageSize": 512}

        async for item in self._paginate_graphql(
            query, variables, ["data", "me", "favorites", "artists"]
        ):
            self.logger.debug("Parsing artist item: %s", item)
            yield self._parse_artist(item)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve library albums from the provider."""
        query = """
        query favoriteAlbums($first: Int!, $after: String, $imageSize: Int = 512) {
            me {
                favorites {
                    albums(first: $first, after: $after) {
                        totalCount,
                        pageInfo {
                            endCursor
                            hasNextPage
                        }
                        items {
                            id
                            title
                            cover(size: $imageSize)
                            artist {
                                id
                                title
                                cover(size: $imageSize)
                            }
                        }
                    }
                }
            }
        }
        """
        variables = {"imageSize": 512}

        async for item in self._paginate_graphql(
            query, variables, ["data", "me", "favorites", "albums"]
        ):
            self.logger.debug("Parsing album item: %s", item)
            yield await self._parse_album(item)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from the provider."""
        query = """
            query favoriteTracks($first: Int!, $after: String, $imageSize: Int = 512) {
                me {
                    favorites {
                    tracks(first: $first, after: $after) {
                        totalCount
                        pageInfo {
                            endCursor
                            hasNextPage
                        }
                        items {
                            id
                            title
                            availableToStream
                            album {
                                id
                                title
                            }
                            artist {
                                id
                                title
                                cover(size: $imageSize)
                            }
                            cover(size: $imageSize)
                            duration
                            share
                            genre
                            isrc
                            playbackContext
                            featuredArtists {
                                items {
                                    id
                                    title
                                    cover(size: $imageSize)
                                    }
                                }
                            }
                        }
                    }
                }
            }
        """
        variables = {"imageSize": 512}

        async for item in self._paginate_graphql(
            query, variables, ["data", "me", "favorites", "tracks"]
        ):
            self.logger.debug("Parsing track item: %s", item)
            yield await self._parse_track(item)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve library/subscribed playlists from the provider."""
        query = """
            query favoritePlaylists($first: Int!, $after: String, $imageSize: Int = 512) {
                me {
                    favorites {
                        playlists(first: $first, after: $after) {
                            totalCount
                            pageInfo {
                                hasNextPage
                                endCursor
                            }
                            items {
                                id
                                title
                                isOwned
                                share
                                cover(size: $imageSize)
                                description
                            }
                        }
                    }
                }
            }
        """
        variables = {"imageSize": 512}
        async for item in self._paginate_graphql(
            query, variables, ["data", "me", "favorites", "playlists"]
        ):
            self.logger.debug("Parsing playlist item: %s", item)
            yield await self._parse_playlist(item)

    @use_cache(3600 * 24 * 30)  # Cache for 30 days
    async def get_artist(self, prov_artist_id: str) -> Artist:  # type: ignore[empty-body]
        """Get full artist details by id."""
        query = """
            query Catalog($id: ID!, $imageSize: Int = 512) {
                catalog {
                    artist(id: $id) {
                        id
                        title
                        cover(size: $imageSize)
                        share
                    }
                }
            }
        """
        variables = {"id": prov_artist_id, "imageSize": 512}

        result = await self._post_graphql(query, variables)
        if not result or not result.get("data", {}).get("catalog", {}).get("artist"):
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found")
        return self._parse_artist(result["data"]["catalog"]["artist"])

    @use_cache(3600 * 24 * 14)  # Cache for 14 days
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:  # type: ignore[empty-body]
        """Get a list of all albums for the given artist."""
        query = """
            query Catalog($id: ID!, $imageSize: Int = 512, $first: Int = 50, $after: String) {
                catalog {
                    artist(id: $id) {
                        id
                        albums(first: $first, after: $after) {
                            totalCount
                            pageInfo {
                                hasNextPage
                                endCursor
                            }
                            items {
                                id
                                title
                                cover(size: $imageSize)
                            }
                        }
                    }
                }
            }
        """

        albums = []
        variables = {
            "id": prov_artist_id,
            "imageSize": 512,
        }

        async for item in self._paginate_graphql(
            query,
            variables,
            ["data", "catalog", "artist", "albums"],
        ):
            albums.append(await self._parse_album(item))

        return albums

    @use_cache(3600 * 24 * 14)  # Cache for 14 days
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:  # type: ignore[empty-body]
        """Get a list of most popular tracks for the given artist."""
        query = """
            query Catalog($id: ID!, $imageSize: Int = 512, $first: Int = 25) {
                catalog {
                    artist(id: $id) {
                        id
                        title
                        cover(size: $imageSize)
                        share
                        tracks(first: $first, after: null, orderBy: POPULARITY) {
                            items {
                                id
                                title
                                cover(size: $imageSize)
                                isrc
                                duration
                                label
                                artist {
                                    id
                                    title
                                    cover(size: $imageSize)
                                }
                                featuredArtists {
                                    items {
                                    id
                                    title
                                    cover(size: $imageSize)
                                    }
                                }
                                share
                                playbackContext
                                genre
                            }
                        }
                    }
                }
            }
        """

        variables = {
            "id": prov_artist_id,
            "imageSize": 512,
            "first": GET_POPULAR_TRACKS_LIMIT,
        }

        result = await self._post_graphql(query, variables)

        if not result or not result.get("data", {}).get("catalog", {}).get("artist"):
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found")
        tracks = []

        for item in result["data"]["catalog"]["artist"]["tracks"]["items"]:
            tracks.append(await self._parse_track(item))

        return tracks

    @use_cache(3600 * 24 * 30)  # Cache for 30 days
    async def get_album(self, prov_album_id: str) -> Album:  # type: ignore[empty-body]
        """Get full album details by id."""
        query = """
            query Catalog($id: ID!, $imageSize: Int = 512) {
                catalog {
                    album(id: $id) {
                        id
                        title
                        tracksCount
                        genre
                        label
                        releaseDate
                        available
                        upc
                        type
                        share
                        cover(size: $imageSize)
                        artist {
                            id
                            title
                            cover(size: $imageSize)
                        }
                        featuredArtists {
                            items {
                                id
                                title
                                cover(size: $imageSize)
                            }
                        }
                    }
                }
            }
        """
        variables = {"id": prov_album_id, "imageSize": 512}

        result = await self._post_graphql(query, variables)
        if not result or not result.get("data", {}).get("catalog", {}).get("album"):
            raise MediaNotFoundError(f"Album {prov_album_id} not found")
        return await self._parse_album(result["data"]["catalog"]["album"])

    @use_cache(3600 * 24 * 30)  # Cache for 30 days
    async def get_track(self, prov_track_id: str) -> Track:  # type: ignore[empty-body]
        """Get full track details by id."""
        query = """
        query getTrack($id: ID!,  $imageSize: Int = 512) {
            catalog {
                track(id: $id) {
                    id
                    title
                    duration
                    genre
                    label
                    releaseDate
                    availableToStream
                    isrc
                    playbackContext
                    share
                    cover(size: $imageSize)
                    album {
                        id
                        title
                    }
                    artist {
                        id
                        title
                        cover(size: $imageSize)
                    }
                    featuredArtists {
                        items {
                            id
                            title
                            cover(size: $imageSize)
                        }
                    }
                }
            }
        }
        """
        variables = {"id": prov_track_id, "imageSize": 512}

        result = await self._post_graphql(query, variables)
        if not result or not result.get("data", {}).get("catalog", {}).get("track"):
            raise MediaNotFoundError(f"Track {prov_track_id} not found")
        return await self._parse_track(result["data"]["catalog"]["track"])

    @use_cache(3600 * 24 * 30)  # Cache for 30 days
    async def get_playlist(self, prov_playlist_id: str) -> Playlist:  # type: ignore[empty-body]
        """Get full playlist details by id."""
        query = """
        query getPlaylist($id: ID!,  $imageSize: Int = 512) {
            playlists {
                playlist(id: $id) {
                    id
                    title
                    description
                    tracksCount
                    createdAt
                    isOwned
                    share
                    cover(size: $imageSize)
                }
            }
        }
        """
        variables = {"id": prov_playlist_id, "imageSize": 512}

        result = await self._post_graphql(query, variables)
        if not result or not result.get("data", {}).get("playlists", {}).get("playlist"):
            raise MediaNotFoundError(f"Playlist {prov_playlist_id} not found")

        return await self._parse_playlist(result["data"]["playlists"]["playlist"])

    @use_cache(3600 * 24 * 30)  # Cache for 30 days
    async def get_album_tracks(  # type: ignore[empty-body]
        self,
        prov_album_id: str,
    ) -> list[Track]:
        """Get album tracks for given album id."""
        query = """
            query GetAlbum($id: ID!, $imageSize: Int = 512, $first: Int = 50, $after: String) {
                catalog {
                    album(id: $id) {
                        id
                        tracks(first: $first, after: $after) {
                            items {
                                id
                                title
                                cover(size: $imageSize)
                                isrc
                                duration
                                label
                                artist {
                                    id
                                    title
                                    cover(size: $imageSize)
                                }
                                featuredArtists {
                                    items {
                                    id
                                    title
                                    cover(size: $imageSize)
                                    }
                                }
                                share
                                playbackContext
                                genre
                            }
                            pageInfo {
                                hasNextPage
                                endCursor
                            }
                        }
                    }
                }
            }
        """
        tracks = []
        variables = {
            "id": prov_album_id,
            "imageSize": 512,
        }

        i = 1
        async for item in self._paginate_graphql(
            query,
            variables,
            ["data", "catalog", "album", "tracks"],
        ):
            track = await self._parse_track(item)
            track.position = i
            tracks.append(track)
            i += 1

        return tracks

    @use_cache(3600 * 3)  # Cache for 3 hours
    async def get_playlist_tracks(  # type: ignore[empty-body]
        self,
        prov_playlist_id: str,
        page: int = 0,
    ) -> list[Track]:
        """Get all playlist tracks for given playlist id."""
        query = """
        query getPlaylist($id: ID!, $imageSize: Int = 512, $first: Int = 50, $after: String) {
            playlists {
                playlist(id: $id) {
                    id
                    tracks(first: $first, after: $after) {
                        items {
                            id
                            title
                            cover(size: $imageSize)
                            isrc
                            duration
                            label
                            artist {
                                id
                                title
                                cover(size: $imageSize)
                            }
                            featuredArtists {
                                items {
                                id
                                title
                                cover(size: $imageSize)
                                }
                            }
                            share
                            playbackContext
                            genre
                        }
                        pageInfo {
                            hasNextPage
                            endCursor
                        }
                    }
                }
            }
        }
        """
        tracks: list[Track] = []

        if page > 0:
            # paging not supported, we always return the whole list at once
            return []
        # TODO: access the underlying paging on the yousee api (if possible))

        variables = {
            "id": prov_playlist_id,
            "imageSize": 512,
        }

        i = 1
        async for item in self._paginate_graphql(
            query, variables, ["data", "playlists", "playlist", "tracks"]
        ):
            track = await self._parse_track(item)
            track.position = i
            tracks.append(track)
            i += 1

        return tracks

    async def library_add(self, item: MediaItemType) -> bool:
        """Add item to provider's library. Return true on success."""
        if item.media_type not in (
            MediaType.ARTIST,
            MediaType.ALBUM,
            MediaType.TRACK,
            MediaType.PLAYLIST,
        ):
            raise InvalidDataError(
                f"Cannot add media type {item.media_type} to library for provider {self.name}"
            )

        media_type_str = item.media_type.capitalize()

        query = f"""
            mutation addToLibrary($id: ID!) {{
                favorites {{
                    add{media_type_str} (id: $id) {{
                        ok
                    }}
                }}
            }}
        """
        variables = {"id": item.item_id}

        result = await self._post_graphql(query, variables)

        return (
            result.get("data", {})
            .get("favorites", {})
            .get(f"add{media_type_str}", {})
            .get("ok", False)
        )

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove item from provider's library. Return true on success."""
        if media_type not in (
            MediaType.ARTIST,
            MediaType.ALBUM,
            MediaType.TRACK,
            MediaType.PLAYLIST,
        ):
            raise InvalidDataError(
                f"Cannot remove media type {media_type} from library for provider {self.name}"
            )

        media_type_str = media_type.capitalize()

        query = f"""
            mutation removeFromLibrary($id: ID!) {{
                favorites {{
                    remove{media_type_str} (id: $id) {{
                        ok
                    }}
                }}
            }}
        """
        variables = {"id": prov_item_id}

        result = await self._post_graphql(query, variables)

        return (
            result.get("data", {})
            .get("favorites", {})
            .get(f"remove{media_type_str}", {})
            .get("ok", False)
        )

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add track(s) to playlist."""
        query = """
            mutation addToLibrary( $id: ID!, $trackIds: [ID]!) {
                playlists {
                    addTracks(id: $id, duplicatesHandling: SKIP_DUPLICATES, trackIds: $trackIds) {
                        ok
                    }
                }
            }
        """
        variables = {"id": prov_playlist_id, "trackIds": prov_track_ids}
        result = await self._post_graphql(query, variables)

        if not result or not result.get("data", {}).get("playlists", {}).get("addTracks", {}).get(
            "ok"
        ):
            raise MediaNotFoundError(
                f"Could not add tracks to playlist {prov_playlist_id}: {prov_track_ids}"
            )

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist."""
        # Since we get positions, we need to obtain fresh copy of playlist

        query = """
            mutation addToLibrary($id: ID!, $mods: [ModifyPlaylistTrackInput!]!) {
                playlists {
                    modifyTracks(id: $id, modifications: $mods) {
                        ok
                    }
                }
            }

        """

        mods = [
            {"positionFrom": pos - 1, "type": "REMOVE"}
            for pos in sorted(positions_to_remove, reverse=True)
        ]

        variables = {"id": prov_playlist_id, "mods": mods}

        result = await self._post_graphql(query, variables)

        if not result or not result.get("data", {}).get("playlists", {}).get(
            "modifyTracks", {}
        ).get("ok"):
            raise MediaNotFoundError(
                f"Could not remove tracks from playlist {prov_playlist_id}: {positions_to_remove}"
            )

    async def create_playlist(self, name: str) -> Playlist:  # type: ignore[empty-body]
        """Create a new playlist on provider with given name."""
        query = """
            mutation createPlaylist($title: String!, $imageSize: Int = 512) {
                playlists {
                    create(playlist: {title: $title}) {
                        playlist {
                            id
                            title
                            description
                            tracksCount
                            createdAt
                            isOwned
                            share
                            cover(size: $imageSize)
                        }
                    }
                }
            }
        """
        variables = {"title": name, "imageSize": 512}
        result = await self._post_graphql(query, variables)
        if not result or not result.get("data", {}).get("playlists", {}).get("create", {}).get(
            "playlist"
        ):
            raise MediaNotFoundError(f"Could not create playlist {name}")

        return await self._parse_playlist(result["data"]["playlists"]["create"]["playlist"])

    @use_cache(3600 * 24)  # Cache for 24 hours
    async def get_similar_tracks(  # type: ignore[empty-body]
        self, prov_track_id: str, limit: int = 25
    ) -> list[Track]:
        """Retrieve a dynamic list of similar tracks based on the provided track."""
        query = """
            query similarTracks($id: ID!, $first: Int = 25, $imageSize: Int = 512) {
                catalog {
                    track(id: $id) {
                        id
                        similarTracks(first: $first) {
                            items {
                                id
                                title
                                cover(size: $imageSize)
                                isrc
                                duration
                                label
                                artist {
                                    id
                                    title
                                    cover(size: $imageSize)
                                }
                                featuredArtists {
                                    items {
                                    id
                                    title
                                    cover(size: $imageSize)
                                    }
                                }
                                share
                                playbackContext
                                genre
                            }
                        }
                    }
                }
            }
        """

        variables = {
            "id": prov_track_id,
            "first": limit,
            "imageSize": 512,
        }
        result = await self._post_graphql(query, variables)
        if not result or not result.get("data", {}).get("catalog", {}).get("track"):
            raise MediaNotFoundError(f"Track {prov_track_id} not found")

        return [
            await self._parse_track(item)
            for item in result["data"]["catalog"]["track"]["similarTracks"]["items"]
        ]

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a track."""
        query = """
            query playbackFull($id: ID!, $quality: StreamQuality!) {
                playback(trackId: $id) {
                    full(quality: $quality)
                }
            }
        """

        if media_type != MediaType.TRACK:
            raise MediaNotFoundError(f"Streaming of media type {media_type} is not supported")

        variables = {"id": item_id, "quality": PLAYBACK_QUALITY}

        result = await self._post_graphql(query, variables)

        playback_url = result.get("data", {}).get("playback", {}).get("full")
        if not playback_url:
            raise ResourceTemporarilyUnavailable(f"Track {item_id} is not available for streaming")

        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.MP4,
                bit_rate=320000,
            ),
            media_type=MediaType.TRACK,
            stream_type=StreamType.HLS,
            allow_seek=True,
            can_seek=True,
            path=playback_url,
            data={"start_ts": utc_timestamp()},
        )

    async def on_streamed(
        self,
        streamdetails: StreamDetails,
    ) -> None:
        """
        Handle callback when given streamdetails completed streaming.

        To get the number of seconds streamed, see streamdetails.seconds_streamed.
        To get the number of seconds seeked/skipped, see streamdetails.seek_position.
        Note that seconds_streamed is the total streamed seconds, so without seeked time.

        NOTE: Due to internal and player buffering,
        this may be called in advance of the actual completion.
        """
        mutation = """
            mutation reportPlayback($report: ReportPlaybackInput!) {
                reportPlayback(report: $report) {
                    ok
                }
            }
        """

        seconds_streamed = min(
            utc_timestamp() - streamdetails.data["start_ts"],
            streamdetails.seconds_streamed,
        )

        variables = {
            "playbackUrl": streamdetails.path,
            "playbackContext": next(
                iter((await self.get_track(streamdetails.item_id)).provider_mappings)
            ).details,  # TODO Is there a better way to obtain the playbackContext? This does not seem intended.
            "playedSeconds": int(seconds_streamed),
            "playedAt": iso_from_utc_timestamp(utc_timestamp()),
        }

        result = await self._post_graphql(mutation, {"report": variables})

        if not result.get("data", {}).get("reportPlayback", {}).get("ok"):
            self.logger.warning(
                "Reporting playback for track %s failed with result %s",
                streamdetails.item_id,
                result,
            )

    @use_cache(3600 * 24)  # Cache for 1 day
    async def recommendations(self) -> list[RecommendationFolder]:
        """
        Get this provider's recommendations.

        Returns an actual (and often personalised) list of recommendations
        from this provider for the user/account.
        """
        query = """
            query Recommendations($imageSize: Int = 512, $first: Int = 50) {
                me {
                    recommendations {
                        albumRecommendations: recommendation(id: "discoveralbums") {
                            id
                            title
                            subtitle
                            description
                            cover(size: $imageSize)
                            ... on AlbumsRecommendation {
                                albums(first: $first) {
                                    items {
                                        id
                                        title
                                        tracksCount
                                        genre
                                        label
                                        releaseDate
                                        available
                                        upc
                                        type
                                        share
                                        cover(size: $imageSize)
                                        artist {
                                            id
                                            title
                                            cover(size: $imageSize)
                                        }
                                        featuredArtists {
                                            items {
                                                id
                                                title
                                                cover(size: $imageSize)
                                            }
                                        }
                                    }
                                }
                            }
                        }
                        trackRecommendations: recommendation(id: "discovertracks") {
                            id
                            title
                            subtitle
                            description
                            cover(size: $imageSize)
                            ... on TracksRecommendation {
                                tracks(first: $first) {
                                    items {
                                        id
                                        title
                                        cover(size: $imageSize)
                                        isrc
                                        duration
                                        label
                                        artist {
                                            id
                                            title
                                            cover(size: $imageSize)
                                        }
                                        featuredArtists {
                                            items {
                                                id
                                                title
                                                cover(size: $imageSize)
                                            }
                                        }
                                        share
                                        playbackContext
                                        genre
                                    }
                                }
                            }
                        }
                        weeklyDiscoveries: recommendation(id: "weeklyDiscoveries") {
                            id
                            title
                            subtitle
                            description
                            cover(size: $imageSize)
                            ... on TracksRecommendation {
                                tracks(first: $first) {
                                    items {
                                        id
                                        title
                                        cover(size: $imageSize)
                                        isrc
                                        duration
                                        label
                                        artist {
                                            id
                                            title
                                            cover(size: $imageSize)
                                        }
                                        featuredArtists {
                                            items {
                                                id
                                                title
                                                cover(size: $imageSize)
                                            }
                                        }
                                        share
                                        playbackContext
                                        genre
                                    }
                                }
                            }
                        }
                        trackRecommendationsFirstMostPlayed: recommendation(
                            id: "tracksbasedonfirstmostplayedartist"
                        ) {
                            id
                            title
                            subtitle
                            description
                            cover(size: $imageSize)
                            ... on TracksRecommendation {
                                tracks(first: $first) {
                                    items {
                                        id
                                        title
                                        cover(size: $imageSize)
                                        isrc
                                        duration
                                        label
                                        artist {
                                            id
                                            title
                                            cover(size: $imageSize)
                                        }
                                        featuredArtists {
                                            items {
                                                id
                                                title
                                                cover(size: $imageSize)
                                            }
                                        }
                                        share
                                        playbackContext
                                        genre
                                    }
                                }
                            }
                        }
                        trackRecommendationsSecondMostPlayed: recommendation(
                            id: "tracksbasedonSecondmostplayedartist"
                        ) {
                            id
                            title
                            subtitle
                            description
                            cover(size: $imageSize)
                            ... on TracksRecommendation {
                                tracks(first: $first) {
                                    items {
                                        id
                                        title
                                        cover(size: $imageSize)
                                        isrc
                                        duration
                                        label
                                        artist {
                                            id
                                            title
                                            cover(size: $imageSize)
                                        }
                                        featuredArtists {
                                            items {
                                                id
                                                title
                                                cover(size: $imageSize)
                                            }
                                        }
                                        share
                                        playbackContext
                                        genre
                                    }
                                }
                            }
                        }
                        historyTopTracks: recommendation(
                            id: "toptracks"
                        ) {
                            id
                            title
                            subtitle
                            description
                            cover(size: $imageSize)
                            ... on TracksRecommendation {
                                tracks(first: $first) {
                                    items {
                                        id
                                        title
                                        cover(size: $imageSize)
                                        isrc
                                        duration
                                        label
                                        artist {
                                            id
                                            title
                                            cover(size: $imageSize)
                                        }
                                        featuredArtists {
                                            items {
                                                id
                                                title
                                                cover(size: $imageSize)
                                            }
                                        }
                                        share
                                        playbackContext
                                        genre
                                    }
                                }
                            }
                        }
                        historyRecentTracks: recommendation(
                            id: "recenttracks"
                        ) {
                            id
                            title
                            subtitle
                            description
                            cover(size: $imageSize)
                            ... on TracksRecommendation {
                                tracks(first: $first) {
                                    items {
                                        id
                                        title
                                        cover(size: $imageSize)
                                        isrc
                                        duration
                                        label
                                        artist {
                                            id
                                            title
                                            cover(size: $imageSize)
                                        }
                                        featuredArtists {
                                            items {
                                                id
                                                title
                                                cover(size: $imageSize)
                                            }
                                        }
                                        share
                                        playbackContext
                                        genre
                                    }
                                }
                            }
                        }
                        yourmix1: recommendation(
                            id: "yourmix"
                        ) {
                            id
                            title
                            subtitle
                            description
                            cover(size: $imageSize)
                            ... on TracksRecommendation {
                                tracks(first: $first) {
                                    items {
                                        id
                                        title
                                        cover(size: $imageSize)
                                        isrc
                                        duration
                                        label
                                        artist {
                                            id
                                            title
                                            cover(size: $imageSize)
                                        }
                                        featuredArtists {
                                            items {
                                                id
                                                title
                                                cover(size: $imageSize)
                                            }
                                        }
                                        share
                                        playbackContext
                                        genre
                                    }
                                }
                            }
                        }
                        yourmix2: recommendation(
                            id: "yourmix2"
                        ) {
                            id
                            title
                            subtitle
                            description
                            cover(size: $imageSize)
                            ... on TracksRecommendation {
                                tracks(first: $first) {
                                    items {
                                        id
                                        title
                                        cover(size: $imageSize)
                                        isrc
                                        duration
                                        label
                                        artist {
                                            id
                                            title
                                            cover(size: $imageSize)
                                        }
                                        featuredArtists {
                                            items {
                                                id
                                                title
                                                cover(size: $imageSize)
                                            }
                                        }
                                        share
                                        playbackContext
                                        genre
                                    }
                                }
                            }
                        }
                        yourmix3: recommendation(
                            id: "yourmix3"
                        ) {
                            id
                            title
                            subtitle
                            description
                            cover(size: $imageSize)
                            ... on TracksRecommendation {
                                tracks(first: $first) {
                                    items {
                                        id
                                        title
                                        cover(size: $imageSize)
                                        isrc
                                        duration
                                        label
                                        artist {
                                            id
                                            title
                                            cover(size: $imageSize)
                                        }
                                        featuredArtists {
                                            items {
                                                id
                                                title
                                                cover(size: $imageSize)
                                            }
                                        }
                                        share
                                        playbackContext
                                        genre
                                    }
                                }
                            }
                        }
                    }
                }
            }
        """

        variables = {
            "imageSize": 512,
            "first": PAGE_SIZE,
        }

        result = await self._post_graphql(query, variables)

        if not result or not result.get("data", {}).get("me", {}).get("recommendations"):
            return []

        recommendations: list[RecommendationFolder] = []

        album_keys = ["albumRecommendations"]
        track_keys = [
            "trackRecommendations",
            "weeklyDiscoveries",
            "trackRecommendationsFirstMostPlayed",
            "trackRecommendationsSecondMostPlayed",
            "historyTopTracks",
            "historyRecentTracks",
            "yourmix1",
            "yourmix2",
            "yourmix3",
        ]

        for key in album_keys:
            rec_data = result["data"]["me"]["recommendations"].get(key)
            if rec_data:
                folder = RecommendationFolder(
                    name=rec_data.get("title"),
                    subtitle=rec_data.get("subtitle"),
                    provider=self.instance_id,
                    item_id=rec_data["id"],
                    media_type=MediaType.ALBUM,
                    items=UniqueList(
                        [
                            await self._parse_album(item)
                            for item in rec_data.get("albums", {}).get("items", [])
                        ]
                    ),
                )
                recommendations.append(folder)
        for key in track_keys:
            rec_data = result["data"]["me"]["recommendations"].get(key)
            if rec_data:
                folder = RecommendationFolder(
                    name=rec_data.get("title"),
                    subtitle=rec_data.get("subtitle"),
                    provider=self.instance_id,
                    item_id=rec_data["id"],
                    media_type=MediaType.TRACK,
                    items=UniqueList(
                        [
                            await self._parse_track(item)
                            for item in rec_data.get("tracks", {}).get("items", [])
                        ]
                    ),
                )
                recommendations.append(folder)

        return recommendations

    @lock
    async def _auth_token(self) -> YouSeeAccessToken | None:
        """Authenticate and return access token."""
        if self._access_token and not self._access_token.is_expired():
            return self._access_token

        # Try refresh token flow first
        if self._refresh_token:
            self.logger.debug("Trying to fetch refresh token")

            async with self.mass.http_session.post(
                "https://musik.yousee.dk/api/token", data={"refresh_token": self._refresh_token}
            ) as refresh_response:
                refresh_result = await refresh_response.json()
                if refresh_result.get("status", 4) == 0:
                    access_token = refresh_result["tokenResult"]["access_token"]

                    self.logger.debug("Refresh token flow success")
                    self._access_token = YouSeeAccessToken(access_token)
                    self._refresh_token = refresh_result["tokenResult"]["refresh_token"]
                    return self._access_token

        async with (
            self.mass.http_session.get(
                "https://musik.yousee.dk/api/delegatedlogin"
            ) as delegate_response,
        ):
            post_action_re = re.search('action="([^"]+)"', await delegate_response.text())
            if not post_action_re:
                return None

            cookies = delegate_response.cookies

            async with self.mass.http_session.post(
                f"https://login.yousee.dk{post_action_re.group(1)}",
                data={
                    "pf.username": self.config.get_value(CONF_USERNAME),
                    "pf.pass": self.config.get_value(CONF_PASSWORD),
                    "pf.ok": "clicked",
                    "pf.adapterId": "MusicUsernamePasswordAdapter",
                },
                cookies=cookies,
            ) as login_response:
                access_token_re = re.search(
                    r'localStorage.setItem\("accesstoken", "([^"]+)"',
                    await login_response.text(),
                )

                refresh_token_re = re.search(
                    r'localStorage.setItem\("refreshtoken", "([^"]+)"',
                    await login_response.text(),
                )

                if not access_token_re or not refresh_token_re:
                    return None

                access_token = access_token_re.group(1)
                self._refresh_token = refresh_token_re.group(1)

                self._access_token = YouSeeAccessToken(access_token)
                self.logger.debug("Got new auth token")

                return self._access_token

    @throttle_with_retries
    async def _post_graphql(
        self, query: str, variables: JsonLike, _headers: JsonLike | None = None
    ) -> JsonLike:
        """Post GraphQL query to YouSee endpoint with authorization."""
        # TODO: Is this the right way to do determine locale?
        # Should we allow a separate language select in provider config?
        locale = self.mass.metadata.locale.split("_")[0]

        async with self.mass.http_session.post(
            YOUSEE_GRAPHQL_ENDPOINT,
            json={"query": query, "variables": variables},
            headers={
                "Authorization": f"Bearer {await self._auth_token()}",
                "Accept-Language": locale,
            }
            | (_headers or {}),
        ) as resp:
            resp.raise_for_status()

            result = await resp.json()
            if len(result.get("errors", [])) > 0:
                raise YouSeeGraphQLError(result)

            return result

    async def _paginate_graphql(
        self,
        query: str,
        variables: JsonLike,
        page_path: list[str],
        variables_first_key: str = "first",
        variables_after_key: str = "after",
    ) -> AsyncGenerator[JsonLike, None]:
        """Paginate GraphQL results."""
        after = None
        has_more = True
        i = 0
        while has_more and (i < MAX_PAGES_PAGINATED):
            self.logger.debug("Paginating GraphQL query, page %s", i + 1)
            vars_with_pagination = variables | {
                variables_first_key: PAGE_SIZE,
                variables_after_key: after,
            }
            result = await self._post_graphql(query, vars_with_pagination)

            # Navigate to the page containing items and pageInfo
            page_data = result
            for key in page_path:
                page_data = page_data.get(key, {})

            for item in page_data.get("items", []):
                yield item

            page_info = page_data.get("pageInfo", {})
            has_more = page_info.get("hasNextPage", False)
            after = page_info.get("endCursor", None)
            i += 1

    async def _parse_track(self, track_obj: JsonLike) -> Track:
        """Parse track data from YouSee API response."""
        track = Track(
            item_id=track_obj["id"],
            provider=self.instance_id,
            name=track_obj["title"],
            duration=track_obj.get("duration", 0),
            provider_mappings={
                ProviderMapping(
                    item_id=str(track_obj["id"]),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    available=track_obj.get("availableToStream", True),
                    audio_format=AudioFormat(
                        content_type=ContentType.MP4,
                        bit_rate=320000,
                    ),
                    url=track_obj.get("share"),
                    details=track_obj.get("playbackContext"),
                )
            },
        )

        if isrc := track_obj.get("isrc"):
            track.external_ids.add((ExternalID.ISRC, isrc))

        if "artist" in track_obj:
            artist = self._parse_artist(track_obj["artist"])
            track.artists.append(artist)

        # TODO Is featured artists needed?
        for feat_artist_obj in track_obj.get("featuredArtists", {}).get("items", []):
            feat_artist = self._parse_artist(feat_artist_obj)
            track.artists.append(feat_artist)

        if "album" in track_obj:
            album = await self._parse_album(track_obj["album"])
            track.album = album

        if track_genre := track_obj.get("genre"):
            track.metadata.genres = set(track_genre)

        if track_label := track_obj.get("label"):
            track.metadata.label = track_label

        if track_obj.get("cover"):
            track.metadata.add_image(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=track_obj["cover"],
                    remotely_accessible=True,
                    provider=self.instance_id,
                )
            )

        return track

    def _parse_artist(self, artist_obj: JsonLike) -> Artist:
        """Parse artist data from YouSee API response."""
        artist = Artist(
            item_id=artist_obj["id"],
            provider=self.instance_id,
            name=artist_obj["title"],
            uri=artist_obj.get("share"),
            provider_mappings={
                ProviderMapping(
                    item_id=str(artist_obj["id"]),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

        if artist.item_id == VARIOUS_ARTISTS_ID:
            artist.mbid = VARIOUS_ARTISTS_MBID
            artist.name = VARIOUS_ARTISTS_NAME

        if artist_obj.get("cover"):
            artist.metadata.add_image(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=artist_obj["cover"],
                    remotely_accessible=True,
                    provider=self.instance_id,
                )
            )

        return artist

    async def _parse_album(self, album_obj: JsonLike) -> Album:
        """Parse album data from YouSee API response."""
        if "artist" not in album_obj:
            return await self.get_album(str(album_obj["id"]))

        name, version = parse_title_and_version(album_obj["title"])
        album = Album(
            item_id=album_obj["id"],
            provider=self.instance_id,
            name=name,
            version=version,
            provider_mappings={
                ProviderMapping(
                    item_id=str(album_obj["id"]),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=AudioFormat(
                        content_type=ContentType.MP4,
                        bit_rate=320000,
                    ),
                    url=album_obj.get("share"),
                )
            },
            is_playable=album_obj.get("available", True),
        )

        if album_upc := album_obj.get("upc"):
            album.external_ids.add((ExternalID.BARCODE, album_upc))

        album.artists.append(self._parse_artist(album_obj["artist"]))

        # TODO Is featured artists needed?
        for feat_artist_obj in album_obj.get("featuredArtists", {}).get("items", []):
            feat_artist = self._parse_artist(feat_artist_obj)
            album.artists.append(feat_artist)

        if album_genre := album_obj.get("genre"):
            album.metadata.genres = set(album_genre)

        if album_obj.get("type") == "COMPILATION":
            album.album_type = AlbumType.COMPILATION
        elif album_obj.get("type") == "SINGLE":
            album.album_type = AlbumType.SINGLE
        elif album_obj.get("type") == "REGULAR":
            album.album_type = AlbumType.ALBUM

        inferred_type = infer_album_type(name, version)
        if inferred_type in (AlbumType.SOUNDTRACK, AlbumType.LIVE):
            album.album_type = inferred_type

        if album_obj.get("cover"):
            album.metadata.add_image(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=album_obj["cover"],
                    remotely_accessible=True,
                    provider=self.instance_id,
                )
            )

        if album_label := album_obj.get("label"):
            album.metadata.label = album_label

        if album_obj.get("releaseDate"):
            album.year = try_parse_int(album_obj["releaseDate"][:4])

        return album

    async def _parse_playlist(self, playlist_obj: JsonLike) -> Playlist:
        playlist = Playlist(
            item_id=str(playlist_obj["id"]),
            provider=self.instance_id,
            name=playlist_obj["title"],
            is_editable=playlist_obj["isOwned"],
            provider_mappings={
                ProviderMapping(
                    item_id=str(playlist_obj["id"]),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=playlist_obj["share"],
                    is_unique=playlist_obj["isOwned"],
                )
            },
        )

        if playlist_obj.get("description"):
            playlist.metadata.description = playlist_obj["description"]

        if playlist_obj.get("cover"):
            playlist.metadata.add_image(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=playlist_obj["cover"],
                    remotely_accessible=True,
                    provider=self.instance_id,
                )
            )

        return playlist
