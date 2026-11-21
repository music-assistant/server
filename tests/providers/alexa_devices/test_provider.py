"""Tests for the Alexa Devices provider."""

from __future__ import annotations

import json
from collections.abc import Coroutine, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aioamazondevices.exceptions import (
    CannotAuthenticate,
    CannotConnect,
    CannotRetrieveData,
    NoOnlineDevicesError,
)
from aioamazondevices.structures import AmazonVolumeState
from music_assistant_models.errors import (
    LoginFailed,
    MusicAssistantError,
    ProviderUnavailableError,
)

from music_assistant.constants import CONF_USERNAME
from music_assistant.providers.alexa_devices.constants import CONF_API_URL, CONF_LOGIN_DATA
from music_assistant.providers.alexa_devices.player import AlexaDevicesPlayer
from music_assistant.providers.alexa_devices.provider import AlexaDevicesProvider
from tests.common import create_mock_config

from .conftest import SERIAL, FakeSkillApi, amazon_device

LOGIN_DATA = {"site": "https://www.amazon.it"}
GROUP_SERIAL = "GROUP0000000001"
NO_SKILL_SERIAL = "G0910000000000CD"


@pytest.fixture
def amazon_api_class() -> Iterator[MagicMock]:
    """Mock the aioamazondevices API class."""
    with patch("music_assistant.providers.alexa_devices.provider.AmazonEchoApi") as api_class:
        yield api_class


@pytest.fixture
def amazon_api(amazon_api_class: MagicMock) -> MagicMock:
    """Mock the aioamazondevices API, with one supported device and two unsupported."""
    api: MagicMock = amazon_api_class.return_value
    api.login.login_mode_stored_data = AsyncMock(return_value=LOGIN_DATA)
    api.get_devices_data = AsyncMock(
        return_value={
            SERIAL: amazon_device(SERIAL),
            GROUP_SERIAL: amazon_device(GROUP_SERIAL, voice_control_supported=False),
            NO_SKILL_SERIAL: amazon_device(NO_SKILL_SERIAL, capabilities=("AUDIO_PLAYER",)),
        }
    )
    api.sync_media_state = AsyncMock()
    api.start_http2_processing = AsyncMock()
    api.stop_http2_processing = AsyncMock()
    return api


@pytest.fixture
async def provider(skill_api: FakeSkillApi) -> Any:
    """Return a provider with stored setup data and a mocked MA instance."""
    setup_data = {
        CONF_USERNAME: "user@example.com",
        CONF_LOGIN_DATA: json.dumps(LOGIN_DATA),
        CONF_API_URL: skill_api.url,
    }
    mass = MagicMock()
    mass.http_session = skill_api.session
    mass.storage_path = "/tmp/ma"  # noqa: S108 - never written to, the library is mocked
    mass.config.get.return_value = setup_data
    mass.config.decrypt_string.side_effect = lambda value: value
    mass.config.get_base_player_config.return_value = create_mock_config("Echo")
    mass.players.register_or_update = AsyncMock()

    def _create_task(coro: Coroutine[Any, Any, Any], **_kwargs: Any) -> MagicMock:
        # the device refresh loop is not run in these tests
        coro.close()
        return MagicMock()

    mass.create_task.side_effect = _create_task
    config = MagicMock()
    config.get_value.return_value = None
    alexa = AlexaDevicesProvider(mass, MagicMock(domain="alexa_devices"), config)
    yield alexa
    if hasattr(alexa, "api"):
        await alexa.unload()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(CannotAuthenticate("expired"), LoginFailed, id="auth"),
        pytest.param(CannotConnect("offline"), ProviderUnavailableError, id="connect"),
        pytest.param(CannotRetrieveData("bad"), ProviderUnavailableError, id="data"),
        pytest.param(NoOnlineDevicesError("none"), ProviderUnavailableError, id="no_devices"),
    ],
)
async def test_load_errors(
    provider: AlexaDevicesProvider,
    amazon_api: MagicMock,
    error: Exception,
    expected: type[MusicAssistantError],
) -> None:
    """A login error asks for a new sign-in, other errors let MA retry later."""
    amazon_api.login.login_mode_stored_data.side_effect = error

    with pytest.raises(expected):
        await provider.handle_async_init()


@pytest.mark.usefixtures("amazon_api")
async def test_load_uses_stored_login(
    provider: AlexaDevicesProvider, amazon_api_class: MagicMock
) -> None:
    """The stored login data is handed to the library, no password is needed."""
    await provider.handle_async_init()

    assert amazon_api_class.call_args.kwargs["login_data"] == LOGIN_DATA
    assert amazon_api_class.call_args.args[1:] == ("user@example.com", "")


@pytest.mark.usefixtures("amazon_api")
async def test_only_skill_capable_devices_become_players(provider: AlexaDevicesProvider) -> None:
    """Speaker groups and devices without the music skill capability are skipped."""
    await provider.handle_async_init()
    await provider.loaded_in_mass()

    registered = [
        call.args[0].player_id
        for call in provider.mass.players.register_or_update.await_args_list  # type: ignore[attr-defined]
    ]
    assert registered == [SERIAL]


async def test_pushed_volume_reaches_player(
    provider: AlexaDevicesProvider, amazon_api: MagicMock
) -> None:
    """Volume states pushed by Amazon update the matching player."""
    await provider.handle_async_init()
    await provider.loaded_in_mass()
    player: AlexaDevicesPlayer = provider.mass.players.register_or_update.await_args.args[0]  # type: ignore[attr-defined]
    on_volume_state = amazon_api.on_volume_state_event.append.call_args.args[0]

    with patch.object(player, "update_state"):
        await on_volume_state(
            {
                SERIAL: AmazonVolumeState(volume=20, is_muted=False),
                "UNKNOWN": AmazonVolumeState(volume=90, is_muted=False),
            }
        )

    assert player.volume_level == 20


async def test_push_channel_reauth_unloads_with_login_error(
    provider: AlexaDevicesProvider, amazon_api: MagicMock
) -> None:
    """When the push channel needs a new login, the provider unloads with a login error."""
    await provider.handle_async_init()
    await provider.loaded_in_mass()
    on_reauth_required = amazon_api.start_http2_processing.call_args.kwargs["on_reauth_required"]

    await on_reauth_required()

    error = provider.mass.call_later.call_args.args[-1]  # type: ignore[attr-defined]
    assert isinstance(error, LoginFailed)
