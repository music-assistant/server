"""Tests for provider status derivation and structured error building."""

from __future__ import annotations

import asyncio

import pytest
from music_assistant_models.config_entries import ProviderConfig, ProviderError
from music_assistant_models.enums import ProviderStatus, ProviderType
from music_assistant_models.errors import (
    AuthenticationRequired,
    LoginFailed,
    SetupFailedError,
    UnsupportedSystemError,
)

from music_assistant.controllers.config.helpers import _provider_status
from music_assistant.mass import _provider_error_from_exc, _provider_load_step


def _conf(*, enabled: bool = True, last_error: ProviderError | None = None) -> ProviderConfig:
    return ProviderConfig(
        values={},
        type=ProviderType.MUSIC,
        domain="demo",
        instance_id="demo--1",
        enabled=enabled,
        last_error=last_error,
    )


def test_provider_status_derivation() -> None:
    """Status reflects the provider's config + load state, keyed off the error code."""
    assert _provider_status(_conf(enabled=False), is_loaded=False) == ProviderStatus.DISABLED
    assert _provider_status(_conf(), is_loaded=True) == ProviderStatus.LOADED
    assert _provider_status(_conf(), is_loaded=False) == ProviderStatus.LOADING
    err = ProviderError(error_code=SetupFailedError.error_code, message="boom")
    assert _provider_status(_conf(last_error=err), is_loaded=False) == ProviderStatus.ERROR
    auth = ProviderError(error_code=AuthenticationRequired.error_code, message="auth")
    assert _provider_status(_conf(last_error=auth), is_loaded=False) == ProviderStatus.AUTH_REQUIRED
    login = ProviderError(error_code=LoginFailed.error_code, message="login")
    assert (
        _provider_status(_conf(last_error=login), is_loaded=False) == ProviderStatus.AUTH_REQUIRED
    )
    incompat = ProviderError(error_code=UnsupportedSystemError.error_code, message="nope")
    assert (
        _provider_status(_conf(last_error=incompat), is_loaded=False) == ProviderStatus.INCOMPATIBLE
    )


def test_recorded_error_is_reported_for_a_loaded_provider() -> None:
    """A loaded provider carrying an error still reports it, so the UI can flag it."""
    err = ProviderError(error_code=SetupFailedError.error_code, message="boom")
    assert _provider_status(_conf(last_error=err), is_loaded=True) == ProviderStatus.ERROR
    auth = ProviderError(error_code=AuthenticationRequired.error_code, message="auth")
    assert _provider_status(_conf(last_error=auth), is_loaded=True) == ProviderStatus.AUTH_REQUIRED


def test_provider_error_from_exc() -> None:
    """A MusicAssistantError keeps its code/translation_key; other exceptions get code 999."""
    err = _provider_error_from_exc(LoginFailed("bad creds"))
    assert err.error_code == LoginFailed.error_code
    assert err.message == "bad creds"
    assert err.translation_key == LoginFailed.translation_key
    generic = _provider_error_from_exc(ValueError("oops"))
    assert generic.error_code == 999
    assert generic.message == "oops"
    assert generic.translation_key is None


async def test_load_step_names_the_step_that_timed_out() -> None:
    """A step that overruns its bound fails with a message naming step and bound."""
    with pytest.raises(SetupFailedError, match="did not initialize within 0 seconds"):
        async with _provider_load_step("demo", "initialize", 0):
            await asyncio.sleep(5)


async def test_load_step_separates_an_inner_timeout_from_its_own_bound() -> None:
    """A bare TimeoutError from the provider's own code is not blamed on the bound."""
    with pytest.raises(SetupFailedError, match="timed out while trying to load"):
        async with _provider_load_step("demo", "load", 30):
            raise TimeoutError


async def test_load_step_names_the_step_for_a_message_less_error() -> None:
    """An exception without a message is not surfaced as just its class name."""
    with pytest.raises(SetupFailedError, match="failed to load: KeyError"):
        async with _provider_load_step("demo", "load", 30):
            raise KeyError


async def test_load_step_leaves_informative_errors_alone() -> None:
    """Errors that carry a message of their own are passed through untouched."""
    with pytest.raises(LoginFailed, match="bad creds"):
        async with _provider_load_step("demo", "load", 30):
            raise LoginFailed("bad creds")
    with pytest.raises(ValueError, match="oops"):
        async with _provider_load_step("demo", "load", 30):
            raise ValueError("oops")
