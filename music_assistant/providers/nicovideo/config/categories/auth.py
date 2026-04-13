"""Authentication configuration category for Nicovideo provider."""

from __future__ import annotations

from music_assistant.providers.nicovideo.config.categories.base import ConfigCategoryBase
from music_assistant.providers.nicovideo.config.factory import ConfigFactory


class AuthConfigCategory(ConfigCategoryBase):
    """Authentication settings category."""

    _auth = ConfigFactory("Authentication")

    mail = _auth.str_config(
        key="mail",
        label="Email",
        default=None,
        description="Your NicoNico account email address.",
    )

    password = _auth.secure_str_or_none_config(
        key="password",
        label="Password",
        description="Your NicoNico account password.",
    )

    mfa = _auth.str_config(
        key="mfa",
        label="MFA Code (One-Time Password)",
        default=None,
        description="Enter the 6-digit confirmation code from your 2-step verification app.",
    )

    user_session = _auth.secure_str_or_none_config(
        key="user_session",
        label="User Session ( 'user_session' in Cookie)",
        description=(
            "Enter the user_session cookie value.\n"
            "If invalid, it will be automatically set from your email and password."
        ),
    )

    def save_user_session(self, value: str) -> None:
        """Save user session to config."""
        self.writer.set_raw_provider_config_value(
            self.provider.instance_id,
            "user_session",
            value,
            True,
        )

    def clear_mfa_code(self) -> None:
        """Clear MFA code after successful use (one-time password should not be reused)."""
        self.writer.set_raw_provider_config_value(
            self.provider.instance_id,
            "mfa",
            None,
            True,
        )
