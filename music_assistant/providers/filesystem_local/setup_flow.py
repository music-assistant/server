"""Setup flow for the local filesystem provider."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from typing import TYPE_CHECKING

from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.filesystem_local.constants import (
    CONF_ENTRY_CONTENT_TYPE,
    CONF_ENTRY_PATH,
    DEFAULT_MEDIA_FOLDER,
)

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession


async def run_setup(session: SetupSession) -> None:
    """
    Run the setup flow: collect the content type and folder, then create the provider.

    A new folder must be an existing folder in a storage location the caller may use; on
    reconfigure the folder the source already reads from is kept as it is.

    :param session: The setup session driving the flow.
    """
    manages_all_sources = session.context.manages_all_sources
    default_folder = (
        DEFAULT_MEDIA_FOLDER
        if session.mass.storage.can_hold_music_source(DEFAULT_MEDIA_FOLDER, manages_all_sources)
        else None
    )
    entries = (CONF_ENTRY_CONTENT_TYPE, replace(CONF_ENTRY_PATH, default_value=default_folder))
    errors: dict[str, str | SetupFlowError] | None = None
    setup_data = dict(session.context.setup_data)
    while True:
        form_entries = [
            replace(entry, value=setup_data.get(entry.key, entry.value)) for entry in entries
        ]
        submitted = await session.form(form_entries, step_id="user", errors=errors, last_step=True)
        setup_data.update(submitted)
        path = str(setup_data[CONF_ENTRY_PATH.key])
        # a source keeps the folder it already reads from, also one outside every location
        if path != session.context.setup_data.get(CONF_ENTRY_PATH.key):
            try:
                await _check_folder(session, path)
            except SetupFlowError as err:
                errors = {CONF_ENTRY_PATH.key: err}
                continue
        try:
            await session.finish(setup_data)
            return
        except SetupFlowError as err:
            errors = {"base": err}


async def _check_folder(session: SetupSession, path: str) -> None:
    """
    Raise when a music source of the caller may not read its files from a folder.

    :param session: The setup session driving the flow.
    :param path: The submitted folder.
    """
    storage = session.mass.storage
    manages_all_sources = session.context.manages_all_sources
    if not storage.can_hold_music_source(path, manages_all_sources):
        raise _folder_not_allowed(path)
    # a symlink must not lead out of the locations the caller may use, nor into the server's
    # own folders
    real_path = await asyncio.to_thread(os.path.realpath, path)
    if not storage.can_hold_music_source(real_path, manages_all_sources):
        raise _folder_not_allowed(path)
    if not await asyncio.to_thread(os.path.isdir, real_path):
        raise SetupFlowError(
            f"Music directory {path} does not exist",
            translation_key="music_directory_not_found",
            translation_args=[path],
        )


def _folder_not_allowed(path: str) -> SetupFlowError:
    """
    Return the error for a folder outside the storage locations the caller may use.

    :param path: The submitted folder.
    """
    return SetupFlowError(
        f"{path} is not in a storage location the caller may use",
        translation_key="folder_not_allowed",
        translation_args=[path],
    )
