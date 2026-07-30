"""Helpers for building shared configuration entries."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant.mass import MusicAssistant


def create_player_selector(
    mass: MusicAssistant,
    key: str,
    selected_value: ConfigValueType = None,
    auto_value: str | None = None,
) -> ConfigEntry:
    """
    Return a required single-player selector populated from the available players.

    :param mass: The Music Assistant instance providing the current players.
    :param key: The config entry key for the selected player.
    :param selected_value: Previously selected player id to prefill when still available.
    :param auto_value: Optional sentinel that enables automatic player selection.
    """
    options = [
        *([ConfigValueOption(auto_value)] if auto_value is not None else []),
        *(
            ConfigValueOption(player.player_id, title=player.display_name)
            for player in sorted(
                mass.players.all_players(False, False),
                key=lambda player: player.display_name.lower(),
            )
        ),
    ]
    selected = (
        selected_value
        if any(option.value == selected_value for option in options)
        else options[0].value
        if options
        else None
    )
    return ConfigEntry(
        key=key,
        type=ConfigEntryType.STRING,
        required=True,
        default_value=selected,
        value=selected,
        options=options,
    )
