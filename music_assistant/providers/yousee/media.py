"""Media retrieval operations for YouSee Musik."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import (
    MediaType,
)
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Album, Artist, Playlist, SearchResults, Track

from music_assistant.providers.yousee.api_client import JsonLike
from music_assistant.providers.yousee.constants import (
    GET_POPULAR_TRACKS_LIMIT,
    IMAGE_SIZE,
)
from music_assistant.providers.yousee.parsers import (
    parse_album,
    parse_artist,
    parse_lyrics,
    parse_playlist,
    parse_track,
)

if TYPE_CHECKING:
    from music_assistant.providers.yousee.provider import YouSeeMusikProvider


class YouSeeMediaManager:
    """Handles retrieval of media items from YouSee Musik."""

    def __init__(self, provider: YouSeeMusikProvider):
        """Initialize media retriever."""
        self.provider = provider
        self.api = provider.api
        self.logger = provider.logger

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
            "imageSize": IMAGE_SIZE,
            "first": limit,
        }

        result = await self.api.post_graphql(query, variables)

        result = result.get("data", {}).get("search", {})

        if not result:
            return search_result

        if "artists" in result:
            search_result.artists = [
                parse_artist(self.provider, item) for item in result["artists"].get("items", [])
            ]
        if "albums" in result:
            search_result.albums = [
                await parse_album(self.provider, item) for item in result["albums"].get("items", [])
            ]
        if "tracks" in result:
            search_result.tracks = [
                await parse_track(self.provider, item) for item in result["tracks"].get("items", [])
            ]
        if "playlists" in result:
            search_result.playlists = [
                await parse_playlist(self.provider, item)
                for item in result["playlists"].get("items", [])
            ]

        return search_result

    async def get_artist(self, prov_artist_id: str) -> Artist:
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
        variables = {"id": prov_artist_id, "imageSize": IMAGE_SIZE}

        result = await self.api.post_graphql(query, variables)
        if not result or not result.get("data", {}).get("catalog", {}).get("artist"):
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found")
        return parse_artist(self.provider, result["data"]["catalog"]["artist"])

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
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
            "imageSize": IMAGE_SIZE,
        }

        async for item in self.api.paginate_graphql(
            query,
            variables,
            ["data", "catalog", "artist", "albums"],
        ):
            albums.append(await parse_album(self.provider, item))

        return albums

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
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
                                genre
                            }
                        }
                    }
                }
            }
        """

        variables = {
            "id": prov_artist_id,
            "imageSize": IMAGE_SIZE,
            "first": GET_POPULAR_TRACKS_LIMIT,
        }

        result = await self.api.post_graphql(query, variables)

        if not result or not result.get("data", {}).get("catalog", {}).get("artist"):
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found")
        tracks = []

        for item in result["data"]["catalog"]["artist"]["tracks"]["items"]:
            tracks.append(await parse_track(self.provider, item))

        return tracks

    async def get_album(self, prov_album_id: str) -> Album:
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
        variables = {"id": prov_album_id, "imageSize": IMAGE_SIZE}

        result = await self.api.post_graphql(query, variables)
        if not result or not result.get("data", {}).get("catalog", {}).get("album"):
            raise MediaNotFoundError(f"Album {prov_album_id} not found")
        return await parse_album(self.provider, result["data"]["catalog"]["album"])

    async def _get_lyrics(self, prov_track_id: str) -> list[JsonLike]:
        """Attempt to retrieve lyrics for the given track id."""
        query = """
            query Lyric($id: ID!, $first: Int = 50, $after: String) {
                catalog {
                    track(id: $id) {
                        lyrics {
                            lrc(first: $first, after: $after) {
                                pageInfo {
                                    hasNextPage
                                    endCursor
                                }
                                items {
                                    startInMs
                                    durationInMs
                                    line
                                }
                            }
                        }
                    }
                }
            }
        """
        variables = {"id": prov_track_id}

        lines = []

        async for line in self.api.paginate_graphql(
            query, variables, ["data", "catalog", "track", "lyrics", "lrc"]
        ):
            lines.append(line)

        return lines

    async def get_track(self, prov_track_id: str) -> Track:
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
                    share
                    cover(size: $imageSize)
                    lyrics {
                        id
                    }
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
        variables = {"id": prov_track_id, "imageSize": IMAGE_SIZE}

        result = await self.api.post_graphql(query, variables)
        if not result or not result.get("data", {}).get("catalog", {}).get("track"):
            raise MediaNotFoundError(f"Track {prov_track_id} not found")

        track = await parse_track(self.provider, result["data"]["catalog"]["track"])

        if result["data"]["catalog"]["track"].get("lyrics"):
            lyrics = await self._get_lyrics(prov_track_id)
            parsed_lyrics, parsed_lrc_lyrics = await parse_lyrics(lyrics)

            if parsed_lyrics:
                self.logger.debug("Attached lyrics to track")
                track.metadata.lyrics = parsed_lyrics
            if parsed_lrc_lyrics:
                self.logger.debug("Attached LRC lyrics to track")
                track.metadata.lrc_lyrics = parsed_lrc_lyrics

        return track

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
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
        variables = {"id": prov_playlist_id, "imageSize": IMAGE_SIZE}

        result = await self.api.post_graphql(query, variables)
        if not result or not result.get("data", {}).get("playlists", {}).get("playlist"):
            raise MediaNotFoundError(f"Playlist {prov_playlist_id} not found")

        return await parse_playlist(self.provider, result["data"]["playlists"]["playlist"])

    async def get_album_tracks(
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
            "imageSize": IMAGE_SIZE,
        }

        i = 1
        async for item in self.api.paginate_graphql(
            query,
            variables,
            ["data", "catalog", "album", "tracks"],
        ):
            track = await parse_track(self.provider, item)
            track.position = i
            tracks.append(track)
            i += 1

        return tracks

    async def get_playlist_tracks(
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
            "imageSize": IMAGE_SIZE,
        }

        i = 1
        async for item in self.api.paginate_graphql(
            query, variables, ["data", "playlists", "playlist", "tracks"]
        ):
            track = await parse_track(self.provider, item)
            track.position = i
            tracks.append(track)
            i += 1

        return tracks

    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
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
            "imageSize": IMAGE_SIZE,
        }
        result = await self.api.post_graphql(query, variables)
        if not result or not result.get("data", {}).get("catalog", {}).get("track"):
            raise MediaNotFoundError(f"Track {prov_track_id} not found")

        return [
            await parse_track(self.provider, item)
            for item in result["data"]["catalog"]["track"]["similarTracks"]["items"]
        ]
