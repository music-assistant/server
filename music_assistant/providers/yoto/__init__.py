"""Yoto music provider for Music Assistant."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any, cast

import aiohttp
from music_assistant_models.enums import (
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import LoginFailed, MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    MediaItemMetadata,
    ProviderMapping,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


BASE_URL = "https://api.yotoplay.com"
TOKEN_URL = "https://api.yotoplay.com/auth/token"
ANDROID_APP_CLIENT_ID = "fk91pmWesmvNcHTfbrAv9uXI7BS6l6JM"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Yoto/3.37 (com.yotoplay.yoto; build:15572; Android 14) Dalvik/2.1.0 "
        "(Linux; U; Android 14; Pixel 4 XL Build/AP2A.240805.005)"
    ),
    "X-Yoto-App-Version": "3.37.15572",
    "X-Yoto-Appisalpha": "false",
    "Accept": "application/json",
}

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_ALBUMS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return YotoProvider(mass, manifest, config, SUPPORTED_FEATURES)


class YotoProvider(MusicProvider):
    """Yoto music/story provider implementation."""

    _access_token: str | None = None
    _library_cards: list[dict[str, Any]] | None = None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the config entries for this provider instance."""
        return ()

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await self._login()

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return True

    async def sync_library(self, media_type: MediaType) -> None:
        """
        Run library sync for this provider.

        :param media_type: Media type to sync.
        """
        if media_type in (MediaType.ALBUM, MediaType.ALL):
            self._library_cards = await self._fetch_library_cards()
        await super().sync_library(media_type)

    async def get_library_albums(self) -> AsyncGenerator[Album]:
        """Retrieve library albums from the provider."""
        if self._library_cards is None:
            self._library_cards = await self._fetch_library_cards()
        for card_item in self._library_cards:
            card = card_item.get("card")
            if card:
                yield self._parse_album(card)

    async def get_album(self, prov_album_id: str) -> Album:
        """
        Get full album details by id.

        :param prov_album_id: Provider's album ID.
        """
        if self._library_cards:
            for card_item in self._library_cards:
                card = card_item.get("card", {})
                if card.get("cardId") == prov_album_id:
                    return self._parse_album(card)
        card_data = await self._resolve_card(prov_album_id)
        return self._parse_album(card_data)

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """
        Get album tracks for given album id.

        :param prov_album_id: Provider's album ID.
        """
        card_data = await self._resolve_card(prov_album_id)
        chapters = card_data.get("content", {}).get("chapters", [])
        album = self._parse_album(card_data)

        tracks: list[Track] = []
        for idx, chapter in enumerate(chapters):
            tracks.append(self._parse_track(prov_album_id, chapter, idx, album))
        return tracks

    async def get_track(self, prov_track_id: str) -> Track:
        """
        Get full track details by id.

        :param prov_track_id: Track ID formatted as {card_id}:{chapter_key}.
        """
        if ":" not in prov_track_id:
            raise MediaNotFoundError(f"Invalid track ID format: {prov_track_id}")
        card_id, _chapter_key = prov_track_id.split(":", 1)
        album_tracks = await self.get_album_tracks(card_id)
        for track in album_tracks:
            if track.item_id == prov_track_id:
                return track
        raise MediaNotFoundError(f"Track {prov_track_id} not found")

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Get stream details for a track.

        :param item_id: Track ID formatted as {card_id}:{chapter_key}.
        :param media_type: Media type of the item.
        """
        if ":" not in item_id:
            raise MediaNotFoundError(f"Invalid track ID format: {item_id}")
        card_id, chapter_key = item_id.split(":", 1)
        card_data = await self._resolve_card(card_id)
        chapters = card_data.get("content", {}).get("chapters", [])

        matching_chapter = None
        for chapter in chapters:
            if str(chapter.get("key")) == chapter_key:
                matching_chapter = chapter
                break

        if not matching_chapter:
            raise MediaNotFoundError(f"Chapter {chapter_key} not found for card {card_id}")

        inner_tracks = matching_chapter.get("tracks", [])
        track_urls = [
            t["trackUrl"] for t in inner_tracks if isinstance(t, dict) and t.get("trackUrl")
        ]

        if not track_urls:
            raise MediaNotFoundError(f"No audio URL found for chapter {chapter_key}")

        first_track = inner_tracks[0] if inner_tracks else {}
        format_str = first_track.get("format")
        content_type = ContentType.try_parse(format_str) if format_str else ContentType.AAC

        if len(track_urls) == 1:
            return StreamDetails(
                provider=self.instance_id,
                item_id=item_id,
                audio_format=AudioFormat(content_type=content_type),
                media_type=MediaType.TRACK,
                stream_type=StreamType.HTTP,
                path=track_urls[0],
                allow_seek=True,
                can_seek=True,
            )

        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(content_type=content_type),
            media_type=MediaType.TRACK,
            stream_type=StreamType.CUSTOM,
            data={"track_urls": track_urls},
            allow_seek=True,
            can_seek=True,
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        """
        Return the audio stream for multi-track chapters.

        :param streamdetails: StreamDetails object containing stream info.
        :param seek_position: Seek position in seconds.
        """
        if streamdetails.stream_type != StreamType.CUSTOM or not isinstance(
            streamdetails.data, dict
        ):
            return

        for url in streamdetails.data.get("track_urls", []):
            async with self.mass.http_session.get(
                url, timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                response.raise_for_status()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    yield chunk

    async def _login(self) -> None:
        """Authenticate with the Yoto API and obtain an access token."""
        username = self.get_setup_value(CONF_USERNAME)
        password = self.get_setup_value(CONF_PASSWORD)
        if not username or not password:
            raise LoginFailed("Missing Yoto credentials")

        payload = {
            "client_id": ANDROID_APP_CLIENT_ID,
            "grant_type": "password",
            "username": str(username),
            "password": str(password),
            "scope": "openid email profile offline_access",
            "audience": BASE_URL,
        }
        headers = {
            **DEFAULT_HEADERS,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        async with self.mass.http_session.post(
            TOKEN_URL, data=payload, headers=headers
        ) as response:
            if response.status != 200:
                res_text = await response.text()
                raise LoginFailed(f"Yoto login failed ({response.status}): {res_text}")
            data = await response.json()
            self._access_token = cast("str | None", data.get("access_token"))
            if not self._access_token:
                raise LoginFailed("No access token returned by Yoto API")

    async def _get(self, url: str) -> dict[str, Any]:
        """
        Perform an authenticated HTTP GET request to the Yoto API.

        :param url: The request URL.
        """
        if not self._access_token:
            await self._login()

        headers = {
            **DEFAULT_HEADERS,
            "Authorization": f"Bearer {self._access_token}",
        }
        async with self.mass.http_session.get(url, headers=headers) as response:
            if response.status == 401:
                await self._login()
                headers["Authorization"] = f"Bearer {self._access_token}"
                async with self.mass.http_session.get(url, headers=headers) as retry_res:
                    retry_res.raise_for_status()
                    return cast("dict[str, Any]", await retry_res.json())
            response.raise_for_status()
            return cast("dict[str, Any]", await response.json())

    async def _fetch_library_cards(self) -> list[dict[str, Any]]:
        """Fetch user's family library cards."""
        data = await self._get(f"{BASE_URL}/card/family/library")
        return cast("list[dict[str, Any]]", data.get("cards", []))

    async def _resolve_card(self, card_id: str) -> dict[str, Any]:
        """
        Resolve card details by card ID.

        :param card_id: The card ID to resolve.
        """
        data = await self._get(f"{BASE_URL}/card/resolve/{card_id}?addToFamily=false")
        card = data.get("card")
        if not card:
            raise MediaNotFoundError(f"Card {card_id} not found")
        return cast("dict[str, Any]", card)

    def _parse_album(self, card: dict[str, Any]) -> Album:
        """
        Parse Yoto card dict into a Music Assistant Album.

        :param card: Yoto card dictionary.
        """
        card_id = card["cardId"]
        title = card.get("title", "Unknown Card")
        metadata = card.get("metadata", {})
        author = metadata.get("author")
        if not author and isinstance(metadata.get("authors"), list) and metadata["authors"]:
            author = metadata["authors"][0]

        artists: list[Artist | ItemMapping] = []
        if author:
            artists.append(
                ItemMapping(
                    item_id=author,
                    provider=self.instance_id,
                    name=author,
                    media_type=MediaType.ARTIST,
                )
            )

        cover_url = None
        if isinstance(metadata.get("cover"), dict):
            cover_url = metadata["cover"].get("imageL")

        album = Album(
            item_id=card_id,
            provider=self.instance_id,
            name=title,
            artists=UniqueList(artists),
            provider_mappings={
                ProviderMapping(
                    item_id=card_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            metadata=MediaItemMetadata(
                description=metadata.get("description"),
                images=UniqueList(
                    [
                        MediaItemImage(
                            type=ImageType.THUMB,
                            path=cover_url,
                            provider=self.instance_id,
                            remotely_accessible=True,
                        )
                    ]
                )
                if cover_url
                else None,
            ),
        )

        if isinstance(metadata.get("copyrights"), list):
            album.metadata.copyright = ", ".join(metadata["copyrights"])

        return album

    def _parse_track(self, card_id: str, chapter: dict[str, Any], idx: int, album: Album) -> Track:
        """
        Parse Yoto chapter dict into a Music Assistant Track.

        :param card_id: Parent card ID.
        :param chapter: Chapter dictionary.
        :param idx: 0-based index of the chapter.
        :param album: Parent Album object.
        """
        chapter_key = chapter.get("key", str(idx + 1))
        track_id = f"{card_id}:{chapter_key}"
        chapter_title = chapter.get("title") or f"Chapter {idx + 1}"

        chapter_duration = chapter.get("duration")
        if not chapter_duration and isinstance(chapter.get("tracks"), list):
            chapter_duration = sum(
                t.get("duration", 0)
                for t in chapter["tracks"]
                if isinstance(t, dict) and isinstance(t.get("duration"), (int, float))
            )

        icon = None
        if isinstance(chapter.get("display"), dict):
            icon = chapter["display"].get("icon16x16")

        format_str = None
        if isinstance(chapter.get("tracks"), list) and chapter["tracks"]:
            first_tr = chapter["tracks"][0]
            if isinstance(first_tr, dict):
                format_str = first_tr.get("format")
        content_type = ContentType.try_parse(format_str) if format_str else ContentType.AAC

        album_cover = None
        if album.metadata and album.metadata.images:
            album_cover = album.metadata.images[0].path

        chapter_cover = icon or album_cover

        return Track(
            item_id=track_id,
            provider=self.instance_id,
            name=chapter_title,
            duration=int(chapter_duration) if chapter_duration else 0,
            disc_number=1,
            track_number=idx + 1,
            album=ItemMapping(
                item_id=card_id,
                provider=self.instance_id,
                name=album.name,
                media_type=MediaType.ALBUM,
            ),
            artists=album.artists,
            provider_mappings={
                ProviderMapping(
                    item_id=track_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=AudioFormat(content_type=content_type),
                )
            },
            metadata=MediaItemMetadata(
                images=UniqueList(
                    [
                        MediaItemImage(
                            type=ImageType.THUMB,
                            path=chapter_cover,
                            provider=self.instance_id,
                            remotely_accessible=True,
                        )
                    ]
                )
                if chapter_cover
                else None,
            ),
        )
