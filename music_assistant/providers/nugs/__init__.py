"""Nugs.net musicprovider support for MusicAssistant."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from time import time
from typing import TYPE_CHECKING, Any

from aiohttp import ClientTimeout
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
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
    ItemMapping,
    MediaItemImage,
    MediaItemMetadata,
    Playlist,
    ProviderMapping,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.helpers.json import json_loads
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = NugsProvider(mass, manifest, config)
    await prov.handle_async_init()
    return prov


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


class NugsProvider(MusicProvider):
    """Provider implementation for Nugs.net."""

    _auth_token: str | None = None
    _token_expiry: float = 0

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return {
            ProviderFeature.BROWSE,
            ProviderFeature.LIBRARY_ARTISTS,
            ProviderFeature.LIBRARY_ALBUMS,
            ProviderFeature.LIBRARY_PLAYLISTS,
            ProviderFeature.ARTIST_ALBUMS,
        }

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        await self.login()

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve library artists from nugs.net."""
        artist_data = await self._get_all_items("stash", "artists/favorite/")
        for item in artist_data:
            if item and item["id"]:
                yield self._parse_artist(item)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve library albums from the provider."""
        album_data = await self._get_all_items("stash", "releases/favorite")
        for item in album_data:
            if item and item["id"]:
                yield self._parse_album(item)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve playlists from the provider."""
        playlist_data = await self._get_all_items("stash", "playlists/")
        for item in playlist_data:
            if item and item["id"]:
                yield self._parse_playlist(item)

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get artist details by id."""
        endpoint = f"/releases/recent?limit=1&artistIds={prov_artist_id}"
        artist_response = await self._get_data("catalog", endpoint)
        artist_data = artist_response["items"][0]["artist"]
        return self._parse_artist(artist_data)

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get a list of all albums for the given artist."""
        params = {
            "artistIds": prov_artist_id,
            "contentType": "any",
        }
        return [
            self._parse_album(item)
            for item in await self._get_all_items("catalog", "releases/recent", **params)
            if (item and item["id"])
        ]

    async def get_album(self, prov_album_id: str) -> Album:
        """Get album details by id."""
        endpoint = f"shows/{prov_album_id}"
        response = await self._get_data("catalog", endpoint)
        return self._parse_album(response["Response"])

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        endpoint = f"playlists/{prov_playlist_id}"
        response = await self._get_data("stash", endpoint)
        return self._parse_playlist(response["items"])

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get all album tracks for given album id."""
        endpoint = f"shows/{prov_album_id}"
        response = await self._get_data("catalog", endpoint)
        album_data = response["Response"]
        artist = await self.get_artist(album_data["artistID"])
        album = self._get_item_mapping(
            MediaType.ALBUM, album_data["containerID"], album_data["containerInfo"]
        )
        image = f"https://api.livedownloads.com{album_data['img']['url']}"
        return [
            self._parse_track(item, artist=artist, album=album, image_url=image)
            for item in album_data["tracks"]
            if item["trackID"]
        ]

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get playlist tracks."""
        result: list[Track] = []
        if page > 0:
            # paging not yet supported
            return []
        endpoint = f"/playlists/{prov_playlist_id}/playlist-tracks/all"
        nugs_result = await self._get_data("stash", endpoint)
        for index, item in enumerate(nugs_result["items"], 1):
            track = self._parse_track(item)
            track.position = index
            result.append(track)
        return result

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        stream_url = await self._get_stream_url(item_id)
        return StreamDetails(
            item_id=item_id,
            provider=self.lookup_key,
            audio_format=AudioFormat(
                content_type=ContentType.UNKNOWN,
            ),
            stream_type=StreamType.HTTP,
            path=stream_url,
        )

    def _parse_artist(self, artist_obj: dict[str, Any]) -> Artist:
        """Parse nugs artist object to generic layout."""
        artist_id = artist_obj.get("artistID") or artist_obj.get("id")
        artist_name = artist_obj.get("artistName") or artist_obj.get("name")
        artist = Artist(
            item_id=str(artist_id),
            provider=self.lookup_key,
            name=str(artist_name),
            provider_mappings={
                ProviderMapping(
                    item_id=str(artist_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=f"https://catalog.nugs.net/api/v1/artists?ids={artist_id}",
                )
            },
        )
        if artist_obj.get("avatarImage"):
            artist.metadata.add_image(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=artist_obj["avatarImage"]["url"],
                    provider=self.lookup_key,
                    remotely_accessible=True,
                )
            )
        return artist

    def _parse_album(self, album_obj: dict[str, Any]) -> Album:
        """Parse nugs release/show/album object to generic album layout."""
        item_id = album_obj.get("releaseId") or album_obj.get("id") or album_obj.get("containerID")
        title = album_obj.get("title") or album_obj.get("containerInfo")
        album = Album(
            item_id=str(item_id),
            provider=self.lookup_key,
            name=str(title),
            # version=album_obj["type"],
            provider_mappings={
                ProviderMapping(
                    item_id=str(item_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

        artist_obj = album_obj.get("artist", False) or {
            "id": album_obj["artistID"],
            "name": album_obj["artistName"],
        }
        if artist_obj.get("name") and artist_obj.get("id"):
            album.artists.append(self._parse_artist(artist_obj))

        path: str | None = None
        if album_obj.get("image"):
            path = album_obj["image"]["url"]
        if album_obj.get("img"):
            path = f"https://api.livedownloads.com{album_obj['img']['url']}"
        if path:
            album.metadata.add_image(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=path,
                    provider=self.lookup_key,
                    remotely_accessible=True,
                )
            )
        year = album_obj.get("performanceDateYear", False)
        if not year:
            date = album_obj.get("performanceDate", False) or album_obj.get(
                "albumreleaseDate", False
            )
            if date:
                year = date.split("-")[0]
        if year:
            album.year = int(year)

        return album

    def _parse_playlist(self, playlist_obj: dict[str, Any]) -> Playlist:
        """Parse nugs playlist object to generic layout."""
        return Playlist(
            item_id=playlist_obj["id"],
            provider=self.lookup_key,
            name=playlist_obj["name"],
            provider_mappings={
                ProviderMapping(
                    item_id=playlist_obj["id"],
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            metadata=MediaItemMetadata(
                images=UniqueList(
                    [
                        MediaItemImage(
                            type=ImageType.THUMB,
                            path=playlist_obj["imageUrl"],
                            provider=self.lookup_key,
                            remotely_accessible=True,
                        )
                    ]
                ),
            ),
            is_editable=False,
        )

    def _parse_track(
        self,
        track_obj: dict[str, Any],
        artist: Artist | None = None,
        album: Album | ItemMapping | None = None,
        image_url: str | None = None,
    ) -> Track:
        """Parse response from inconsistent nugs.net APIs to a Track model object."""
        track_id = (
            track_obj.get("trackId") or track_obj.get("trackID") or track_obj.get("trackLabel")
        )
        track_name = track_obj.get("name") or track_obj.get("songTitle")

        track = Track(
            item_id=str(track_id),
            provider=self.lookup_key,
            name=str(track_name),
            provider_mappings={
                ProviderMapping(
                    item_id=str(track_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    available=True,
                )
            },
        )

        if artist:
            track.artists.append(artist)
        if (
            track_obj.get("artist")
            and isinstance(track_obj.get("artist"), dict)
            and track_obj["artist"].get("id")
        ):
            track.artists.append(
                self._get_item_mapping(
                    MediaType.ARTIST, track_obj["artist"]["id"], track_obj["artist"]["name"]
                )
            )
        if not track.artists:
            msg = "Track is missing artists"
            raise InvalidDataError(msg)

        if album:
            track.album = album
        if image_url is None and track_obj.get("image"):
            image_url = track_obj["image"]["url"]
        if image_url:
            track.metadata.add_image(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=image_url,
                    provider=self.lookup_key,
                    remotely_accessible=True,
                )
            )
        duration = track_obj.get("durationSeconds") or track_obj.get("totalRunningTime")
        if duration:
            track.duration = int(duration)
        return track

    async def _get_stream_url(self, item_id: str) -> Any:
        subscription_info = await self._get_data("subscription", "")
        dt_start = datetime.strptime(subscription_info["startedAt"], "%m/%d/%Y %H:%M:%S").replace(
            tzinfo=UTC
        )
        dt_end = datetime.strptime(subscription_info["endsAt"], "%m/%d/%Y %H:%M:%S").replace(
            tzinfo=UTC
        )
        user_info = await self._get_data("user", "")
        url = "https://streamapi.nugs.net/bigriver/subplayer.aspx"
        timeout = ClientTimeout(total=120)
        params = {
            "platformID": -1,
            "app": 1,
            "HLS": 1,
            "orgn": "websdk",
            "method": "subPlayer",
            "trackId": item_id,
            "subCostplanIDAccessList": subscription_info["plan"]["id"],
            "startDateStamp": int(dt_start.timestamp()),
            "endDateStamp": int(dt_end.timestamp()),
            "nn_userID": user_info["userId"],
            "subscriptionID": subscription_info["legacySubscriptionId"],
        }
        async with (
            self.mass.http_session.get(url, params=params, ssl=True, timeout=timeout) as response,
        ):
            response.raise_for_status()
            content = await response.text()
            stream = json_loads(content)
            if not stream.get("streamLink"):
                raise MediaNotFoundError("No stream found for song %s.", item_id)
            return stream["streamLink"]

    def _get_item_mapping(self, media_type: MediaType, key: str, name: str) -> ItemMapping:
        return ItemMapping(
            media_type=media_type,
            item_id=key,
            provider=self.lookup_key,
            name=name,
        )

    async def login(self) -> Any:
        """Login to nugs.net and return the token."""
        if self._auth_token and (self._token_expiry > time()):
            return self._auth_token
        if not self.config.get_value(CONF_USERNAME) or not self.config.get_value(CONF_PASSWORD):
            msg = "Invalid login credentials"
            raise LoginFailed(msg)
        login_data = {
            "username": self.config.get_value(CONF_USERNAME),
            "password": self.config.get_value(CONF_PASSWORD),
            "scope": "offline_access nugsnet:api nugsnet:legacyapi openid profile email",
            "grant_type": "password",
            "client_id": "Eg7HuH873H65r5rt325UytR5429",
        }
        token = None
        url = "https://id.nugs.net/connect/token"
        timeout = ClientTimeout(total=120)
        async with (
            self.mass.http_session.post(
                url, data=login_data, ssl=True, timeout=timeout
            ) as response,
        ):
            # Handle errors
            if response.status == 401:
                raise LoginFailed("Invalid Nugs.net username or password")
            # handle temporary server error
            if response.status in (502, 503):
                raise ResourceTemporarilyUnavailable(backoff_time=30)
            response.raise_for_status()
            token = await response.json()
            self._auth_token = token["access_token"]
            self._token_expiry = time() + token["expires_in"]
        return token["access_token"]

    async def _get_data(self, nugs_api: str, endpoint: str, **kwargs: Any) -> Any:
        """Return the requested data from one of various nugs.net API."""
        headers = {}
        url: str | None = None
        timeout = ClientTimeout(total=120)
        if nugs_api in ("stash", "subscription", "user"):
            tokeninfo = kwargs.pop("tokeninfo", None)
            if tokeninfo is None:
                tokeninfo = await self.login()
            headers = {"Authorization": f"Bearer {tokeninfo}"}
        if nugs_api == "catalog":
            url = f"https://catalog.nugs.net/api/v1/{endpoint}"
        if nugs_api == "stash":
            url = f"https://stash.nugs.net/api/v1/me/{endpoint}"
        if nugs_api == "subscription":
            url = "https://subscriptions.nugs.net/api/v1/me/subscriptions"
        if nugs_api == "user":
            url = "https://stash.nugs.net/api/v1/stash"
        if not url:
            raise MediaNotFoundError(f"{nugs_api} not found")
        async with (
            self.mass.http_session.get(
                url, headers=headers, params=kwargs, ssl=True, timeout=timeout
            ) as response,
        ):
            if response.status == 404:
                raise MediaNotFoundError(f"{url} not found")
            response.raise_for_status()
            return await response.json()

    async def _get_all_items(
        self, nugs_api: str, endpoint: str, **kwargs: Any
    ) -> list[dict[str, Any]]:
        limit = 100
        offset = 0
        total = 0
        all_items = []
        while True:
            kwargs["limit"] = limit
            kwargs["offset"] = offset
            result = await self._get_data(nugs_api, endpoint, **kwargs)
            total = result["total"]
            all_items += result["items"]
            if total <= offset + limit:
                break
            offset += limit
        return all_items
