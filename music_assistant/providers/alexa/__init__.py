"""Alexa player provider support for Music Assistant."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import BasicAuth, web
from alexapy import AlexaAPI, AlexaLogin, AlexaProxy
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    PlayerFeature,
    PlayerState,
    PlayerType,
    ProviderFeature,
)
from music_assistant_models.errors import LoginFailed
from music_assistant_models.player import DeviceInfo, Player, PlayerMedia

from music_assistant.constants import (
    CONF_ENTRY_CROSSFADE,
    CONF_ENTRY_CROSSFADE_DURATION,
    CONF_ENTRY_FLOW_MODE_ENFORCED,
    CONF_ENTRY_HTTP_PROFILE,
    CONF_PASSWORD,
    CONF_USERNAME,
)
from music_assistant.helpers.auth import AuthenticationHelper
from music_assistant.models.player_provider import PlayerProvider

_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import (
        ConfigValueType,
        ProviderConfig,
    )
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CONF_URL = "url"
CONF_ACTION_AUTH = "auth"
CONF_AUTH_SECRET = "secret"
CONF_API_BASIC_AUTH_USERNAME = "api_username"
CONF_API_BASIC_AUTH_PASSWORD = "api_password"
CONF_API_URL = "api_url"

SUPPORTED_FEATURES: set[ProviderFeature] = set()


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return AlexaProvider(mass, manifest, config)


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
            default_value="http://localhost:3000",
            required=True,
            value=values.get(CONF_API_URL) if values else None,
        ),
        ConfigEntry(
            key=CONF_API_BASIC_AUTH_USERNAME,
            type=ConfigEntryType.STRING,
            label="API Basic Auth Username",
            required=False,
            value=values.get(CONF_API_BASIC_AUTH_USERNAME) if values else None,
        ),
        ConfigEntry(
            key=CONF_API_BASIC_AUTH_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="API Basic Auth Password",
            required=False,
            value=values.get(CONF_API_BASIC_AUTH_PASSWORD) if values else None,
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


class AlexaProvider(PlayerProvider):
    """Implementation of an Alexa Device Provider."""

    class AlexaDevice:
        """Representation of an Alexa Device."""

        _device_type: str
        device_serial_number: str
        _device_family: str
        _cluster_members: str
        _locale: str

    login: AlexaLogin
    devices: dict[str, AlexaProvider.AlexaDevice]

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return SUPPORTED_FEATURES

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize AlexaProvider and its device mapping."""
        super().__init__(mass, manifest, config)
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

        for device in devices:
            if device.get("capabilities") and "MUSIC_SKILL" in device.get("capabilities"):
                dev_name = device["accountName"]
                player_id = dev_name
                player = Player(
                    player_id=player_id,
                    provider=self.instance_id,
                    type=PlayerType.PLAYER,
                    name=player_id,
                    available=True,
                    powered=False,
                    device_info=DeviceInfo(),
                    supported_features={
                        PlayerFeature.VOLUME_SET,
                        PlayerFeature.PAUSE,
                        PlayerFeature.VOLUME_MUTE,
                    },
                )
                await self.mass.players.register_or_update(player)
                # Initialize AlexaDevice and store in self.devices
                device_object = self.AlexaDevice()
                device_object._device_type = device["deviceType"]
                device_object.device_serial_number = device["serialNumber"]
                device_object._device_family = device["deviceOwnerCustomerId"]
                device_object._cluster_members = device["clusterMembers"]
                device_object._locale = "en-US"
                self.devices[player_id] = device_object

    async def get_player_config_entries(self, player_id: str) -> tuple[ConfigEntry, ...]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        base_entries = await super().get_player_config_entries(player_id)
        return (
            *base_entries,
            CONF_ENTRY_FLOW_MODE_ENFORCED,
            CONF_ENTRY_CROSSFADE,
            CONF_ENTRY_CROSSFADE_DURATION,
            CONF_ENTRY_HTTP_PROFILE,
        )

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player."""
        if not (player := self.mass.players.get(player_id, raise_unavailable=False)):
            return
        device_object = self.devices[player_id]
        api = AlexaAPI(device_object, self.login)
        await api.stop()

        player.state = PlayerState.IDLE
        self.mass.players.update(player_id)

    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY command to given player."""
        if not (player := self.mass.players.get(player_id, raise_unavailable=False)):
            return
        device_object = self.devices[player_id]
        api = AlexaAPI(device_object, self.login)
        await api.play()

        player.state = PlayerState.PLAYING
        self.mass.players.update(player_id)

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player."""
        if not (player := self.mass.players.get(player_id, raise_unavailable=False)):
            return
        device_object = self.devices[player_id]
        api = AlexaAPI(device_object, self.login)
        await api.pause()

        player.state = PlayerState.PLAYING
        self.mass.players.update(player_id)

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        if not (player := self.mass.players.get(player_id, raise_unavailable=False)):
            return
        device_object = self.devices[player_id]
        api = AlexaAPI(device_object, self.login)
        await api.set_volume(volume_level / 100)

        player.volume_level = volume_level
        self.mass.players.update(player_id)

    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send VOLUME MUTE command to given player."""
        if not (player := self.mass.players.get(player_id, raise_unavailable=False)):
            return
        device_object = self.devices[player_id]
        api = AlexaAPI(device_object, self.login)
        await api.set_volume(0)

        player.volume_level = 0
        self.mass.players.update(player_id)

    async def play_media(
        self,
        player_id: str,
        media: PlayerMedia,
    ) -> None:
        """Handle PLAY MEDIA on given player.

        This is called by the Players controller to start playing a mediaitem on the given player.
        The provider's own implementation should work out how to handle this request.

            - player_id: player_id of the player to handle the command.
            - media: Details of the item that needs to be played on the player.
        """
        if not (player := self.mass.players.get(player_id)):
            return

        username = self.config.get_value(CONF_API_BASIC_AUTH_USERNAME)
        password = self.config.get_value(CONF_API_BASIC_AUTH_PASSWORD)

        auth = None
        if username is not None and password is not None:
            auth = BasicAuth(str(username), str(password))

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(
                    f"{self.config.get_value(CONF_API_URL)}/ma/push-url",
                    json={"streamUrl": media.uri},
                    timeout=aiohttp.ClientTimeout(total=10),
                    auth=auth,
                ) as resp:
                    await resp.text()
            except Exception as exc:
                _LOGGER.error("Failed to push URL to Alexa: %s", exc)
                return
        device_object = self.devices[player_id]
        api = AlexaAPI(device_object, self.login)
        await api.run_custom("Ask music assistant to play audio")

        state = await api.get_state()
        if state:
            state = state.get("playerInfo", None)

        if state:
            device_media = state.get("infoText")
            if device_media:
                media.title = device_media.get("title")
                media.artist = device_media.get("subText1")
                player.current_media = media
            player.elapsed_time = 0
            player.elapsed_time_last_updated = time.time()
            if state.get("playbackState") == "PLAYING":
                player.state = PlayerState.PLAYING
        self.mass.players.update(player_id)
