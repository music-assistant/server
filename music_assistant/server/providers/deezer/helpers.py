"""Helper module for parsing the Deezer API. Also helper for getting audio streams.

This helpers file is an async wrapper around the excellent deezer-python package.
While the deezer-python package does an excellent job at parsing the Deezer results,
it is unfortunately not async, which is required for Music Assistant to run smoothly.
This also nicely separates the parsing logic from the Deezer provider logic.

CREDITS:
deezer-python: https://github.com/browniebroke/deezer-python by @browniebroke
dzr: (inspired the track-url gatherer) https://github.com/yne/dzr by @yne.
"""

import asyncio

import deezer

from music_assistant.common.models.enums import AlbumType, ContentType, ImageType, MediaType
from music_assistant.common.models.errors import LoginFailed
from music_assistant.common.models.media_items import (
    Album,
    Artist,
    MediaItemImage,
    MediaItemMetadata,
    Playlist,
    ProviderMapping,
    Track,
)


class Credential:
    """Class for storing credentials."""

    def __init__(self, app_id: int, app_secret: str):
        """Set the correct things."""
        self.app_id = app_id
        self.app_secret = app_secret

    app_id: int
    app_secret: str
    access_token: str


async def get_deezer_client(creds: Credential = None) -> deezer.Client:  # type: ignore
    """
    Return a deezer-python Client.

    If credentials are given the client is authorized.
    If no credentials are given the deezer client is not authorized.

    :param creds: Credentials. If none are given client is not authorized, defaults to None
    :type creds: credential, optional
    """
    if creds and not isinstance(creds, Credential):
        raise TypeError("Creds must be of type credential")

    def _authorize():
        if creds:
            client = deezer.Client(
                app_id=creds.app_id, app_secret=creds.app_secret, access_token=creds.access_token
            )
        else:
            client = deezer.Client()
        return client

    return await asyncio.to_thread(_authorize)


async def get_artist(client: deezer.Client, artist_id: int) -> deezer.Artist:
    """Async wrapper of the deezer-python get_artist function."""

    def _get_artist():
        artist = client.get_artist(artist_id=artist_id)
        return artist

    return await asyncio.to_thread(_get_artist)


async def get_album(client: deezer.Client, album_id: int) -> deezer.Album:
    """Async wrapper of the deezer-python get_album function."""

    def _get_album():
        album = client.get_album(album_id=album_id)
        return album

    return await asyncio.to_thread(_get_album)


async def get_playlist(client: deezer.Client, playlist_id) -> deezer.Playlist:
    """Async wrapper of the deezer-python get_playlist function."""

    def _get_playlist():
        playlist = client.get_playlist(playlist_id=playlist_id)
        return playlist

    return await asyncio.to_thread(_get_playlist)


async def get_track(client: deezer.Client, track_id: int) -> deezer.Track:
    """Async wrapper of the deezer-python get_track function."""

    def _get_track():
        track = client.get_track(track_id=track_id)
        return track

    return await asyncio.to_thread(_get_track)


async def get_user_artists(client: deezer.Client) -> deezer.PaginatedList:
    """Async wrapper of the deezer-python get_user_artists function."""

    def _get_artist():
        artists = client.get_user_artists()
        return artists

    return await asyncio.to_thread(_get_artist)


async def get_user_playlists(client: deezer.Client) -> deezer.PaginatedList:
    """Async wrapper of the deezer-python get_user_playlists function."""

    def _get_playlist():
        playlists = client.get_user().get_playlists()
        return playlists

    return await asyncio.to_thread(_get_playlist)


async def get_user_albums(client: deezer.Client) -> deezer.PaginatedList:
    """Async wrapper of the deezer-python get_user_albums function."""

    def _get_album():
        albums = client.get_user_albums()
        return albums

    return await asyncio.to_thread(_get_album)


async def get_user_tracks(client: deezer.Client) -> deezer.PaginatedList:
    """Async wrapper of the deezer-python get_user_tracks function."""

    def _get_track():
        tracks = client.get_user_tracks()
        return tracks

    return await asyncio.to_thread(_get_track)


async def add_user_albums(client: deezer.Client, album_id: int) -> bool:
    """Async wrapper of the deezer-python add_user_albums function."""

    def _get_track():
        success = client.add_user_album(album_id=album_id)
        return success

    return await asyncio.to_thread(_get_track)


async def remove_user_albums(client: deezer.Client, album_id: int) -> bool:
    """Async wrapper of the deezer-python remove_user_albums function."""

    def _get_track():
        success = client.remove_user_album(album_id=album_id)
        return success

    return await asyncio.to_thread(_get_track)


async def add_user_tracks(client: deezer.Client, track_id: int) -> bool:
    """Async wrapper of the deezer-python add_user_tracks function."""

    def _get_track():
        success = client.add_user_track(track_id=track_id)
        return success

    return await asyncio.to_thread(_get_track)


async def remove_user_tracks(client: deezer.Client, track_id: int) -> bool:
    """Async wrapper of the deezer-python remove_user_tracks function."""

    def _get_track():
        success = client.remove_user_track(track_id=track_id)
        return success

    return await asyncio.to_thread(_get_track)


async def add_user_artists(client: deezer.Client, artist_id: int) -> bool:
    """Async wrapper of the deezer-python add_user_artists function."""

    def _get_artist():
        success = client.add_user_artist(artist_id=artist_id)
        return success

    return await asyncio.to_thread(_get_artist)


async def remove_user_artists(client: deezer.Client, artist_id: int) -> bool:
    """Async wrapper of the deezer-python remove_user_artists function."""

    def _get_artist():
        success = client.remove_user_artist(artist_id=artist_id)
        return success

    return await asyncio.to_thread(_get_artist)


async def search_album(client: deezer.Client, query: str, limit: int = 5) -> list[deezer.Album]:
    """Async wrapper of the deezer-python search_albums function."""

    def _search():
        result = client.search_albums(query=query)[:limit]
        return result

    return await asyncio.to_thread(_search)


async def search_track(client: deezer.Client, query: str, limit: int = 5) -> list[deezer.Track]:
    """Async wrapper of the deezer-python search function."""

    def _search():
        result = client.search(query=query)[:limit]
        return result

    return await asyncio.to_thread(_search)


async def search_artist(client: deezer.Client, query: str, limit: int = 5) -> list[deezer.Artist]:
    """Async wrapper of the deezer-python search_artist function."""

    def _search():
        result = client.search_artists(query=query)[:limit]
        return result

    return await asyncio.to_thread(_search)


async def search_playlist(
    client: deezer.Client, query: str, limit: int = 5
) -> list[deezer.Playlist]:
    """Async wrapper of the deezer-python search_playlist function."""

    def _search():
        result = client.search_playlists(query=query)[:limit]
        return result

    return await asyncio.to_thread(_search)


"""
async def _parse_cookies(cookies: RequestsCookieJar) -> dict[str, SimpleCookie]:
    result = {}
    for cookie in cookies:
        new_cookie = SimpleCookie()
        new_cookie[cookie.name] = str(cookie.value)
        new_cookie[cookie.name]["Path"] = str(cookie.path)
        new_cookie[cookie.name]["Domain"] = str(cookie.domain)
        new_cookie[cookie.name]["Expires"] = str(cookie.expires)
        result[cookie.name] = new_cookie
    return result


async def _get_sid(mass, client: deezer.Client):
    ""Get a session id.""
    cookies = await _parse_cookies(cookies=client.session.cookies)
    return (
        await _get_http(
            mass=mass,
            url="https://www.deezer.com/ajax/gw-light.php",
            params={"method": "deezer.ping", "api_version": "1.0", "api_token": ""},
            headers=None,
            cookies=cookies,
        )
    )[  # type: ignore
        "results"
    ][
        "SESSION"
    ]


async def _get_user_data(mass, tok, sid):
    ""Get user data.""
    return (
        await _get_http(
            mass=mass,
            url="https://www.deezer.com/ajax/gw-light.php",
            params={
                "method": "deezer.getUserData",
                "input": "3",
                "api_version": "1.0",
                "api_token": tok,
            },
            cookies="",
            headers={"Cookie": f"sid={sid}"},
        )
    )[  # type: ignore
        "results"
    ]


async def _get_song_info(mass, tok, sid, track_id):
    ""Get info for song. Can't use that of deezer-python because we need the track token.""
    return (
        await _post_http(
            mass=mass,
            url="https://www.deezer.com/ajax/gw-light.php",
            params={
                "method": "song.getListData",
                "input": "3",
                "api_version": "1.0",
                "api_token": tok,
            },
            headers={"Cookie": f"sid={sid}"},
            data=json.dumps({"sng_ids": track_id}),
        )
    )[  # type: ignore
        "results"
    ][
        "data"
    ][
        0
    ]


async def _generate_url(mass, usr_lic, track_tok):
    ""Get the url for the given track.""
    url = "https://media.deezer.com/v1/get_url"
    payload = {
        "license_token": usr_lic,
        "media": [{"type": "FULL", "formats": [{"cipher": "BF_CBC_STRIPE", "format": "MP3_128"}]}],
        "track_tokens": track_tok,
    }
    response = await _post_http(
        mass=mass, url=url, data=json.dumps(payload), params=None, headers=None
    )
    return response


async def _get_http(mass, url, params, headers, cookies):
    async with mass._throttler:  # pylint: disable=W0212
        time_start = time()
        try:
            async with mass.mass.http_session as session:
                jar = session.cookie_jar
                jar.update_cookies(cookies=cookies)
                response = await session.get(
                    url, headers=headers, params=params, ssl=False, timeout=120, cookies=cookies
                )
                result = await response.json()
                if "error" in result or ("status" in result and "error" in result["status"]):
                    mass.logger.error("%s - %s", url, result)
                    return None
        except (
            aiohttp.ContentTypeError,
            json.JSONDecodeError,
        ) as err:
            mass.logger.error("%s - %s", url, str(err))
            return None
        finally:
            mass.logger.debug(
                "Processing GET/%s took %s seconds",
                url,
                round(time() - time_start, 2),
            )
        return result
"""


async def update_access_token(mass, creds: Credential, code) -> Credential:
    """Update the access_token."""
    response = await _post_http(
        mass=mass,
        url="https://connect.deezer.com/oauth/access_token.php",
        data={
            "code": code,
            "app_id": creds.app_id,
            "secret": creds.app_secret,
        },
        params={
            "code": code,
            "app_id": creds.app_id,
            "secret": creds.app_secret,
        },
        headers=None,
    )
    try:
        print(response.split("=")[1].split("&")[0])
        creds.access_token = response.split("=")[1].split("&")[0]
    except Exception as error:
        raise LoginFailed("Invalid auth code") from error
    return creds


async def _post_http(mass, url, data, params=None, headers=None) -> str:
    async with mass.mass.http_session.post(
        url, headers=headers, params=params, json=data, ssl=False
    ) as response:
        if response.status != 200:
            raise ConnectionError(f"HTTP Error {response.status}: {response.reason}")
        response_text = await response.text()
        return response_text


"""
async def get_url(mass, track_id, creds: Credential, client: deezer.Client) -> str:
    ""Get the url of the track.""
    sid = await _get_sid(mass=mass, client=client)
    user_data = await _get_user_data(mass=mass, tok=creds.access_token, sid=sid)
    licence_token = user_data["USER"]["OPTIONS"]["license_token"]
    check_form = user_data["checkForm"]
    song_info = await _get_song_info(mass=mass, track_id=[track_id], sid=sid, tok=check_form)
    track_token = song_info["TRACK_TOKEN"]
    track_id = song_info["SNG_ID"]
    url_resp = await _generate_url(mass, licence_token, [track_token])
    url_info = url_resp["data"][0]  # type: ignore
    return json.loads(url_info)["media"][0]["sources"][0]["url"]
"""


async def parse_artist(mass, artist: deezer.Artist) -> Artist:
    """Parse the deezer-python artist to a MASS artist."""
    return Artist(
        item_id=str(artist.id),
        provider=mass.domain,
        name=artist.name,
        media_type=MediaType.ARTIST,
        provider_mappings={
            ProviderMapping(
                item_id=str(artist.id),
                provider_domain=mass.domain,
                provider_instance=mass.instance_id,
                content_type=ContentType.MP3,
            )
        },
        metadata=await parse_metadata_artist(artist=artist),
    )


async def parse_album_type(album_type: str) -> AlbumType:
    """Parse the album type."""
    return AlbumType(
        album_type,
    )


async def parse_album(mass, album: deezer.Album) -> Album:
    """Parse the deezer-python album to a MASS album."""
    return Album(
        album_type=await parse_album_type(album_type=album.type),
        item_id=str(album.id),
        provider=mass.domain,
        name=album.title,
        artists=[await parse_artist(mass=mass, artist=await get_artist_from_album(album=album))],
        media_type=MediaType.ALBUM,
        provider_mappings={
            ProviderMapping(
                item_id=str(album.id),
                provider_domain=mass.domain,
                provider_instance=mass.instance_id,
                content_type=ContentType.MP3,
            )
        },
        metadata=await parse_metadata_album(album=album),
    )


async def parse_playlist(mass, playlist: deezer.Playlist) -> Playlist:
    """Parse the deezer-python playlist to a MASS playlist."""
    return Playlist(
        item_id=str(playlist.id),
        provider=mass.domain,
        name=playlist.title,
        media_type=MediaType.PLAYLIST,
        provider_mappings={
            ProviderMapping(
                item_id=str(playlist.id),
                provider_domain=mass.domain,
                provider_instance=mass.instance_id,
                content_type=ContentType.MP3,
            )
        },
        metadata=await parse_metadata_playlist(playlist=playlist),
    )


async def parse_metadata_playlist(playlist: deezer.Playlist) -> MediaItemMetadata:
    """Parse the playlist metadata."""
    return MediaItemMetadata(
        images=[MediaItemImage(type=ImageType.THUMB, path=playlist.picture_big)],
    )


async def parse_metadata_track(track: deezer.Track) -> MediaItemMetadata:
    """Parse the track metadata."""
    try:
        return MediaItemMetadata(
            preview=track.preview,
            images=[
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=(await get_album_from_track(track=track)).cover_big,
                )
            ],
        )
    except AttributeError:
        return MediaItemMetadata(
            preview=track.preview,
        )


async def parse_metadata_album(album: deezer.Album) -> MediaItemMetadata:
    """Parse the album metadata."""
    return MediaItemMetadata(
        images=[MediaItemImage(type=ImageType.THUMB, path=album.cover_big)],
    )


async def parse_metadata_artist(artist: deezer.Artist) -> MediaItemMetadata:
    """Parse the artist metadata."""
    return MediaItemMetadata(
        images=[MediaItemImage(type=ImageType.THUMB, path=artist.picture_big)],
    )


async def _get_album(mass, track: deezer.Track) -> Album | None:
    try:
        return await parse_album(mass=mass, album=await get_album_from_track(track=track))
    except AttributeError:
        return None


async def get_album_from_track(track: deezer.Track) -> deezer.Album:
    """Get track's artist."""

    def _get_album_from_track():
        try:
            return track.get_album()
        except deezer.exceptions.DeezerErrorResponse:
            return None

    return await asyncio.to_thread(_get_album_from_track)


async def get_artist_from_track(track: deezer.Track) -> deezer.Artist:
    """Get track's artist."""

    def _get_artist_from_track():
        return track.get_artist()

    return await asyncio.to_thread(_get_artist_from_track)


async def get_artist_from_album(album: deezer.Album) -> deezer.Artist:
    """Get track's artist."""

    def _get_artist_from_album():
        return album.get_artist()

    return await asyncio.to_thread(_get_artist_from_album)


async def get_albums_by_artist(artist: deezer.Artist) -> deezer.PaginatedList:
    """Get albums by an artist."""

    def _get_albums_by_artist():
        return artist.get_albums()

    return await asyncio.to_thread(_get_albums_by_artist)


async def get_artist_top(artist: deezer.Artist) -> deezer.PaginatedList:
    """Get top tracks by an artist."""

    def _get_artist_top():
        return artist.get_top()

    return await asyncio.to_thread(_get_artist_top)


async def parse_track(mass, track: deezer.Track) -> Track:
    """Parse the deezer-python track to a MASS track."""
    artist = await get_artist_from_track(track=track)
    return Track(
        item_id=str(track.id),
        provider=mass.domain,
        name=track.title,
        media_type=MediaType.TRACK,
        sort_name=track.title_short,
        position=track.track_position,
        duration=track.duration,
        artists=[await parse_artist(mass=mass, artist=artist)],
        album=(await _get_album(mass=mass, track=track)),
        provider_mappings={
            ProviderMapping(
                item_id=str(track.id),
                provider_domain=mass.domain,
                provider_instance=mass.instance_id,
                content_type=ContentType.MP3,
            )
        },
        metadata=await parse_metadata_track(track=track),
    )


async def search_and_parse_track(
    mass, client: deezer.Client, query: str, limit: int = 5
) -> list[Track]:
    """Search for tracks and parse them."""
    deezer_tracks = await search_track(client=client, query=query, limit=limit)
    tracks = []
    for track in deezer_tracks:
        tracks.append(await parse_track(track=track, mass=mass))
    return tracks


async def search_and_parse_artist(
    mass, client: deezer.Client, query: str, limit: int = 5
) -> list[Artist]:
    """Search for artists and parse them."""
    deezer_artist = await search_artist(client=client, query=query, limit=limit)
    artists = []
    for artist in deezer_artist:
        artists.append(await parse_artist(artist=artist, mass=mass))
    return artists


async def search_and_parse_album(
    mass, client: deezer.Client, query: str, limit: int = 5
) -> list[Album]:
    """Search for album and parse them."""
    deezer_albums = await search_album(client=client, query=query, limit=limit)
    albums = []
    for album in deezer_albums:
        albums.append(await parse_album(album=album, mass=mass))
    return albums


async def search_and_parse_playlist(
    mass, client: deezer.Client, query: str, limit: int = 5
) -> list[Playlist]:
    """Search for playlists and parse them."""
    deezer_playlists = await search_playlist(client=client, query=query, limit=limit)
    playlists = []
    for playlist in deezer_playlists:
        playlists.append(await parse_playlist(playlist=playlist, mass=mass))
    return playlists
