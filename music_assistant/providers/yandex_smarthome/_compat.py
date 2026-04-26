"""Compatibility shim for ya-passport-auth.

Provides a single source of `SecretStr` for the provider. When `ya-passport-auth`
is installed (the normal runtime case — declared in manifest.json), we re-export
the real implementation. When it's missing (bare test envs, pre-install linting)
we expose a minimal drop-in so importing the provider package doesn't crash.

Centralized here to avoid duplicating the fallback across modules.
"""

from __future__ import annotations

try:
    from ya_passport_auth import SecretStr
except ImportError:

    class SecretStr:  # type: ignore[no-redef]
        """Minimal fallback when ya-passport-auth is not yet installed."""

        def __init__(self, value: str) -> None:
            """Initialize with a secret value."""
            if not value:
                raise ValueError("SecretStr value must not be empty")
            self._value = value

        def get_secret(self) -> str:
            """Return the secret value."""
            return self._value


__all__ = ["SecretStr"]
