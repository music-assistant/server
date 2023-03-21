""" Asyncio """
from __future__ import annotations

import asyncio
import itertools
import json
import random
import socket
import sys
from datetime import date, datetime, timedelta
from enum import IntEnum
from importlib import metadata
from typing import Any

import aiohttp
import async_timeout

# import mutagen
from aiohttp.client import ClientSession
from aiohttp.hdrs import METH_GET
from attr import dataclass
from bs4 import BeautifulSoup
from yarl import URL

# from codetiming import Timer

# from . import sync, util

BASE_URL = "https://api-v2.soundcloud.com"


# async def task(name, work_queue):
#     timer = Timer(text=f"Task {name} elapsed time: {{:.1f}}")
#     async with aiohttp.ClientSession() as session:
#         while not work_queue.empty():
#             url = await work_queue.get()
#             print(f"Task {name} getting URL: {url}")
#             timer.start()
#             async with session.get(url) as response:
#                 await response.text()
#             timer.stop()


# async def get_resource(url) -> bytes:
#     """Get a resource based on url"""
#     async with aiohttp.ClientSession() as session:
#         async with session as conn:
#             async with conn.request("GET", url) as request:
#                 return await request.content.read()


# async def fetch_soundcloud_client_id():
#     """Get soundlcoud client id"""
#     url = random.choice(util.SCRAPE_URLS)
#     page_text = await get_resource(url)
#     script_urls = util.find_script_urls(page_text.decode())
#     results = await asyncio.gather(*[get_resource(u) for u in script_urls])
#     script_text = "".join([r.decode() for r in results])
#     return util.find_client_id(script_text)


# __all__ = ["Track", "Playlist", "SoundcloudAPI"]


# def eprint(*values, **kwargs):
#     """Stderr print"""
#     print(*values, file=sys.stderr, **kwargs)


# async def get_obj_from(url):
#     """Get a json object from a url"""
#     try:
#         return json.loads(await get_resource(url))
#     except Exception as exc:  # pylint: disable=broad-except
#         eprint(type(exc), str(exc))
#         return False


# async def get(url):
#     async with aiohttp.ClientSession() as session:
#         async with session.get(url) as response:
#             print(await response.text())


# async def get(self, url, headers=None, params=None):
#     async with aiohttp.ClientSession(headers=headers) as session:
#         async with session.get(url=url, params=params) as response:
#             # await asyncio.sleep()
#             return await response.json


# await self.get())


# async def post():
#     async with aiohttp.ClientSession() as session:
#         async with session.post("http://httpbin.org/post", data={"key": "value"}) as response:
#             print(await response.text())


# async def post(self, url, headers, params):
#     async with aiohttp.ClientSession(headers=headers) as session:
#         async with (self.limit, session.post(url=url, params=params) as response):
#             await asyncio.sleep(self.rate)
#             return await response.read()


# await self.post())


@dataclass
class SoundcloudAsync:
    """Soundcloud"""

    o_auth: str
    client_id: str
    headers = None
    app_version = None
    firefox_version = None
    request_timeout: float = 8.0

    session: ClientSession | None = None

    async def get(self, url, headers=None, params=None):
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url=url, params=params) as response:
                # await asyncio.sleep()
                # print("Status:", response.status)
                data = await response.json()
                # print("Data: ", data)
                return data

    # async def _request(
    #     self,
    #     url: str,
    #     # *,
    #     method: str = METH_GET,
    #     data: dict | None = None,
    #     headers=None,
    # ) -> str:
    #     """ """
    #     print("request")
    #     # version = metadata.version(__package__)
    #     # url = url
    #     print(url)

    #     if self.session is None:
    #         self.session = ClientSession()
    #         self._close_session = True

    #     try:
    #         async with async_timeout.timeout(self.request_timeout):
    #             response = await self.session.request(
    #                 method,
    #                 url,
    #                 data=data,
    #                 headers=headers,
    #             )
    #             response.raise_for_status()

    #     except asyncio.TimeoutError as exception:
    #         print("error")

    #     print("_response")
    #     print(response.text())
    #     # return await response.text()
    #     await response.json()

    async def login(self):
        print("login")
        # async with ClientSession() as session:
        #: Client id for soundcloud account(must be 32bytes length)
        if len(self.client_id) != 32:
            raise ValueError("Not valid client id")
        # self.client_id = client_id

        #: O-Auth code for requests headers
        # self.o_auth = o_auth
        print(self.o_auth)

        # To get the last version of Firefox to prevent some type of deprecated version
        json_versions = await self.get(
            # self,
            url="https://product-details.mozilla.org/1.0/firefox_versions.json",
        )
        # print(await json_versions)

        self.firefox_version = dict(json_versions).get("LATEST_FIREFOX_VERSION")
        print(self.firefox_version)

        #: Default headers that work properly for the API
        #: User-Agent as if it was requested through Firefox Browser
        self.headers = {
            "Authorization": self.o_auth,
            "Accept": "application/json",
            "User-Agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{self.firefox_version}) Gecko/20100101 Firefox/{self.firefox_version}",
        }

        # Version of soundcloud app
        app_json = await self.get(url="https://soundcloud.com/versions.json")
        # print(app_json)
        self.app_version = dict(app_json).get("app")
        print(self.app_version)

    # ---------------- USER ----------------

    async def get_account_details(self):
        req = await self.get(url=f"{BASE_URL}/me", headers=self.headers)
        print(req)
        return req

    async def get_user_details(self, user_id):
        """
        :param user_id: id of the user requested
        """

        req = await self.get(
            url=f"{BASE_URL}/users/{user_id}?client_id={self.client_id}",
            headers=self.headers,
        )

        return req

    async def get_followers(self, limit=500):
        """
        :param limit: max numbers of follower accounts to get
        """

        req = await self.get(
            f"{BASE_URL}/me/followers/ids?linked_partitioning=1&client_id={self.client_id}&limit={limit}&app_version={self.app_version}",
            headers=self.headers,
        )

        return req

    async def get_following(self, own_user_id, limit=120):
        """
        :param limit: max numbers of following accounts
        """

        # own_user_id = dict(json.loads(self.get_account_details())).get("id")

        req = await self.get(
            f"{BASE_URL}/users/{own_user_id}/followings?client_id={self.client_id}&limit={limit}&offset=0&linked_partitioning=1&app_version={self.app_version}",
            headers=self.headers,
        )

        return req

    async def get_recommended_users(self, limit=5):
        """
        :param limit: max numbers of follower accounts to get
        """

        req = await self.get(
            f"{BASE_URL}/me/suggested/users/who_to_follow?view=recommended-first&client_id={self.client_id}&limit={limit}&offset=0&linked_partitioning=1&app_version={self.app_version}",
            headers=self.headers,
        )

        return req

    async def follow(self, user_id):
        """
        :param user_id: id of the user requested
        """

        req = await self.post(
            f"{BASE_URL}/me/followings/{user_id}?client_id={self.client_id}&app_version={self.app_version}",
            headers=self.headers,
        )

        return req.status_code

    async def unfollow(self, user_id):
        """
        :param user_id: id of the user requested
        """

        req = await self.delete(
            f"{BASE_URL}/me/followings/{user_id}?client_id={self.client_id}&app_version={self.app_version}",
            headers=self.headers,
        )

        return req.status_code

    async def send_message(self, user_id, message):
        """
        :param user_id: id of the user requested
        :param message: string with the content of the message
        """

        body = {"contents": message}

        own_user_id = dict(json.loads(self.get_account_details())).get("id")

        req = await self.post(
            f"{BASE_URL}/users/{own_user_id}/conversations/{user_id}?client_id={self.client_id}&app_version={self.app_version}",
            headers=self.headers,
            json=body,
        )

        return req

    async def delete_conversation(self, user_id):
        """
        :param user_id: id of the user requested
        """

        own_user_id = dict(json.loads(self.get_account_details())).get("id")

        req = await self.delete(
            f"{BASE_URL}/users/{own_user_id}/conversations/{user_id}?client_id={self.client_id}&app_version={self.app_version}",
            headers=self.headers,
        )

        return req

    async def get_repost_from_user(self, user_id, limit):
        """
        :param user_id: id of the user requested
        :param limit: number of repost to get
        """

        req = await self.get(
            f"{BASE_URL}/stream/users/{user_id}/reposts?client_id={self.client_id}&limit={limit}&offset=0&linked_partitioning=1&app_version={self.app_version}",
            headers=self.headers,
        )

        return req

    # ---------------- TRACKS ----------------

    async def get_last_track_info(self, limit=1):
        """
        :param limit: number of last tracks reproduced
        """

        req = await self.get(
            f"{BASE_URL}/me/play-history/tracks?client_id={self.client_id}&limit={limit}",
            headers=self.headers,
        )

        return req

    async def get_track_likes_users(self, track_id):
        """
        :param track_id: track id
        """

        req = await self.get(
            f"{BASE_URL}/tracks/{track_id}/likers?client_id={self.client_id}",
            headers=self.headers,
        )

        return req

    async def get_track_details(self, track_id):
        """
        :param track_id: track id
        """

        req = await self.get(
            f"{BASE_URL}/tracks?ids={track_id}&client_id={self.client_id}",
            headers=self.headers,
        )

        return req

    async def get_tracks_liked(self, limit=50):
        """
        :param limit: number of tracks to get
        """

        req = await self.get(
            f"{BASE_URL}/me/track_likes/ids?client_id={self.client_id}&limit={limit}&app_version={self.app_version}",
            headers=self.headers,
        )

        return req

    async def like_a_track(self, track_id):
        # To like a track we need the account id
        user_id = dict(json.loads(self.get_account_details())).get("id")

        req = await self.put(
            f"{BASE_URL}/users/{user_id}/track_likes/{track_id}?client_id={self.client_id}&app_version={self.app_version}",
            headers=self.headers,
        )

        # Return "OK" if successful request)
        return req

    async def unlike_a_track(self, track_id):
        # To unlike a track we need the account id
        user_id = dict(self.get_account_details()).get("id")

        req = await self.delete(
            f"{BASE_URL}/users/{user_id}/track_likes/{track_id}?client_id={self.client_id}&app_version={self.app_version}",
            headers=self.headers,
        )

        # Return "OK" if successful request
        return req.text

    async def repost_track(self, track_id):
        req = await self.put(
            f"{BASE_URL}/me/track_reposts/{track_id}?client_id={self.client_id}&app_version={self.app_version}",
            headers=self.headers,
        )

        # Return the status code of the request
        return req.status_code

    async def unrepost_track(self, track_id):
        req = await self.delete(
            f"{BASE_URL}/me/track_reposts/{track_id}?client_id={self.client_id}&app_version={self.app_version}",
            headers=self.headers,
        )

        # Return the status code of the request
        return req.status_code

    async def get_track_by_genre_recent(self, genre, limit=10):
        """
        :param genre: string of the genre to get tracks
        :param limit: limit of playlists to get
        """

        req = await self.get(
            f"{BASE_URL}/recent-tracks/{genre}?client_id={self.client_id}&limit={limit}&offset=0&linked_partitioning=1&app_version={self.app_version}",
            headers=self.headers,
        )

        return req

    async def get_track_by_genre_popular(self, genre, limit=10):
        """
        :param genre: string of the genre to get tracks
        :param limit: limit of playlists to get
        """

        req = await self.get(
            f"{BASE_URL}/search/tracks?q=&filter.genre_or_tag={genre}&sort=popular&client_id={self.client_id}&limit={limit}&offset=0&linked_partitioning=1&app_version={self.app_version}",
            headers=self.headers,
        )

        return req

    async def get_track_id_from_permalink(self, url_track):
        """
        :param url_track: string with the url like: "https://soundcloud.com/postmalone/post-malone-hateful"
        """

        req = await self.get(
            url_track,
        )

        scrap = str(
            BeautifulSoup(req.content, "html.parser")
            .find("meta", property="al:android:url")
            .get("content")
        )

        index = scrap.find(":", 14, -1)

        return scrap[index + 1 :]

    async def get_tracks_from_user(self, user_id, limit=10):
        """
        :param user_id: id of the user to get his tracks
        :param limit:   number of tracks to get from user
        """
        req = await self.get(
            f"{BASE_URL}/users/{user_id}/tracks?representation=&client_id={self.client_id}&limit={limit}&offset=0&linked_partitioning=1&app_version={self.app_version}",
            headers=self.headers,
        )

        return req

    async def get_popular_tracks_user(self, user_id, limit=12):
        """
        :param user_id: id of the user to get his tracks
        :param limit:   number of tracks to get from user
        """

        req = await self.get(
            f"{BASE_URL}/users/{user_id}/toptracks?client_id={self.client_id}&limit={limit}&offset=0&linked_partitioning=1&app_version={self.app_version}",
            headers=self.headers,
        )

        return req

    # ---------------- PLAYLISTS ----------------

    async def get_account_playlists(self):
        req = await self.get(
            f"{BASE_URL}/me/library/all?client_id{self.client_id}",
            headers=self.headers,
        )

        return req

    async def get_playlist_details(self, playlist_id):
        """
        :param playlist_id: playlist id
        """

        req = await self.get(
            f"{BASE_URL}/playlists/{playlist_id}?representation=full&client_id={self.client_id}",
            headers=self.headers,
        )

        return req

    async def create_playlist(self, title, track_list, public=False, description=None):
        """
        :param title: str of the playlist title
        :param track_list: python list with the tracks_ids to be in the new playlist
        :param public: boolean (True if public/False if private)
        :param description: description of the playlist
        """

        # If there is no track on the list throw an error
        if len(track_list) == 0:
            raise ValueError("Empty list for creating playlist")

        # Public or Private depends on what you selected (Private default)
        privacy = "public" if public else "private"

        # Body for POST requests of playlist
        body = {
            "playlist": {
                "title": title,
                "sharing": privacy,
                "tracks": track_list,
                "_resource_type": "playlist",
            }
        }

        # If there is a description include it
        if description != None:
            body["playlist"].update({"description": description})

        req = await self.post(
            f"{BASE_URL}/playlists?client_id={self.client_id}&app_version={self.app_version}",
            headers=self.headers,
            json=body,
        )

        return req

    async def delete_playlist(self, playlist_id):
        req = await self.delete(
            f"{BASE_URL}/playlists/{playlist_id}?client_id={self.client_id}&app_version={self.app_version}",
            headers=self.headers,
        )

        return req

    async def get_playlists_liked(self, limit=50):
        """
        :param limit: limit of recommended playlists make for you
        """

        req = await self.get(
            f"{BASE_URL}/me/playlist_likes/ids?limit={limit}&linked_partitioning=1&client_id={self.client_id}&app_version={self.app_version}",
            headers=self.headers,
        )

        return req

    async def like_playlist(self, playlist_id):
        """
        :param playlist_id: playlist id
        """

        user_id = dict(json.loads(self.get_account_details())).get("id")

        req = await self.put(
            f"{BASE_URL}/users/{user_id}/playlist_likes/{playlist_id}?client_id={self.client_id}&app_version={self.app_version}",
            headers=self.headers,
        )

        # Return "OK" if like successful
        return req.text

    async def unlike_playlist(self, playlist_id):
        """
        :param playlist_id: playlist id
        """

        user_id = dict(json.loads(self.get_account_details())).get("id")

        req = await self.delete(
            f"{BASE_URL}/users/{user_id}/playlist_likes/{playlist_id}?client_id={self.client_id}&app_version={self.app_version}",
            headers=self.headers,
        )

        # Return "OK" if like successful
        return req.text

    async def repost_playlist(self, playlist_id):
        """
        :param playlist_id: playlist id
        """

        req = await self.put(
            f"{BASE_URL}/me/playlist_reposts/{playlist_id}?client_id={self.client_id}&app_version={self.app_version}",
            headers=self.headers,
        )

        return req.status_code

    async def unrepost_playlist(self, playlist_id):
        """
        :param playlist_id: playlist id
        """

        req = await self.delete(
            f"{BASE_URL}/me/playlist_reposts/{playlist_id}?client_id={self.client_id}&app_version={self.app_version}",
            headers=self.headers,
        )

        return req.status_code

    async def get_playlists_by_genre(self, genre, limit=10):
        """
        :param genre: string of the genre to get tracks
        :param limit: limit of playlists to get
        """

        req = await self.get(
            f"{BASE_URL}/playlists/discovery?tag={genre}&client_id={self.client_id}&limit={limit}&offset=0&linked_partitioning=1&app_version={self.app_version}",
            headers=self.headers,
        )

        return req

    async def get_playlists_from_user(self, user_id, limit=10):
        """
        :param user_id: user id to get his playlists
        :param limit: limit of playlists to get
        """

        req = await self.get(
            f"{BASE_URL}/users/{user_id}/playlists_without_albums?client_id={self.client_id}&limit={limit}&offset=0&linked_partitioning=1&app_version={self.app_version}",
            headers=self.headers,
        )

        return req

    # ---------------- MISCELLANEOUS ----------------

    async def get_recommended(self, track_id):
        """
        :param track_id: track id to get recommended tracks from this
        """

        req = await self.get(
            f"{BASE_URL}/tracks/{track_id}/related?client_id={self.client_id}",
            headers=self.headers,
        )

        return req

    async def get_stream_url(self, track_id):
        """
        :param track_id: track id
        """

        full_json = list(json.loads(self.get_track_details(track_id)))
        media_url = full_json[0]["media"]["transcodings"][0]["url"]
        track_auth = full_json[0]["track_authorization"]

        stream_url = f"{media_url}?client_id={self.client_id}&track_authorization={track_auth}"

        req = await self.get(
            stream_url,
            headers=self.headers,
        )

        return dict(req).get("url")

    async def get_comments_track(self, track_id, limit=100):
        """
        :param track_id: track id for get the comments from
        :param limit:    limit of tracks to get
        :add: with the "next_href" in the return json you can keep getting more comments than the limit
        """

        req = await self.get(
            f"{BASE_URL}/tracks/{track_id}/comments?threaded=0&filter_replies=1&client_id={self.client_id}&limit={limit}&offset=0&linked_partitioning=1&app_version={self.app_version}",
            headers=self.headers,
        )

        return req

    async def get_mixed_selection(self, limit=5):
        """
        :param limit: limit of recommended playlists make for you
        """

        req = await self.get(
            f"{BASE_URL}/mixed-selections?variant_ids=&client_id={self.client_id}&limit={limit}&app_version={self.app_version}",
            headers=self.headers,
        )

        return req

    async def search_tracks(self, query_string, limit=10):
        """
        :param query_string: string to search on soundcloud for tracks
        :param limit: limit of recommended playlists make for you
        """

        req = await self.get(
            f"{BASE_URL}/search?q={query_string}&variant_ids=&facet=model&client_id={self.client_id}&limit={limit}&offset=0&app_version={self.app_version}",
            headers=self.headers,
        )

        return req

    async def get_suscribe_feed(self, limit=10):
        req = await self.get(
            f"{BASE_URL}/stream?offset=10&limit={limit}&promoted_playlist=true&client_id={self.client_id}&app_version={self.app_version}",
            headers=self.headers,
        )

        return req

    async def get_album_from_user(self, user_id, limit=5):
        """
        :param user_id: user to get the albums from
        :param limit:   numbers of albums to get from the user
        """

        req = await self.get(
            f"{BASE_URL}/users/{user_id}/albums?client_id={self.client_id}&limit={limit}&offset=0&linked_partitioning=1&app_version={self.app_version}",
            headers=self.headers,
        )

        return req

    async def get_all_feed_user(self, user_id, limit=20):
        """
        :param user_id: user to get the albums from
        :param limit:   numbers of items to get from the user's feed
        """

        req = await self.get(
            f"{BASE_URL}/stream/users/{user_id}?client_id={self.client_id}&limit={limit}&offset=0&linked_partitioning=1&app_version={self.app_version}",
            headers=self.headers,
        )

        return req

    async def close(self) -> None:
        """Close open client session."""
        if self.session and self._close_session:
            await self.session.close()

    async def __aenter__(self) -> SoundcloudAsync:
        """Async enter.
        Returns:
            The PVOutput object.
        """
        return self

    async def __aexit__(self, *_exc_info) -> None:
        """Async exit.
        Args:
            _exc_info: Exec type.
        """
        await self.close()
