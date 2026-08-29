"""
Setup flow for the Plex Connect plugin.

Each instance links one Music Assistant player to one configured Plex music provider.
Both choices are derived from live runtime state (the loaded plex instances and the
currently known players), so the form is built when the flow runs rather than declared
as a static set of entries.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, ProviderType

from music_assistant.helpers.config_entries import PLAYBACK_TARGET_TYPES
from music_assistant.models.setup_flow import AbortFlow, SetupFlowError

from . import CONF_MASS_PLAYER_ID, CONF_PLEX_PROVIDER_ID

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.setup_flow import SetupSession

PLEX_DOMAIN = "plex"


async def run_setup(session: SetupSession) -> None:
    """
    Run the Plex Connect setup flow: pick the Plex provider and the player to expose.

    :param session: The setup session driving the flow.
    """
    plex_options = _plex_provider_options(session.mass)
    if not plex_options:
        # the engine's depends_on check only guarantees an enabled plex config exists,
        # not that the instance actually loaded
        raise AbortFlow("no_plex_provider")
    player_options = _player_options(session.mass)
    if not player_options:
        raise AbortFlow("no_players")
    setup_data = dict(session.context.setup_data)
    errors: dict[str, str] | None = None
    while True:
        # setup_data (reconfigure) wins over the instance's stored option values, which
        # still hold the selection on installs made before this flow existed
        prefill: dict[str, Any] = {**session.context.values, **setup_data}
        submitted = await session.form(
            [
                _select_entry(
                    CONF_PLEX_PROVIDER_ID, plex_options, prefill.get(CONF_PLEX_PROVIDER_ID)
                ),
                _select_entry(
                    CONF_MASS_PLAYER_ID, player_options, prefill.get(CONF_MASS_PLAYER_ID)
                ),
            ],
            step_id="user",
            errors=errors,
            last_step=True,
        )
        setup_data.update(submitted)
        try:
            await session.finish(setup_data)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}


def _plex_provider_options(mass: MusicAssistant) -> list[ConfigValueOption]:
    """Return a picker option for every loaded Plex music provider instance."""
    return [
        ConfigValueOption(provider.instance_id, title=provider.name)
        for provider in mass.get_provider_instances(
            PLEX_DOMAIN, return_unavailable=True, provider_type=ProviderType.MUSIC
        )
    ]


def _player_options(mass: MusicAssistant) -> list[ConfigValueOption]:
    """Return a picker option for every available and enabled Music Assistant player."""
    return [
        ConfigValueOption(player.player_id, title=player.display_name)
        for player in sorted(
            mass.players.all_players(False, False),
            key=lambda player: player.display_name.lower(),
        )
        if player.type in PLAYBACK_TARGET_TYPES
    ]


def _select_entry(
    key: str, options: list[ConfigValueOption], prefill: ConfigValueType
) -> ConfigEntry:
    """
    Build the required single-select form entry for the given key.

    :param key: The setup data key the selection is collected under.
    :param options: The available options (never empty).
    :param prefill: Previously selected value, used when it is still a valid option.
    """
    selected = prefill if any(option.value == prefill for option in options) else options[0].value
    return ConfigEntry(
        key=key,
        type=ConfigEntryType.STRING,
        required=True,
        options=options,
        default_value=selected,
        value=selected,
    )
