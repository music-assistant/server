import json

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://api-v2.soundcloud.com"


class Soundcloud:
    def __init__(self, o_auth, client_id):
        #: Client id for soundcloud account(must be 32bytes length)
        if len(client_id) != 32:
            raise ValueError("Not valid client id")
        self.client_id = client_id

        #: O-Auth code for requests headers
        self.o_auth = o_auth

        # To get the last version of Firefox to prevent some type of deprecated version
        json_versions = dict(
            requests.get("https://product-details.mozilla.org/1.0/firefox_versions.json").json()
        )
        firefox_version = json_versions.get("LATEST_FIREFOX_VERSION")

        #: Default headers that work properly for the API
        #: User-Agent as if it was requested through Firefox Browser
        self.headers = {
            "Authorization": o_auth,
            "Accept": "application/json",
            "User-Agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{firefox_version}) Gecko/20100101 Firefox/{firefox_version}",
        }

        # Version of soundcloud app
        app_json = requests.get("https://soundcloud.com/versions.json")
        self.app_version = dict(app_json.json()).get("app")

    # ---------------- USER ----------------

    def get_account_details(self):
        req = requests.get(f"{BASE_URL}/me", headers=self.headers)
        return req.json()

    def get_user_details(self, user_id):
        """
        :param user_id: id of the user requested
        """

        req = requests.get(
            f"{BASE_URL}/users/{user_id}?client_id={self.client_id}", headers=self.headers
        )
        return req.json()

    def get_followers(self, limit=500):
        """
        :param limit: max numbers of follower accounts to get
        """

        req = requests.get(
            f"{BASE_URL}/me/followers/ids?linked_partitioning=1&client_id={self.client_id}&limit={limit}&app_version={self.app_version}",
            headers=self.headers,
        )
        return req.json()

    def get_following(self, own_user_id, limit=120):
        """
        :param limit: max numbers of following accounts
        """

        # own_user_id = dict(json.loads(self.get_account_details())).get("id")

        req = requests.get(
            f"{BASE_URL}/users/{own_user_id}/followings?client_id={self.client_id}&limit={limit}&offset=0&linked_partitioning=1&app_version={self.app_version}",
            headers=self.headers,
        )
        return req.json()

    def get_recommended_users(self, limit=5):
        """
        :param limit: max numbers of follower accounts to get
        """

        req = requests.get(
            f"{BASE_URL}/me/suggested/users/who_to_follow?view=recommended-first&client_id={self.client_id}&limit={limit}&offset=0&linked_partitioning=1&app_version={self.app_version}",
            headers=self.headers,
        )
        return req.json()

    def follow(self, user_id):
        """
        :param user_id: id of the user requested
        """

        req = requests.post(
            f"{BASE_URL}/me/followings/{user_id}?client_id={self.client_id}&app_version={self.app_version}",
            headers=self.headers,
        )
        return req.status_code

    def unfollow(self, user_id):
        """
        :param user_id: id of the user requested
        """

        req = requests.delete(
            f"{BASE_URL}/me/followings/{user_id}?client_id={self.client_id}&app_version={self.app_version}",
            headers=self.headers,
        )
        return req.status_code

    def send_message(self, user_id, message):
        """
        :param user_id: id of the user requested
        :param message: string with the content of the message
        """

        body = {"contents": message}

        own_user_id = dict(json.loads(self.get_account_details())).get("id")

        req = requests.post(
            f"{BASE_URL}/users/{own_user_id}/conversations/{user_id}?client_id={self.client_id}&app_version={self.app_version}",
            headers=self.headers,
            json=body,
        )
        return req.json()

    def delete_conversation(self, user_id):
        """
        :param user_id: id of the user requested
        """

        own_user_id = dict(json.loads(self.get_account_details())).get("id")

        req = requests.delete(
            f"{BASE_URL}/users/{own_user_id}/conversations/{user_id}?client_id={self.client_id}&app_version={self.app_version}",
            headers=self.headers,
        )
        return req.json()

    def get_repost_from_user(self, user_id, limit):
        """
        :param user_id: id of the user requested
        :param limit: number of repost to get
        """

        req = requests.get(
            f"{BASE_URL}/stream/users/{user_id}/reposts?client_id={self.client_id}&limit={limit}&offset=0&linked_partitioning=1&app_version={self.app_version}",
            headers=self.headers,
        )
        return req.json()

    # ---------------- TRACKS ----------------

    def get_last_track_info(self, limit=1):
        """
        :param limit: number of last tracks reproduced
        """

        req = requests.get(
            f"{BASE_URL}/me/play-history/tracks?client_id={self.client_id}&limit={limit}",
            headers=self.headers,
        )
        return req.json()

    def get_track_likes_users(self, track_id):
        """
        :param track_id: track id
        """

        req = requests.get(
            f"{BASE_URL}/tracks/{track_id}/likers?client_id={self.client_id}", headers=self.headers
        )
        return req.json()

    def get_track_details(self, track_id):
        """
        :param track_id: track id
        """

        req = requests.get(
            f"{BASE_URL}/tracks?ids={track_id}&client_id={self.client_id}", headers=self.headers
        )
        return req.json()

    def get_tracks_liked(self, limit=50):
        """
        :param limit: number of tracks to get
        """

        req = requests.get(
            f"{BASE_URL}/me/track_likes/ids?client_id={self.client_id}&limit={limit}&app_version={self.app_version}",
            headers=self.headers,
        )
        return req.json()

    def like_a_track(self, track_id):
        # To like a track we need the account id
        user_id = dict(json.loads(self.get_account_details())).get("id")

        req = requests.put(
            f"{BASE_URL}/users/{user_id}/track_likes/{track_id}?client_id={self.client_id}&app_version={self.app_version}",
            headers=self.headers,
        )

        # Return "OK" if successful request
        return req.json()

    def unlike_a_track(self, track_id):
        # To unlike a track we need the account id
        user_id = dict(self.get_account_details()).get("id")

        req = requests.delete(
            f"{BASE_URL}/users/{user_id}/track_likes/{track_id}?client_id={self.client_id}&app_version={self.app_version}",
            headers=self.headers,
        )

        # Return "OK" if successful request
        return req.text

    def repost_track(self, track_id):
        req = requests.put(
            f"{BASE_URL}/me/track_reposts/{track_id}?client_id={self.client_id}&app_version={self.app_version}",
            headers=self.headers,
        )

        # Return the status code of the request
        return req.status_code

    def unrepost_track(self, track_id):
        req = requests.delete(
            f"{BASE_URL}/me/track_reposts/{track_id}?client_id={self.client_id}&app_version={self.app_version}",
            headers=self.headers,
        )

        # Return the status code of the request
        return req.status_code

    def get_track_by_genre_recent(self, genre, limit=10):
        """
        :param genre: string of the genre to get tracks
        :param limit: limit of playlists to get
        """

        req = requests.get(
            f"{BASE_URL}/recent-tracks/{genre}?client_id={self.client_id}&limit={limit}&offset=0&linked_partitioning=1&app_version={self.app_version}",
            headers=self.headers,
        )
        return req.json()

    def get_track_by_genre_popular(self, genre, limit=10):
        """
        :param genre: string of the genre to get tracks
        :param limit: limit of playlists to get
        """

        req = requests.get(
            f"{BASE_URL}/search/tracks?q=&filter.genre_or_tag={genre}&sort=popular&client_id={self.client_id}&limit={limit}&offset=0&linked_partitioning=1&app_version={self.app_version}",
            headers=self.headers,
        )

    def get_track_id_from_permalink(self, url_track):
        """
        :param url_track: string with the url like: "https://soundcloud.com/postmalone/post-malone-hateful"
        """

        req = requests.get(url_track)

        scrap = str(
            BeautifulSoup(req.content, "html.parser")
            .find("meta", property="al:android:url")
            .get("content")
        )

        index = str(scrap).find(":", 14, -1)

        track = scrap[index + 1 :]

        return track

    def get_tracks_from_user(self, user_id, limit=10):
        """
        :param user_id: id of the user to get his tracks
        :param limit:   number of tracks to get from user
        """
        req = requests.get(
            f"{BASE_URL}/users/{user_id}/tracks?representation=&client_id={self.client_id}&limit={limit}&offset=0&linked_partitioning=1&app_version={self.app_version}",
            headers=self.headers,
        )
        return req.json()

    def get_popular_tracks_user(self, user_id, limit=12):
        """
        :param user_id: id of the user to get his tracks
        :param limit:   number of tracks to get from user
        """

        req = requests.get(
            f"{BASE_URL}/users/{user_id}/toptracks?client_id={self.client_id}&limit={limit}&offset=0&linked_partitioning=1&app_version={self.app_version}",
            headers=self.headers,
        )
        return req.json()

    # ---------------- PLAYLISTS ----------------

    def get_account_playlists(self):
        req = requests.get(
            f"{BASE_URL}/me/library/all?client_id{self.client_id}", headers=self.headers
        )
        return req.json()

    def get_playlist_details(self, playlist_id):
        """
        :param playlist_id: playlist id
        """

        req = requests.get(
            f"{BASE_URL}/playlists/{playlist_id}?representation=full&client_id={self.client_id}",
            headers=self.headers,
        )
        return req.json()

    def create_playlist(self, title, track_list, public=False, description=None):
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

        req = requests.post(
            f"{BASE_URL}/playlists?client_id={self.client_id}&app_version={self.app_version}",
            headers=self.headers,
            json=body,
        )
        return req.json()

    def delete_playlist(self, playlist_id):
        req = requests.delete(
            f"{BASE_URL}/playlists/{playlist_id}?client_id={self.client_id}&app_version={self.app_version}",
            headers=self.headers,
        )
        return req.json()

    def get_playlists_liked(self, limit=50):
        """
        :param limit: limit of recommended playlists make for you
        """

        req = requests.get(
            f"{BASE_URL}/me/playlist_likes/ids?limit={limit}&linked_partitioning=1&client_id={self.client_id}&app_version={self.app_version}",
            headers=self.headers,
        )
        return req.json()

    def like_playlist(self, playlist_id):
        """
        :param playlist_id: playlist id
        """

        user_id = dict(json.loads(self.get_account_details())).get("id")

        req = requests.put(
            f"{BASE_URL}/users/{user_id}/playlist_likes/{playlist_id}?client_id={self.client_id}&app_version={self.app_version}",
            headers=self.headers,
        )

        # Return "OK" if like successful
        return req.text

    def unlike_playlist(self, playlist_id):
        """
        :param playlist_id: playlist id
        """

        user_id = dict(json.loads(self.get_account_details())).get("id")

        req = requests.delete(
            f"{BASE_URL}/users/{user_id}/playlist_likes/{playlist_id}?client_id={self.client_id}&app_version={self.app_version}",
            headers=self.headers,
        )

        # Return "OK" if like successful
        return req.text

    def repost_playlist(self, playlist_id):
        """
        :param playlist_id: playlist id
        """

        req = requests.put(
            f"{BASE_URL}/me/playlist_reposts/{playlist_id}?client_id={self.client_id}&app_version={self.app_version}",
            headers=self.headers,
        )
        return req.status_code

    def unrepost_playlist(self, playlist_id):
        """
        :param playlist_id: playlist id
        """

        req = requests.delete(
            f"{BASE_URL}/me/playlist_reposts/{playlist_id}?client_id={self.client_id}&app_version={self.app_version}",
            headers=self.headers,
        )
        return req.status_code

    def get_playlists_by_genre(self, genre, limit=10):
        """
        :param genre: string of the genre to get tracks
        :param limit: limit of playlists to get
        """

        req = requests.get(
            f"{BASE_URL}/playlists/discovery?tag={genre}&client_id={self.client_id}&limit={limit}&offset=0&linked_partitioning=1&app_version={self.app_version}",
            headers=self.headers,
        )
        return req.json()

    def get_playlists_from_user(self, user_id, limit=10):
        """
        :param user_id: user id to get his playlists
        :param limit: limit of playlists to get
        """

        req = requests.get(
            f"{BASE_URL}/users/{user_id}/playlists_without_albums?client_id={self.client_id}&limit={limit}&offset=0&linked_partitioning=1&app_version={self.app_version}",
            headers=self.headers,
        )
        return req.json()

    # ---------------- MISCELLANEOUS ----------------

    def get_recommended(self, track_id):
        """
        :param track_id: track id to get recommended tracks from this
        """

        req = requests.get(
            f"{BASE_URL}/tracks/{track_id}/related?client_id={self.client_id}", headers=self.headers
        )
        return req.json()

    def get_stream_url(self, track_id):
        """
        :param track_id: track id
        """

        full_json = list(json.loads(self.get_track_details(track_id)))
        media_url = full_json[0]["media"]["transcodings"][0]["url"]
        track_auth = full_json[0]["track_authorization"]

        stream_url = f"{media_url}?client_id={self.client_id}&track_authorization={track_auth}"

        req = requests.get(stream_url, headers=self.headers)

        return dict(req.json()).get("url")

    def get_comments_track(self, track_id, limit=100):
        """
        :param track_id: track id for get the comments from
        :param limit:    limit of tracks to get
        :add: with the "next_href" in the return json you can keep getting more comments than the limit
        """

        req = requests.get(
            f"{BASE_URL}/tracks/{track_id}/comments?threaded=0&filter_replies=1&client_id={self.client_id}&limit={limit}&offset=0&linked_partitioning=1&app_version={self.app_version}",
            headers=self.headers,
        )
        return req.json()

    def get_mixed_selection(self, limit=5):
        """
        :param limit: limit of recommended playlists make for you
        """

        req = requests.get(
            f"{BASE_URL}/mixed-selections?variant_ids=&client_id={self.client_id}&limit={limit}&app_version={self.app_version}",
            headers=self.headers,
        )
        return req.json()

    def search_tracks(self, query_string, limit=10):
        """
        :param query_string: string to search on souncloud for tracks
        :param limit: limit of recommended playlists make for you
        """

        req = requests.get(
            f"{BASE_URL}/search?q={query_string}&variant_ids=&facet=model&client_id={self.client_id}&limit={limit}&offset=0&app_version={self.app_version}",
            headers=self.headers,
        )
        return req.json()

    def get_suscribe_feed(self, limit=10):
        req = requests.get(
            f"{BASE_URL}/stream?offset=10&limit={limit}&promoted_playlist=true&client_id={self.client_id}&app_version={self.app_version}",
            headers=self.headers,
        )
        return req.json()

    def get_album_from_user(self, user_id, limit=5):
        """
        :param user_id: user to get the albums from
        :param limit:   numbers of albums to get from the user
        """

        req = requests.get(
            f"{BASE_URL}/users/{user_id}/albums?client_id={self.client_id}&limit={limit}&offset=0&linked_partitioning=1&app_version={self.app_version}",
            headers=self.headers,
        )
        return req.json()

    def get_all_feed_user(self, user_id, limit=20):
        """
        :param user_id: user to get the albums from
        :param limit:   numbers of items to get from the user's feed
        """

        req = requests.get(
            f"{BASE_URL}/stream/users/{user_id}?client_id={self.client_id}&limit={limit}&offset=0&linked_partitioning=1&app_version={self.app_version}",
            headers=self.headers,
        )
        return req.json()
