"""
The Genius Lyrics Metadata provider for Music Assistant.

Used for retrieval of lyrics.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderFeature
from music_assistant_models.media_items import MediaItemMetadata, Track

from music_assistant.controllers.cache import use_cache
from music_assistant.models.metadata_provider import MetadataProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

from lyricsgenius import Genius

from .helpers import clean_song_title, cleanup_lyrics

SUPPORTED_FEATURES = {
    ProviderFeature.TRACK_METADATA,
    ProviderFeature.LYRICS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return GeniusProvider(mass, manifest, config, SUPPORTED_FEATURES)


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
    return ()  # we do not have any config entries (yet)


class GeniusProvider(MetadataProvider):
    """Genius Lyrics provider for handling lyrics."""

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._genius = Genius("public", skip_non_songs=True, remove_section_headers=True)

    async def get_track_metadata(self, track: Track) -> MediaItemMetadata | None:
        """Retrieve synchronized lyrics for a track."""
        if track.metadata and (track.metadata.lyrics or track.metadata.lrc_lyrics):
            self.logger.debug("Skipping lyrics lookup for %s: Already has lyrics", track.name)
            return None

        if not track.artists:
            self.logger.info("Skipping lyrics lookup for %s: No artist information", track.name)
            return None

        artist_name = track.artists[0].name

        if not track.name or len(track.name.strip()) == 0:
            self.logger.info(
                "Skipping lyrics lookup for %s: No track name information", artist_name
            )
            return None

        song_lyrics = await self.fetch_lyrics(artist_name, track.name)

        if song_lyrics:
            metadata = MediaItemMetadata()
            metadata.lyrics = song_lyrics

            self.logger.debug("Found lyrics for %s by %s", track.name, artist_name)
            return metadata

        self.logger.info("No lyrics found for %s by %s", track.name, artist_name)
        return None

    @use_cache(86400 * 7)  # Cache for 7 days
    async def fetch_lyrics(self, artist: str, title: str) -> str | None:
        """Fetch lyrics for a given artist and title."""

        def _fetch_lyrics(artist: str, title: str) -> str | None:
            """Fetch lyrics - NOTE: not async friendly."""
            # blank artist / title?
            if (
                artist is None
                or len(artist.strip()) == 0
                or title is None
                or len(title.strip()) == 0
            ):
                self.logger.error("Cannot fetch lyrics without artist and title")
                return None

            # clean song title to increase chance and accuracy of a result
            cleaned_title = clean_song_title(title)
            if cleaned_title != title:
                self.logger.debug(f'Song title was cleaned: "{title}"  ->  "{cleaned_title}"')

            self.logger.info(f"Searching lyrics for artist='{artist}' and title='{cleaned_title}'")

            # perform search
            song = self._genius.search_song(cleaned_title, artist, get_full_info=False)

            # second search needed?
            if not song and " - " in cleaned_title:
                # aggressively truncate title from the first hyphen
                cleaned_title = cleaned_title.split(" - ", 1)[0]
                self.logger.info(f"Second attempt, aggressively cleaned title='{cleaned_title}'")

                # perform search
                song = self._genius.search_song(cleaned_title, artist, get_full_info=False)

            if song:
                # attempts to clean lyrics of erroneous text
                return cleanup_lyrics(song)

            return None

        return await asyncio.to_thread(_fetch_lyrics, artist, title)
