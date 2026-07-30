"""Pure helper functions for the config controller package."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderStatus
from music_assistant_models.errors import (
    AuthenticationFailed,
    AuthenticationRequired,
    LoginFailed,
    UnsupportedSystemError,
)
from music_assistant_models.errors import (
    InvalidToken as InvalidTokenError,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import (
        ConfigEntry,
        ProviderConfig,
    )


def _with_translation_owner(
    entries: list[ConfigEntry],
    owner: str,
) -> list[ConfigEntry]:
    """Return entry copies stamped with the owner namespace used to resolve their strings."""
    result: list[ConfigEntry] = []
    for entry in entries:
        # replace() returns a copy so we never mutate the shared (often module-level) entry defs.
        # An entry that already declares an owner (e.g. an injected protocol entry that belongs to
        # its origin provider, not the host player) keeps it; everything else gets the passed owner.
        result.append(replace(entry, translation_owner=entry.translation_owner or owner))
    return result


_AUTH_ERROR_CODES = frozenset(
    {
        AuthenticationRequired.error_code,
        AuthenticationFailed.error_code,
        LoginFailed.error_code,
        InvalidTokenError.error_code,
    }
)


def _provider_status(conf: ProviderConfig, is_loaded: bool) -> ProviderStatus:
    """Derive the (lifecycle) status of a provider from its config and load state."""
    if not conf.enabled:
        return ProviderStatus.DISABLED
    # a recorded error wins over being loaded: a provider that hit a problem the user has to
    # act on (e.g. one unloading itself after an auth failure) must not read as healthy, or
    # the UI has no way to point at it - the status is what flags it in the providers list
    if conf.last_error is not None:
        if conf.last_error.error_code in _AUTH_ERROR_CODES:
            return ProviderStatus.AUTH_REQUIRED
        if conf.last_error.error_code == UnsupportedSystemError.error_code:
            return ProviderStatus.INCOMPATIBLE
        return ProviderStatus.ERROR
    if is_loaded:
        # runtime (un)availability of a loaded provider is conveyed via ProviderInstance.available
        return ProviderStatus.LOADED
    return ProviderStatus.LOADING
