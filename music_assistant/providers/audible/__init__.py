"""Audible provider for Music Assistant, utilizing the audible library."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator
from logging import getLevelName
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
from music_assistant.providers.audible.audible_helper import (
    AudibleHelper,
    audible_custom_login,
    audible_get_auth_info,
    cached_authenticator_from_file,
    check_file_exists,
    remove_file,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import Audiobook, MediaItemType
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


# Constants for config actions
CONF_ACTION_AUTH = "authenticate"
CONF_ACTION_VERIFY = "verify_link"
CONF_ACTION_CLEAR_AUTH = "clear_auth"
CONF_AUTH_FILE = "auth_file"
CONF_POST_LOGIN_URL = "post_login_url"
CONF_CODE_VERIFIER = "code_verifier"
CONF_SERIAL = "serial"
CONF_LOGIN_URL = "login_url"
CONF_LOCALE = "locale"


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

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    if values is None:
        values = {}

    locale = cast("str", values.get("locale", "") or "us")
    auth_file = cast("str", values.get(CONF_AUTH_FILE))

    auth_required = True
    if auth_file and await check_file_exists(auth_file):
        try:
            auth = await cached_authenticator_from_file(auth_file)
            auth_required = False
        except Exception:
            auth_required = True
    label_text = ""
    if auth_required:
        label_text = (
            "You need to authenticate with Audible. Click the authenticate button below"
            "to start the authentication process which will open in a new (popup) window,"
            "so make sure to disable any popup blockers.\n\n"
            "NOTE: \n"
            "After successful login you will get a 'page not found' message - this is expected."
            "Copy the address to the textbox below and press verify."
            "This will register this provider as a virtual device with Audible."
        )
    else:
        label_text = (
            "Successfully authenticated with Audible."
            "\nNote: Changing marketplace needs new authorization"
        )

    if action == CONF_ACTION_AUTH:
        if auth_file and await check_file_exists(auth_file):
            await remove_file(auth_file)
            values[CONF_AUTH_FILE] = None
            auth_file = ""

        code_verifier, login_url, serial = await audible_get_auth_info(locale)
        values[CONF_CODE_VERIFIER] = code_verifier
        values[CONF_SERIAL] = serial
        values[CONF_LOGIN_URL] = login_url
        session_id = str(values["session_id"])
        mass.signal_event(EventType.AUTH_SESSION, session_id, login_url)
        await asyncio.sleep(15)

    if action == CONF_ACTION_VERIFY:
        code_verifier = str(values.get(CONF_CODE_VERIFIER))
        serial = str(values.get(CONF_SERIAL))
        post_login_url = str(values.get(CONF_POST_LOGIN_URL))
        storage_path = mass.storage_path

        auth = await audible_custom_login(code_verifier, post_login_url, serial, locale)
        auth_file_path = os.path.join(storage_path, f"audible_auth_{uuid4().hex}.json")
        await asyncio.to_thread(auth.to_file, auth_file_path)
        values[CONF_AUTH_FILE] = auth_file_path
        auth_required = False

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
            options=[
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
            ],
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
            value=cast("str | None", values.get(CONF_POST_LOGIN_URL)),
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
            value=cast("str | None", values.get(CONF_CODE_VERIFIER)),
        ),
        ConfigEntry(
            key=CONF_SERIAL,
            type=ConfigEntryType.STRING,
            label="Serial",
            hidden=True,
            required=False,
            value=cast("str | None", values.get(CONF_SERIAL)),
        ),
        ConfigEntry(
            key=CONF_LOGIN_URL,
            type=ConfigEntryType.STRING,
            label="Login Url",
            hidden=True,
            required=False,
            value=cast("str | None", values.get(CONF_LOGIN_URL)),
        ),
        ConfigEntry(
            key=CONF_AUTH_FILE,
            type=ConfigEntryType.STRING,
            label="Authentication File",
            hidden=True,
            required=True,
            value=cast("str | None", values.get(CONF_AUTH_FILE)),
        ),
    )


class Audibleprovider(MusicProvider):
    """Implementation of a Audible Audiobook Provider."""

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize the Audible Audiobook Provider."""
        super().__init__(mass, manifest, config)
        self.locale = cast("str", self.config.get_value(CONF_LOCALE) or "us")
        self.auth_file = cast("str", self.config.get_value(CONF_AUTH_FILE))
        self._client: audible.AsyncClient | None = None
        audible.log_helper.set_level(getLevelName(self.logger.level))

    async def handle_async_init(self) -> None:
        """Handle asynchronous initialization of the provider."""
        await self._login()

    # Cache for authenticators to avoid repeated file I/O
    _AUTH_CACHE: dict[str, audible.Authenticator] = {}

    async def _login(self) -> None:
        """Authenticate with Audible using the saved authentication file."""
        try:
            auth = self._AUTH_CACHE.get(self.instance_id)

            if auth is None:
                self.logger.debug("Loading authenticator from file")
                auth = await cached_authenticator_from_file(self.auth_file)
                self._AUTH_CACHE[self.instance_id] = auth
            else:
                self.logger.debug("Using cached authenticator")

            if auth.access_token_expired:
                self.logger.debug("Access token expired, refreshing")
                await asyncio.to_thread(auth.refresh_access_token)
                await asyncio.to_thread(auth.to_file, self.auth_file)
                self._AUTH_CACHE[self.instance_id] = auth

            self._client = audible.AsyncClient(auth)

            self.helper = AudibleHelper(
                mass=self.mass,
                client=self._client,
                provider_instance=self.instance_id,
                provider_domain=self.domain,
                logger=self.logger,
            )

            self.logger.info("Successfully authenticated with Audible.")

        except Exception as e:
            self.logger.error(f"Failed to authenticate with Audible: {e}")
            raise LoginFailed("Failed to authenticate with Audible.")

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return {ProviderFeature.BROWSE, ProviderFeature.LIBRARY_AUDIOBOOKS}

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return True

    async def get_library_audiobooks(self) -> AsyncGenerator[Audiobook, None]:
        """Get all audiobooks from the library."""
        async for audiobook in self.helper.get_library():
            yield audiobook

    async def get_audiobook(self, prov_audiobook_id: str) -> Audiobook:
        """Get full audiobook details by id."""
        return await self.helper.get_audiobook(asin=prov_audiobook_id, use_cache=False)

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a audiobook based of asin."""
        try:
            return await self.helper.get_stream(asin=item_id)
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
        await self.helper.set_last_position(prov_item_id, position)

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        is_removed will be set to True when the provider is removed from the configuration.
        """
        if is_removed:
            await self.helper.deregister()
