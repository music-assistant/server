"""Audible provider for Music Assistant, utilizing the audible library."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Sequence
from contextlib import suppress
from datetime import datetime
from logging import getLevelName
from typing import TYPE_CHECKING, cast
from urllib.parse import quote, unquote

import audible
from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import LoginFailed, MediaNotFoundError
from music_assistant_models.media_items import BrowseFolder, ItemMapping

from music_assistant.constants import CONF_ENTRY_UNOFFICIAL_PROVIDER
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.audible.audible_helper import (
    AudibleHelper,
    cached_authenticator_from_file,
    evict_cached_authenticator,
    refresh_access_token_compat,
    remove_file,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import (
        ConfigEntry,
        ProviderConfig,
    )
    from music_assistant_models.media_items import (
        Audiobook,
        MediaItemType,
        Podcast,
        PodcastEpisode,
    )
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


# Config keys collected by the setup flow and read back at runtime
CONF_AUTH_FILE = "auth_file"
CONF_LOCALE = "locale"

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.LIBRARY_AUDIOBOOKS,
    ProviderFeature.LIBRARY_PODCASTS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return Audibleprovider(mass, manifest, config, SUPPORTED_FEATURES)


class Audibleprovider(MusicProvider):
    """Implementation of a Audible Audiobook Provider."""

    locale: str
    auth_file: str
    _client: audible.AsyncClient | None = None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """
        Return the config entries for the Audible provider.

        Authentication (Amazon sign-in on their own page + device registration) runs in the
        interactive setup flow (see ``setup_flow.py``); this provider has no further options.
        """
        return (CONF_ENTRY_UNOFFICIAL_PROVIDER,)

    async def handle_async_init(self) -> None:
        """Handle asynchronous initialization of the provider."""
        self.locale = cast("str", self.get_setup_value(CONF_LOCALE) or "us")
        self.auth_file = cast("str", self.get_setup_value(CONF_AUTH_FILE))
        self._client: audible.AsyncClient | None = None
        audible.log_helper.set_level(getLevelName(self.logger.level))
        await self._login()

    async def _login(self) -> None:
        """Authenticate with Audible using the saved authentication file."""
        try:
            # the cache is keyed on the auth file path, so a reconfigure (which writes
            # a new auth file) never reuses the previous registration's authenticator
            auth = await cached_authenticator_from_file(self.auth_file, self.locale)

            # Check if we have signing auth (preferred, stable - not affected by API changes)
            has_signing_auth = auth.adp_token and auth.device_private_key
            if has_signing_auth:
                self.logger.debug("Using signing auth (stable RSA-signed requests)")
            else:
                self.logger.debug("Signing auth not available, using bearer auth")

            # Handle token refresh if needed
            if auth.access_token_expired:
                self.logger.debug("Access token expired, refreshing")
                try:
                    # Use compatible refresh that handles new API token format
                    if auth.refresh_token and auth.locale:
                        refresh_data = await refresh_access_token_compat(
                            refresh_token=auth.refresh_token,
                            domain=auth.locale.domain,
                            http_session=self.mass.http_session,
                            with_username=auth.with_username or False,
                        )
                        auth._update_attrs(**refresh_data)
                        await asyncio.to_thread(auth.to_file, self.auth_file)
                        self.logger.debug("Token refreshed successfully")
                    else:
                        self.logger.warning("Cannot refresh: missing refresh_token or locale")
                except Exception as refresh_error:
                    self.logger.warning(f"Token refresh failed: {refresh_error}")
                    if not has_signing_auth:
                        # Only fail if we don't have signing auth as fallback
                        raise LoginFailed(
                            "Token refresh failed and signing auth not available. "
                            "Please re-authenticate with Audible."
                        ) from refresh_error
                    # Continue with signing auth

            self._client = audible.AsyncClient(auth)

            self.helper = AudibleHelper(
                mass=self.mass,
                client=self._client,
                provider_instance=self.instance_id,
                provider_domain=self.domain,
                provider=self,
                logger=self.logger,
            )

            self.logger.info("Successfully authenticated with Audible.")

        except LoginFailed:
            raise
        except Exception as e:
            self.logger.error(f"Failed to authenticate with Audible: {e}")
            raise LoginFailed(f"Failed to authenticate with Audible: {e}") from e

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return True

    async def get_library_audiobooks(self) -> AsyncGenerator[Audiobook]:
        """Get all audiobooks from the library."""
        async for audiobook in self.helper.get_library():
            yield audiobook

    async def get_audiobook(self, prov_audiobook_id: str) -> Audiobook:
        """Get full audiobook details by id."""
        return await self.helper.get_audiobook(asin=prov_audiobook_id, use_cache=False)

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse this provider's items.

        :param path: The path to browse, (e.g. provider_id://authors).
        """
        item_path = path.split("://", 1)[1] if "://" in path else ""
        parts = item_path.split("/") if item_path else []

        # Root - return main folders
        if not item_path:
            return self._browse_root(path)

        # Authors listing
        if parts[0] == "authors":
            if len(parts) == 1:
                return await self._browse_authors(path)
            # Specific author's books
            return await self._browse_author_books(unquote(parts[1]))

        # Series listing
        if parts[0] == "series":
            if len(parts) == 1:
                return await self._browse_series(path)
            # Specific series' books
            return await self._browse_series_books(unquote(parts[1]))

        # Narrators listing
        if parts[0] == "narrators":
            if len(parts) == 1:
                return await self._browse_narrators(path)
            return await self._browse_narrator_books(unquote(parts[1]))

        # Genres listing
        if parts[0] == "genres":
            if len(parts) == 1:
                return await self._browse_genres(path)
            return await self._browse_genre_books(unquote(parts[1]))

        # Publishers listing
        if parts[0] == "publishers":
            if len(parts) == 1:
                return await self._browse_publishers(path)
            return await self._browse_publisher_books(unquote(parts[1]))

        # Fall back to base implementation for audiobooks/podcasts
        return await super().browse(path)

    def _browse_root(self, base_path: str) -> list[BrowseFolder]:
        """Return root browse folders."""
        return [
            BrowseFolder(
                item_id="audiobooks",
                provider=self.instance_id,
                path=f"{base_path}audiobooks",
                name="Audiobooks",
                translation_key="audiobooks",
            ),
            BrowseFolder(
                item_id="podcasts",
                provider=self.instance_id,
                path=f"{base_path}podcasts",
                name="Podcasts",
                translation_key="podcasts",
            ),
            BrowseFolder(
                item_id="authors",
                provider=self.instance_id,
                path=f"{base_path}authors",
                name="Authors",
                translation_key="authors",
            ),
            BrowseFolder(
                item_id="series",
                provider=self.instance_id,
                path=f"{base_path}series",
                name="Series",
                translation_key="series",
            ),
            BrowseFolder(
                item_id="narrators",
                provider=self.instance_id,
                path=f"{base_path}narrators",
                name="Narrators",
                translation_key="narrators",
            ),
            BrowseFolder(
                item_id="genres",
                provider=self.instance_id,
                path=f"{base_path}genres",
                name="Genres",
                translation_key="genres",
            ),
            BrowseFolder(
                item_id="publishers",
                provider=self.instance_id,
                path=f"{base_path}publishers",
                name="Publishers",
                translation_key="publishers",
            ),
        ]

    async def _browse_authors(self, base_path: str) -> list[BrowseFolder]:
        """Return list of all authors."""
        authors = await self.helper.get_authors()
        return [
            BrowseFolder(
                item_id=asin,
                provider=self.instance_id,
                path=f"{base_path}/{quote(asin)}",
                name=name,
            )
            for asin, name in sorted(authors.items(), key=lambda x: x[1])
        ]

    async def _browse_author_books(self, author_asin: str) -> list[Audiobook]:
        """Return audiobooks by a specific author."""
        return await self.helper.get_audiobooks_by_author(author_asin)

    async def _browse_series(self, base_path: str) -> list[BrowseFolder]:
        """Return list of all series."""
        series = await self.helper.get_series()
        return [
            BrowseFolder(
                item_id=asin,
                provider=self.instance_id,
                path=f"{base_path}/{quote(asin)}",
                name=title,
            )
            for asin, title in sorted(series.items(), key=lambda x: x[1])
        ]

    async def _browse_series_books(self, series_asin: str) -> list[Audiobook]:
        """Return audiobooks in a specific series."""
        return await self.helper.get_audiobooks_by_series(series_asin)

    async def _browse_narrators(self, base_path: str) -> list[BrowseFolder]:
        """Return list of all narrators."""
        narrators = await self.helper.get_narrators()
        return [
            BrowseFolder(
                item_id=asin,
                provider=self.instance_id,
                path=f"{base_path}/{quote(asin)}",
                name=name,
            )
            for asin, name in sorted(narrators.items(), key=lambda x: x[1])
        ]

    async def _browse_narrator_books(self, narrator_asin: str) -> list[Audiobook]:
        """Return audiobooks by a specific narrator."""
        return await self.helper.get_audiobooks_by_narrator(narrator_asin)

    async def _browse_genres(self, base_path: str) -> list[BrowseFolder]:
        """Return list of all genres."""
        genres = await self.helper.get_genres()
        return [
            BrowseFolder(
                item_id=genre,
                provider=self.instance_id,
                path=f"{base_path}/{quote(genre)}",
                name=genre,
            )
            for genre in sorted(genres)
        ]

    async def _browse_genre_books(self, genre: str) -> list[Audiobook]:
        """Return audiobooks matching a genre."""
        return await self.helper.get_audiobooks_by_genre(genre)

    async def _browse_publishers(self, base_path: str) -> list[BrowseFolder]:
        """Return list of all publishers."""
        publishers = await self.helper.get_publishers()
        return [
            BrowseFolder(
                item_id=publisher,
                provider=self.instance_id,
                path=f"{base_path}/{quote(publisher)}",
                name=publisher,
            )
            for publisher in sorted(publishers)
        ]

    async def _browse_publisher_books(self, publisher: str) -> list[Audiobook]:
        """Return audiobooks from a specific publisher."""
        return await self.helper.get_audiobooks_by_publisher(publisher)

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast]:
        """Get all podcasts from the library."""
        async for podcast in self.helper.get_library_podcasts():
            yield podcast

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get full podcast details by id."""
        return await self.helper.get_podcast(asin=prov_podcast_id)

    async def get_podcast_episodes(self, prov_podcast_id: str) -> AsyncGenerator[PodcastEpisode]:
        """Get all episodes for a podcast."""
        async for episode in self.helper.get_podcast_episodes(prov_podcast_id):
            yield episode

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get full podcast episode details by id."""
        return await self.helper.get_podcast_episode(prov_episode_id)

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Get stream details for an audiobook or podcast episode.

        :param item_id: The ASIN of the audiobook or podcast episode.
        :param media_type: The type of media (audiobook or podcast episode).
        """
        try:
            return await self.helper.get_stream(asin=item_id, media_type=media_type)
        except ValueError as exc:
            raise MediaNotFoundError(f"Failed to get stream details for {item_id}") from exc

    async def on_played(
        self,
        media_type: MediaType,
        prov_item_id: str,
        fully_played: bool,
        position: int,
        media_item: MediaItemType,
        is_playing: bool = False,
    ) -> None:
        """
        Handle callback when a (playable) media item has been played.

        This is called by the Queue controller when;
            - a track has been fully played
            - a track has been stopped (or skipped) after being played
            - every 30s when a track is playing

        Fully played is True when the track has been played to the end.

        Position is the last known position of the track in seconds, to sync resume state.
        When fully_played is set to false and position is 0,
        the user marked the item as unplayed in the UI.

        is_playing is True when the track is currently playing.

        media_item is the full media item details of the played/playing track.
        """
        await self.helper.set_last_position(prov_item_id, position, media_type)

    async def get_resume_position(
        self, item_id: str, media_type: MediaType
    ) -> tuple[bool, int, datetime | None]:
        """
        Return the resume position from Audible for the given item.

        :param item_id: The provider item ID (ASIN) of the audiobook.
        :param media_type: The media type of the item.
        """
        if media_type != MediaType.AUDIOBOOK:
            raise NotImplementedError
        return await self.helper.get_audible_resume_position(item_id)

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        is_removed will be set to True when the provider is removed from the configuration.
        """
        if is_removed:
            try:
                await self.helper.deregister()
            finally:
                evict_cached_authenticator(self.auth_file)
                with suppress(OSError):
                    await remove_file(self.auth_file)
