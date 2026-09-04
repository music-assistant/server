"""Compatibility helpers for borrowing Yandex Music credentials."""

from __future__ import annotations

from typing import Any

from ya_passport_auth import SecretStr
from ya_passport_auth.ma import BorrowedCredentialSource


def _secret_or_none(value: object) -> SecretStr | None:
    """Return a non-empty value as a protected secret."""
    if isinstance(value, SecretStr):
        return value if value.get_secret() else None
    return SecretStr(str(value)) if value else None


class YandexMusicCredentialSource(BorrowedCredentialSource):
    """Read Yandex Music credentials from setup data with legacy fallback."""

    def __init__(self, mass: Any, instance_id: str) -> None:
        """Initialize the credential source for a linked provider instance."""
        super().__init__(mass, instance_id)
        self._station_mass = mass

    def read_tokens(self) -> tuple[SecretStr | None, SecretStr | None]:
        """Return owner tokens from legacy config or guided setup data."""
        music_token, x_token = super().read_tokens()
        if music_token is not None or x_token is not None:
            return music_token, x_token

        owner = self._station_mass.get_provider(self.instance_id, return_unavailable=True)
        get_setup_value = getattr(owner, "get_setup_value", None)
        if not callable(get_setup_value):
            return None, None
        return (
            _secret_or_none(get_setup_value("token")),
            _secret_or_none(get_setup_value("x_token")),
        )
