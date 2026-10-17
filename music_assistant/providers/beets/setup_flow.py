"""Setup flow for the beets music provider."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from pathlib import PurePath
from typing import TYPE_CHECKING

from music_assistant.models.setup_flow import SetupFlowError

from .constants import (
    CONF_ENTRY_BEETS_DIRECTORY,
    CONF_ENTRY_LIBRARY_DB,
    CONF_ENTRY_MUSIC_DIRECTORY,
    CONF_LIBRARY_DB,
    CONF_MUSIC_DIRECTORY,
)

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

_ENTRIES = (CONF_ENTRY_LIBRARY_DB, CONF_ENTRY_MUSIC_DIRECTORY, CONF_ENTRY_BEETS_DIRECTORY)
# the paths Music Assistant itself reads; beets_directory only names a path on beets' host
_LOCAL_PATH_KEYS = (CONF_LIBRARY_DB, CONF_MUSIC_DIRECTORY)


async def run_setup(session: SetupSession) -> None:
    """
    Collect where the beets library lives, then create the provider.

    The library database and the music directory must lie in a storage location the caller
    may use; on reconfigure an unchanged path is accepted without this check.

    :param session: The setup flow session used to interact with the user.
    """
    errors: dict[str, str | SetupFlowError] | None = None
    setup_data = dict(session.context.setup_data)
    while True:
        entries = [
            replace(entry, value=setup_data.get(entry.key, entry.value)) for entry in _ENTRIES
        ]
        submitted = await session.form(entries, step_id="user", errors=errors, last_step=True)
        setup_data.update(submitted)
        errors = {}
        for key in _LOCAL_PATH_KEYS:
            path = str(setup_data.get(key) or "")
            # a source keeps the path it already reads from, also one outside every location
            if path == session.context.setup_data.get(key):
                continue
            try:
                await _check_path(session, path)
            except SetupFlowError as err:
                errors[key] = err
        if errors:
            continue
        try:
            await session.finish(setup_data)
            return
        except SetupFlowError as err:
            errors = {"base": err}


async def _check_path(session: SetupSession, path: str) -> None:
    """
    Raise when a music source of the caller may not read a path.

    :param session: The setup session driving the flow.
    :param path: The submitted path.
    """
    storage = session.mass.storage
    manages_all_sources = session.context.manages_all_sources
    # a symlink must not lead out of the locations the caller may use, nor into the server's
    # own folders
    real_path = await asyncio.to_thread(os.path.realpath, path)
    if not (
        PurePath(path).is_absolute()
        and storage.can_hold_music_source(path, manages_all_sources)
        and storage.can_hold_music_source(real_path, manages_all_sources)
    ):
        raise SetupFlowError(
            f"{path} is not in a storage location the caller may use",
            translation_key="path_not_allowed",
        )
