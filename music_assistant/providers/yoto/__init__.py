"""Yoto music provider for Music Assistant."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable
from typing import TYPE_CHECKING

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
    ProviderPermissionDenied,
    RateLimited,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    AudioFormat,
    ItemMapping,
    MediaItemChapter,
    MediaItemImage,
    MediaItemMetadata,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    Radio,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import MultiPartPath, StreamDetails
from yoto_api import Card as YotoCard
from yoto_api import Chapter as YotoChapter
from yoto_api import YotoAPIError, YotoClient, YotoError

from music_assistant.controllers.cache import use_cache
from music_assistant.models.music_provider import MusicProvider

from .setup_flow import CONF_CLIENT_ID, CONF_REFRESH_TOKEN

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_AUDIOBOOKS,
    ProviderFeature.LIBRARY_PODCASTS,
    ProviderFeature.LIBRARY_RADIOS,
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
        await self._handle_yoto_api_call(self.client.update_library())

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload of provider."""
        if self.client:
            await self.client.close()
        await super().unload(is_removed)

    async def get_library_albums(self) -> AsyncGenerator[Album]:
        """Retrieve library albums from the provider."""
        await self._handle_yoto_api_call(self.client.update_library())
        for card in filter(
            lambda card: card.category in ["music", "activities", "sfx", "none", None],
            self.client.library.values(),
        ):
            yield self._parse_album(card)

    @use_cache(3600 * 7)
    async def get_album(self, prov_album_id: str) -> Album:
        """
        Get full album details by id.

        :param prov_album_id: Provider's album ID.
        """
        return self._parse_album(await self._get_card(prov_album_id))

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """
        Get full artist details by id.

        :param prov_artist_id: Provider's artist ID.
        """
        return Artist(
            item_id=prov_artist_id,
            provider=self.instance_id,
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
        await self._handle_yoto_api_call(self.client.update_card_detail(prov_album_id))
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
            raise InvalidProviderID(f"Invalid track ID format: {prov_track_id}")
        card_id, _chapter_key = prov_track_id.split(":", 1)
        album_tracks = await self.get_album_tracks(card_id)
        for track in album_tracks:
            if track.item_id == prov_track_id:
                return track
        raise MediaNotFoundError(f"Track {prov_track_id} not found")

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Get stream details for a track, audiobook, podcast episode, or radio.

        :param item_id: Item ID.
        :param media_type: Media type of the item.
        """
        if media_type == MediaType.AUDIOBOOK:
            card_id = item_id
            await self._handle_yoto_api_call(self.client.update_card_detail(card_id))
            card = self.client.library.get(card_id)
            if not card:
                raise MediaNotFoundError(f"Card {card_id} not found")

            audiobook = self._parse_audiobook(card)

            track_paths = []
            total_duration = 0
            for chapter in card.chapters.values():
                for track in chapter.tracks.values():
                    if track.trackUrl:
                        track_paths.append(
                            MultiPartPath(path=track.trackUrl, duration=track.duration)
                        )
                        if track.duration:
                            total_duration += track.duration

            if not track_paths:
                raise MediaNotFoundError(f"No audio URLs found for card {card_id}")

            # Use format from first track
            first_chapter = next(iter(card.chapters.values()), None)
            assert first_chapter  # We know there are chapters due to the track enumeration above
            first_track = next(iter(first_chapter.tracks.values()), None)
            assert first_track  # We know there are tracks due to the track enumeration above
            format_str = first_track.format if first_track else None
            content_type = ContentType.try_parse(format_str) if format_str else ContentType.AAC

            return StreamDetails(
                provider=self.instance_id,
                item_id=item_id,
                audio_format=AudioFormat(content_type=content_type),
                media_type=media_type,
                stream_type=StreamType.HTTP,
                duration=audiobook.duration,
                path=track_paths,
                allow_seek=True,
                can_seek=True,
            )
        if ":" not in item_id:
            raise InvalidProviderID(f"Invalid item ID format: {item_id}")
        card_id, chapter_key = item_id.split(":", 1)
        await self._handle_yoto_api_call(self.client.update_card_detail(card_id))
        card = self.client.library.get(card_id)
        if not card:
            raise MediaNotFoundError(f"Card {card_id} not found")

        card_chapter = card.chapters.get(chapter_key)
        if not card_chapter:
            raise MediaNotFoundError(f"Chapter {chapter_key} not found for card {card_id}")

        first_track = next(iter(card_chapter.tracks.values()), None)
        format_str = first_track.format if first_track else None
        content_type = ContentType.try_parse(format_str) if format_str else ContentType.AAC

        track_paths = [
            MultiPartPath(path=track.trackUrl, duration=track.duration)
            for track in card_chapter.tracks.values()
            if track.trackUrl
        ]

        if len(track_paths) == 0:
            raise MediaNotFoundError(f"No audio URLs found for chapter {chapter_key}")

        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(content_type=content_type),
            media_type=media_type,
            stream_type=StreamType.HTTP,
            duration=card_chapter.duration
            if card_chapter.duration
            else None,  # seems like a nop, but this maps Literal[0] -> None
            path=track_paths,
            allow_seek=True,
            can_seek=True,
        )

    async def get_library_audiobooks(self) -> AsyncGenerator[Audiobook]:
        """Retrieve library audiobooks from the provider."""
        await self._handle_yoto_api_call(self.client.update_library())
        for card in filter(lambda card: card.category == "stories", self.client.library.values()):
            await self._handle_yoto_api_call(self.client.update_card_detail(card.id))
            yield self._parse_audiobook(card)

    @use_cache(3600 * 24 * 7)
    async def get_audiobook(self, prov_audiobook_id: str) -> Audiobook:
        """Get full audiobook details by id."""
        card: YotoCard | None = None
        if prov_audiobook_id in self.client.library:
            card = self.client.library[prov_audiobook_id]
        else:
            await self._handle_yoto_api_call(self.client.update_card_detail(prov_audiobook_id))
            card = self.client.library.get(prov_audiobook_id)
        if not card:
            raise MediaNotFoundError(f"Card {prov_audiobook_id} not found")
        return self._parse_audiobook(card)

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast]:
        """Retrieve library podcasts from the provider."""
        await self._handle_yoto_api_call(self.client.update_library())
        for card in filter(lambda card: card.category == "podcast", self.client.library.values()):
            yield self._parse_podcast(card)

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get full podcast details by id."""
        return self._parse_podcast(await self._get_card(prov_podcast_id))

    async def get_podcast_episodes(self, prov_podcast_id: str) -> AsyncGenerator[PodcastEpisode]:
        """Get all PodcastEpisodes for given podcast id."""
        card = await self._get_card(prov_podcast_id)
        podcast = self._parse_podcast(await self._get_card(prov_podcast_id))
        for idx, episode in enumerate(card.chapters.values()):
            parsed_episode = self._parse_podcast_episode(prov_podcast_id, episode, idx, podcast)
            yield parsed_episode

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get (full) podcast episode details by id."""
        if ":" not in prov_episode_id:
            raise InvalidProviderID(f"Invalid episode ID format: {prov_episode_id}")
        card_id, _chapter_key = prov_episode_id.split(":", 1)
        card = await self._get_card(card_id)
        podcast = self._parse_podcast(card)
        for idx, chapter in enumerate(card.chapters.values()):
            if f"{card_id}:{chapter.key}" == prov_episode_id:
                return self._parse_podcast_episode(card_id, chapter, idx, podcast)
        raise MediaNotFoundError(f"Episode {prov_episode_id} not found")

    async def get_library_radios(self) -> AsyncGenerator[Radio]:
        """Retrieve library radio stations from the provider."""
        await self._handle_yoto_api_call(self.client.update_library())
        for card in filter(lambda card: card.category == "radio", self.client.library.values()):
            await self._handle_yoto_api_call(self.client.update_card_detail(card.id))
            async for radio in self._parse_radio_stations(card):
                yield radio

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id."""
        card_id, _station_id = prov_radio_id.split(":", 1)
        async for station in self._parse_radio_stations(await self._get_card(card_id)):
            if station.item_id == prov_radio_id:
                return station
        raise MediaNotFoundError(f"Radio station {prov_radio_id} not found")

    async def _get_card(self, card_id: str) -> YotoCard:
        """Get a Yoto card from the provider."""
        if len(self.client.library) == 0:
            await self._handle_yoto_api_call(self.client.update_library())

        # if card_id in self.client.library:
        #    return self.client.library[card_id]
        await self._handle_yoto_api_call(self.client.update_card_detail(card_id))
        card = self.client.library.get(card_id)
        if not card:
            raise MediaNotFoundError(f"Card {card_id} not found")
        return card

    async def _handle_yoto_api_call(self, api_call: Awaitable[None]) -> None:
        """Handle Yoto API calls and wrap errors in appropriate exceptions."""
        assert self.client.token
        refresh_token = self.client.token.refresh_token
        try:
            await api_call
        except YotoAPIError as err:
            if err.status_code is not None:
                match err.status_code:
                    case 403:
                        raise ProviderPermissionDenied(
                            "Error returned from Yoto API: Forbidden (403)"
                        ) from err
                    case 404:
                        raise MediaNotFoundError(
                            "Error returned from Yoto API: Not Found (404)"
                        ) from err
                    case 429:
                        raise RateLimited(
                            "Error returned from Yoto API: too many requests (429)"
                        ) from err
                    case code:
                        raise ResourceTemporarilyUnavailable(
                            f"Error returned from Yoto API: HTTP error code {code}"
                        ) from err
            else:
                raise ResourceTemporarilyUnavailable(
                    f"Error returned from Yoto API - no HTTP code available: {err}"
                ) from err
        except YotoError as err:
            raise ResourceTemporarilyUnavailable(f"Error returned from Yoto API: {err}") from err
        except TimeoutError:
            raise ResourceTemporarilyUnavailable("Error returned from Yoto API: Timeout")
        finally:
            if refresh_token != self.client.token.refresh_token:
                self._update_setup_data(CONF_REFRESH_TOKEN, self.client.token.refresh_token)

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
        chapter_key = chapter.key
        track_id = f"{card_id}:{chapter_key}"
        chapter_title = chapter.title or f"Track {idx + 1}"

        chapter_duration = chapter.duration
        if not chapter_duration and chapter.tracks:
            chapter_duration = sum(
                t.duration for t in chapter.tracks.values() if isinstance(t.duration, (int, float))
            )

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
                            path=album.metadata.images[0].path,
                            provider=self.instance_id,
                            remotely_accessible=True,
                        )
                    ]
                )
                if (album.metadata and album.metadata.images)
                else None,
            ),
        )

    def _parse_audiobook(self, card: YotoCard) -> Audiobook:
        """Parse Yoto card into a Music Assistant Audiobook."""
        card_id = card.id
        title = card.title or f"Unknown Audiobook ({card_id})"

        chapters: list[MediaItemChapter] = []
        total_duration = 0
        for idx, chapter in enumerate(card.chapters.values(), start=1):
            track = next(iter(chapter.tracks.values()))
            if track and (chapter.duration or track.duration):
                chapter_title = chapter.title or track.title or f"Chapter {idx}"
                chapter_duration = chapter.duration or track.duration or 0
                chapters.append(
                    MediaItemChapter(
                        position=idx,
                        name=chapter_title,
                        start=total_duration,
                        end=total_duration + chapter_duration,
                    )
                )
                total_duration += chapter_duration
            else:
                self.logger.warning(
                    f"Audiobook {card_id} has a chapter {chapter.title} with no track data, or zero duration - skipping"
                )

        return Audiobook(
            item_id=card_id,
            provider=self.instance_id,
            name=title,
            authors=UniqueList([card.author] if card.author else []),
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
                            path=card.cover_image_large,
                            provider=self.instance_id,
                            remotely_accessible=True,
                        )
                    ]
                )
                if card.cover_image_large
                else None,
                chapters=UniqueList(chapters),
            ),
            duration=total_duration,
        )

    def _parse_podcast(self, card: YotoCard) -> Podcast:
        """Parse Yoto card into a Music Assistant Podcast."""
        card_id = card.id
        title = card.title or f"Unknown Podcast {card_id}"

        return Podcast(
            item_id=card_id,
            provider=self.instance_id,
            name=title,
            publisher=card.author,
            total_episodes=len(card.chapters) if card.chapters else None,
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
                            path=card.cover_image_large,
                            provider=self.instance_id,
                            remotely_accessible=True,
                        )
                    ]
                )
                if card.cover_image_large
                else None,
            ),
        )

    def _parse_podcast_episode(
        self, card_id: str, episode: YotoChapter, idx: int, podcast: Podcast
    ) -> PodcastEpisode:
        """Parse Yoto chapter into a Music Assistant PodcastEpisode."""
        episode_id = f"{card_id}:{episode.key}"
        if not episode.tracks:
            raise MediaNotFoundError(f"No track found for podcast episode {episode_id}")
        track = next(iter(episode.tracks.values()))

        chapter_duration = episode.duration or track.duration or 0

        content_type = ContentType.try_parse(track.format) if track.format else ContentType.AAC

        return PodcastEpisode(
            item_id=episode_id,
            provider=self.instance_id,
            name=episode.title or track.title or f"Episode {idx + 1}",
            duration=chapter_duration,
            position=idx + 1,
            podcast=ItemMapping(
                item_id=card_id,
                provider=self.instance_id,
                name=podcast.name,
                media_type=MediaType.PODCAST,
            ),
            provider_mappings={
                ProviderMapping(
                    item_id=episode_id,
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
                            path=podcast.metadata.images[0].path,
                            provider=self.instance_id,
                            remotely_accessible=True,
                        )
                    ]
                )
                if podcast.metadata and podcast.metadata.images
                else None,
            ),
        )

    async def _parse_radio_stations(self, card: YotoCard) -> AsyncGenerator[Radio]:
        """Parse Yoto card into a Music Assistant Radio."""
        # Radios are set up so that if there are multiple streams from in the same Station,
        # each stream is a chapter with a single track. For some reason that name of the
        # stream is also on the track while the chapter name is a shorter name not shown in the UI.

        for idx, station in enumerate(card.chapters.values(), start=1):
            if station.tracks:
                stream = next(iter(station.tracks.values()))
                item_id = f"{card.id}:{station.key}"
                yield Radio(
                    item_id=item_id,
                    provider=self.instance_id,
                    name=stream.title or f"Station {idx}",
                    provider_mappings={
                        ProviderMapping(
                            item_id=item_id,
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
                                    path=card.cover_image_large,
                                    provider=self.instance_id,
                                    remotely_accessible=True,
                                )
                            ]
                        )
                        if card.cover_image_large
                        else None,
                    ),
                )
            else:
                self.logger.warning(f"No tracks found for radio station {station.key}")
