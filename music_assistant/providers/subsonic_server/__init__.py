"""
Subsonic API plugin for Music Assistant.

Exposes the Music Assistant library through the (Open)Subsonic REST API on ``/rest/*`` of the
webserver, so Subsonic clients can browse it and control a Music Assistant player through the
Subsonic jukebox mode. Audio is only ever played on Music Assistant players: the ``stream`` and
``download`` endpoints are not offered.

Subsonic clients authenticate with ``md5(password + salt)``, which cannot be checked against the
hashed Music Assistant passwords. Each user therefore gets a separate Subsonic app password,
derived from a secret stored (encrypted) in the plugin's setup data.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from music_assistant_models.auth import Scope, UserRole
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_user,
    has_scope,
)
from music_assistant.models.plugin import PluginProvider

from .api import SubsonicApi
from .auth import derive_app_password, generate_secret
from .constants import (
    CONF_ACTION_REGENERATE_PASSWORDS,
    CONF_APP_PASSWORD_PREFIX,
    CONF_APP_PASSWORD_TRANSLATION_KEY,
    CONF_JUKEBOX_PLAYER,
    CONF_JUKEBOX_PLAYER_NONE,
    REST_PREFIX,
    SETUP_KEY_SECRET,
)

if TYPE_CHECKING:
    from music_assistant_models.auth import User
    from music_assistant_models.config_entries import ConfigActionResult, ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES: set[ProviderFeature] = set()


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return SubsonicServerProvider(mass, manifest, config, SUPPORTED_FEATURES)


class SubsonicServerProvider(PluginProvider):
    """Plugin that serves the Subsonic API."""

    _unregister_route: Callable[[], None] | None = None

    @property
    def jukebox_player_id(self) -> str | None:
        """Return the id of the player Subsonic clients control in jukebox mode, if any."""
        value = self.config.get_value(CONF_JUKEBOX_PLAYER)
        if not value or value == CONF_JUKEBOX_PLAYER_NONE:
            return None
        return str(value)

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the (options) config entries for this provider instance."""
        caller = get_current_user()
        caller_manages_users = caller is None or has_scope(caller, Scope.USERS_MANAGE)
        entries: list[ConfigEntry] = [
            ConfigEntry(
                key=CONF_JUKEBOX_PLAYER,
                type=ConfigEntryType.STRING,
                default_value=CONF_JUKEBOX_PLAYER_NONE,
                required=False,
                options=[
                    ConfigValueOption(CONF_JUKEBOX_PLAYER_NONE),
                    *[
                        ConfigValueOption(player.player_id, title=player.display_name)
                        for player in sorted(
                            self.mass.players.all_players(False, False),
                            key=lambda p: p.display_name.lower(),
                        )
                    ],
                ],
            )
        ]
        entries.extend(
            ConfigEntry(
                key=f"{CONF_APP_PASSWORD_PREFIX}{user.user_id}",
                type=ConfigEntryType.STRING,
                translation_key=CONF_APP_PASSWORD_TRANSLATION_KEY,
                translation_params=[user.username],
                default_value=self.get_app_password(user.user_id),
                required=False,
                read_only=True,
            )
            for user in await self._users_visible_to(caller)
        )
        if caller_manages_users:
            entries.append(
                ConfigEntry(
                    key=CONF_ACTION_REGENERATE_PASSWORDS,
                    type=ConfigEntryType.ACTION,
                    action=CONF_ACTION_REGENERATE_PASSWORDS,
                    required=False,
                )
            )
        return tuple(entries)

    async def handle_config_action(
        self, action: str
    ) -> tuple[ConfigEntry, ...] | ConfigActionResult | None:
        """Handle a pressed action button from the options page."""
        if action == CONF_ACTION_REGENERATE_PASSWORDS:
            self._update_setup_data(SETUP_KEY_SECRET, generate_secret())
            return await self.get_config_entries()
        return await super().handle_config_action(action)

    async def loaded_in_mass(self) -> None:
        """Register the Subsonic API routes once the provider is loaded."""
        api = SubsonicApi(self)
        self._unregister_route = self.mass.webserver.register_dynamic_route(
            f"{REST_PREFIX}*", api.handle_request
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Remove the Subsonic API routes."""
        if self._unregister_route is not None:
            self._unregister_route()
            self._unregister_route = None

    def get_app_password(self, user_id: str) -> str:
        """
        Return the Subsonic app password of a user.

        :param user_id: The Music Assistant user id.
        """
        return derive_app_password(self._get_secret(), user_id)

    async def _users_visible_to(self, caller: User | None) -> list[User]:
        if caller is not None and not has_scope(caller, Scope.USERS_READ):
            return [caller]
        return [
            user
            for user in await self.mass.webserver.auth.list_users()
            if user.enabled and user.role != UserRole.SERVICE
        ]

    def _get_secret(self) -> str:
        secret = self.get_setup_value(SETUP_KEY_SECRET)
        if not secret:
            secret = generate_secret()
            self._update_setup_data(SETUP_KEY_SECRET, secret)
        return str(secret)
