"""Setup flow for linking Ynison to one configured Yandex Music account."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from music_assistant.helpers.config_entries import create_player_selector
from music_assistant.models.setup_flow import AbortFlow, SetupFlowError

from .config_helpers import list_yandex_music_instances
from .constants import (
    CONF_MASS_PLAYER_ID,
    CONF_YM_INSTANCE,
    LEGACY_AUTH_KEYS,
    LEGACY_YM_INSTANCE_OWN,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant.models.setup_flow import SetupSession


async def run_setup(session: SetupSession) -> None:
    """
    Collect the linked Yandex Music instance and concrete target player.

    :param session: Setup session that presents and persists the provider form.
    """
    if not session.mass.players.all_players(False, False):
        raise AbortFlow("no_players")

    ym_instances = list_yandex_music_instances(session.mass)
    if not ym_instances:
        raise AbortFlow("missing_dependency")

    setup_data: dict[str, ConfigValueType] = dict(session.context.setup_data)
    original_values = session.context.values
    prefill: dict[str, ConfigValueType] = {**original_values, **setup_data}
    valid_sources = {instance_id for instance_id, _name in ym_instances}
    existing_source = prefill.get(CONF_YM_INSTANCE)
    selected_source = (
        existing_source
        if isinstance(existing_source, str) and existing_source in valid_sources
        else ym_instances[0][0]
        if len(ym_instances) == 1
        else None
    )
    selected_player = prefill.get(CONF_MASS_PLAYER_ID) or prefill.get("player")
    legacy_present = existing_source == LEGACY_YM_INSTANCE_OWN or any(
        key in setup_data or key in original_values for key in LEGACY_AUTH_KEYS
    )

    errors: dict[str, str] | None = None
    while True:
        submitted = await session.form(
            [
                _source_entry(selected_source, ym_instances),
                create_player_selector(session.mass, CONF_MASS_PLAYER_ID, selected_player),
            ],
            step_id="user",
            errors=errors,
            last_step=True,
        )
        selected_source = str(submitted[CONF_YM_INSTANCE])
        selected_player = str(submitted[CONF_MASS_PLAYER_ID])
        collected: dict[str, ConfigValueType] = {
            CONF_YM_INSTANCE: selected_source,
            CONF_MASS_PLAYER_ID: selected_player,
        }
        if legacy_present:
            collected.update(dict.fromkeys(LEGACY_AUTH_KEYS))
        try:
            await session.finish(collected)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}
            setup_data = collected


def _source_entry(
    selected_source: str | None,
    ym_instances: list[tuple[str, str]],
) -> ConfigEntry:
    """Build the required linked Yandex Music provider selector."""
    return ConfigEntry(
        key=CONF_YM_INSTANCE,
        type=ConfigEntryType.STRING,
        required=True,
        default_value=selected_source,
        value=selected_source,
        options=[
            ConfigValueOption(value=instance_id, title=f"Yandex Music: {name}")
            for instance_id, name in ym_instances
        ],
    )
