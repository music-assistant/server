"""Test/Demo provider that creates a collection of fake media items."""

from __future__ import annotations

import random
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ArtistType,
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    AudioFormat,
    ItemMapping,
    MediaItemChapter,
    MediaItemCollection,
    MediaItemImage,
    MediaItemMetadata,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import (
    DEFAULT_GENRES,
    MASS_LOGO,
    SILENCE_FILE_LONG,
    VARIOUS_ARTISTS_FANART,
)
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


DEFAULT_THUMB = MediaItemImage(
    type=ImageType.THUMB,
    path=MASS_LOGO,
    provider="builtin",
    remotely_accessible=False,
)

DEFAULT_FANART = MediaItemImage(
    type=ImageType.FANART,
    path=VARIOUS_ARTISTS_FANART,
    provider="builtin",
    remotely_accessible=False,
)

CONF_KEY_NUM_ARTISTS = "num_artists"
CONF_KEY_NUM_ALBUMS = "num_albums"
CONF_KEY_NUM_TRACKS = "num_tracks"
CONF_KEY_NUM_PODCASTS = "num_podcasts"
CONF_KEY_NUM_AUDIOBOOKS = "num_audiobooks"
CONF_KEY_AUTHORS_NARRATORS_AS_ARTISTS = "authors_narrators_as_artists"

# item_id prefixes that keep the authors and narrators apart from the music artists
AUTHOR_ID_PREFIX = "author"
NARRATOR_ID_PREFIX = "narrator"

AUDIOBOOK_COLLECTIONS_TITLE = {
    1: "Collection 1",
    2: "Collection 2",
}

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PODCASTS,
    ProviderFeature.LIBRARY_AUDIOBOOKS,
    ProviderFeature.SIMILAR_TRACKS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    features = set(SUPPORTED_FEATURES)
    if config.get_value(CONF_KEY_AUTHORS_NARRATORS_AS_ARTISTS):
        features.update((ProviderFeature.AUTHOR_AUDIOBOOKS, ProviderFeature.NARRATOR_AUDIOBOOKS))
    return TestProvider(mass, manifest, config, features)


class TestProvider(MusicProvider):
    """Test/Demo provider that creates a collection of fake media items."""

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        return (
            ConfigEntry(
                key=CONF_KEY_NUM_ARTISTS,
                type=ConfigEntryType.INTEGER,
                label="Number of (test) artists",
                description="Number of test artists to generate",
                default_value=5,
                required=False,
            ),
            ConfigEntry(
                key=CONF_KEY_NUM_ALBUMS,
                type=ConfigEntryType.INTEGER,
                label="Number of (test) albums per artist",
                description="Number of test albums to generate per artist",
                default_value=5,
                required=False,
            ),
            ConfigEntry(
                key=CONF_KEY_NUM_TRACKS,
                type=ConfigEntryType.INTEGER,
                label="Number of (test) tracks per album",
                description="Number of test tracks to generate per artist-album",
                default_value=20,
                required=False,
            ),
            ConfigEntry(
                key=CONF_KEY_NUM_PODCASTS,
                type=ConfigEntryType.INTEGER,
                label="Number of (test) podcasts",
                description="Number of test podcasts to generate",
                default_value=5,
                required=False,
            ),
            ConfigEntry(
                key=CONF_KEY_NUM_AUDIOBOOKS,
                type=ConfigEntryType.INTEGER,
                label="Number of (test) audiobooks",
                description="Number of test audiobooks to generate",
                default_value=20,
                required=False,
            ),
            ConfigEntry(
                key=CONF_KEY_AUTHORS_NARRATORS_AS_ARTISTS,
                type=ConfigEntryType.BOOLEAN,
                label="Expose authors and narrators as artists",
                description="Expose authors and narrators of the audiobooks as full artist items.",
                default_value=False,
                required=False,
            ),
        )

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return False

    @property
    def supported_artist_types(self) -> set[ArtistType]:
        """Supported artist types."""
        if self.config.get_value(CONF_KEY_AUTHORS_NARRATORS_AS_ARTISTS):
            return {ArtistType.SINGER, ArtistType.AUTHOR, ArtistType.NARRATOR}
        return {ArtistType.SINGER}

    async def get_library_genres(self) -> AsyncGenerator[str]:
        """Retrieve library genres from the provider."""
        for genre in DEFAULT_GENRES:
            yield genre

    async def get_item_genre_names(self, media_type: MediaType, item_id: str) -> set[str]:
        """Return genre names for a single item."""
        if media_type == MediaType.ARTIST:
            if item_id.startswith((f"{AUTHOR_ID_PREFIX}_", f"{NARRATOR_ID_PREFIX}_")):
                return set()
            seed = item_id
        elif media_type == MediaType.ALBUM:
            seed = item_id.split("_", 2)[0]
        elif media_type == MediaType.TRACK:
            seed = item_id.split("_", 3)[0]
        elif media_type == MediaType.PODCAST:
            seed = item_id
        elif media_type == MediaType.PODCAST_EPISODE:
            seed = item_id.split("_", 2)[0]
        elif media_type == MediaType.AUDIOBOOK:
            seed = item_id
        else:
            return set()
        return {random.Random(seed).choice(DEFAULT_GENRES)}

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        artist_idx, album_idx, track_idx = prov_track_id.split("_", 3)
        genre = random.Random(artist_idx).choice(DEFAULT_GENRES)
        return Track(
            item_id=prov_track_id,
            provider=self.instance_id,
            name=f"{genre} Test Track {artist_idx} - {album_idx} - {track_idx}",
            duration=60,
            artists=UniqueList([await self.get_artist(artist_idx)]),
            album=await self.get_album(f"{artist_idx}_{album_idx}"),
            provider_mappings={
                ProviderMapping(
                    item_id=prov_track_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                ),
            },
            metadata=MediaItemMetadata(
                images=UniqueList([DEFAULT_THUMB]),
                genres={genre},
                release_date=datetime(2021, 6, 15, tzinfo=UTC),
            ),
            disc_number=1,
            track_number=int(track_idx),
        )

    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Return a deterministic set of similar tracks (the catalogue neighbours of the seed)."""
        num_artists = self.config.get_value(CONF_KEY_NUM_ARTISTS) or 5
        num_albums = self.config.get_value(CONF_KEY_NUM_ALBUMS) or 5
        num_tracks = self.config.get_value(CONF_KEY_NUM_TRACKS) or 20
        assert isinstance(num_artists, int)
        assert isinstance(num_albums, int)
        assert isinstance(num_tracks, int)
        total = num_artists * num_albums * num_tracks
        artist_idx, album_idx, track_idx = (int(part) for part in prov_track_id.split("_", 3))
        seed_ordinal = (artist_idx * num_albums + album_idx) * num_tracks + track_idx
        # walk the catalogue starting just after the seed so the result is stable and seed-excluded
        similar: list[Track] = []
        for step in range(1, total):
            if len(similar) >= limit:
                break
            ordinal = (seed_ordinal + step) % total
            artist, remainder = divmod(ordinal, num_albums * num_tracks)
            album, track = divmod(remainder, num_tracks)
            similar.append(await self.get_track(f"{artist}_{album}_{track}"))
        return similar

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        if prov_artist_id.startswith((f"{AUTHOR_ID_PREFIX}_", f"{NARRATOR_ID_PREFIX}_")):
            return self._get_audiobook_artist(prov_artist_id)
        genre = random.Random(prov_artist_id).choice(DEFAULT_GENRES)
        return Artist(
            item_id=prov_artist_id,
            provider=self.instance_id,
            name=f"{genre} Test Artist {prov_artist_id}",
            metadata=MediaItemMetadata(
                images=UniqueList([DEFAULT_THUMB, DEFAULT_FANART]),
                genres={genre},
            ),
            provider_mappings={
                ProviderMapping(
                    item_id=prov_artist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full artist details by id."""
        artist_idx, album_idx = prov_album_id.split("_", 2)
        genre = random.Random(artist_idx).choice(DEFAULT_GENRES)
        return Album(
            item_id=prov_album_id,
            provider=self.instance_id,
            name=f"{genre} Test Album {album_idx}",
            artists=UniqueList([await self.get_artist(artist_idx)]),
            provider_mappings={
                ProviderMapping(
                    item_id=prov_album_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            metadata=MediaItemMetadata(images=UniqueList([DEFAULT_THUMB]), genres={genre}),
        )

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get all tracks for the given album id."""
        num_tracks = self.config.get_value(CONF_KEY_NUM_TRACKS) or 20
        assert isinstance(num_tracks, int)
        artist_idx, album_idx = prov_album_id.split("_", 2)
        return [
            await self.get_track(f"{artist_idx}_{album_idx}_{track_idx}")
            for track_idx in range(num_tracks)
        ]

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get full podcast details by id."""
        genre = random.Random(prov_podcast_id).choice(DEFAULT_GENRES)
        return Podcast(
            item_id=prov_podcast_id,
            provider=self.instance_id,
            name=f"{genre} Test Podcast {prov_podcast_id}",
            metadata=MediaItemMetadata(images=UniqueList([DEFAULT_THUMB]), genres={genre}),
            provider_mappings={
                ProviderMapping(
                    item_id=prov_podcast_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            publisher="Test Publisher",
        )

    async def get_audiobook(self, prov_audiobook_id: str) -> Audiobook:
        """Get full audiobook details by id."""
        genre = random.Random(prov_audiobook_id).choice(DEFAULT_GENRES)
        authors: UniqueList[Artist | ItemMapping | str]
        narrators: UniqueList[Artist | ItemMapping | str]
        if self.config.get_value(CONF_KEY_AUTHORS_NARRATORS_AS_ARTISTS):
            authors = UniqueList(
                [self._get_audiobook_artist(f"{AUTHOR_ID_PREFIX}_{prov_audiobook_id}")]
            )
            narrators = UniqueList(
                [self._get_audiobook_artist(f"{NARRATOR_ID_PREFIX}_{prov_audiobook_id}")]
            )
        else:
            authors = UniqueList(["AudioBook Author"])
            narrators = UniqueList(["AudioBook Narrator"])
        return Audiobook(
            item_id=prov_audiobook_id,
            provider=self.instance_id,
            name=f"{genre} Test Audiobook {prov_audiobook_id}",
            metadata=MediaItemMetadata(
                images=UniqueList([DEFAULT_THUMB]),
                description="This is a description for Test Audiobook",
                chapters=[
                    MediaItemChapter(position=1, name="Chapter 1", start=10, end=20),
                    MediaItemChapter(position=2, name="Chapter 2", start=20, end=40),
                    MediaItemChapter(position=2, name="Chapter 3", start=40),
                ],
                genres={genre},
                collections=self._get_audiobook_collections(prov_audiobook_id),
            ),
            provider_mappings={
                ProviderMapping(
                    item_id=prov_audiobook_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            publisher="Test Publisher",
            authors=authors,
            narrators=narrators,
            duration=60,
        )

    async def get_library_artists(self) -> AsyncGenerator[Artist]:
        """Retrieve library artists from the provider."""
        num_artists = self.config.get_value(CONF_KEY_NUM_ARTISTS)
        assert isinstance(num_artists, int)
        for artist_idx in range(num_artists):
            yield await self.get_artist(str(artist_idx))
        if not self.config.get_value(CONF_KEY_AUTHORS_NARRATORS_AS_ARTISTS):
            return
        num_audiobooks = self.config.get_value(CONF_KEY_NUM_AUDIOBOOKS)
        if TYPE_CHECKING:
            assert isinstance(num_audiobooks, int)
        for audiobook_idx in range(num_audiobooks):
            yield self._get_audiobook_artist(f"{AUTHOR_ID_PREFIX}_{audiobook_idx}")
            yield self._get_audiobook_artist(f"{NARRATOR_ID_PREFIX}_{audiobook_idx}")

    async def get_library_albums(self) -> AsyncGenerator[Album]:
        """Retrieve library albums from the provider."""
        num_artists = self.config.get_value(CONF_KEY_NUM_ARTISTS) or 5
        assert isinstance(num_artists, int)
        num_albums = self.config.get_value(CONF_KEY_NUM_ALBUMS)
        assert isinstance(num_albums, int)
        for artist_idx in range(num_artists):
            for album_idx in range(num_albums):
                album_item_id = f"{artist_idx}_{album_idx}"
                yield await self.get_album(album_item_id)

    async def get_library_tracks(self) -> AsyncGenerator[Track]:
        """Retrieve library tracks from the provider."""
        num_artists = self.config.get_value(CONF_KEY_NUM_ARTISTS) or 5
        assert isinstance(num_artists, int)
        num_albums = self.config.get_value(CONF_KEY_NUM_ALBUMS) or 5
        assert isinstance(num_albums, int)
        num_tracks = self.config.get_value(CONF_KEY_NUM_TRACKS)
        assert isinstance(num_tracks, int)
        for artist_idx in range(num_artists):
            for album_idx in range(num_albums):
                for track_idx in range(num_tracks):
                    track_item_id = f"{artist_idx}_{album_idx}_{track_idx}"
                    yield await self.get_track(track_item_id)

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast]:
        """Retrieve library tracks from the provider."""
        num_podcasts = self.config.get_value(CONF_KEY_NUM_PODCASTS)
        assert isinstance(num_podcasts, int)
        for podcast_idx in range(num_podcasts):
            yield await self.get_podcast(str(podcast_idx))

    async def get_library_audiobooks(self) -> AsyncGenerator[Audiobook]:
        """Retrieve library audiobooks from the provider."""
        num_audiobooks = self.config.get_value(CONF_KEY_NUM_AUDIOBOOKS)
        assert isinstance(num_audiobooks, int)
        for audiobook_idx in range(num_audiobooks):
            yield await self.get_audiobook(str(audiobook_idx))

    async def get_podcast_episodes(
        self,
        prov_podcast_id: str,
    ) -> AsyncGenerator[PodcastEpisode]:
        """Get all PodcastEpisodes for given podcast id."""
        num_episodes = 25
        for episode_idx in range(num_episodes):
            yield await self.get_podcast_episode(f"{prov_podcast_id}_{episode_idx}")

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get (full) podcast episode details by id."""
        podcast_id, episode_idx = prov_episode_id.split("_", 2)
        genre = random.Random(podcast_id).choice(DEFAULT_GENRES)
        return PodcastEpisode(
            item_id=prov_episode_id,
            provider=self.instance_id,
            name=f"{genre} Test PodcastEpisode {podcast_id}-{episode_idx}",
            duration=60,
            podcast=ItemMapping(
                item_id=podcast_id,
                provider=self.instance_id,
                name=f"Test Podcast {podcast_id}",
                media_type=MediaType.PODCAST,
                image=DEFAULT_THUMB,
            ),
            provider_mappings={
                ProviderMapping(
                    item_id=prov_episode_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            metadata=MediaItemMetadata(
                description="This is a description for "
                f"Test PodcastEpisode {episode_idx} of Test Podcast {podcast_id}",
                genres={genre},
            ),
            position=int(episode_idx),
        )

    async def get_author_audiobooks(self, prov_artist_id: str) -> list[Audiobook]:
        """Get a list of all audiobooks for the given author."""
        return [await self._get_audiobook_for_artist(prov_artist_id)]

    async def get_narrator_audiobooks(self, prov_artist_id: str) -> list[Audiobook]:
        """Get a list of all audiobooks for the given narrator."""
        return [await self._get_audiobook_for_artist(prov_artist_id)]

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a track/radio."""
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.OGG,
                sample_rate=48000,
                bit_depth=16,
                channels=2,
            ),
            media_type=media_type,
            stream_type=StreamType.HTTP,
            path=SILENCE_FILE_LONG,
            can_seek=True,
            allow_seek=True,
        )

    def _get_audiobook_artist(self, prov_artist_id: str) -> Artist:
        """Build the author or narrator artist for the given prefixed item id."""
        prefix, audiobook_idx = prov_artist_id.split("_", 1)
        artist_type = ArtistType.AUTHOR if prefix == AUTHOR_ID_PREFIX else ArtistType.NARRATOR
        return Artist(
            item_id=prov_artist_id,
            provider=self.instance_id,
            name=f"Test {prefix.capitalize()} {audiobook_idx}",
            artist_type=artist_type,
            metadata=MediaItemMetadata(images=UniqueList([DEFAULT_THUMB])),
            provider_mappings={
                ProviderMapping(
                    item_id=prov_artist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

    async def _get_audiobook_for_artist(self, prov_artist_id: str) -> Audiobook:
        """Get the audiobook that the given author/narrator item id belongs to."""
        # each test audiobook has exactly one author and one narrator, carrying its own index
        _, audiobook_idx = prov_artist_id.split("_", 1)
        return await self.get_audiobook(audiobook_idx)

    def _get_audiobook_collections(
        self, prov_audiobook_id: str
    ) -> UniqueList[MediaItemCollection] | None:
        """Get the collection(s) the given audiobook is part of, if any."""
        audiobook_idx = int(prov_audiobook_id)
        num_collections = len(AUDIOBOOK_COLLECTIONS_TITLE)
        collection_title = AUDIOBOOK_COLLECTIONS_TITLE.get(audiobook_idx % (num_collections + 1))
        if collection_title is None:
            return None
        collection = MediaItemCollection(
            title=collection_title, sequence=audiobook_idx // (num_collections + 1)
        )
        return UniqueList([collection])
