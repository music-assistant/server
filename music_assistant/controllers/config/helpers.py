"""Pure helper functions for the config controller package."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.constants import SECURE_STRING_SUBSTITUTE
from music_assistant_models.enums import ProviderStatus
from music_assistant_models.errors import (
    AuthenticationFailed,
    AuthenticationRequired,
    InvalidDataError,
    LoginFailed,
    UnsupportedSystemError,
)
from music_assistant_models.errors import (
    InvalidToken as InvalidTokenError,
)

from music_assistant.constants import ENCRYPT_SUFFIX

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


def _reject_encrypted_values(values: dict[str, Any]) -> None:
    """Raise InvalidDataError when a submitted config value is an encrypted string."""
    for key, value in values.items():
        if isinstance(value, str) and value.startswith(ENCRYPT_SUFFIX):
            raise InvalidDataError(f"Invalid value for {key}")


def _mask_encrypted[T](value: T) -> T:
    """Return a copy of the value with every encrypted string replaced by the placeholder."""
    masked: Any = value
    if isinstance(value, str) and value.startswith(ENCRYPT_SUFFIX):
        masked = SECURE_STRING_SUBSTITUTE
    elif isinstance(value, dict):
        masked = {key: _mask_encrypted(item) for key, item in value.items()}
    elif isinstance(value, list):
        masked = [_mask_encrypted(item) for item in value]
    return cast("T", masked)
