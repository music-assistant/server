"""Configuration utilities for NiconicoMusicProvider."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast

from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueOption,
)
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.controllers.config import ConfigController
from music_assistant.providers.niconico.constants import (
    CONF_AUTO_LIKE_ON_LIBRARY_ADD,
    CONF_FOLLOWING_ACTIVITIES_COUNT,
    CONF_HISTORY_COUNT,
    CONF_INCLUDE_FOLLOWING_MYLISTS,
    CONF_INCLUDE_FOLLOWING_MYLISTS_TRACKS,
    CONF_INCLUDE_OWN_MYLISTS_TRACKS,
    CONF_MFA,
    CONF_RECOMMENDATION_COUNT,
    CONF_REQUIRED_TAGS_FOR_RECOMMENDATIONS,
    CONF_SENSITIVE_CONTENTS,
    CONF_USER_SESSION,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.provider import Provider


class AuthCredentials:
    """Type for authentication credentials."""

    def __init__(
        self, username: str | None, password: str | None, mfa: str | None, user_session: str | None
    ) -> None:
        """Initialize authentication credentials."""
        self.username = username
        self.password = password
        self.mfa = mfa
        self.user_session = user_session


class NiconicoConfig:
    """Configuration helper for Niconico provider settings."""

    def __init__(self, provider: Provider) -> None:
        """Initialize config helper with provider instance."""
        self.provider = provider

    @property
    def reader(self) -> ProviderConfig:
        """Get the config reader interface."""
        return self.provider.config

    @property
    def writer(self) -> ConfigController:
        """Get the config writer interface."""
        return self.provider.mass.config

    def _get_config_value(self, key: str, default: ConfigValueType = None) -> ConfigValueType:
        """Get config value with optional default."""
        value = self.reader.get_value(key)
        return value if value is not None else default

    def _set_config_value(self, key: str, value: ConfigValueType, save: bool = True) -> None:
        """Set config value."""
        self.writer.set_raw_provider_config_value(
            self.provider.instance_id,
            key,
            value,
            save,
        )

    def set(self, key: str, value: ConfigValueType, save: bool = True) -> None:
        """Set configuration value by key."""
        self._set_config_value(key, value, save)

    def get_str(self, key: str, default: ConfigValueType = "") -> str:
        """Get configuration value by key."""
        return str(self._get_config_value(key, default))

    def get_str_or_none(self, key: str) -> str | None:
        """Get configuration value by key."""
        value = self._get_config_value(key, None)
        return str(value) if value is not None else None

    def get_int(self, key: str, default: int = 0, min_val: int = 1, max_val: int = 100) -> int:
        """Get integer config value with validation."""
        value = self._get_config_value(key)
        if not isinstance(value, int) or value < min_val:
            return default
        return min(value, max_val)

    def get_bool(self, key: str, default: bool = False) -> bool:
        """Get boolean config value."""
        return bool(self._get_config_value(key, default))

    def _cast_sensitive_contents(self, config_value: object) -> Literal["mask", "filter"] | None:
        """Cast configuration value to valid sensitive content option."""
        return (
            cast("Literal['mask', 'filter']", config_value)
            if config_value in ("mask", "filter")
            else None
        )

    def get_required_tags_for_recommendations(self) -> list[str]:
        """Get required tags for recommendations from provider config."""
        tags_config = self._get_config_value(CONF_REQUIRED_TAGS_FOR_RECOMMENDATIONS)
        if not tags_config or not isinstance(tags_config, str):
            return []

        # Split by comma and clean up whitespace
        return [tag.strip() for tag in tags_config.split(",") if tag.strip()]

    def get_recommendation_count(self) -> int:
        """Get target recommendation count from provider config."""
        return self.get_int(CONF_RECOMMENDATION_COUNT, default=25, max_val=100)

    def get_history_count(self) -> int:
        """Get target history count from provider config."""
        return self.get_int(CONF_HISTORY_COUNT, default=50, max_val=100)

    def get_following_activities_count(self) -> int:
        """Get target following activities count from provider config."""
        return self.get_int(CONF_FOLLOWING_ACTIVITIES_COUNT, default=30, max_val=100)

    def get_auto_like_on_library_add(self) -> bool:
        """Get auto-like on library add setting."""
        return self.get_bool(CONF_AUTO_LIKE_ON_LIBRARY_ADD)

    def get_include_following_mylists(self) -> bool:
        """Get include following mylists setting."""
        return self.get_bool(CONF_INCLUDE_FOLLOWING_MYLISTS)

    def get_include_following_mylists_tracks(self) -> bool:
        """Get include following mylists tracks setting."""
        return self.get_bool(CONF_INCLUDE_FOLLOWING_MYLISTS_TRACKS)

    def get_include_own_mylists_tracks(self) -> bool:
        """Get include own mylists tracks setting."""
        return self.get_bool(CONF_INCLUDE_OWN_MYLISTS_TRACKS)

    def get_auth_credentials(self) -> AuthCredentials:
        """Get authentication credentials."""
        return AuthCredentials(
            username=self.get_str_or_none(CONF_USERNAME),
            password=self.get_str_or_none(CONF_PASSWORD),
            mfa=self.get_str_or_none(CONF_MFA),
            user_session=self.get_str_or_none(CONF_USER_SESSION),
        )

    def get_sensitive_contents_handling(self) -> str:
        """Get sensitive contents handling setting."""
        return self.get_str(CONF_SENSITIVE_CONTENTS, "mask")

    def get_sensitive_contents_config(self) -> Literal["mask", "filter"] | None:
        """Get and cast sensitive contents configuration value."""
        raw_value = self.get_sensitive_contents_handling()
        return self._cast_sensitive_contents(raw_value)

    def save_user_session(self, user_session: str) -> None:
        """Save user session to config."""
        self.set(CONF_USER_SESSION, user_session)

    def clear_mfa_code(self) -> None:
        """Clear MFA code after successful use (one-time password should not be reused)."""
        self.set(CONF_MFA, None)


async def get_config_entries_impl(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    return (
        ConfigEntry(key=CONF_USERNAME, type=ConfigEntryType.STRING, label="Mail", required=False),
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Password",
            required=False,
            description="The Login password.",
        ),
        ConfigEntry(
            key=CONF_MFA,
            type=ConfigEntryType.STRING,
            label="MFA Code(one time password)",
            required=False,
            description=(
                "Enter the 6-digit confirmation code generated by your 2-step "
                "verification app (e.g., Google Authenticator)."
            ),
        ),
        ConfigEntry(
            key=CONF_USER_SESSION,
            type=ConfigEntryType.SECURE_STRING,
            label="User Session(cookie)",
            required=False,
            description=(
                "Enter the user_session obtained from the cookie."
                "If invalid, it will be set automatically from your email and password."
            ),
        ),
        ConfigEntry(
            key=CONF_SENSITIVE_CONTENTS,
            type=ConfigEntryType.STRING,
            label="Sensitive Content Handling",
            required=False,
            default_value=None,
            options=[
                ConfigValueOption(title="Default (No filtering)", value=None),
                ConfigValueOption(title="Mask sensitive content", value="mask"),
                ConfigValueOption(title="Filter out sensitive content", value="filter"),
            ],
            description=(
                "Choose how to handle sensitive content in searches and recommendations. "
                "'Mask' will show sensitive content with warnings, "
                "'Filter' will hide it completely."
            ),
            category="content",
        ),
        ConfigEntry(
            key=CONF_AUTO_LIKE_ON_LIBRARY_ADD,
            type=ConfigEntryType.BOOLEAN,
            label="Auto-like when adding to library",
            required=False,
            default_value=True,
            description=(
                "Automatically like videos on NicoNico when adding tracks to your "
                "Music Assistant library. This helps keep your NicoNico account "
                "synchronized with your music preferences."
            ),
            category="content",
        ),
        ConfigEntry(
            key=CONF_INCLUDE_OWN_MYLISTS_TRACKS,
            type=ConfigEntryType.BOOLEAN,
            label="Include own mylists tracks in library",
            required=False,
            default_value=True,
            description=(
                "Include tracks from your own mylists in your library tracks. "
                "This allows you to manage whether playlist tracks appear in your main "
                "track library."
            ),
            category="content",
        ),
        ConfigEntry(
            key=CONF_INCLUDE_FOLLOWING_MYLISTS,
            type=ConfigEntryType.BOOLEAN,
            label="Include following users' mylists",
            required=False,
            default_value=False,
            description=(
                "Include mylists from users you follow in your library playlists. "
                "These playlists will be read-only and marked as not editable."
            ),
            category="content",
        ),
        ConfigEntry(
            key=CONF_INCLUDE_FOLLOWING_MYLISTS_TRACKS,
            type=ConfigEntryType.BOOLEAN,
            label="Include following users' mylists tracks in library",
            required=False,
            default_value=False,
            description=(
                "Include tracks from mylists of users you follow in your library tracks. "
                "This is separate from including the mylists themselves as playlists."
            ),
            category="content",
        ),
        ConfigEntry(
            key=CONF_REQUIRED_TAGS_FOR_RECOMMENDATIONS,
            type=ConfigEntryType.STRING,
            label="Required tags for recommendations",
            required=False,
            default_value="",
            description=(
                "Comma-separated list of tags that tracks must have at least one of "
                "to appear in recommendations and similar tracks. Leave empty to disable "
                "tag filtering. Example: 'VOCALOID,音楽,ボカロ'"
            ),
            category="recommendations",
        ),
        ConfigEntry(
            key=CONF_RECOMMENDATION_COUNT,
            type=ConfigEntryType.INTEGER,
            label="Number of recommendations",
            required=False,
            default_value=25,
            description=(
                "Number of tracks to fetch for recommendations. "
                "If tag filtering is enabled, the system will automatically "
                "fetch additional tracks to meet this target count."
            ),
            category="recommendations",
            range=(1, 100),
        ),
        ConfigEntry(
            key=CONF_HISTORY_COUNT,
            type=ConfigEntryType.INTEGER,
            label="Number of history tracks",
            required=False,
            default_value=50,
            description=("Number of recently watched tracks to show in recommendations."),
            category="recommendations",
            range=(1, 100),
        ),
        ConfigEntry(
            key=CONF_FOLLOWING_ACTIVITIES_COUNT,
            type=ConfigEntryType.INTEGER,
            label="Number of following activity tracks",
            required=False,
            default_value=30,
            description=("Number of tracks from following activities to show in recommendations."),
            category="recommendations",
            range=(1, 100),
        ),
    )
