"""Helper module for parsing the Youtube Music API.

This helpers file is an async wrapper around the excellent ytmusicapi package.
While the ytmusicapi package does an excellent job at parsing the Youtube Music results,
it is unfortunately not async, which is required for Music Assistant to run smoothly.
This also nicely separates the parsing logic from the Youtube Music provider logic.
"""

import asyncio
from http.cookies import SimpleCookie
from time import time

import ytmusicapi


async def get_artist(
    prov_artist_id: str, headers: dict[str, str], language: str = "en"
) -> dict[str, str]:
    """Async wrapper around the ytmusicapi get_artist function."""

    def _get_artist():
        ytm = ytmusicapi.YTMusic(auth=headers, language=language)
        try:
            artist = ytm.get_artist(channelId=prov_artist_id)
            # ChannelId can sometimes be different and original ID is not part of the response
            artist["channelId"] = prov_artist_id
        except KeyError:
            try:
                user = ytm.get_user(channelId=prov_artist_id)
                artist = {"channelId": prov_artist_id, "name": user["name"]}
            except KeyError:
                artist = {"channelId": prov_artist_id, "name": "Unknown"}
        return artist

    return await asyncio.to_thread(_get_artist)


async def get_album(prov_album_id: str, language: str = "en") -> dict[str, str]:
    """Async wrapper around the ytmusicapi get_album function."""

    def _get_album():
        ytm = ytmusicapi.YTMusic(language=language)
        return ytm.get_album(browseId=prov_album_id)

    return await asyncio.to_thread(_get_album)


async def get_playlist(
    prov_playlist_id: str, headers: dict[str, str], language: str = "en", user: str | None = None
) -> dict[str, str]:
    """Async wrapper around the ytmusicapi get_playlist function."""

    def _get_playlist():
        ytm = ytmusicapi.YTMusic(auth=headers, language=language, user=user)
        playlist = ytm.get_playlist(playlistId=prov_playlist_id, limit=None)
        playlist["checksum"] = get_playlist_checksum(playlist)
        # Fix missing playlist id in some edge cases
        playlist["id"] = prov_playlist_id if not playlist.get("id") else playlist["id"]
        return playlist

    return await asyncio.to_thread(_get_playlist)


async def get_track(
    prov_track_id: str, headers: dict[str, str], language: str = "en"
) -> dict[str, str] | None:
    """Async wrapper around the ytmusicapi get_playlist function."""

    def _get_song():
        ytm = ytmusicapi.YTMusic(auth=headers, language=language)
        track_obj = ytm.get_song(videoId=prov_track_id)
        track = {}
        if "videoDetails" not in track_obj:
            # video that no longer exists
            return None
        track["videoId"] = track_obj["videoDetails"]["videoId"]
        track["title"] = track_obj["videoDetails"]["title"]
        track["artists"] = [
            {
                "channelId": track_obj["videoDetails"]["channelId"],
                "name": track_obj["videoDetails"]["author"],
            }
        ]
        track["duration"] = track_obj["videoDetails"]["lengthSeconds"]
        track["thumbnails"] = track_obj["microformat"]["microformatDataRenderer"]["thumbnail"][
            "thumbnails"
        ]
        if track_thumbs := track_obj["videoDetails"].get("thumbnail", {}).get("thumbnails"):
            track["thumbnails"] = track.get("thumbnails", []) + track_thumbs
        track["isAvailable"] = track_obj["playabilityStatus"]["status"] == "OK"
        return track

    return await asyncio.to_thread(_get_song)


async def get_podcast(
    prov_podcast_id: str, headers: dict[str, str], language: str = "en"
) -> dict[str, str] | None:
    """Async wrapper around the get_podcast function."""

    def _get_podcast():
        ytm = ytmusicapi.YTMusic(auth=headers, language=language)
        podcast_obj = ytm.get_podcast(playlistId=prov_podcast_id)
        if "podcastId" not in podcast_obj:
            podcast_obj["podcastId"] = prov_podcast_id
        return podcast_obj

    return await asyncio.to_thread(_get_podcast)


async def get_podcast_episode(
    prov_episode_id: str, headers: dict[str, str], language: str = "en"
) -> dict[str, str] | None:
    """Async wrapper around the podcast episode function."""

    def _get_podcast_episode():
        ytm = ytmusicapi.YTMusic(auth=headers, language=language)
        episode = ytm.get_episode(videoId=prov_episode_id)
        if "videoId" not in episode:
            episode["videoId"] = prov_episode_id
        return episode

    return await asyncio.to_thread(_get_podcast_episode)


async def get_library_artists(
    headers: dict[str, str], language: str = "en", user: str | None = None
) -> dict[str, str]:
    """Async wrapper around the ytmusicapi get_library_artists function."""

    def _get_library_artists():
        ytm = ytmusicapi.YTMusic(auth=headers, language=language, user=user)
        artists = ytm.get_library_subscriptions(limit=9999)
        # Sync properties with uniformal artist object
        for artist in artists:
            artist["id"] = artist["browseId"]
            artist["name"] = artist["artist"]
            del artist["browseId"]
            del artist["artist"]
        return artists

    return await asyncio.to_thread(_get_library_artists)


async def get_library_albums(
    headers: dict[str, str], language: str = "en", user: str | None = None
) -> dict[str, str]:
    """Async wrapper around the ytmusicapi get_library_albums function."""

    def _get_library_albums():
        ytm = ytmusicapi.YTMusic(auth=headers, language=language, user=user)
        return ytm.get_library_albums(limit=9999)

    return await asyncio.to_thread(_get_library_albums)


async def get_library_playlists(
    headers: dict[str, str], language: str = "en", user: str | None = None
) -> dict[str, str]:
    """Async wrapper around the ytmusicapi get_library_playlists function."""

    def _get_library_playlists():
        ytm = ytmusicapi.YTMusic(auth=headers, language=language, user=user)
        playlists = ytm.get_library_playlists(limit=9999)
        # Sync properties with uniformal playlist object
        for playlist in playlists:
            playlist["id"] = playlist["playlistId"]
            del playlist["playlistId"]
            playlist["checksum"] = get_playlist_checksum(playlist)
        return playlists

    return await asyncio.to_thread(_get_library_playlists)


async def get_library_tracks(
    headers: dict[str, str], language: str = "en", user: str | None = None
) -> dict[str, str]:
    """Async wrapper around the ytmusicapi get_library_tracks function."""

    def _get_library_tracks():
        ytm = ytmusicapi.YTMusic(auth=headers, language=language, user=user)
        return ytm.get_library_songs(limit=9999)

    return await asyncio.to_thread(_get_library_tracks)


async def get_library_podcasts(
    headers: dict[str, str], language: str = "en", user: str | None = None
) -> dict[str, str]:
    """Async wrapper around the ytmusic api get_library_podcasts function."""

    def _get_library_podcasts():
        ytm = ytmusicapi.YTMusic(auth=headers, language=language, user=user)
        return ytm.get_library_podcasts(limit=None)

    return await asyncio.to_thread(_get_library_podcasts)


async def library_add_remove_artist(
    headers: dict[str, str], prov_artist_id: str, add: bool = True, user: str | None = None
) -> bool:
    """Add or remove an artist to the user's library."""

    def _library_add_remove_artist():
        ytm = ytmusicapi.YTMusic(auth=headers, user=user)
        if add:
            return "actions" in ytm.subscribe_artists(channelIds=[prov_artist_id])
        if not add:
            return "actions" in ytm.unsubscribe_artists(channelIds=[prov_artist_id])
        return None

    return await asyncio.to_thread(_library_add_remove_artist)


async def library_add_remove_album(
    headers: dict[str, str], prov_item_id: str, add: bool = True, user: str | None = None
) -> bool:
    """Add or remove an album or playlist to the user's library."""
    album = await get_album(prov_album_id=prov_item_id)

    def _library_add_remove_album():
        ytm = ytmusicapi.YTMusic(auth=headers, user=user)
        playlist_id = album["audioPlaylistId"]
        if add:
            return ytm.rate_playlist(playlist_id, "LIKE")
        if not add:
            return ytm.rate_playlist(playlist_id, "INDIFFERENT")
        return None

    return await asyncio.to_thread(_library_add_remove_album)


async def library_add_remove_playlist(
    headers: dict[str, str], prov_item_id: str, add: bool = True, user: str | None = None
) -> bool:
    """Add or remove an album or playlist to the user's library."""

    def _library_add_remove_playlist():
        ytm = ytmusicapi.YTMusic(auth=headers, user=user)
        if add:
            return "actions" in ytm.rate_playlist(prov_item_id, "LIKE")
        if not add:
            return "actions" in ytm.rate_playlist(prov_item_id, "INDIFFERENT")
        return None

    return await asyncio.to_thread(_library_add_remove_playlist)


async def add_remove_playlist_tracks(
    headers: dict[str, str],
    prov_playlist_id: str,
    prov_track_ids: list[str],
    add: bool,
    user: str | None = None,
) -> bool:
    """Async wrapper around adding/removing tracks to a playlist."""

    def _add_playlist_tracks():
        ytm = ytmusicapi.YTMusic(auth=headers, user=user)
        if add:
            return ytm.add_playlist_items(playlistId=prov_playlist_id, videoIds=prov_track_ids)
        if not add:
            return ytm.remove_playlist_items(playlistId=prov_playlist_id, videos=prov_track_ids)
        return None

    return await asyncio.to_thread(_add_playlist_tracks)


async def get_song_radio_tracks(
    headers: dict[str, str], prov_item_id: str, limit=25, user: str | None = None
) -> dict[str, str]:
    """Async wrapper around the ytmusicapi radio function."""

    def _get_song_radio_tracks():
        ytm = ytmusicapi.YTMusic(auth=headers, user=user)
        playlist_id = f"RDAMVM{prov_item_id}"
        result = ytm.get_watch_playlist(
            videoId=prov_item_id, playlistId=playlist_id, limit=limit, radio=True
        )
        # Replace inconsistensies for easier parsing
        for track in result["tracks"]:
            if track.get("thumbnail"):
                track["thumbnails"] = track["thumbnail"]
                del track["thumbnail"]
            if track.get("length"):
                track["duration"] = get_sec(track["length"])
        return result

    return await asyncio.to_thread(_get_song_radio_tracks)


async def search(
    query: str, ytm_filter: str | None = None, limit: int = 20, language: str = "en"
) -> list[dict]:
    """Async wrapper around the ytmusicapi search function."""

    def _search():
        ytm = ytmusicapi.YTMusic(language=language)
        results = ytm.search(query=query, filter=ytm_filter, limit=limit)
        # Sync result properties with uniformal objects
        for result in results:
            if result["resultType"] == "artist":
                if "artists" in result and len(result["artists"]) > 0:
                    result["id"] = result["artists"][0]["id"]
                    result["name"] = result["artists"][0]["name"]
                    del result["artists"]
                else:
                    result["id"] = result["browseId"]
                    result["name"] = result["artist"]
                    del result["browseId"]
                    del result["artist"]
            elif result["resultType"] == "playlist":
                if "playlistId" in result:
                    result["id"] = result["playlistId"]
                    del result["playlistId"]
                elif "browseId" in result:
                    result["id"] = result["browseId"]
                    del result["browseId"]
        return results[:limit]

    return await asyncio.to_thread(_search)


def get_playlist_checksum(playlist_obj: dict) -> str:
    """Try to calculate a checksum so we can detect changes in a playlist."""
    for key in ("duration_seconds", "trackCount", "count"):
        if key in playlist_obj:
            return playlist_obj[key]
    return str(int(time()))


def is_brand_account(username: str) -> bool:
    """Check if the provided username is a brand-account."""
    return len(username) == 21 and username.isdigit()


def get_sec(time_str):
    """Get seconds from time."""
    parts = time_str.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    return 0


def convert_to_netscape(raw_cookie_str: str, domain: str) -> str:
    """Convert a raw cookie into Netscape format, so yt-dl can use it."""
    domain = domain.replace("https://", "")
    cookie = SimpleCookie()
    cookie.load(rawdata=raw_cookie_str)
    netscape_cookie = "# Netscape HTTP Cookie File\n"
    for morsel in cookie.values():
        netscape_cookie += f"{domain}\tTRUE\t/\tTRUE\t0\t{morsel.key}\t{morsel.value}\n"
    return netscape_cookie
