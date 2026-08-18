"""
Setup flow for the Spotify Connect plugin.

The flow starts with an explicit backend choice: Spotify Soloist (Spotify's
official headless client, guarded by a ToS warning/consent step and a personal
API key) or the legacy go-librespot daemon. Both branches end with the shared
target-player / device-name step. Reconfigure preselects the stored backend and
re-runs the branch steps; switching away from soloist clears the soloist
secrets, but only once the new setup finishes successfully.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_ENTRY_WARN_PREVIEW
from music_assistant.helpers.config_entries import create_player_selector
from music_assistant.models.setup_flow import SetupFlowError

from . import (
    BACKEND_GO_LIBRESPOT,
    BACKEND_SOLOIST,
    CONF_API_KEY,
    CONF_BACKEND,
    CONF_MASS_PLAYER_ID,
    CONF_PUBLISH_NAME,
    CONF_SOLOIST_CONSENT,
    CONF_VOLUME_MODE,
    DEFAULT_PUBLISH_NAME,
    PLAYER_ID_AUTO,
    VOLUME_MODE_OPTIONS,
)
from .backends.soloist import VOLUME_MODE_PLAYER_ONLY
from .soloist import UnsupportedPlatformError, verify_platform_supported

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

# Minimum plausible length of a pasted Soloist API key: anything shorter is a
# partial paste. No further format rules are applied locally — Spotify rejects
# an invalid key when soloist authenticates.
MIN_API_KEY_LENGTH = 16


async def run_setup(session: SetupSession) -> None:
    """
    Configure the Spotify Connect backend, target player and device name.

    :param session: The setup session driving the flow.
    """
    setup_data = dict(session.context.setup_data)
    stored_backend = str(
        setup_data.get(CONF_BACKEND) or session.context.values.get(CONF_BACKEND) or ""
    )
    selected = stored_backend or BACKEND_GO_LIBRESPOT
    choice_errors: dict[str, str] | None = None
    while True:
        selected = await _choose_backend(session, selected, choice_errors)
        choice_errors = None
        # rebuild from the stored setup data each round so a refused soloist
        # attempt does not leak partial values into a later selection
        collected = dict(setup_data)
        collected[CONF_BACKEND] = selected
        if selected == BACKEND_SOLOIST:
            if not await _run_soloist_steps(session, collected):
                # consent refused: back to the backend choice with a clear error
                choice_errors = {"base": "soloist_consent_required"}
                continue
        elif stored_backend == BACKEND_SOLOIST:
            # switching away from soloist: overwrite the soloist secrets; they
            # only reach the stored setup_data when finish() succeeds, so an
            # aborted or failed switch keeps them intact
            collected[CONF_API_KEY] = ""
            collected[CONF_SOLOIST_CONSENT] = False
        await _finish_with_player_and_name(session, collected)
        return


async def _choose_backend(
    session: SetupSession, preselect: str, errors: dict[str, str] | None
) -> str:
    """
    Show the backend choice step until a usable backend is selected.

    :param session: The setup session driving the flow.
    :param preselect: Backend to preselect (the stored or previously chosen one).
    :param errors: Optional errors to display on the first render.
    """
    while True:
        values = await session.form(
            [
                CONF_ENTRY_WARN_PREVIEW,
                ConfigEntry(
                    key=CONF_BACKEND,
                    type=ConfigEntryType.STRING,
                    required=True,
                    default_value=BACKEND_GO_LIBRESPOT,
                    value=preselect,
                    options=[
                        ConfigValueOption(BACKEND_SOLOIST),
                        ConfigValueOption(BACKEND_GO_LIBRESPOT),
                    ],
                ),
            ],
            step_id="backend",
            errors=errors,
        )
        selected = str(values[CONF_BACKEND])
        if selected == BACKEND_SOLOIST:
            try:
                verify_platform_supported()
            except UnsupportedPlatformError:
                errors = {"base": "soloist_unsupported_platform"}
                preselect = BACKEND_GO_LIBRESPOT
                continue
        return selected


async def _run_soloist_steps(session: SetupSession, collected: dict[str, Any]) -> bool:
    """
    Run the soloist branch: the ToS warning/consent step, then the API key step.

    :param session: The setup session driving the flow.
    :param collected: The values collected so far; updated in place.
    :return: True when the branch completed, False when consent was refused.
    """
    if not await _ask_consent(session, bool(collected.get(CONF_SOLOIST_CONSENT))):
        return False
    collected[CONF_SOLOIST_CONSENT] = True
    await _ask_api_key(session, collected)
    return True


async def _ask_consent(session: SetupSession, prefill: bool) -> bool:
    """
    Show the soloist warning/consent step and return whether consent was given.

    :param session: The setup session driving the flow.
    :param prefill: Whether consent was already given on an earlier run.
    """
    values = await session.form(
        [
            ConfigEntry(
                key=CONF_SOLOIST_CONSENT,
                type=ConfigEntryType.BOOLEAN,
                required=False,
                default_value=False,
                value=prefill,
            ),
        ],
        step_id="soloist_terms",
    )
    return bool(values.get(CONF_SOLOIST_CONSENT))


async def _ask_api_key(session: SetupSession, collected: dict[str, Any]) -> None:
    """
    Collect the Soloist API key and the volume mode.

    An already stored key (reconfigure) is kept when the field is left empty;
    it is never shown back to the user.

    :param session: The setup session driving the flow.
    :param collected: The values collected so far; updated in place.
    """
    has_stored_key = bool(collected.get(CONF_API_KEY))
    volume_mode = str(
        session.context.values.get(CONF_VOLUME_MODE)
        or collected.get(CONF_VOLUME_MODE)
        or VOLUME_MODE_PLAYER_ONLY
    )
    errors: dict[str, str] | None = None
    while True:
        entries = [
            ConfigEntry(
                key=CONF_API_KEY,
                type=ConfigEntryType.SECURE_STRING,
                required=not has_stored_key,
            ),
            ConfigEntry(
                key=CONF_VOLUME_MODE,
                type=ConfigEntryType.STRING,
                required=True,
                default_value=VOLUME_MODE_PLAYER_ONLY,
                value=volume_mode,
                options=VOLUME_MODE_OPTIONS,
            ),
        ]
        if has_stored_key:
            entries.insert(0, ConfigEntry(key="soloist_api_key_hint", type=ConfigEntryType.LABEL))
        values = await session.form(entries, step_id="soloist_api_key", errors=errors)
        api_key = str(values.get(CONF_API_KEY) or "").strip()
        volume_mode = str(values.get(CONF_VOLUME_MODE) or VOLUME_MODE_PLAYER_ONLY)
        if api_key or not has_stored_key:
            if len(api_key) < MIN_API_KEY_LENGTH:
                errors = {CONF_API_KEY: "soloist_api_key_invalid"}
                continue
            collected[CONF_API_KEY] = api_key
        collected[CONF_VOLUME_MODE] = volume_mode
        return


async def _finish_with_player_and_name(session: SetupSession, collected: dict[str, Any]) -> None:
    """
    Run the shared target-player / device-name step and finish the flow.

    :param session: The setup session driving the flow.
    :param collected: The values collected by the earlier steps.
    """
    errors: dict[str, str] | None = None
    while True:
        prefill: dict[str, Any] = {**session.context.values, **collected}
        publish_name = str(prefill.get(CONF_PUBLISH_NAME) or DEFAULT_PUBLISH_NAME)
        values = await session.form(
            [
                create_player_selector(
                    session.mass,
                    CONF_MASS_PLAYER_ID,
                    prefill.get(CONF_MASS_PLAYER_ID),
                    PLAYER_ID_AUTO,
                ),
                ConfigEntry(
                    key=CONF_PUBLISH_NAME,
                    type=ConfigEntryType.STRING,
                    required=True,
                    default_value=publish_name,
                    value=publish_name,
                ),
            ],
            step_id="user",
            errors=errors,
            last_step=True,
        )
        collected.update(values)
        try:
            await session.finish(collected)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}
