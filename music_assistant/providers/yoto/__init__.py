"""Yoto music provider for Music Assistant."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

import aiohttp
from music_assistant_models.enums import (
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    InvalidProviderID,
    LoginFailed,
    MediaNotFoundError,
)
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
from music_assistant_models.streamdetails import MultiPartPath, StreamDetails
from yoto_api import Card as YotoCard
from yoto_api import Chapter as YotoChapter
from yoto_api import YotoClient, YotoError

from music_assistant.models.music_provider import MusicProvider

from .setup_flow import CONF_CLIENT_ID, CONF_REFRESH_TOKEN

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_ALBUMS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """
    Initialize provider(instance) with given configuration.

    :param mass: MusicAssistant instance.
    :param manifest: ProviderManifest object.
    :param config: ProviderConfig object.
    """
    return YotoProvider(mass, manifest, config, SUPPORTED_FEATURES)


class YotoProvider(MusicProvider):
    """Yoto music/story provider implementation."""

    client: YotoClient

    async def handle_async_init(self) -> None:
        """Handle async initialization of the Yoto provider."""
        client_id = str(self.get_setup_value(CONF_CLIENT_ID) or "")
        refresh_token = str(self.get_setup_value(CONF_REFRESH_TOKEN) or "")
        if not client_id or not refresh_token:
            raise LoginFailed("Missing Yoto credentials")

        self.client = YotoClient(client_id=client_id, session=self.mass.http_session)
        self.client.set_refresh_token(refresh_token)
        try:
            token = await self.client.check_and_refresh_token()
            if token.refresh_token and token.refresh_token != refresh_token:
                self._update_setup_data(CONF_REFRESH_TOKEN, token.refresh_token)
        except YotoError as err:
            raise LoginFailed(f"Yoto login via refresh token failed: {err}") from err

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload of provider."""
        if self.client:
            await self.client.close()
        await super().unload(is_removed)

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the config entries for this provider instance."""
        return ()

    async def get_library_albums(self) -> AsyncGenerator[Album]:
        """Retrieve library albums from the provider."""
        await self.client.update_library()
        for card in self.client.library.values():
            yield self._parse_album(card)

    async def get_album(self, prov_album_id: str) -> Album:
        """
        Get full album details by id.

        :param prov_album_id: Provider's album ID.
        """
        card: YotoCard | None = None
        if prov_album_id in self.client.library:
            card = self.client.library[prov_album_id]
        else:
            await self.client.update_card_detail(prov_album_id)
            card = self.client.library.get(prov_album_id)
        if not card:
            raise MediaNotFoundError(f"Card {prov_album_id} not found")
        return self._parse_album(card)

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """
        Get full artist details by id.

        :param prov_artist_id: Provider's artist ID.
        """
        return Artist(
            item_id=prov_artist_id,
            provider=self.domain,
            name=prov_artist_id,
            provider_mappings={
                ProviderMapping(
                    item_id=prov_artist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """
        Get album tracks for given album id.

        :param prov_album_id: Provider's album ID.
        """
        await self.client.update_card_detail(prov_album_id)
        card = self.client.library.get(prov_album_id)
        if not card:
            raise MediaNotFoundError(f"Card {prov_album_id} not found")
        album = self._parse_album(card)

        tracks: list[Track] = []
        for idx, chapter in enumerate(card.chapters.values()):
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
            raise InvalidProviderID(f"Invalid track ID format: {item_id}")
        card_id, chapter_key = item_id.split(":", 1)
        await self.client.update_card_detail(card_id)
        card = self.client.library.get(card_id)
        if not card:
            raise MediaNotFoundError(f"Card {card_id} not found")

        chapter = card.chapters.get(chapter_key)
        if not chapter:
            raise MediaNotFoundError(f"Chapter {chapter_key} not found for card {card_id}")

        first_track = next(iter(chapter.tracks.values()), None)
        format_str = first_track.format if first_track else None
        content_type = ContentType.try_parse(format_str) if format_str else ContentType.AAC

        track_paths = [
            MultiPartPath(path=track.trackUrl, duration=track.duration)
            for track in chapter.tracks.values()
            if track.trackUrl
        ]

        if len(track_paths) == 0:
            raise MediaNotFoundError(f"No audio URLs found for chapter {chapter_key}")

        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(content_type=content_type),
            media_type=MediaType.TRACK,
            stream_type=StreamType.HTTP,
            duration=chapter.duration,
            path=track_paths,
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

    def _parse_album(self, card: YotoCard) -> Album:
        """
        Parse Yoto card into a Music Assistant Album.

        :param card: Yoto Card instance.
        """
        card_id = card.id
        title = card.title or "Unknown Card"
        author = card.author

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

        cover_url = card.cover_image_large

        return Album(
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
                description=card.description,
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

    def _parse_track(self, card_id: str, chapter: YotoChapter, idx: int, album: Album) -> Track:
        """
        Parse Yoto chapter into a Music Assistant Track.

        :param card_id: Parent card ID.
        :param chapter: Yoto Chapter instance.
        :param idx: 0-based index of the chapter.
        :param album: Parent Album object.
        """
        chapter_key = chapter.key or str(idx + 1)
        track_id = f"{card_id}:{chapter_key}"
        chapter_title = chapter.title or f"Chapter {idx + 1}"

        chapter_duration = chapter.duration
        if not chapter_duration and chapter.tracks:
            chapter_duration = sum(
                t.duration for t in chapter.tracks.values() if isinstance(t.duration, (int, float))
            )

        chapter_cover = chapter.icon
        album_cover = (
            album.metadata.images[0].path if (album.metadata and album.metadata.images) else None
        )
        track_cover = chapter_cover or album_cover

        format_str = None
        if chapter.tracks:
            first_tr = next(iter(chapter.tracks.values()))
            format_str = first_tr.format
        content_type = ContentType.try_parse(format_str) if format_str else ContentType.AAC

        return Track(
            item_id=track_id,
            provider=self.instance_id,
            name=chapter_title,
            duration=chapter_duration if chapter_duration else 0,
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
                            path=track_cover,
                            provider=self.instance_id,
                            remotely_accessible=True,
                        )
                    ]
                )
                if track_cover
                else None,
            ),
        )
