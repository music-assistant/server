"""Helpers for building shared configuration entries."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant.mass import MusicAssistant

# the Music Assistant players a per-player plugin exposes a device for
CONF_CONNECTED_PLAYERS = "connected_players"
# how the advertised device name is composed from the connected player's name
CONF_PUBLISH_NAME_TEMPLATE = "publish_name_template"
PUBLISH_NAME_PLAYER_MASS = "player_mass"
PUBLISH_NAME_PLAYER = "player"
PUBLISH_NAME_MASS_PLAYER = "mass_player"
PUBLISH_NAME_TEMPLATES = (
    PUBLISH_NAME_PLAYER_MASS,
    PUBLISH_NAME_PLAYER,
    PUBLISH_NAME_MASS_PLAYER,
)


def create_player_selector(
    mass: MusicAssistant,
    key: str,
    selected_value: ConfigValueType = None,
) -> ConfigEntry:
    """
    Return a required single-player selector populated from the available players.

    :param mass: The Music Assistant instance providing the current players.
    :param key: The config entry key for the selected player.
    :param selected_value: Previously selected player id to prefill when still available.
    """
    options = _player_options(mass)
    selected = selected_value if any(option.value == selected_value for option in options) else None
    return ConfigEntry(
        key=key,
        type=ConfigEntryType.STRING,
        required=True,
        default_value=selected,
        value=selected,
        options=options,
    )


def create_connected_players_entry(
    mass: MusicAssistant, selected: list[str] | None = None
) -> ConfigEntry:
    """
    Return the multi-select of players a per-player plugin exposes a device for.

    :param mass: The Music Assistant instance providing the current players.
    :param selected: Currently selected player ids; ids not registered right now stay
        listed (with the id as title) so saving the form does not silently drop them.
    """
    options = _player_options(mass)
    known_ids = {option.value for option in options}
    options += [
        ConfigValueOption(player_id, title=player_id)
        for player_id in selected or []
        if player_id not in known_ids
    ]
    return ConfigEntry(
        key=CONF_CONNECTED_PLAYERS,
        type=ConfigEntryType.STRING,
        multi_value=True,
        required=False,
        default_value=[],
        value=list(selected or []),
        options=options,
    )


def create_publish_name_template_entry(current: ConfigValueType = None) -> ConfigEntry:
    """
    Return the dropdown selecting how the advertised device name is composed.

    :param current: Previously stored template value to preselect.
    """
    selected = current if current in PUBLISH_NAME_TEMPLATES else PUBLISH_NAME_PLAYER_MASS
    return ConfigEntry(
        key=CONF_PUBLISH_NAME_TEMPLATE,
        type=ConfigEntryType.STRING,
        required=True,
        default_value=PUBLISH_NAME_PLAYER_MASS,
        value=selected,
        options=[ConfigValueOption(template) for template in PUBLISH_NAME_TEMPLATES],
    )


def resolve_publish_name(template: ConfigValueType, player_name: str) -> str:
    """
    Render the advertised device name for a player from the selected template.

    :param template: One of the PUBLISH_NAME_TEMPLATES option values.
    :param player_name: The connected player's display name.
    """
    if template == PUBLISH_NAME_PLAYER:
        return player_name
    if template == PUBLISH_NAME_MASS_PLAYER:
        return f"Music Assistant | {player_name}"
    return f"{player_name} | Music Assistant"


def _player_options(mass: MusicAssistant) -> list[ConfigValueOption]:
    """Return a picker option for every registered player, sorted by display name."""
    return [
        ConfigValueOption(player.player_id, title=player.display_name)
        for player in sorted(
            mass.players.all_players(False, False),
            key=lambda player: player.display_name.lower(),
        )
    ]
