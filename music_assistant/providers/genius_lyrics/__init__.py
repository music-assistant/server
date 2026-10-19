"""
The Genius Lyrics Metadata provider for Music Assistant.

Used for retrieval of lyrics.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderFeature
from music_assistant_models.media_items import MediaItemMetadata, Track

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.compare import compare_strings
from music_assistant.models.metadata_provider import MetadataProvider

if TYPE_CHECKING:
    from lyricsgenius.types import Song
    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

from lyricsgenius import Genius

from .helpers import clean_song_title, cleanup_lyrics

# Genius credits collaborations as a single combined primary artist ("JAY-Z & Kanye West")
GENIUS_ARTIST_SPLITTER = re.compile(r"\s*(?:&|,|\bfeat\.|\bft\.)\s*", re.IGNORECASE)

SUPPORTED_FEATURES = {
    ProviderFeature.TRACK_METADATA,
    ProviderFeature.LYRICS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return GeniusProvider(mass, manifest, config, SUPPORTED_FEATURES)


class GeniusProvider(MetadataProvider):
    """Genius Lyrics provider for handling lyrics."""

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to setup this provider."""
        return ()  # we do not have any config entries (yet)

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
            song = self._search_song(artist, cleaned_title)

            # second search needed?
            if not song and " - " in cleaned_title:
                # aggressively truncate title from the first hyphen
                cleaned_title = cleaned_title.split(" - ", 1)[0]
                self.logger.info(f"Second attempt, aggressively cleaned title='{cleaned_title}'")

                # perform search
                song = self._search_song(artist, cleaned_title)

            if song:
                # attempts to clean lyrics of erroneous text
                return cleanup_lyrics(song)

            return None

        return await asyncio.to_thread(_fetch_lyrics, artist, title)

    def _search_song(self, artist: str, title: str) -> Song | None:
        """Search Genius and return the hit only if its title and artist match the request."""
        song = self._genius.search_song(title, artist, get_full_info=False)
        if not song:
            return None
        # lyricsgenius falls back to the first search hit, which is often an unrelated song
        title_match = compare_strings(clean_song_title(song.title), title)
        hit_artists = [song.artist, *GENIUS_ARTIST_SPLITTER.split(song.artist)]
        if title_match and any(compare_strings(name, artist) for name in hit_artists):
            return song
        self.logger.debug(
            "Ignoring Genius hit '%s' by '%s' for '%s' by '%s'",
            song.title,
            song.artist,
            title,
            artist,
        )
        return None
