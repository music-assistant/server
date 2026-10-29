"""Authentication configuration category for Nicovideo provider."""

from __future__ import annotations

from music_assistant.providers.nicovideo.config.categories.base import ConfigCategoryBase
from music_assistant.providers.nicovideo.config.factory import ConfigFactory


class AuthConfigCategory(ConfigCategoryBase):
    """Authentication settings category."""

    _auth = ConfigFactory("Authentication")

    user_session = _auth.secure_str_or_none_config(
        key="user_session",
        label="User Session ( 'user_session' in Cookie)",
        description=(
            "Log in to nicovideo.jp in your browser and copy the value of its "
            "'user_session' cookie (e.g. from the browser's developer tools). "
            "Enter a new value here when the session expires."
        ),
    )

    def save_user_session(self, value: str) -> None:
        """Save user session to the setup data."""
        self.provider._update_setup_data("user_session", value)
