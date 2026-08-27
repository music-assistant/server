"""Helper class to aid scrobblers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar, cast

from music_assistant_models.config_entries import (
    Config,
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
)
from music_assistant_models.enums import ConfigEntryType, MediaType

from music_assistant.helpers.config_entries import PLAYBACK_TARGET_TYPES

if TYPE_CHECKING:
    from music_assistant_models.event import MassEvent
    from music_assistant_models.playback_progress_report import MediaItemPlaybackProgressReport

    from music_assistant import MusicAssistant


class ScrobblerHelper:
    """Base class to aid scrobbling media items."""

    logger: logging.Logger
    config: ScrobblerConfig
    supported_media_types: frozenset[MediaType] | None
    currently_playing: str | None = None
    last_scrobbled: str | None = None
    # Exceptions the concrete scrobble client raises when a submission can't reach
    # the service (network blips, service-side errors). Subclasses set this to their
    # client library's error hierarchy so those are logged and swallowed, while any
    # exception outside the set surfaces as the bug it is.
    scrobble_exceptions: ClassVar[tuple[type[Exception], ...]] = ()

    def __init__(
        self,
        logger: logging.Logger,
        config: ScrobblerConfig | None = None,
        supported_media_types: frozenset[MediaType] | None = None,
    ) -> None:
        """Initialize."""
        self.logger = logger
        self.config = config or ScrobblerConfig(suffix_version=False)
        self.supported_media_types = supported_media_types

    def get_name(self, report: MediaItemPlaybackProgressReport) -> str:
        """Get the track name to use for scrobbling, possibly appended with version info."""
        if self.config.suffix_version and report.version:
            return f"{report.name} ({report.version})"

        return report.name

    def should_scrobble(self, report: MediaItemPlaybackProgressReport) -> bool:
        """Determine if a track should be scrobbled, to be extended later."""
        if self.last_scrobbled == report.uri:
            self.logger.debug("skipped scrobbling due to duplicate event")
            return False

        # ideally we want more precise control
        # but because the event is triggered every 30s
        # and we don't have full queue details to determine
        # the exact context in which the event was fired
        # we can only rely on fully_played for now
        return bool(report.fully_played)

    def _is_configured(self) -> bool:
        """Override if subclass needs specific configuration."""
        return True

    async def _update_now_playing(self, report: MediaItemPlaybackProgressReport) -> None:
        """Send a Now Playing update to the scrobbling service."""

    async def _scrobble(self, report: MediaItemPlaybackProgressReport) -> None:
        """Scrobble."""

    async def _on_mass_media_item_played(self, event: MassEvent) -> None:
        """Media item has finished playing, we'll scrobble the item."""
        if not self._is_configured():
            return

        report: MediaItemPlaybackProgressReport = event.data

        if self.supported_media_types and report.media_type not in self.supported_media_types:
            self.logger.debug("skipped scrobbling for unsupported media type %s", report.media_type)
            return

        # handle optional user_id filtering
        if self.config.mass_userids and report.userid not in self.config.mass_userids:
            self.logger.debug("skipped scrobbling for user %s due to user filter", report.userid)
            return

        # handle optional player_id filtering
        if self.config.mass_playerids and report.player_id not in self.config.mass_playerids:
            self.logger.debug(
                "skipped scrobbling for player %s due to player filter", report.player_id
            )
            return

        # poor mans attempt to detect a song on loop
        if not report.fully_played and report.uri == self.last_scrobbled:
            self.logger.debug(
                "reset _last_scrobbled and _currently_playing because the song was restarted"
            )
            self.last_scrobbled = None
            # reset currently playing to avoid it expiring when looping single songs
            self.currently_playing = None

        async def update_now_playing() -> None:
            try:
                await self._update_now_playing(report)
                self.logger.debug(f"track {report.uri} marked as 'now playing'")
                self.currently_playing = report.uri
            except self.scrobble_exceptions:
                self.logger.exception("Error while marking track as 'now playing'")

        async def scrobble() -> None:
            try:
                await self._scrobble(report)
                self.last_scrobbled = report.uri
            except self.scrobble_exceptions:
                self.logger.exception("Error while scrobbling track")

        # update now playing if needed
        if report.is_playing and (
            self.currently_playing is None or self.currently_playing != report.uri
        ):
            await update_now_playing()

        if self.should_scrobble(report):
            await scrobble()


CONF_VERSION_SUFFIX = "suffix_version"
CONF_SCROBBLE_USERS = "scrobble_users"
CONF_SCROBBLE_PLAYERS = "scrobble_players"


class ScrobblerConfig:
    """Shared configuration options for scrobblers."""

    def __init__(
        self,
        suffix_version: bool,
        mass_userids: list[str] | None = None,
        mass_playerids: list[str] | None = None,
    ) -> None:
        """Initialize."""
        self.suffix_version = suffix_version
        self.mass_userids = mass_userids or []
        self.mass_playerids = mass_playerids or []

    @staticmethod
    async def get_shared_config_entries(
        mass: MusicAssistant, values: dict[str, ConfigValueType] | None
    ) -> list[ConfigEntry]:
        """Shared config entries."""
        return [
            ConfigEntry(
                key=CONF_VERSION_SUFFIX,
                type=ConfigEntryType.BOOLEAN,
                required=True,
                default_value=True,
                value=values.get(CONF_VERSION_SUFFIX) if values else None,
            ),
            # User and player filter options for scrobbling providers
            await create_scrobble_users_config_entry(mass),
            create_scrobble_players_config_entry(mass),
        ]

    @staticmethod
    def create_from_config(config: Config) -> ScrobblerConfig:
        """Extract relevant shared config values."""
        return ScrobblerConfig(
            suffix_version=bool(config.get_value(CONF_VERSION_SUFFIX, True)),
            mass_userids=cast("list[str]", config.get_value(CONF_SCROBBLE_USERS, [])),
            mass_playerids=cast("list[str]", config.get_value(CONF_SCROBBLE_PLAYERS, [])),
        )


async def create_scrobble_users_config_entry(mass: MusicAssistant) -> ConfigEntry:
    """Create a reusable configentry to specify a userlist for scrobbling providers."""
    # User options for scrobble filtering
    ma_user_list = await mass.webserver.auth.list_users()  # excludes system users
    ma_user_list = [user for user in ma_user_list if user.enabled]
    user_options = [
        ConfigValueOption(user.user_id, title=user.display_name or user.username)
        for user in ma_user_list
    ]
    return ConfigEntry(
        key=CONF_SCROBBLE_USERS,
        type=ConfigEntryType.STRING,
        required=False,
        options=user_options,
        multi_value=True,
        default_value=[],
    )


def create_scrobble_players_config_entry(mass: MusicAssistant) -> ConfigEntry:
    """Create a reusable configentry to specify a player list for scrobbling providers."""
    ma_player_list = sorted(
        mass.players.all_players(return_unavailable=True, return_disabled=False),
        key=lambda player: player.display_name.lower(),
    )
    player_options = [
        ConfigValueOption(player.player_id, title=player.display_name)
        for player in ma_player_list
        if player.type in PLAYBACK_TARGET_TYPES
    ]
    return ConfigEntry(
        key=CONF_SCROBBLE_PLAYERS,
        type=ConfigEntryType.STRING,
        required=False,
        options=player_options,
        multi_value=True,
        default_value=[],
    )
