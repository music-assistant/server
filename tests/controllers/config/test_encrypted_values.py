"""Tests for how encrypted config values are served to and accepted from API callers."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import ConfigEntry, ProviderAccess
from music_assistant_models.constants import SECURE_STRING_SUBSTITUTE
from music_assistant_models.enums import (
    ConfigEntryType,
    FlowStepType,
    ProviderSharing,
    ProviderType,
)
from music_assistant_models.errors import InvalidDataError

from music_assistant.constants import CONF_PLAYERS, CONF_PROVIDERS, ENCRYPT_SUFFIX
from music_assistant.controllers.config.helpers import _reject_encrypted_values
from music_assistant.controllers.webserver.helpers.auth_middleware import set_current_user
from music_assistant.models.setup_flow import SetupFlowContext, SetupSession
from tests.common import set_music_source_access

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

_PROVIDER = "test_player"
_CORE = "faketestcore"
_OWN_SOURCE = "filesystem_local--member"
_ADMIN = User(user_id="admin", username="admin", role=UserRole.ADMIN)
_MEMBER = User(user_id="member", username="member", role=UserRole.USER)
_PLAYER = "test_player_id"
_TEXT_ENTRY = ConfigEntry(key="folder", type=ConfigEntryType.STRING, required=True)
_LIST_ENTRY = ConfigEntry(
    key="folders", type=ConfigEntryType.STRING, multi_value=True, required=True
)


@pytest.fixture(autouse=True)
def _reset_current_user() -> Iterator[None]:
    """Leave no calling user behind for the next test."""
    yield
    set_current_user(None)


@pytest.fixture
def secret_mass(mass_minimal: MusicAssistant) -> MusicAssistant:
    """Return a minimal instance with a provider and a core config holding encrypted values."""
    encrypted = mass_minimal.config.encrypt_string("secret")
    mass_minimal.config.set(
        f"{CONF_PROVIDERS}/{_PROVIDER}",
        {
            "type": ProviderType.PLAYER.value,
            "domain": _PROVIDER,
            "instance_id": _PROVIDER,
            "values": {"password": encrypted, "host": "192.168.1.2"},
            "setup_data": {"token": encrypted, "accounts": [encrypted], "user": "bob"},
        },
    )
    controller = SimpleNamespace(
        get_config_entries=AsyncMock(return_value=()), update_config=AsyncMock()
    )
    setattr(mass_minimal, _CORE, controller)
    mass_minimal.config.set_raw_core_config_value(_CORE, "api_key", encrypted)
    mass_minimal.config.set(
        f"{CONF_PLAYERS}/{_PLAYER}",
        {
            "player_id": _PLAYER,
            "provider": _PROVIDER,
            "values": {"password": encrypted},
            "setup_data": {"token": encrypted, "accounts": [encrypted], "user": "bob"},
        },
    )
    return mass_minimal


def _make_session() -> SetupSession:
    """Build a SetupSession backed by a Mock mass."""
    context = SetupFlowContext(kind="setup", reason="user", domain="filesystem_local")
    return SetupSession(Mock(), "flow-test", context, AsyncMock())


async def _wait_for_step(session: SetupSession, step_type: FlowStepType) -> None:
    """Wait until the session publishes a step of the given type."""
    async with asyncio.timeout(5):
        while session.current_step is None or session.current_step.type != step_type:
            await asyncio.sleep(0.01)


async def test_setup_form_rejects_an_encrypted_value() -> None:
    """An encrypted value submitted in a setup form is an invalid value."""
    session = _make_session()
    task = asyncio.create_task(session.form([_TEXT_ENTRY]))
    await _wait_for_step(session, FlowStepType.FORM)

    error_step = session.handle_submit({"folder": f"{ENCRYPT_SUFFIX}gAAAAAB"})

    assert error_step is not None
    assert error_step.errors == {"folder": "invalid_value"}
    assert not task.done()
    assert session.handle_submit({"folder": "/media/music"}) is None
    assert await task == {"folder": "/media/music"}


async def test_setup_form_rejects_a_nested_encrypted_value() -> None:
    """An encrypted value inside a submitted list is an invalid value."""
    session = _make_session()
    task = asyncio.create_task(session.form([_LIST_ENTRY]))
    await _wait_for_step(session, FlowStepType.FORM)

    error_step = session.handle_submit({"folders": ["/media", f"{ENCRYPT_SUFFIX}gAAAAAB"]})

    assert error_step is not None
    assert error_step.errors == {"folders": "invalid_value"}
    assert not task.done()
    task.cancel()


async def test_setup_callback_drops_encrypted_params() -> None:
    """An encrypted value in an external-step callback never reaches the flow."""
    session = _make_session()
    task = asyncio.create_task(session.external("https://example.com/auth"))
    await _wait_for_step(session, FlowStepType.EXTERNAL)

    await session.handle_callback(
        make_mocked_request("GET", f"/callback?code=abc&token={ENCRYPT_SUFFIX}gAAAAAB")
    )

    assert await task == {"code": "abc"}


def test_reject_encrypted_values_accepts_plain_values() -> None:
    """Plain values and the secure string placeholder are accepted."""
    _reject_encrypted_values({"password": "hunter2", "other": SECURE_STRING_SUBSTITUTE, "n": 1})


@pytest.mark.parametrize(
    "value",
    [[f"{ENCRYPT_SUFFIX}gAAAAAB"], {"token": f"{ENCRYPT_SUFFIX}gAAAAAB"}],
    ids=["list", "dict"],
)
async def test_saves_reject_a_nested_encrypted_value(
    secret_mass: MusicAssistant, value: Any
) -> None:
    """Config saves refuse an encrypted value nested in a list or dict."""
    with pytest.raises(InvalidDataError):
        _reject_encrypted_values({"nested": value})
    with pytest.raises(InvalidDataError):
        await secret_mass.config.save_core_config(_CORE, {"log_level": value})
    with (
        patch.object(secret_mass.config, "get_player_config", AsyncMock()) as get_config,
        pytest.raises(InvalidDataError),
    ):
        await secret_mass.config.save_player_config(_PLAYER, {"name": value})
    get_config.assert_not_awaited()


async def test_provider_save_rejects_an_encrypted_value(secret_mass: MusicAssistant) -> None:
    """A member saving its own music source with an encrypted value is refused."""
    access = ProviderAccess(owner=_MEMBER.user_id, sharing=ProviderSharing.PRIVATE)
    set_music_source_access(secret_mass, {_OWN_SOURCE: access})
    set_current_user(_MEMBER)
    with (
        patch.object(secret_mass.config, "_update_provider_config", AsyncMock()) as update,
        patch.object(secret_mass.config, "get_provider_config", AsyncMock()),
        pytest.raises(InvalidDataError),
    ):
        await secret_mass.config.save_provider_config(
            "filesystem_local", {"path": f"{ENCRYPT_SUFFIX}gAAAAAB"}, instance_id=_OWN_SOURCE
        )
    update.assert_not_awaited()


async def test_core_save_rejects_an_encrypted_value(secret_mass: MusicAssistant) -> None:
    """A core config save with an encrypted value is refused and nothing is stored."""
    with pytest.raises(InvalidDataError):
        await secret_mass.config.save_core_config(_CORE, {"log_level": f"{ENCRYPT_SUFFIX}gAAAAAB"})
    assert secret_mass.config.get_raw_core_config_value(_CORE, "log_level") is None


async def test_player_save_rejects_an_encrypted_value(secret_mass: MusicAssistant) -> None:
    """A player config save with an encrypted value is refused."""
    with (
        patch.object(secret_mass.config, "get_player_config", AsyncMock()) as get_config,
        pytest.raises(InvalidDataError),
    ):
        await secret_mass.config.save_player_config("some_player", {"name": f"{ENCRYPT_SUFFIX}x"})
    get_config.assert_not_awaited()


@pytest.mark.parametrize("user", [_MEMBER, _ADMIN, None], ids=["member", "admin", "internal"])
async def test_get_value_masks_encrypted_values(
    secret_mass: MusicAssistant, user: User | None
) -> None:
    """Encrypted values read as the secure string placeholder, also when nested."""
    set_current_user(user)

    password = await secret_mass.config.get_provider_config_value(
        _PROVIDER, "password", return_type=str
    )
    setup_data: Any = await secret_mass.config.get_provider_config_value(_PROVIDER, "setup_data")
    api_key = await secret_mass.config.get_core_config_value(_CORE, "api_key", return_type=str)

    assert password == SECURE_STRING_SUBSTITUTE
    assert setup_data == {
        "token": SECURE_STRING_SUBSTITUTE,
        "accounts": [SECURE_STRING_SUBSTITUTE],
        "user": "bob",
    }
    assert api_key == SECURE_STRING_SUBSTITUTE
    assert await secret_mass.config.get_provider_config_value(_PROVIDER, "host") == "192.168.1.2"
    # masking never touches the stored value
    stored = secret_mass.config.get_raw_provider_config_value(_PROVIDER, "password")
    assert isinstance(stored, str)
    assert stored.startswith(ENCRYPT_SUFFIX)


async def test_player_get_value_masks_encrypted_values(secret_mass: MusicAssistant) -> None:
    """Encrypted player config values read as the secure string placeholder, also when nested."""
    password = await secret_mass.config.get_player_config_value(
        _PLAYER, "password", return_type=str
    )
    setup_data: Any = await secret_mass.config.get_player_config_value(_PLAYER, "setup_data")

    assert password == SECURE_STRING_SUBSTITUTE
    assert setup_data == {
        "token": SECURE_STRING_SUBSTITUTE,
        "accounts": [SECURE_STRING_SUBSTITUTE],
        "user": "bob",
    }
