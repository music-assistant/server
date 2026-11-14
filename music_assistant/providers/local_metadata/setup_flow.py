"""Setup flow for the local metadata provider."""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_PATH
from music_assistant.models.setup_flow import SetupFlowError

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession


async def run_setup(session: SetupSession) -> None:
    """
    Run the setup flow: collect the artist information folder, then create the provider.

    The folder must be an available folder in a media storage location.

    :param session: The setup session driving the flow.
    """
    errors: dict[str, str | SetupFlowError] | None = None
    setup_data = dict(session.context.setup_data)
    while True:
        entry = ConfigEntry(
            key=CONF_PATH, type=ConfigEntryType.FOLDER, value=setup_data.get(CONF_PATH)
        )
        submitted = await session.form([entry], step_id="user", errors=errors, last_step=True)
        setup_data.update(submitted)
        path = str(setup_data.get(CONF_PATH) or "").strip()
        setup_data[CONF_PATH] = path
        if not path:
            # the same error the engine gives a required field that was left out
            errors = {CONF_PATH: "required"}
            continue
        try:
            await _check_folder(session, path)
        except SetupFlowError as err:
            errors = {CONF_PATH: err}
            continue
        try:
            await session.finish(setup_data)
            return
        except SetupFlowError as err:
            errors = {"base": err}


async def _check_folder(session: SetupSession, path: str) -> None:
    """Raise when a path is not an available folder in a media storage location."""
    storage = session.mass.storage
    manages_all_sources = session.context.manages_all_sources
    if not storage.can_hold_music_source(path, manages_all_sources):
        raise _folder_not_allowed(path)
    # the storage controller answers first, so a share whose server is gone is never touched
    available = await storage.is_available(path)
    location = storage.get_location_for_path(path)
    if not available and location is not None and not location.available:
        raise SetupFlowError(
            f"Storage location {location.path} is not available",
            translation_key="storage_location_unavailable",
            translation_args=[location.path],
        )
    # checked before a missing folder is reported, so a symlink does not tell what exists
    # outside the locations the caller may use
    real_path = await asyncio.to_thread(os.path.realpath, path)
    if not storage.can_hold_music_source(real_path, manages_all_sources):
        raise _folder_not_allowed(path)
    if not available:
        raise SetupFlowError(
            f"Folder {path} does not exist",
            translation_key="folder_not_found",
            translation_args=[path],
        )


def _folder_not_allowed(path: str) -> SetupFlowError:
    """Return the error for a folder outside the storage locations the caller may use."""
    return SetupFlowError(
        f"{path} is not in a storage location the caller may use",
        translation_key="folder_not_allowed",
        translation_args=[path],
    )
