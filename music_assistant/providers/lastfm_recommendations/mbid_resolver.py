"""MBID to ISRC resolver using MusicBrainz provider."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from aiohttp import ClientError
from music_assistant_models.errors import InvalidDataError, ProviderUnavailableError

if TYPE_CHECKING:
    from music_assistant.providers.lastfm_recommendations import LastFMRecommendationsProvider
    from music_assistant.providers.musicbrainz import MusicbrainzProvider


CACHE_CATEGORY_MBID_ISRC = 0  # Cache category for MBID->ISRC mappings


class MBIDResolver:
    """Resolves MusicBrainz recording IDs to ISRCs."""

    # 90 days: ISRC mappings rarely change, and this sits on top of MusicBrainz's own 30-day cache.
    CACHE_EXPIRATION = 86400 * 90

    def __init__(self, provider: LastFMRecommendationsProvider) -> None:
        """Initialize MBID resolver.

        :param provider: The Last.fm recommendations provider instance.
        """
        self.provider = provider
        self.mass = provider.mass
        self.logger = provider.logger

    async def get_isrcs_for_recording(self, mbid: str) -> list[str]:
        """Get ISRCs for a recording MBID via MusicBrainz.

        :param mbid: MusicBrainz recording ID.
        """
        cache_key = f"recording_{mbid}"

        cached = await self.mass.cache.get(
            key=cache_key,
            category=CACHE_CATEGORY_MBID_ISRC,
        )

        if cached is not None:
            return cast("list[str]", cached.get("isrcs", []))

        mb_provider = self.mass.get_provider("musicbrainz")
        if not mb_provider:
            msg = "MusicBrainz provider not available"
            raise ProviderUnavailableError(msg)

        try:
            recording = await cast("MusicbrainzProvider", mb_provider).get_recording_details(mbid)

            isrcs = recording.isrcs if recording and recording.isrcs else []

            # Cache empty results too, to avoid repeated failed lookups.
            await self.mass.cache.set(
                key=cache_key,
                data={"isrcs": isrcs},
                category=CACHE_CATEGORY_MBID_ISRC,
                expiration=self.CACHE_EXPIRATION,
            )

            return isrcs

        except (TimeoutError, ClientError, AttributeError, InvalidDataError) as err:
            self.logger.debug("Failed to get ISRCs for MBID %s: %s", mbid, type(err).__name__)

            await self.mass.cache.set(
                key=cache_key,
                data={"isrcs": []},
                category=CACHE_CATEGORY_MBID_ISRC,
                expiration=self.CACHE_EXPIRATION,
            )

            return []
