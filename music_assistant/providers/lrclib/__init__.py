"""
The LRCLIB Metadata provider for Music Assistant.

Used for retrieval of synchronized lyrics.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from aiohttp import ClientResponseError
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, ProviderFeature
from music_assistant_models.media_items import MediaItemMetadata, Track

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.throttle_retry import ThrottlerManager, throttle_with_retries
from music_assistant.models.metadata_provider import MetadataProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.TRACK_METADATA,
    ProviderFeature.LYRICS,
}

CONF_API_URL = "api_url"
DEFAULT_API_URL = "https://lrclib.net/api"
USER_AGENT = "MusicAssistant (https://github.com/music-assistant/server)"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return LrclibProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    # ruff: noqa: ARG001
    return (
        ConfigEntry(
            key=CONF_API_URL,
            type=ConfigEntryType.STRING,
            label="API URL",
            description="URL of the LRCLib API (including 'api' but excluding '/get')",
            default_value=DEFAULT_API_URL,
            required=False,
        ),
    )


class LrclibProvider(MetadataProvider):
    """LRCLIB provider for handling synchronized lyrics."""

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # Get the API URL from config
        self.api_url = self.config.get_value(CONF_API_URL)

        # Only use strict throttling if using the default API
        if self.api_url == DEFAULT_API_URL:
            self.throttler = ThrottlerManager(rate_limit=1, period=30)
            self.logger.debug("Using default API with standard throttling (1 request per 30s)")
        else:
            # Less strict throttling for custom API endpoint
            self.throttler = ThrottlerManager(rate_limit=1, period=1)
            self.logger.debug("Using custom API endpoint: %s (throttling disabled)", self.api_url)

    @use_cache(3600 * 24 * 14)  # Cache for 14 days
    @throttle_with_retries
    async def _get_data(self, **params: Any) -> dict[str, Any] | None:
        """Get data from LRCLib API with throttling and retries."""
        headers = {"User-Agent": USER_AGENT}

        try:
            async with self.mass.http_session.get(
                f"{self.api_url}/get", params=params, headers=headers
            ) as response:
                response.raise_for_status()
                if response.status == 204:  # No content
                    return None
                return cast("dict[str, Any]", await response.json())
        except ClientResponseError as err:
            self.logger.debug("Error fetching data from LRCLib API (%s): %s", self.api_url, err)
            return None
        except json.JSONDecodeError as err:
            self.logger.debug("Error parsing response from LRCLib API: %s", err)
            return None

    async def get_track_metadata(self, track: Track) -> MediaItemMetadata | None:
        """Retrieve synchronized lyrics for a track."""
        if track.metadata and (track.metadata.lyrics or track.metadata.lrc_lyrics):
            self.logger.debug(
                "Lyrics already exist for %s, skipping LRCLIB lookup for this track.",
                track.name,
            )
            return None

        if not track.artists:
            self.logger.info("Skipping lyrics lookup for %s: No artist information", track.name)
            return None

        artist_name = track.artists[0].name
        album_name = track.album.name if track.album else ""

        duration = track.duration or 0

        if not duration:
            self.logger.info("Skipping lyrics lookup for %s: No duration information", track.name)
            return None

        self.logger.debug(
            "Fetching synchronized lyrics for %s by %s (%s) on lrclib.net",
            track.name,
            artist_name,
            album_name,
        )

        search_params = {
            "track_name": track.name,
            "artist_name": artist_name,
            "album_name": album_name,
            "duration": duration,
        }

        self.logger.debug("Searching lyrics (sync-ed preferred) with params: %s", search_params)

        if data := await self._get_data(**search_params):
            synced_lyrics = data.get("syncedLyrics")

            if synced_lyrics:
                metadata = MediaItemMetadata()
                metadata.lrc_lyrics = synced_lyrics

                self.logger.debug("Found synchronized lyrics for %s by %s", track.name, artist_name)
                return metadata

            else:
                self.logger.debug(
                    "No synchronized lyrics found for %s by %s with album name %s and with a "
                    "duration within 2 secs of %s",
                    track.name,
                    artist_name,
                    album_name,
                    duration,
                )

            plain_lyrics = data.get("plainLyrics")

            if plain_lyrics:
                metadata = MediaItemMetadata()
                metadata.lrc_lyrics = plain_lyrics

                self.logger.debug("Found plain lyrics for %s by %s", track.name, artist_name)
                return metadata
            else:
                self.logger.info(
                    "No lyrics found for %s by %s with album name %s and with a "
                    "duration within 2 secs of %s",
                    track.name,
                    artist_name,
                    album_name,
                    duration,
                )
        return None
