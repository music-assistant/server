"""Audible provider for Music Assistant, utilizing the audible library."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import audible
from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
    ProviderConfig,
)
from music_assistant_models.enums import ConfigEntryType, EventType, MediaType, ProviderFeature
from music_assistant_models.errors import LoginFailed, MediaNotFoundError

from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.audible.api import (
    _AUTH_CACHE,
    AudibleAPI,
    audible_custom_login,
    audible_get_auth_info,
    cached_authenticator_from_file,
    check_file_exists,
    remove_file,
)
from music_assistant.providers.audible.audiobook import AudiobookHelper
from music_assistant.providers.audible.podcast import PodcastHelper

if TYPE_CHECKING:
    from music_assistant_models.media_items import Audiobook, MediaItemType, Podcast, PodcastEpisode
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


CONF_ACTION_AUTH = "authenticate"
CONF_ACTION_VERIFY = "verify_link"
CONF_ACTION_CLEAR_AUTH = "clear_auth"
CONF_AUTH_FILE = "auth_file"
CONF_POST_LOGIN_URL = "post_login_url"
CONF_CODE_VERIFIER = "code_verifier"
CONF_SERIAL = "serial"
CONF_LOGIN_URL = "login_url"
CONF_LOCALE = "locale"

MARKETPLACE_OPTIONS = [
    ConfigValueOption("US and all other countries not listed", "us"),
    ConfigValueOption("Canada", "ca"),
    ConfigValueOption("UK and Ireland", "uk"),
    ConfigValueOption("Australia and New Zealand", "au"),
    ConfigValueOption("France, Belgium, Switzerland", "fr"),
    ConfigValueOption("Germany, Austria, Switzerland", "de"),
    ConfigValueOption("Japan", "jp"),
    ConfigValueOption("Italy", "it"),
    ConfigValueOption("India", "in"),
    ConfigValueOption("Spain", "es"),
    ConfigValueOption("Brazil", "br"),
]

AUTH_REQUIRED_MESSAGE = (
    "You need to authenticate with Audible. Click the authenticate button below"
    "to start the authentication process which will open in a new (popup) window,"
    "so make sure to disable any popup blockers.\n\n"
    "NOTE: \n"
    "After successful login you will get a 'page not found' message - this is expected."
    "Copy the address to the textbox below and press verify."
    "This will register this provider as a virtual device with Audible."
)

AUTH_SUCCESS_MESSAGE = (
    "Successfully authenticated with Audible.\nNote: Changing marketplace needs new authorization"
)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return Audibleprovider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    Args:
        mass: The MusicAssistant instance
        instance_id: id of an existing provider instance (None if new instance setup)
        action: [optional] action key called from config entries UI
        values: the (intermediate) raw values for config entries sent with the action

    Returns:
        Tuple of ConfigEntry objects for the provider setup
    """
    if values is None:
        values = {}

    locale = cast(str, values.get(CONF_LOCALE, "") or "us")
    auth_file = cast(str, values.get(CONF_AUTH_FILE))
    auth_required = await _check_auth_required(auth_file)
    label_text = AUTH_REQUIRED_MESSAGE if auth_required else AUTH_SUCCESS_MESSAGE
    if action == CONF_ACTION_AUTH:
        await _handle_auth_action(mass, values, auth_file, locale)

    if action == CONF_ACTION_VERIFY:
        await _handle_verify_action(mass, values, locale)
        auth_required = False

    return _create_config_entries(label_text, locale, auth_required, values)


async def _check_auth_required(auth_file: str | None) -> bool:
    """Check if authentication is required.

    Args:
        auth_file: Path to the authentication file

    Returns:
        True if authentication is required, False otherwise
    """
    if not auth_file or not await check_file_exists(auth_file):
        return True

    try:
        await cached_authenticator_from_file(auth_file)
        return False
    except Exception:
        return True


async def _handle_auth_action(
    mass: MusicAssistant, values: dict[str, ConfigValueType], auth_file: str | None, locale: str
) -> None:
    """Handle the authentication action.

    Args:
        mass: The MusicAssistant instance
        values: The config values dictionary
        auth_file: Path to the authentication file
        locale: The locale string
    """
    if auth_file and await check_file_exists(auth_file):
        await remove_file(auth_file)
        values[CONF_AUTH_FILE] = None

    code_verifier, login_url, serial = await audible_get_auth_info(locale)

    values[CONF_CODE_VERIFIER] = code_verifier
    values[CONF_SERIAL] = serial
    values[CONF_LOGIN_URL] = login_url

    session_id = str(values["session_id"])
    mass.signal_event(EventType.AUTH_SESSION, session_id, login_url)

    await asyncio.sleep(15)


async def _handle_verify_action(
    mass: MusicAssistant, values: dict[str, ConfigValueType], locale: str
) -> None:
    """Handle the verification action.

    Args:
        mass: The MusicAssistant instance
        values: The config values dictionary
        locale: The locale string
    """
    code_verifier = str(values.get(CONF_CODE_VERIFIER))
    serial = str(values.get(CONF_SERIAL))
    post_login_url = str(values.get(CONF_POST_LOGIN_URL))

    auth = await audible_custom_login(code_verifier, post_login_url, serial, locale)

    storage_path = mass.storage_path
    auth_file_path = os.path.join(storage_path, f"audible_auth_{uuid4().hex}.json")
    await asyncio.to_thread(auth.to_file, auth_file_path)

    values[CONF_AUTH_FILE] = auth_file_path


def _create_config_entries(
    label_text: str,
    locale: str,
    auth_required: bool,
    values: dict[str, ConfigValueType],
) -> tuple[ConfigEntry, ...]:
    """Create config entries for the provider setup.

    Args:
        label_text: The label text to display
        locale: The locale string
        auth_required: Whether authentication is required
        values: The config values dictionary

    Returns:
        Tuple of ConfigEntry objects
    """
    return (
        ConfigEntry(
            key="label_text",
            type=ConfigEntryType.LABEL,
            label=label_text,
        ),
        ConfigEntry(
            key=CONF_LOCALE,
            type=ConfigEntryType.STRING,
            label="Marketplace",
            hidden=not auth_required,
            required=True,
            value=locale,
            options=MARKETPLACE_OPTIONS,
            default_value="us",
        ),
        ConfigEntry(
            key=CONF_ACTION_AUTH,
            type=ConfigEntryType.ACTION,
            label="(Re)Authenticate with Audible",
            description="This button will redirect you to Audible to authenticate.",
            action=CONF_ACTION_AUTH,
        ),
        ConfigEntry(
            key=CONF_POST_LOGIN_URL,
            type=ConfigEntryType.STRING,
            label="Post Login Url",
            required=False,
            value=cast(str | None, values.get(CONF_POST_LOGIN_URL)),
            hidden=not auth_required,
        ),
        ConfigEntry(
            key=CONF_ACTION_VERIFY,
            type=ConfigEntryType.ACTION,
            label="Verify Audible URL",
            description="This button will check the url and register this provider.",
            action=CONF_ACTION_VERIFY,
            hidden=not auth_required,
        ),
        ConfigEntry(
            key=CONF_CODE_VERIFIER,
            type=ConfigEntryType.STRING,
            label="Code Verifier",
            hidden=True,
            required=False,
            value=cast(str | None, values.get(CONF_CODE_VERIFIER)),
        ),
        ConfigEntry(
            key=CONF_SERIAL,
            type=ConfigEntryType.STRING,
            label="Serial",
            hidden=True,
            required=False,
            value=cast(str | None, values.get(CONF_SERIAL)),
        ),
        ConfigEntry(
            key=CONF_LOGIN_URL,
            type=ConfigEntryType.STRING,
            label="Login Url",
            hidden=True,
            required=False,
            value=cast(str | None, values.get(CONF_LOGIN_URL)),
        ),
        ConfigEntry(
            key=CONF_AUTH_FILE,
            type=ConfigEntryType.STRING,
            label="Authentication File",
            hidden=True,
            required=True,
            value=cast(str | None, values.get(CONF_AUTH_FILE)),
        ),
    )


class Audibleprovider(MusicProvider):
    """Implementation of a Audible Audiobook Provider."""

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize the Audible Audiobook Provider."""
        super().__init__(mass, manifest, config)
        self.locale = cast(str, self.config.get_value(CONF_LOCALE) or "us")
        self.auth_file = cast(str, self.config.get_value(CONF_AUTH_FILE))
        self._client: audible.AsyncClient
        self.api: AudibleAPI
        self.audiobook_helper: AudiobookHelper
        self.podcast_helper: PodcastHelper

        audible.log_helper.set_level("INFO")

    async def handle_async_init(self) -> None:
        """Handle asynchronous initialization of the provider."""
        await self._login()

    async def _login(self) -> None:
        """Authenticate with Audible using the saved authentication file.

        Raises:
            LoginFailed: If authentication fails
        """
        try:
            self.logger.debug("Authenticating with Audible")

            auth = _AUTH_CACHE.get(self.instance_id)
            if auth is None:
                self.logger.debug("Loading authenticator from file: %s", self.auth_file)
                auth = await cached_authenticator_from_file(self.auth_file)
                _AUTH_CACHE[self.instance_id] = auth
                self.logger.debug("Authenticator loaded and cached")
            else:
                self.logger.debug("Using cached authenticator")

            if auth.access_token_expired:
                self.logger.debug("Access token expired, refreshing")
                await asyncio.to_thread(auth.refresh_access_token)
                await asyncio.to_thread(auth.to_file, self.auth_file)
                _AUTH_CACHE[self.instance_id] = auth
                self.logger.debug("Access token refreshed")

            self._client = audible.AsyncClient(auth)
            self.logger.debug("Audible client created")

            self.api = AudibleAPI(
                client=self._client,
                mass=self.mass,
                provider_instance=self.instance_id,
                provider_domain=self.domain,
                logger=self.logger,
            )
            self.logger.debug("API helper created")

            self.audiobook_helper = AudiobookHelper(
                api=self.api,
                mass=self.mass,
                provider_instance=self.instance_id,
                provider_domain=self.domain,
                logger=self.logger,
            )
            self.logger.debug("Audiobook helper created")

            self.podcast_helper = PodcastHelper(
                api=self.api,
                mass=self.mass,
                provider_instance=self.instance_id,
                provider_domain=self.domain,
                logger=self.logger,
            )
            self.logger.debug("Podcast helper created")

            self.logger.info("Successfully authenticated with Audible")

        except Exception as exc:
            self.logger.error("Failed to authenticate with Audible: %s", exc)
            raise LoginFailed(f"Failed to authenticate with Audible: {exc}") from exc

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return {
            ProviderFeature.BROWSE,
            ProviderFeature.LIBRARY_AUDIOBOOKS,
            ProviderFeature.LIBRARY_PODCASTS,
        }

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return True

    async def get_library_audiobooks(self) -> AsyncGenerator[Audiobook, None]:
        """Get all audiobooks from the user's Audible library.

        Yields:
            Audiobook objects from the user's library
        """
        self.logger.debug("Fetching audiobooks from library")
        try:
            async for audiobook in self.audiobook_helper.get_library():
                yield audiobook
        except Exception as exc:
            self.logger.error("Error fetching audiobooks from library: %s", exc)
            raise

    async def get_audiobook(self, prov_audiobook_id: str) -> Audiobook:
        """Get full audiobook details by id.

        Args:
            prov_audiobook_id: The Audible ASIN for the audiobook

        Returns:
            Audiobook object with full details

        Raises:
            MediaNotFoundError: If the audiobook cannot be found
        """
        self.logger.debug("Fetching audiobook details for ID: %s", prov_audiobook_id)
        try:
            return await self.audiobook_helper.get_audiobook(
                asin=prov_audiobook_id, use_cache=False
            )
        except Exception as exc:
            self.logger.error("Error fetching audiobook %s: %s", prov_audiobook_id, exc)
            if not isinstance(exc, MediaNotFoundError):
                raise MediaNotFoundError(f"Audiobook not found: {prov_audiobook_id}") from exc
            raise

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast, None]:
        """Get all podcasts from the user's Audible library.

        Yields:
            Podcast objects from the user's library
        """
        self.logger.debug("Fetching podcasts from library")
        try:
            async for podcast in self.podcast_helper.get_podcasts():
                yield podcast
        except Exception as exc:
            self.logger.error("Error fetching podcasts from library: %s", exc)
            raise

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get full podcast details by id.

        Args:
            prov_podcast_id: The Audible ASIN for the podcast

        Returns:
            Podcast object with full details

        Raises:
            Exception: If there's an error fetching the podcast
        """
        try:
            return await self.podcast_helper.get_podcast(prov_podcast_id)
        except Exception as exc:
            self.logger.error("Error fetching podcasts from library: %s", exc)
            raise

    async def get_podcast_episodes(
        self, prov_podcast_id: str
    ) -> AsyncGenerator[PodcastEpisode, None]:
        """Get episodes for a podcast.

        Args:
            prov_podcast_id: The Audible ASIN for the podcast

        Yields:
            PodcastEpisode objects for the specified podcast
        """
        self.logger.debug("Fetching episodes for podcast ID: %s", prov_podcast_id)
        try:
            async for episode in self.podcast_helper.get_podcast_episodes(
                podcast_asin=prov_podcast_id
            ):
                yield episode
        except Exception as exc:
            self.logger.error("Error fetching episodes for podcast %s: %s", prov_podcast_id, exc)
            raise

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get single podcast episode by id.

        Args:
            prov_episode_id: The Audible episode ID (format: podcast_asin:episode_asin)

        Returns:
            PodcastEpisode object

        Raises:
            MediaNotFoundError: If the episode cannot be found
        """
        self.logger.debug("Fetching podcast episode: %s", prov_episode_id)
        try:
            return await self.podcast_helper.get_podcast_episode(episode_id=prov_episode_id)
        except Exception as exc:
            self.logger.error("Error fetching podcast episode %s: %s", prov_episode_id, exc)
            if not isinstance(exc, MediaNotFoundError):
                raise MediaNotFoundError(f"Podcast episode not found: {prov_episode_id}") from exc
            raise

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get stream details for an audiobook or podcast episode.

        Args:
            item_id: The Audible ID for the item
            media_type: The type of media (AUDIOBOOK or PODCAST_EPISODE)

        Returns:
            StreamDetails object with streaming information

        Raises:
            MediaNotFoundError: If the stream details cannot be retrieved
        """
        self.logger.debug("Getting stream details for %s (type: %s)", item_id, media_type)
        try:
            if media_type == MediaType.PODCAST_EPISODE:
                return await self.podcast_helper.get_stream(item_id)
            return await self.audiobook_helper.get_stream(asin=item_id)
        except ValueError as exc:
            self.logger.error("Failed to get stream details for %s: %s", item_id, exc)
            raise MediaNotFoundError(f"Failed to get stream details for {item_id}") from exc
        except Exception as exc:
            self.logger.error("Unexpected error getting stream details for %s: %s", item_id, exc)
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
        if media_type == MediaType.PODCAST_EPISODE:
            await self.podcast_helper.set_last_position(prov_item_id, position)
        else:
            await self.audiobook_helper.set_last_position(prov_item_id, position)

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        is_removed will be set to True when the provider is removed from the configuration.
        """
        if is_removed:
            await self.api.deregister()
