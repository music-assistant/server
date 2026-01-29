"""Library management for YouSee Musik."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.providers.yousee.constants import IMAGE_SIZE
from music_assistant.providers.yousee.parsers import (
    parse_album,
    parse_artist,
    parse_playlist,
    parse_track,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import Album, Artist, MediaItemType, Playlist, Track

    from music_assistant.providers.yousee.provider import YouSeeMusikProvider


class YouSeeLibraryManager:
    """Manages YouSee Musik library operations."""

    def __init__(self, provider: YouSeeMusikProvider):
        """Initialize library manager."""
        self.provider = provider
        self.api = provider.api
        self.auth = provider.auth
        self.logger = provider.logger

    async def get_artists(self) -> AsyncGenerator[Artist, None]:
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
        variables = {"imageSize": IMAGE_SIZE}

        async for item in self.api.paginate_graphql(
            query, variables, ["data", "me", "favorites", "artists"]
        ):
            self.logger.log(VERBOSE_LOG_LEVEL, "Parsing artist item: %s", item)
            yield parse_artist(self.provider, item)

    async def get_albums(self) -> AsyncGenerator[Album, None]:
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
        variables = {"imageSize": IMAGE_SIZE}

        async for item in self.api.paginate_graphql(
            query, variables, ["data", "me", "favorites", "albums"]
        ):
            self.logger.log(VERBOSE_LOG_LEVEL, "Parsing album item: %s", item)
            yield await parse_album(self.provider, item)

    async def get_tracks(self) -> AsyncGenerator[Track, None]:
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
        variables = {"imageSize": IMAGE_SIZE}

        async for item in self.api.paginate_graphql(
            query, variables, ["data", "me", "favorites", "tracks"]
        ):
            self.logger.log(VERBOSE_LOG_LEVEL, "Parsing track item: %s", item)
            yield await parse_track(self.provider, item)

    async def get_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve library/subscribed playlists from the provider."""
        query = """
            query favoritePlaylists($first: Int!, $after: String, $imageSize: Int = 512) {
                me {
                    playlists {
                        combinedPlaylists(first: $first, after: $after, orderBy: MODIFIED_DATE) {
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
        variables = {"imageSize": IMAGE_SIZE}
        async for item in self.api.paginate_graphql(
            query, variables, ["data", "me", "playlists", "combinedPlaylists"]
        ):
            self.logger.log(VERBOSE_LOG_LEVEL, "Parsing playlist item: %s", item)
            yield await parse_playlist(self.provider, item)

    async def add_item(self, item: MediaItemType) -> bool:
        """Add item to provider's library. Return true on success."""
        if item.media_type not in (
            MediaType.ARTIST,
            MediaType.ALBUM,
            MediaType.TRACK,
            MediaType.PLAYLIST,
        ):
            raise InvalidDataError(
                f"Cannot add media type {item.media_type} to library for provider "
                f"{self.provider.name}"
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

        result = await self.api.post_graphql(query, variables)

        return bool(
            result.get("data", {})
            .get("favorites", {})
            .get(f"add{media_type_str}", {})
            .get("ok", False)
        )

    async def remove_item(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove item from provider's library. Return true on success."""
        if media_type not in (
            MediaType.ARTIST,
            MediaType.ALBUM,
            MediaType.TRACK,
            MediaType.PLAYLIST,
        ):
            raise InvalidDataError(
                f"Cannot remove media type {media_type} from library for provider "
                f"{self.provider.name}"
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

        result = await self.api.post_graphql(query, variables)

        return bool(
            result.get("data", {})
            .get("favorites", {})
            .get(f"remove{media_type_str}", {})
            .get("ok", False)
        )
