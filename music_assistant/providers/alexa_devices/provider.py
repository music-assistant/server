"""Alexa Devices player provider, built on aioamazondevices."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

import aiohttp
import httpx
from aioamazondevices.api import AmazonEchoApi
from aioamazondevices.exceptions import (
    CannotAuthenticate,
    CannotConnect,
    CannotRetrieveData,
    NoOnlineDevicesError,
)
from aioamazondevices.structures import AmazonSaveDataConfig
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import LoginFailed, ProviderUnavailableError

from music_assistant.constants import CONF_USERNAME
from music_assistant.models.player_provider import PlayerProvider

from .companion import CompanionSkillApi
from .constants import (
    ALEXA_LANGUAGE_COMMANDS,
    CAPABILITY_MUSIC_SKILL,
    CONF_ALEXA_LANGUAGE,
    CONF_API_BASIC_AUTH_PASSWORD,
    CONF_API_BASIC_AUTH_USERNAME,
    CONF_API_URL,
    CONF_LOGIN_DATA,
    DEFAULT_API_URL,
    DEFAULT_LANGUAGE,
    DEVICES_REFRESH_INTERVAL,
)
from .player import AlexaDevicesPlayer

if TYPE_CHECKING:
    from aioamazondevices.structures import AmazonDevice, AmazonMediaState, AmazonVolumeState

# errors that a retry later may resolve
RETRYABLE_ERRORS = (CannotConnect, CannotRetrieveData, NoOnlineDevicesError)


def create_api(
    http_session: aiohttp.ClientSession,
    username: str,
    password: str,
    mass_storage_path: str,
    login_data: dict[str, object] | None = None,
) -> AmazonEchoApi:
    """
    Create the aioamazondevices API client.

    :param http_session: A dedicated aiohttp session: the library keeps Amazon's
        cookies in its cookie jar, so it must not be the shared MA session.
    :param username: Amazon account e-mail.
    :param password: Amazon account password (only needed for interactive login).
    :param mass_storage_path: MA storage path, the library settings go below it.
    :param login_data: Stored login data from a previous interactive login.
    """
    return AmazonEchoApi(
        http_session,
        username,
        password,
        login_data=login_data,
        save_data=AmazonSaveDataConfig(path=Path(mass_storage_path, ".alexa_devices")),
    )


class AlexaDevicesProvider(PlayerProvider):
    """Player provider for Amazon Echo devices, using aioamazondevices."""

    api: AmazonEchoApi
    companion: CompanionSkillApi

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """
        Return the config entries for this provider.

        Amazon login and the companion skill API are collected by the setup flow (see
        ``setup_flow.py``), only the language of the voice commands is configured here.
        """
        return (
            ConfigEntry(
                key=CONF_ALEXA_LANGUAGE,
                type=ConfigEntryType.STRING,
                required=True,
                options=[ConfigValueOption(language) for language in ALEXA_LANGUAGE_COMMANDS],
                default_value=DEFAULT_LANGUAGE,
            ),
        )

    async def handle_async_init(self) -> None:
        """Log in to Amazon with the stored session and fetch the devices."""
        self._players: dict[str, AlexaDevicesPlayer] = {}
        self._refresh_task: asyncio.Task[None] | None = None
        self._httpx_client: httpx.AsyncClient | None = None
        self.companion = CompanionSkillApi(
            self.mass.http_session,
            str(self.get_setup_value(CONF_API_URL) or DEFAULT_API_URL),
            cast("str | None", self.get_setup_value(CONF_API_BASIC_AUTH_USERNAME)),
            cast("str | None", self.get_setup_value(CONF_API_BASIC_AUTH_PASSWORD)),
            self.logger,
        )
        self._amazon_session = aiohttp.ClientSession()
        self.api = create_api(
            self._amazon_session,
            str(self.get_setup_value(CONF_USERNAME)),
            # the password is only needed for an interactive login (in the setup flow)
            "",
            self.mass.storage_path,
            json.loads(str(self.get_setup_value(CONF_LOGIN_DATA))),
        )
        self.api.on_volume_state_event.append(self._on_volume_state)
        self.api.on_volume_state_event.freeze()
        self.api.on_media_state_event.append(self._on_media_state)
        self.api.on_media_state_event.freeze()
        try:
            await self._login()
        except BaseException:
            # the provider is not registered yet, so unload() won't close the session
            await self._amazon_session.close()
            raise

    async def loaded_in_mass(self) -> None:
        """Register the players and start receiving pushed state."""
        await self._register_new_players()
        try:
            await self.api.sync_media_state()
        except RETRYABLE_ERRORS as err:
            self.logger.warning("Initial media state sync failed, waiting for pushes: %s", err)
        # the push channel is a long-lived HTTP/2 stream, aioamazondevices needs no timeout
        self._httpx_client = httpx.AsyncClient(http2=True, timeout=None)  # noqa: S113
        await self.api.start_http2_processing(
            self._httpx_client, on_reauth_required=self._on_reauth_required
        )
        self._refresh_task = self.mass.create_task(self._refresh_devices_loop())
        # warm the intent cache; the skill api may come up later, failures are fine
        await self.companion.get_intent_utterance("AMAZON.PauseIntent", "")

    async def unload(self, is_removed: bool = False) -> None:
        """Stop the push channel and close the Amazon sessions."""
        if self._refresh_task:
            self._refresh_task.cancel()
        await self.api.stop_http2_processing()
        if self._httpx_client:
            await self._httpx_client.aclose()
        await self._amazon_session.close()

    async def _login(self) -> None:
        """Log in with the stored login data and fetch the devices."""
        try:
            await self.api.login.login_mode_stored_data()
            self._devices = await self.api.get_devices_data()
        except CannotAuthenticate as err:
            msg = f"Amazon login expired, please sign in again: {err}"
            raise LoginFailed(msg) from err
        except RETRYABLE_ERRORS as err:
            msg = f"Unable to retrieve Alexa devices: {err}"
            raise ProviderUnavailableError(msg) from err

    async def _register_new_players(self) -> None:
        """Register a player for every new device that can run the companion skill."""
        for serial_number, device in self._devices.items():
            if serial_number in self._players or not _is_supported(device):
                continue
            player = AlexaDevicesPlayer(self, device)
            self._players[serial_number] = player
            await self.mass.players.register_or_update(player)

    async def _refresh_devices_loop(self) -> None:
        """Periodically refresh the device list and online state."""
        while True:
            await asyncio.sleep(DEVICES_REFRESH_INTERVAL)
            try:
                self._devices = await self.api.get_devices_data()
            except CannotAuthenticate as err:
                self.unload_with_error(LoginFailed(f"Amazon login expired: {err}"))
                return
            except RETRYABLE_ERRORS as err:
                self.logger.debug("Unable to refresh Alexa devices: %s", err)
                continue
            for serial_number, player in self._players.items():
                player.update_device(self._devices.get(serial_number))
            await self._register_new_players()

    async def _on_reauth_required(self) -> None:
        """Unload with a login error when the push channel lost its authentication."""
        self.unload_with_error(LoginFailed("Amazon login expired, please sign in again"))

    async def _on_volume_state(self, volume_states: dict[str, AmazonVolumeState]) -> None:
        """Apply pushed volume states to the players."""
        for serial_number, volume_state in volume_states.items():
            if player := self._players.get(serial_number):
                player.update_volume(volume_state)

    async def _on_media_state(self, media_states: dict[str, AmazonMediaState]) -> None:
        """Apply pushed media states to the players."""
        for serial_number, media_state in media_states.items():
            if player := self._players.get(serial_number):
                player.update_media(media_state)


def _is_supported(device: AmazonDevice) -> bool:
    """Return if the companion skill can stream on this device."""
    # speaker groups are excluded: a voice command goes out to each member on its own
    return device.voice_control_supported and CAPABILITY_MUSIC_SKILL in device.capabilities
