"""
Async helpers for connecting to the Soundcloud API.

This file is based on soundcloudpy from Naím Rodríguez https://github.com/naim-prog
Original package https://github.com/naim-prog/soundcloud-py
"""  # noqa: INP001

from __future__ import annotations

from typing import TYPE_CHECKING

BASE_URL = "https://api-v2.soundcloud.com"

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from aiohttp.client import ClientSession

# TODO: Fix docstring
# TODO: Add annotations


class SoundcloudAsyncAPI:
    """Soundcloud."""

    session: ClientSession | None = None

    def __init__(self, auth_token: str, client_id: str, http_session: ClientSession) -> None:
        """Initialize SoundcloudAsyncAPI."""
        self.o_auth = auth_token
        self.client_id = client_id
        self.http_session = http_session
        self.headers = None
        self.app_version = None
        self.firefox_version = None
        self.request_timeout: float = 8.0

    async def get(self, url, headers=None, params=None):
        """Async get."""
        async with self.http_session.get(url=url, params=params, headers=headers) as response:
            return await response.json()

    async def login(self) -> None:
        """Login to soundcloud."""
        if len(self.client_id) != 32:
            msg = "Not valid client id"
            raise ValueError(msg)

        # To get the last version of Firefox to prevent some type of deprecated version
        json_versions = await self.get(
            # self,
            url="https://product-details.mozilla.org/1.0/firefox_versions.json",
        )

        self.firefox_version = dict(json_versions).get("LATEST_FIREFOX_VERSION")

        #: Default headers that work properly for the API
        #: User-Agent as if it was requested through Firefox Browser
        self.headers = {
            "Authorization": self.o_auth,
            "Accept": "application/json",
            "User-Agent": (
                f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{self.firefox_version})"
                f" Gecko/20100101 Firefox/{self.firefox_version}"
            ),
        }

        # Version of soundcloud app
        app_json = await self.get(url="https://soundcloud.com/versions.json")
        self.app_version = dict(app_json).get("app")

    # ---------------- USER ----------------

    async def get_account_details(self):
        """Get account details."""
        return await self.get(url=f"{BASE_URL}/me", headers=self.headers)

    async def get_user_details(self, user_id):
        """:param user_id: id of the user requested"""
        return await self.get(
            url=f"{BASE_URL}/users/{user_id}?client_id={self.client_id}",
            headers=self.headers,
        )

    async def get_following(self, own_user_id, limit=120):
        """:param limit: max numbers of following accounts"""
        return await self.get(
            f"{BASE_URL}/users/{own_user_id}/followings?client_id={self.client_id}"
            f"&limit={limit}&offset=0&linked_partitioning=1&app_version={self.app_version}",
            headers=self.headers,
        )

    async def get_recommended_users(self, limit=5):
        """:param limit: max numbers of follower accounts to get"""
        return await self.get(
            f"{BASE_URL}/me/suggested/users/who_to_follow?view=recommended-first&client_id="
            f"{self.client_id}&limit={limit}&offset=0&linked_partitioning=1&app_version="
            f"{self.app_version}",
            headers=self.headers,
        )

    # ---------------- TRACKS ----------------

    async def get_last_track_info(self, limit=1):
        """:param limit: number of last tracks reproduced"""
        return await self.get(
            f"{BASE_URL}/me/play-history/tracks?client_id={self.client_id}&limit={limit}",
            headers=self.headers,
        )

    async def get_track_likes_users(self, track_id):
        """:param track_id: track id"""
        return await self.get(
            f"{BASE_URL}/tracks/{track_id}/likers?client_id={self.client_id}",
            headers=self.headers,
        )

    async def get_track_details(self, track_id):
        """:param track_id: track id"""
        return await self.get(
            f"{BASE_URL}/tracks?ids={track_id}&client_id={self.client_id}",
            headers=self.headers,
        )

    async def get_tracks_liked(self, limit: int = 0) -> AsyncGenerator[int, None]:
        """Obtain the authenticated user's liked tracks.

        :param limit: number of tracks to get. if 0, will fetch all tracks.
        :returns: list of track ids liked by the current user
        """
        query_limit = limit
        if query_limit == 0:
            # NOTE(2023-11-11): At the time of writing, soundcloud does not look like it caps
            # the limit. However, we still implement pagination for future proofing.
            query_limit = 100

        num_items = 0
        async for track in self._paginated_query(
            "/me/track_likes/ids", params={"limit": str(query_limit)}
        ):
            num_items += 1
            if limit > 0 and num_items >= limit:
                return

            yield track

    async def get_track_by_genre_recent(self, genre, limit=10):
        """Get track by genre recent.

        :param genre: string of the genre to get tracks
        :param limit: limit of playlists to get
        """
        return await self.get(
            f"{BASE_URL}/recent-tracks/{genre}?client_id={self.client_id}"
            f"&limit={limit}&offset=0&linked_partitioning=1&app_version={self.app_version}",
            headers=self.headers,
        )

    async def get_track_by_genre_popular(self, genre, limit=10):
        """Get track by genre popular.

        :param genre: string of the genre to get tracks
        :param limit: limit of playlists to get
        """
        return await self.get(
            f"{BASE_URL}/search/tracks?q=&filter.genre_or_tag={genre}&sort=popular&client_id="
            f"{self.client_id}&limit={limit}&offset=0&linked_partitioning=1&app_version="
            f"{self.app_version}",
            headers=self.headers,
        )

    async def get_tracks_from_user(self, user_id, limit=10):
        """Get tracks from user.

        :param user_id: id of the user to get his tracks
        :param limit:   number of tracks to get from user
        """
        return await self.get(
            f"{BASE_URL}/users/{user_id}/tracks?representation=&client_id="
            f"{self.client_id}&limit={limit}&offset=0&linked_partitioning=1&app_version="
            f"{self.app_version}",
            headers=self.headers,
        )

    async def get_popular_tracks_user(self, user_id, limit=12):
        """Get popular track from user.

        :param user_id: id of the user to get his tracks
        :param limit:   number of tracks to get from user
        """
        return await self.get(
            f"{BASE_URL}/users/{user_id}/toptracks?client_id={self.client_id}"
            f"&limit={limit}&offset=0&linked_partitioning=1&app_version={self.app_version}",
            headers=self.headers,
        )

    # ---------------- PLAYLISTS ----------------

    async def get_account_playlists(self):
        """Get account playlists, albums and stations."""
        # NOTE: This returns all track lists in reverse chronological order (most recent first).
        async for playlist in self._paginated_query("/me/library/all"):
            yield playlist

    async def get_playlist_details(self, playlist_id):
        """:param playlist_id: playlist id"""
        return await self.get(
            f"{BASE_URL}/playlists/{playlist_id}?representation=full&client_id={self.client_id}",
            headers=self.headers,
        )

    async def get_playlists_by_genre(self, genre, limit=10):
        """Get playlists by genre.

        :param genre: string of the genre to get tracks
        :param limit: limit of playlists to get
        """
        return await self.get(
            f"{BASE_URL}/playlists/discovery?tag={genre}&client_id="
            f"{self.client_id}&limit={limit}&offset=0&linked_partitioning=1&"
            f"app_version={self.app_version}",
            headers=self.headers,
        )

    async def get_playlists_from_user(self, user_id, limit=10):
        """Get playlists from user.

        :param user_id: user id to get his playlists
        :param limit: limit of playlists to get
        """
        return await self.get(
            f"{BASE_URL}/users/{user_id}/playlists_without_albums?client_id="
            f"{self.client_id}&limit={limit}&offset=0&linked_partitioning=1&"
            f"app_version={self.app_version}",
            headers=self.headers,
        )

    # ---------------- MISCELLANEOUS ----------------

    async def get_recommended(self, track_id: str, limit: int = 10):
        """:param track_id: track id to get recommended tracks from this"""
        return await self.get(
            f"{BASE_URL}/tracks/{track_id}/related?client_id={self.client_id}",
            headers=self.headers,
        )

    async def get_stream_url(self, track_id):
        """:param track_id: track id"""
        full_json = await self.get_track_details(track_id)
        media_url = full_json[0]["media"]["transcodings"][0]["url"]
        track_auth = full_json[0]["track_authorization"]
        stream_url = f"{media_url}?client_id={self.client_id}&track_authorization={track_auth}"
        req = await self.get(
            stream_url,
            headers=self.headers,
        )

        return dict(req).get("url")

    async def get_comments_track(self, track_id, limit=100):
        """Get track comments.

        :param track_id: track id for get the comments from
        :param limit:    limit of tracks to get
        :add: with "next_href" in the return json you can keep getting more comments than limit
        """
        return await self.get(
            f"{BASE_URL}/tracks/{track_id}/comments?threaded=0&filter_replies="
            f"1&client_id={self.client_id}&limit={limit}&offset=0&linked_partitioning="
            f"1&app_version={self.app_version}",
            headers=self.headers,
        )

    async def get_mixed_selection(self, limit=5):
        """:param limit: limit of recommended playlists make for you"""
        return await self.get(
            f"{BASE_URL}/mixed-selections?variant_ids=&client_id="
            f"{self.client_id}&limit={limit}&app_version={self.app_version}",
            headers=self.headers,
        )

    async def search(self, query_string, limit=25):
        """Search.

        :param query_string: string to search on soundcloud for tracks
        :param limit: limit of recommended playlists make for you
        """
        return await self.get(
            f"{BASE_URL}/search?q={query_string}&variant_ids=&facet=model&client_id="
            f"{self.client_id}&limit={limit}&offset=0&app_version={self.app_version}",
            headers=self.headers,
        )

    async def get_subscribe_feed(self, limit=10):
        """Get subscribe feed."""
        return await self.get(
            f"{BASE_URL}/stream?offset=10&limit={limit}&promoted_playlist=true&client_id"
            f"={self.client_id}&app_version={self.app_version}",
            headers=self.headers,
        )

    async def get_album_from_user(self, user_id, limit=5):
        """Get album from user.

        :param user_id: user to get the albums from
        :param limit:   numbers of albums to get from the user
        """
        return await self.get(
            f"{BASE_URL}/users/{user_id}/albums?client_id={self.client_id}"
            f"&limit={limit}&offset=0&linked_partitioning=1&app_version={self.app_version}",
            headers=self.headers,
        )

    async def get_all_feed_user(self, user_id, limit=20):
        """Get all feed from user.

        :param user_id: user to get the albums from
        :param limit:   numbers of items to get from the user's feed
        """
        return await self.get(
            f"{BASE_URL}/stream/users/{user_id}?client_id={self.client_id}"
            f"&limit={limit}&offset=0&linked_partitioning=1&app_version={self.app_version}",
            headers=self.headers,
        )

    async def _paginated_query(
        self,
        path: str,
        params: dict[str, str] | None = None,
    ) -> AsyncGenerator[list[dict[str, any]], None]:
        """Paginate response queries.

        Soundcloud paginates its queries using the same pattern. As such, we leverage the
        same pattern to implement a pagination pattern to iterate over their APIs.

        :param path: endpoint to query
        :param params: key-value pairs to use as query parameters when constructing the initial URL
        """
        if params is None:
            params = {}

        url = f"{BASE_URL}{path}?client_id={self.client_id}&app_version={self.app_version}"
        for k, v in params.items():
            url += f"&{k}={v}"

        while True:
            response = await self.get(url, headers=self.headers)

            # Sanity check.
            if "collection" not in response:
                msg = "Unexpected Soundcloud API response"
                raise RuntimeError(msg)

            for item in response["collection"]:
                yield item

            # Handle case when results requested exceeds number of actual results.
            if int(params.get("limit", 0)) and len(response["collection"]) < int(params["limit"]):
                return

            try:
                url = response["next_href"]
                if not url:
                    return
            except KeyError:
                return
