"""Tests for provider status derivation and structured error building."""

from __future__ import annotations

from music_assistant_models.config_entries import ProviderConfig, ProviderError
from music_assistant_models.enums import ProviderStatus, ProviderType
from music_assistant_models.errors import (
    AuthenticationRequired,
    LoginFailed,
    SetupFailedError,
    UnsupportedSystemError,
)

from music_assistant.controllers.config.helpers import _provider_status
from music_assistant.mass import _provider_error_from_exc


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
