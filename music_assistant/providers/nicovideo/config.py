"""Configuration utilities for NicovideoMusicProvider."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from music_assistant_models.config_entries import (
    ConfigEntry,
)
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_PASSWORD
from music_assistant.controllers.config import ConfigController
from music_assistant.providers.nicovideo.constants import (
    CONF_AUTO_LIKE_ON_LIBRARY_ADD,
    CONF_FOLLOWING_ACTIVITIES_COUNT,
    CONF_HISTORY_COUNT,
    CONF_INCLUDE_FOLLOWED_MYLISTS,
    CONF_INCLUDE_FOLLOWED_MYLISTS_TRACKS,
    CONF_INCLUDE_LIBRARY_TRACK_ARTISTS,
    CONF_INCLUDE_OWN_MYLISTS_TRACKS,
    CONF_INCLUDE_OWN_SERIES_ALBUMS,
    CONF_INCLUDE_OWN_VIDEOS_TRACKS,
    CONF_MAIL,
    CONF_MFA,
    CONF_RECOMMENDATION_COUNT,
    CONF_RECOMMENDATION_FILTER_TAGS,
    CONF_SENSITIVE_CONTENTS,
    CONF_TAG_RECOMMENDATION_NEW_TRACKS_TAGS,
    CONF_TAG_RECOMMENDATION_TAGS,
    CONF_USE_FOLLOW_UNFOLLOW_ARTISTS,
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


class NicovideoConfig:
    """Configuration helper for nicovideo provider settings."""

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

    def get_recommendation_filter_tags(self) -> list[str]:
        """Get filter tags for recommendations from provider config."""
        tags_config = self._get_config_value(CONF_RECOMMENDATION_FILTER_TAGS)
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

    def get_tag_recommendation_tags(self) -> list[str]:
        """Get tags for tag-based recommendation search from provider config."""
        tags_config = self._get_config_value(CONF_TAG_RECOMMENDATION_TAGS)
        if not tags_config or not isinstance(tags_config, str):
            return []
        # Split by comma and clean up whitespace
        return [tag.strip() for tag in tags_config.split(",") if tag.strip()]

    def get_tag_recommendation_new_tracks_tags(self) -> list[str]:
        """Get tags for tag-based new tracks search from provider config."""
        tags_config = self._get_config_value(CONF_TAG_RECOMMENDATION_NEW_TRACKS_TAGS)
        if not tags_config or not isinstance(tags_config, str):
            return []
        # Split by comma and clean up whitespace
        return [tag.strip() for tag in tags_config.split(",") if tag.strip()]

    def get_auto_like_on_library_add(self) -> bool:
        """Get auto-like on library add setting."""
        return self.get_bool(CONF_AUTO_LIKE_ON_LIBRARY_ADD)

    def get_use_follow_unfollow_artists(self) -> bool:
        """Get use follow/unfollow artists setting."""
        return self.get_bool(CONF_USE_FOLLOW_UNFOLLOW_ARTISTS)

    def get_include_followed_mylists(self) -> bool:
        """Get include followed mylists setting."""
        return self.get_bool(CONF_INCLUDE_FOLLOWED_MYLISTS)

    def get_include_followed_mylists_tracks(self) -> bool:
        """Get include followed mylists tracks setting."""
        return self.get_bool(CONF_INCLUDE_FOLLOWED_MYLISTS_TRACKS)

    def get_include_own_series_albums(self) -> bool:
        """Get include own series as albums setting."""
        return self.get_bool(CONF_INCLUDE_OWN_SERIES_ALBUMS)

    def get_include_own_videos_tracks(self) -> bool:
        """Get include own videos as tracks setting."""
        return self.get_bool(CONF_INCLUDE_OWN_VIDEOS_TRACKS)

    def get_include_own_mylists_tracks(self) -> bool:
        """Get include own mylists tracks setting."""
        return self.get_bool(CONF_INCLUDE_OWN_MYLISTS_TRACKS)

    def get_include_library_track_artists(self) -> bool:
        """Get include library track artists setting."""
        return self.get_bool(CONF_INCLUDE_LIBRARY_TRACK_ARTISTS, default=True)

    def get_auth_credentials(self) -> AuthCredentials:
        """Get authentication credentials."""
        return AuthCredentials(
            username=self.get_str_or_none(CONF_MAIL),
            password=self.get_str_or_none(CONF_PASSWORD),
            mfa=self.get_str_or_none(CONF_MFA),
            user_session=self.get_str_or_none(CONF_USER_SESSION),
        )

    def get_sensitive_contents_handling(self) -> str | None:
        """Get sensitive contents handling setting."""
        value = self._get_config_value(CONF_SENSITIVE_CONTENTS, None)
        return str(value) if value is not None else None

    def get_sensitive_contents_config(self) -> Literal["mask", "filter"] | None:
        """Get and cast sensitive contents configuration value."""
        # Since it seems to be automatically controlled by the display settings of niconico users,
        # “mask” is always returned here.
        return "mask"

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
        ConfigEntry(
            key=CONF_MAIL,
            type=ConfigEntryType.STRING,
            label="Email",
            required=False,
            description="Your NicoNico account email address.",
            category="Authentication",
        ),
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Password",
            required=False,
            description="Your NicoNico account password.",
            category="Authentication",
        ),
        ConfigEntry(
            key=CONF_MFA,
            type=ConfigEntryType.STRING,
            label="MFA Code (One-Time Password)",
            required=False,
            description="Enter the 6-digit confirmation code from your 2-step verification app.",
            category="Authentication",
        ),
        ConfigEntry(
            key=CONF_USER_SESSION,
            type=ConfigEntryType.SECURE_STRING,
            label="User Session (Cookie)",
            required=False,
            description=(
                "Enter the user_session cookie value.\n"
                "If invalid, it will be automatically set from your email and password."
            ),
            category="Authentication",
        ),
        # Basic integration features
        ConfigEntry(
            key=CONF_AUTO_LIKE_ON_LIBRARY_ADD,
            type=ConfigEntryType.BOOLEAN,
            label="Auto-like when adding to library",
            required=False,
            default_value=True,
            description=(
                "Automatically like videos on NicoNico when adding tracks to your "
                "Music Assistant library.\n"
                "Tracks removed from the library will not be unliked on NicoNico.\n"
            ),
            category="Content",
        ),
        ConfigEntry(
            key=CONF_USE_FOLLOW_UNFOLLOW_ARTISTS,
            type=ConfigEntryType.BOOLEAN,
            label="Use follow/unfollow artists on NicoNico",
            required=False,
            default_value=False,
            description=(
                "Enable follow/unfollow functionality when adding/removing artists from your "
                "library.\n"
                "When enabled, adding an artist requires successfully following them on NicoNico.\n"
                "⚠️ NicoNico limits following to 800 users."
            ),
            category="Content",
        ),
        ConfigEntry(
            key=CONF_INCLUDE_LIBRARY_TRACK_ARTISTS,
            type=ConfigEntryType.BOOLEAN,
            label="Include artists from library tracks",
            required=False,
            default_value=True,
            description=(
                "Include artists from your library tracks in the artist library.\n"
                "When enabled, all artists from tracks in your library will appear "
                "in the artist section, even if you don't explicitly follow them on NicoNico."
            ),
            category="Content",
        ),
        # Own content settings
        ConfigEntry(
            key=CONF_INCLUDE_OWN_MYLISTS_TRACKS,
            type=ConfigEntryType.BOOLEAN,
            label="Include tracks from own mylists",
            required=False,
            default_value=True,
            description=(
                "Include tracks from your own mylists in your library tracks.\n"
                "This allows you to manage whether playlist tracks appear in your main "
                "track library."
            ),
            category="Content",
        ),
        ConfigEntry(
            key=CONF_INCLUDE_OWN_SERIES_ALBUMS,
            type=ConfigEntryType.BOOLEAN,
            label="Include albums from own uploaded series",
            required=False,
            default_value=False,
            description=(
                "Include your own uploaded series as albums in your library.\n"
                "This allows you to manage whether your created series appear in your "
                "album library."
            ),
            category="Content",
        ),
        ConfigEntry(
            key=CONF_INCLUDE_OWN_VIDEOS_TRACKS,
            type=ConfigEntryType.BOOLEAN,
            label="Include tracks from own uploaded videos",
            required=False,
            default_value=False,
            description=(
                "Include your own uploaded videos as tracks in your library.\n"
                "This allows you to manage whether your uploaded content appears in your "
                "track library."
            ),
            category="Content",
        ),
        # Followed content settings
        ConfigEntry(
            key=CONF_INCLUDE_FOLLOWED_MYLISTS,
            type=ConfigEntryType.BOOLEAN,
            label="Include playlists from followed mylists",
            required=False,
            default_value=False,
            description=(
                "Include mylists you directly follow in your library playlists.\n"
                "These playlists will be read-only and marked as not editable."
            ),
            category="Content",
        ),
        ConfigEntry(
            key=CONF_INCLUDE_FOLLOWED_MYLISTS_TRACKS,
            type=ConfigEntryType.BOOLEAN,
            label="Include tracks from followed mylists",
            required=False,
            default_value=False,
            description=(
                "Include tracks from mylists you directly follow in your library tracks.\n"
                "This refers to mylists that you have explicitly followed,\n"
                "not to mylists from users you have followed."
            ),
            category="Content",
        ),
        ConfigEntry(
            key=CONF_RECOMMENDATION_FILTER_TAGS,
            type=ConfigEntryType.STRING,
            label="Filter tags for recommendations / similar tracks",
            required=False,
            default_value="",
            description=(
                "Comma-separated list of tags that tracks must have at least one of "
                "to appear in main recommendations and similar tracks.\n"
                "Leave empty to disable tag filtering.\n"
                "Not used for tag-based recommendations.\n"
                "Example: 'VOCALOID,音楽,ボカロ'"
            ),
            category="Recommendations",
        ),
        ConfigEntry(
            key=CONF_RECOMMENDATION_COUNT,
            type=ConfigEntryType.INTEGER,
            label="Number of recommendations",
            required=False,
            default_value=25,
            description=(
                "Number of tracks to fetch for recommendations.\n"
                "If tag filtering is enabled, the system will automatically "
                "fetch additional tracks to meet this target count."
            ),
            category="Recommendations",
            range=(1, 100),
        ),
        ConfigEntry(
            key=CONF_TAG_RECOMMENDATION_TAGS,
            type=ConfigEntryType.STRING,
            label="Tags for tag-based recommendations",
            required=False,
            default_value="",
            description=(
                "Comma-separated list of tags to search for recommended tracks.\n"
                "Tracks with these tags will be shown in 'Tag-based Recommendations' section.\n"
                "Leave empty to disable tag-based recommendations.\n"
                "Example: 'VOCALOID,音楽,ボカロ'"
            ),
            category="Recommendations",
        ),
        ConfigEntry(
            key=CONF_TAG_RECOMMENDATION_NEW_TRACKS_TAGS,
            type=ConfigEntryType.STRING,
            label="Tags for tag-based new tracks recommendations",
            required=False,
            default_value="",
            description=(
                "Comma-separated list of tags to search for new tracks.\n"
                "Latest tracks with these tags will be shown in 'New Tracks by Tags' section.\n"
                "Leave empty to disable tag-based new tracks.\n"
                "Example: 'VOCALOID,音楽,ボカロ'"
            ),
            category="Recommendations",
        ),
        ConfigEntry(
            key=CONF_HISTORY_COUNT,
            type=ConfigEntryType.INTEGER,
            label="Number of history tracks",
            required=False,
            default_value=50,
            description=("Number of recently watched tracks to show in recommendations."),
            category="Recommendations",
            range=(1, 100),
        ),
        ConfigEntry(
            key=CONF_FOLLOWING_ACTIVITIES_COUNT,
            type=ConfigEntryType.INTEGER,
            label="Number of following activity tracks",
            required=False,
            default_value=30,
            description=("Number of tracks from following activities to show in recommendations."),
            category="Recommendations",
            range=(1, 100),
        ),
    )
