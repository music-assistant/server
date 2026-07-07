"""Allows scrobbling of tracks with the help of PyLast."""

import asyncio
import enum
import logging
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, ClassVar, Final, cast

import pylast
from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
    ProviderConfig,
)
from music_assistant_models.constants import SECURE_STRING_SUBSTITUTE
from music_assistant_models.enums import ConfigEntryType, EventType, MediaType, ProviderFeature
from music_assistant_models.errors import LoginFailed, SetupFailedError

from music_assistant.constants import MASS_LOGGER_NAME
from music_assistant.helpers.app_vars import app_var
from music_assistant.helpers.auth import AuthenticationHelper
from music_assistant.helpers.scrobbler import ScrobblerConfig, ScrobblerHelper
from music_assistant.mass import MusicAssistant
from music_assistant.models import ProviderInstanceType
from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    from music_assistant_models.playback_progress_report import MediaItemPlaybackProgressReport
    from music_assistant_models.provider import ProviderManifest

# Built-in Last.fm API credentials (not available for Libre.fm)
_DEFAULT_API_KEY: str = app_var("lastfm_api_key")
_DEFAULT_API_SECRET: str = app_var("lastfm_api_secret")


# we don't have any special supported features (yet)
# TODO(@anyone): this really should be a frozenset, but that requires
# updating the PluginProvider base class
# as well as other similar classes that also use set[ProviderFeature].
SUPPORTED_FEATURES: Final[set[ProviderFeature]] = set()
SUPPORTED_SCROBBLE_MEDIA_TYPES: Final[frozenset[MediaType]] = frozenset({MediaType.TRACK})

# Configuration keys
CONF_API_KEY: Final[str] = "_api_key"
CONF_API_SECRET: Final[str] = "_api_secret"
CONF_SESSION_KEY: Final[str] = "_api_session_key"
CONF_USERNAME: Final[str] = "_username"
CONF_PROVIDER: Final[str] = "_provider"

# Configuration actions
CONF_ACTION_AUTH: Final[str] = "_auth"


class _NetworkType(enum.Enum):
    """
    Available scrobbling network provider types.

    This is a plain Enum class with string values.
    Use ``.value`` when passing to ``ConfigEntry`` or ``ConfigValueOption``
    which require raw strings.
    """

    LASTFM = "lastfm"
    LIBREFM = "librefm"


def _resolve_credentials(
    values: Mapping[str, ConfigValueType],
    network_type: _NetworkType = _NetworkType.LASTFM,
) -> tuple[str, str]:
    """
    Resolve the effective API key and secret.

    Uses user-provided values if present, otherwise falls back to the
    built-in Last.fm credentials. Libre.fm always requires user-provided values.

    :param values: Config values dict that may contain user-provided key/secret.
    :param network_type: The network provider type.
    :returns: A tuple of (api_key, api_secret) strings.
    :raises SetupFailedError: If credentials cannot be resolved.
    """
    key = cast("str | None", values.get(CONF_API_KEY))
    secret = cast("str | None", values.get(CONF_API_SECRET))

    has_custom_key = bool(key and key != SECURE_STRING_SUBSTITUTE)
    has_custom_secret = bool(secret and secret != SECURE_STRING_SUBSTITUTE)

    if has_custom_key and has_custom_secret:
        return str(key), str(secret)
    if has_custom_key or has_custom_secret:
        err_msg = "Both API Key and Shared Secret are required (only one provided)."
        raise SetupFailedError(err_msg)

    match network_type:
        case _NetworkType.LASTFM:
            return str(_DEFAULT_API_KEY), str(_DEFAULT_API_SECRET)
        case _:
            err_msg = f"API Key and Secret are required for {network_type.value}. "
            raise SetupFailedError(err_msg)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """
    Initialize provider(instance) with given configuration.

    :returns: A configured LastFMScrobbleProvider instance.
    """
    provider = LastFMScrobbleProvider(mass, manifest, config, SUPPORTED_FEATURES)
    pylast.logger.setLevel(provider.logger.level)

    # httpcore is very spammy on debug without providing useful information 99% of the time
    if provider.logger.level == logging.DEBUG:
        logging.getLogger("httpcore").setLevel(logging.INFO)
    else:
        logging.getLogger("httpcore").setLevel(logging.WARNING)

    return provider


class LastFMScrobbleProvider(PluginProvider):
    """Plugin provider to support scrobbling of tracks."""

    _network: pylast._Network | None
    _on_unload: list[Callable[[], None]]

    async def handle_async_init(self) -> None:
        """Handle async setup."""
        self._on_unload: list[Callable[[], None]] = []
        self._network = None

        if not self.config.get_value(CONF_SESSION_KEY):
            self.logger.info("No session key available, don't forget to authenticate!")
            return
        # creating the network instance is (potentially) blocking IO
        # so run it in an executor thread to be safe
        self._network = await asyncio.to_thread(get_network, self._get_network_config())

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await super().loaded_in_mass()

        if self._network is None:
            return

        # subscribe to media_item_played event
        handler = LastFMEventHandler(self._network, self.logger, self.config)
        self._on_unload.append(
            self.mass.subscribe(handler._on_mass_media_item_played, EventType.MEDIA_ITEM_PLAYED)
        )

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        """
        for unload_cb in self._on_unload:
            unload_cb()

    def _get_network_config(self) -> dict[str, ConfigValueType]:
        """
        Build the network configuration dict from provider config values.

        :returns: Dict of config keys to their current stored values.
        """
        return {
            CONF_API_KEY: self.config.get_value(CONF_API_KEY),
            CONF_API_SECRET: self.config.get_value(CONF_API_SECRET),
            CONF_PROVIDER: self.config.get_value(CONF_PROVIDER),
            CONF_USERNAME: self.config.get_value(CONF_USERNAME),
            CONF_SESSION_KEY: self.config.get_value(CONF_SESSION_KEY),
        }


class LastFMEventHandler(ScrobblerHelper):
    """Handle Last.fm event processing for scrobbling and now-playing updates."""

    # pylast wraps every failure — including network errors — in PyLastError.
    scrobble_exceptions: ClassVar[tuple[type[Exception], ...]] = (pylast.PyLastError,)

    def __init__(
        self, network: pylast._Network, logger: logging.Logger, config: ProviderConfig
    ) -> None:
        """Initialize."""
        super().__init__(
            logger,
            ScrobblerConfig.create_from_config(config),
            SUPPORTED_SCROBBLE_MEDIA_TYPES,
        )
        self._network = network

    async def _update_now_playing(self, report: MediaItemPlaybackProgressReport) -> None:
        """Send a now-playing update to Last.fm."""
        # the lastfm client is not async friendly,
        # so we need to run it in a executor thread
        await asyncio.to_thread(
            self._network.update_now_playing,
            report.artist,
            self.get_name(report),
            report.album,
            duration=report.duration,
            mbid=report.mbid,
        )

    async def _scrobble(self, report: MediaItemPlaybackProgressReport) -> None:
        """Scrobble a track to Last.fm."""
        # the listenbrainz client is not async friendly,
        # so we need to run it in a executor thread
        # NOTE: album artist and track number are not available without an extra API call
        # so they won't be scrobbled
        await asyncio.to_thread(
            self._network.scrobble,
            report.artist or "unknown artist",
            self.get_name(report),
            int(time.time()),
            report.album,
            duration=report.duration,
            mbid=report.mbid,
        )


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return config entries to setup this provider.

    :param mass: The MusicAssistant instance.
    :param instance_id: ID of an existing provider instance (None if new instance setup).
    :param action: Optional action key called from config entries UI.
    :param values: The (intermediate) raw values for config entries sent with the action.
    :returns: Tuple of ConfigEntry objects for the frontend to render.
    """
    logger = logging.getLogger(MASS_LOGGER_NAME).getChild("lastfm")

    network_type: _NetworkType
    if values is not None and (provider_val := values.get(CONF_PROVIDER)) is not None:
        network_type = _NetworkType(str(provider_val))
    else:
        network_type = _NetworkType.LASTFM

    entries: list[ConfigEntry] = await ScrobblerConfig.get_shared_config_entries(mass, values)
    entries += [
        ConfigEntry(
            key=CONF_PROVIDER,
            type=ConfigEntryType.STRING,
            required=True,
            options=[
                ConfigValueOption(_NetworkType.LASTFM.value),
                ConfigValueOption(_NetworkType.LIBREFM.value),
            ],
            default_value=network_type.value,
            value=network_type.value,
        ),
        ConfigEntry(
            key=CONF_API_KEY,
            type=ConfigEntryType.SECURE_STRING,
            required=network_type != _NetworkType.LASTFM,
            value=values.get(CONF_API_KEY) if values else None,
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_API_SECRET,
            type=ConfigEntryType.SECURE_STRING,
            required=network_type != _NetworkType.LASTFM,
            value=values.get(CONF_API_SECRET) if values else None,
            advanced=True,
        ),
    ]

    # early return so we can assume values are present
    if values is None:
        return tuple(entries)

    if action == CONF_ACTION_AUTH and values.get("session_id") is not None:
        session_id = str(values.get("session_id"))

        async with AuthenticationHelper(mass, session_id) as auth_helper:
            api_key, api_secret = _resolve_credentials(values, network_type)
            network = get_network({**values, CONF_API_KEY: api_key, CONF_API_SECRET: api_secret})
            skg = pylast.SessionKeyGenerator(network)

            # pylast says it does web auth, but actually does desktop auth
            # so we need to do some URL juggling ourselves
            # to get a proper web auth flow with a callback
            url = (
                f"{network.homepage}/api/auth/"
                f"?api_key={network.api_key}"
                f"&cb={auth_helper.callback_url}"
            )

            logger.info("authenticating on %s", url)
            response = await auth_helper.authenticate(url)
            if response.get("token") is None:
                raise LoginFailed(f"no token available in {network_type.value} response")

            session_key, username = skg.get_web_auth_session_key_username(
                url, str(response.get("token"))
            )
            values[CONF_USERNAME] = username
            values[CONF_SESSION_KEY] = session_key

            entries.append(
                ConfigEntry(
                    key="save_reminder",
                    type=ConfigEntryType.ALERT,
                    required=False,
                    default_value=None,
                    translation_params=[username],
                ),
            )

    if not values.get(CONF_SESSION_KEY):
        entries.append(
            ConfigEntry(
                key=CONF_ACTION_AUTH,
                type=ConfigEntryType.ACTION,
                translation_key="authorize",
                translation_params=[network_type.value],
                action=CONF_ACTION_AUTH,
            ),
        )

    entries += [
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            hidden=True,
            value=values.get(CONF_USERNAME) if values else None,
        ),
        ConfigEntry(
            key=CONF_SESSION_KEY,
            type=ConfigEntryType.SECURE_STRING,
            hidden=True,
            required=False,
            value=values.get(CONF_SESSION_KEY) if values else None,
        ),
    ]

    return tuple(entries)


def get_network(config: dict[str, ConfigValueType]) -> pylast._Network:
    """
    Create a pylast network instance with resolved credentials.

    Called in two contexts:
    1. during the auth flow (from ``get_config_entries``)
       to build the authorization URL before any session exists
    2. during provider startup (from ``handle_async_init``)
       for scrobbling with a stored session.

    Session key and username default to empty strings
    because the auth flow legitimately needs a network without them.

    :param config: Config values dict containing provider type, credentials, etc.
    :returns: A pylast LastFMNetwork or LibreFMNetwork instance.
    :raises SetupFailedError: If the provider is unknown or credentials cannot be resolved.
    """
    network_type = _NetworkType(str(config.get(CONF_PROVIDER, _NetworkType.LASTFM.value)))
    key, secret = _resolve_credentials(config, network_type)
    session_key = str(config.get(CONF_SESSION_KEY) or "")
    username = str(config.get(CONF_USERNAME) or "")

    match network_type:
        case _NetworkType.LASTFM:
            return pylast.LastFMNetwork(key, secret, username=username, session_key=session_key)
        case _NetworkType.LIBREFM:
            return pylast.LibreFMNetwork(key, secret, username=username, session_key=session_key)
