"""YouSee Musik playlist manager."""

from typing import TYPE_CHECKING

from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.yousee.constants import IMAGE_SIZE
from music_assistant.providers.yousee.parsers import parse_playlist

if TYPE_CHECKING:
    from music_assistant_models.media_items import Playlist

    from music_assistant.providers.yousee.provider import YouSeeMusikProvider


class YouSeePlaylistManager:
    """Manages YouSee Musik playlist operations."""

    def __init__(self, provider: "YouSeeMusikProvider"):
        """Initialize playlist manager."""
        self.provider = provider
        self.api = provider.api
        self.auth = provider.auth
        self.logger = provider.logger

    async def create(self, name: str) -> "Playlist":
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
        variables = {"title": name, "imageSize": IMAGE_SIZE}
        result = await self.api.post_graphql(query, variables)
        if not result or not result.get("data", {}).get("playlists", {}).get("create", {}).get(
            "playlist"
        ):
            raise MediaNotFoundError(f"Could not create playlist {name}")

        return await parse_playlist(
            self.provider, result["data"]["playlists"]["create"]["playlist"]
        )

    async def add_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
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
        result = await self.api.post_graphql(query, variables)

        if not result or not result.get("data", {}).get("playlists", {}).get("addTracks", {}).get(
            "ok"
        ):
            raise MediaNotFoundError(
                f"Could not add tracks to playlist {prov_playlist_id}: {prov_track_ids}"
            )

    async def remove_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist."""
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

        result = await self.api.post_graphql(query, variables)

        if not result or not result.get("data", {}).get("playlists", {}).get(
            "modifyTracks", {}
        ).get("ok"):
            raise MediaNotFoundError(
                f"Could not remove tracks from playlist {prov_playlist_id}: {positions_to_remove}"
            )
