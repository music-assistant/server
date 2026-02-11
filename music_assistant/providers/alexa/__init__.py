"""Alexa player provider support for Music Assistant."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING, Any, cast

import aiohttp
from aiohttp import BasicAuth, web
from alexapy import AlexaAPI, AlexaLogin, AlexaProxy
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    PlaybackState,
    PlayerFeature,
    ProviderFeature,
)
from music_assistant_models.errors import ActionUnavailable, LoginFailed
from music_assistant_models.player import DeviceInfo, PlayerMedia

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.helpers.auth import AuthenticationHelper
from music_assistant.models.player import Player
from music_assistant.models.player_provider import PlayerProvider

_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CONF_URL = "url"
CONF_ACTION_AUTH = "auth"
CONF_AUTH_SECRET = "secret"
CONF_API_BASIC_AUTH_USERNAME = "api_username"
CONF_API_BASIC_AUTH_PASSWORD = "api_password"
CONF_API_URL = "api_url"
CONF_ALEXA_LANGUAGE = "alexa_language"

ALEXA_LANGUAGE_COMMANDS = {
    "play_audio_de-DE": "sag music assistant spiele audio",
    "play_audio_en-US": "ask music assistant to play audio",
    "play_audio_es-ES": "pídele a music assistant que reproduzca audio",
    "play_audio_fr-FR": "music assistant",
    "play_audio_it-IT": "chiedi a music assistant di riprodurre audio",
    "play_audio_default": "ask music assistant to play audio",
}

SUPPORTED_FEATURES: set[ProviderFeature] = set()  # no special features supported (yet)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return AlexaProvider(mass, manifest, config, SUPPORTED_FEATURES)


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
    # config flow auth action/step (authenticate button clicked)
    if action == CONF_ACTION_AUTH and values:
        async with AuthenticationHelper(mass, str(values["session_id"])) as auth_helper:
            login = AlexaLogin(
                url=str(values[CONF_URL]),
                email=str(values[CONF_USERNAME]),
                password=str(values[CONF_PASSWORD]),
                otp_secret=str(values.get(CONF_AUTH_SECRET, "")),
                outputpath=lambda x: x,
            )

            # --- Proxy authentication logic using AlexaProxy ---
            # Build the proxy path and URL
            proxy_path = "/alexa/auth/proxy/"
            post_path = "/alexa/auth/proxy/ap/signin/*"
            base_url = mass.webserver.base_url.rstrip("/")
            proxy_url = f"{base_url}{proxy_path}"

            # Create AlexaProxy instance
            proxy = AlexaProxy(login, proxy_url)

            # Handler that delegates to AlexaProxy's all_handler
            async def proxy_handler(request: web.Request) -> Any:
                response = await proxy.all_handler(request)
                if "Successfully logged in" in getattr(response, "text", ""):
                    # Notify the callback URL
                    async with aiohttp.ClientSession() as session:
                        await session.get(auth_helper.callback_url)
                        _LOGGER.info("Alexa Callback URL: %s", auth_helper.callback_url)
                    return web.Response(
                        text="""
                        <html>
                            <body>
                                <h2>Login successful!</h2>
                                <p>You may now close this window.</p>
                            </body>
                        </html>
                        """,
                        content_type="text/html",
                    )
                return response

            # Register GET for the base proxy path
            mass.webserver.register_dynamic_route(proxy_path, proxy_handler, "GET")
            # Register POST for the specific signin helper path
            mass.webserver.register_dynamic_route(post_path, proxy_handler, "POST")

            try:
                await auth_helper.authenticate(proxy_url)
                if await login.test_loggedin():
                    await save_cookie(login, str(values[CONF_USERNAME]), mass)
                else:
                    raise LoginFailed(
                        "Authentication login failed, please provide logs to the discussion #431."
                    )
            except KeyError:
                # no URL param was found so user probably cancelled the auth
                pass
            except Exception as error:
                raise LoginFailed(f"Failed to authenticate with Amazon '{error}'.")
            finally:
                mass.webserver.unregister_dynamic_route(proxy_path, "GET")
                mass.webserver.unregister_dynamic_route(post_path, "POST")

    return (
        ConfigEntry(
            key=CONF_URL,
            type=ConfigEntryType.STRING,
            label="URL",
            required=True,
            default_value="amazon.com",
        ),
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            label="E-Mail",
            required=True,
            value=values.get(CONF_USERNAME) if values else None,
        ),
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Password",
            required=True,
            value=values.get(CONF_PASSWORD) if values else None,
        ),
        ConfigEntry(
            key=CONF_AUTH_SECRET,
            type=ConfigEntryType.SECURE_STRING,
            label="OTP Secret",
            required=False,
            value=values.get(CONF_AUTH_SECRET) if values else None,
        ),
        ConfigEntry(
            key=CONF_ACTION_AUTH,
            type=ConfigEntryType.ACTION,
            label="Authenticate with Amazon",
            description="Click to start the authentication process.",
            action=CONF_ACTION_AUTH,
            depends_on=CONF_URL,
        ),
        ConfigEntry(
            key=CONF_API_URL,
            type=ConfigEntryType.STRING,
            label="API Url",
            default_value="http://localhost:5000",
            required=True,
            value=values.get(CONF_API_URL) if values else None,
        ),
        ConfigEntry(
            key=CONF_API_BASIC_AUTH_USERNAME,
            type=ConfigEntryType.STRING,
            label="API Basic Auth Username",
            default_value="admin",
            required=False,
            value=values.get(CONF_API_BASIC_AUTH_USERNAME) if values else None,
        ),
        ConfigEntry(
            key=CONF_API_BASIC_AUTH_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="API Basic Auth Password",
            default_value="test",
            required=False,
            value=values.get(CONF_API_BASIC_AUTH_PASSWORD) if values else None,
        ),
        ConfigEntry(
            key=CONF_ALEXA_LANGUAGE,
            type=ConfigEntryType.STRING,
            label="Alexa Language",
            required=True,
            default_value="en-US",
        ),
    )


async def save_cookie(login: AlexaLogin, username: str, mass: MusicAssistant) -> None:
    """Save the cookie file for the Alexa login."""
    if login._session is None:
        _LOGGER.error("AlexaLogin session is not initialized.")
        return

    cookie_dir = os.path.join(mass.storage_path, ".alexa")
    await asyncio.to_thread(os.makedirs, cookie_dir, exist_ok=True)
    cookie_path = os.path.join(cookie_dir, f"alexa_media.{username}.pickle")
    login._cookiefile = [login._outputpath(cookie_path)]
    if (login._cookiefile[0]) and await asyncio.to_thread(os.path.exists, login._cookiefile[0]):
        _LOGGER.debug("Removing outdated cookiefile %s", login._cookiefile[0])
        await delete_cookie(login._cookiefile[0])
    cookie_jar = login._session.cookie_jar
    assert isinstance(cookie_jar, aiohttp.CookieJar)
    if login._debug:
        _LOGGER.debug("Saving cookie to %s", login._cookiefile[0])
    try:
        await asyncio.to_thread(cookie_jar.save, login._cookiefile[0])
    except (OSError, EOFError, TypeError, AttributeError):
        _LOGGER.debug("Error saving pickled cookie to %s", login._cookiefile[0])


async def delete_cookie(cookiefile: str) -> None:
    """Delete the specified cookie file."""
    if await asyncio.to_thread(os.path.exists, cookiefile):
        try:
            await asyncio.to_thread(os.remove, cookiefile)
            _LOGGER.debug("Deleted cookie file: %s", cookiefile)
        except OSError as e:
            _LOGGER.error("Failed to delete cookie file %s: %s", cookiefile, e)
    else:
        _LOGGER.debug("Cookie file %s does not exist, nothing to delete.", cookiefile)


class AlexaDevice:
    """Representation of an Alexa Device."""

    _device_type: str
    device_serial_number: str
    _device_family: str
    _cluster_members: str
    _locale: str


class AlexaPlayer(Player):
    """Implementation of an Alexa Player."""

    def __init__(
        self,
        provider: AlexaProvider,
        player_id: str,
        device: AlexaDevice,
    ) -> None:
        """Initialize AlexaPlayer."""
        super().__init__(provider, player_id)
        self.device = device
        self._attr_supported_features = {
            PlayerFeature.VOLUME_SET,
            PlayerFeature.PAUSE,
        }
        self._attr_name = player_id
        self._attr_device_info = DeviceInfo()
        self._attr_powered = False
        self._attr_available = True

    @property
    def requires_flow_mode(self) -> bool:
        """Return if the player requires flow mode."""
        return True

    @property
    def api(self) -> AlexaAPI:
        """Get the AlexaAPI instance for this player."""
        provider = cast("AlexaProvider", self.provider)
        return AlexaAPI(self.device, provider.login)

    async def _get_intent_first_utterance(self, intent_name: str) -> str:
        """Fetch the first utterance for a given Alexa intent from the Alexa API.

        Falls back to a sensible default if the request fails or no utterances
        are available.
        """
        api_url = self.provider.config.get_value(CONF_API_URL)
        defaults = {
            "AMAZON.PauseIntent": "pause",
            "AMAZON.ResumeIntent": "resume",
            "AMAZON.StopIntent": "stop",
        }
        if not api_url:
            return defaults.get(intent_name, "")

        try:
            url = f"{str(api_url).rstrip('/')}/alexa/intents"
            # Apply optional BasicAuth credentials if configured for the Alexa API.
            api_username = self.provider.config.get_value(CONF_API_BASIC_AUTH_USERNAME)
            api_password = self.provider.config.get_value(CONF_API_BASIC_AUTH_PASSWORD)
            auth: BasicAuth | None = None
            if api_username and api_password:
                auth = BasicAuth(str(api_username), str(api_password))
            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    url,
            session = self.provider.mass.http_session
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status < 200 or resp.status >= 300:
                    return defaults.get(intent_name, "")
                payload = await resp.json()
                intents = payload.get("intents", []) if isinstance(payload, dict) else []
                for it in intents:
                    if it.get("intent") == intent_name:
                        utts = it.get("utterances") or []
                        if len(utts) > 0:
                            return str(utts[0])
        except Exception:
            # Any failure -> safe fallback
            return defaults.get(intent_name, "")

        return defaults.get(intent_name, "")

    async def stop(self) -> None:
        """Handle STOP command on the player."""
        utterance = await self._get_intent_first_utterance("AMAZON.StopIntent")
        await self.api.run_custom(utterance)
        self._attr_current_media = None
        self._attr_playback_state = PlaybackState.IDLE
        self.update_state()

    async def play(self) -> None:
        """Handle PLAY command on the player."""
        utterance = await self._get_intent_first_utterance("AMAZON.ResumeIntent")
        await self.api.run_custom(utterance)
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def pause(self) -> None:
        """Handle PAUSE command on the player."""
        utterance = await self._get_intent_first_utterance("AMAZON.PauseIntent")
        await self.api.run_custom(utterance)
        self._attr_playback_state = PlaybackState.PAUSED
        self.update_state()

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command on the player."""
        await self.api.set_volume(volume_level / 100)
        self._attr_volume_level = volume_level
        self.update_state()

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on the player."""
        username = self.provider.config.get_value(CONF_API_BASIC_AUTH_USERNAME)
        password = self.provider.config.get_value(CONF_API_BASIC_AUTH_PASSWORD)

        auth = None
        # Only enable BasicAuth when both username and password are non-empty
        if username:
            username = str(username).strip()
        if password:
            password = str(password).strip()
        if username and password:
            auth = BasicAuth(username, password)

        if self.current_media is not None:
            title = self.current_media.title or media.title
            artist = self.current_media.artist or media.artist
            album = self.current_media.album or media.album
            image_url = self.current_media.image_url or media.image_url

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(
                    f"{self.provider.config.get_value(CONF_API_URL)}/ma/push-url",
                    json={
                        "streamUrl": media.uri,
                        "title": title,
                        "artist": artist,
                        "album": album,
                        "imageUrl": image_url,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                    auth=auth,
                ) as resp:
                    resp_text = await resp.text()
                    if resp.status < 200 or resp.status >= 300:
                        msg = (
                            f"Failed to push URL to MA Alexa API: "
                            f"Status code: {resp.status}, Response: {resp_text}. "
                            "Please verify your API connection and configuration"
                        )
                        _LOGGER.error(msg)
                        raise ActionUnavailable(msg)
            except ActionUnavailable:
                raise
            except Exception as exc:
                msg = (
                    "Failed to push URL to MA Alexa API: "
                    "Please verify your API connection and configuration"
                )
                _LOGGER.error("Failed to push URL to MA Alexa API: %s", exc)
                raise ActionUnavailable(msg)

        alexa_locale = self.provider.config.get_value(CONF_ALEXA_LANGUAGE)

        ask_command_key = f"play_audio_{alexa_locale if alexa_locale else 'default'}"

        if ask_command_key not in ALEXA_LANGUAGE_COMMANDS:
            _LOGGER.debug(
                "Ask command key %s not found in ALEXA_LANGUAGE_COMMANDS.",
                ask_command_key,
            )
            ask_command_key = "play_audio_default"

        _LOGGER.debug(
            "Using ask command key: %s -> %s",
            ask_command_key,
            ALEXA_LANGUAGE_COMMANDS[ask_command_key],
        )

        await self.api.run_custom(ALEXA_LANGUAGE_COMMANDS[ask_command_key])
        self._attr_elapsed_time = 0
        self._attr_elapsed_time_last_updated = time.time()
        self._attr_playback_state = PlaybackState.PLAYING
        self._attr_current_media = media
        self.update_state()


class AlexaProvider(PlayerProvider):
    """Implementation of an Alexa Device Provider."""

    login: AlexaLogin
    devices: dict[str, AlexaDevice]

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.devices = {}

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        self.login = AlexaLogin(
            url=str(self.config.get_value(CONF_URL)),
            email=str(self.config.get_value(CONF_USERNAME)),
            password=str(self.config.get_value(CONF_PASSWORD)),
            outputpath=lambda x: x,
        )

        cookie_dir = os.path.join(self.mass.storage_path, ".alexa")
        await asyncio.to_thread(os.makedirs, cookie_dir, exist_ok=True)
        cookie_path = os.path.join(
            cookie_dir, f"alexa_media.{self.config.get_value(CONF_USERNAME)}.pickle"
        )
        self.login._cookiefile = [self.login._outputpath(cookie_path)]

        await self.login.login(cookies=await self.login.load_cookie())

        devices = await AlexaAPI.get_devices(self.login)

        if devices is None:
            return

        alexa_locale = str(self.config.get_value(CONF_ALEXA_LANGUAGE, "en-US"))

        for device in devices:
            if device.get("capabilities") and "MUSIC_SKILL" in device.get("capabilities"):
                dev_name = device["accountName"]
                player_id = dev_name
                # Initialize AlexaDevice
                device_object = AlexaDevice()
                device_object._device_type = device["deviceType"]
                device_object.device_serial_number = device["serialNumber"]
                device_object._device_family = device["deviceOwnerCustomerId"]
                device_object._cluster_members = device["clusterMembers"]
                device_object._locale = alexa_locale
                self.devices[player_id] = device_object

                # Create AlexaPlayer instance
                player = AlexaPlayer(self, player_id, device_object)
                await self.mass.players.register_or_update(player)
