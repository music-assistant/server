"""ListenBrainz playlist endpoints using the server's shared API transport."""

from __future__ import annotations

from typing import Any

from music_assistant.providers.listenbrainz.models import ListenBrainzPlaylist
from music_assistant.providers.musicbrainz.listenbrainz import ListenBrainzClient


class ListenBrainzAPIClient(ListenBrainzClient):
    """Extend the built-in client with the playlist endpoints used by this provider."""

    async def get_createdfor_playlists(self, username: str) -> list[ListenBrainzPlaylist]:
        """
        Get all system-generated playlists for a user.

        :param username: ListenBrainz username.
        """
        self.logger.debug("Fetching ListenBrainz playlists for user %s", username)
        path = f"/user/{username}/playlists/createdfor"
        playlists: list[ListenBrainzPlaylist] = []
        offset = 0
        try:
            while True:
                data: dict[str, Any] = await self._get_data(
                    path if offset == 0 else f"{path}?offset={offset}"
                )
                page_playlists = data.get("playlists", [])
                playlists.extend(
                    ListenBrainzPlaylist.from_jspf(item["playlist"]) for item in page_playlists
                )

                page_count: int = data["count"]
                page_offset: int = data["offset"]
                playlist_count: int = data["playlist_count"]
                offset = page_offset + page_count

                self.logger.debug(
                    "Fetched %d ListenBrainz playlists at offset %d (%d of %d)",
                    len(page_playlists),
                    page_offset,
                    len(playlists),
                    playlist_count,
                )

                if page_count == 0 or offset >= playlist_count:
                    return playlists
        except Exception as err:
            self.logger.error(
                "Error fetching ListenBrainz playlists for user %s: %s", username, err
            )
            raise

    async def get_playlist(self, playlist_mbid: str) -> ListenBrainzPlaylist:
        """
        Return one playlist, including its recordings.

        :param playlist_mbid: ListenBrainz playlist MusicBrainz ID.
        """
        data: dict[str, Any] = await self._get_data(f"/playlist/{playlist_mbid}")
        return ListenBrainzPlaylist.from_jspf(data["playlist"])
