"""
Base class/model for a Player within Music Assistant.

All providerspecific players should inherit from this class and implement the required methods.

Note that the serverside Player object is not the same as the clientside Player object,
which is a dataclass in the models package containing the player state.
"""

from __future__ import annotations

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.constants import PLAYER_CONTROL_NATIVE, PLAYER_CONTROL_NONE
from music_assistant_models.enums import ConfigEntryType, PlayerType
from propcache import under_cached_property as cached_property

from music_assistant.constants import (
    CONF_ENTRY_AUTO_PLAY,
    CONF_ENTRY_EXPOSE_PLAYER_TO_HA,
    CONF_ENTRY_EXPOSE_PLAYER_TO_HA_DEFAULT_DISABLED,
    CONF_ENTRY_HIDE_PLAYER_IN_UI_ALWAYS_DEFAULT,
    CONF_ENTRY_HIDE_PLAYER_IN_UI_GROUP_PLAYER,
    CONF_ENTRY_PLAYER_ICON_GROUP,
    CONF_MUTE_CONTROL,
    CONF_POWER_CONTROL,
    CONF_VOLUME_CONTROL,
)

from .player import BASE_CONFIG_ENTRIES, Player


class GroupPlayer(Player):
    """Helper class for a (generic) group player."""

    _attr_type: PlayerType = PlayerType.GROUP

    @cached_property
    def synced_to(self) -> str | None:
        """Return the id of the player this player is synced to (sync leader)."""
        # default implementation: groups can't be synced
        return None

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the player."""
        # Return all base config entries for a group player.
        # Feel free to override but ensure to include the base entries by calling super() first.
        # To override the default config entries, simply define an entry with the same key
        # and it will be used instead of the default one.
        return [
            *BASE_CONFIG_ENTRIES,
            CONF_ENTRY_PLAYER_ICON_GROUP,
            # add player control entries as hidden entries
            ConfigEntry(
                key=CONF_POWER_CONTROL,
                type=ConfigEntryType.STRING,
                label=CONF_POWER_CONTROL,
                default_value=PLAYER_CONTROL_NATIVE,
                hidden=True,
            ),
            ConfigEntry(
                key=CONF_VOLUME_CONTROL,
                type=ConfigEntryType.STRING,
                label=CONF_VOLUME_CONTROL,
                default_value=PLAYER_CONTROL_NATIVE,
                hidden=True,
            ),
            ConfigEntry(
                key=CONF_MUTE_CONTROL,
                type=ConfigEntryType.STRING,
                label=CONF_MUTE_CONTROL,
                # disable mute control for group players for now
                # TODO: work out if all child players support mute control
                default_value=PLAYER_CONTROL_NONE,
                hidden=True,
            ),
            CONF_ENTRY_AUTO_PLAY,
            # add default entries to hide player in UI and expose to HA
            (
                CONF_ENTRY_HIDE_PLAYER_IN_UI_ALWAYS_DEFAULT
                if self.hidden_by_default
                else CONF_ENTRY_HIDE_PLAYER_IN_UI_GROUP_PLAYER
            ),
            (
                CONF_ENTRY_EXPOSE_PLAYER_TO_HA
                if self.expose_to_ha_by_default
                else CONF_ENTRY_EXPOSE_PLAYER_TO_HA_DEFAULT_DISABLED
            ),
        ]

    async def volume_set(self, volume_level: int) -> None:
        """
        Handle VOLUME_SET command on the player.

        :param volume_level: volume level (0..100) to set on the player.
        """
        # Default implementation:
        # This will set the (relative) volume level on all child players.
        # free to override if you want to handle this differently.
        await self.mass.players.set_group_volume(self, volume_level)
