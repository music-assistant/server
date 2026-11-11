"""Setup flow for the local filesystem provider."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.filesystem_local.constants import (
    CONF_ENTRY_CONTENT_TYPE,
    CONF_ENTRY_PATH,
    DEFAULT_MEDIA_FOLDER,
)

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

# the setup asks for the content type as a question with a button per type, and starts on
# music without marking it as the default; the options page keeps the shared entry
_CONTENT_TYPE_ENTRY = replace(
    CONF_ENTRY_CONTENT_TYPE,
    translation_key="setup_content_type",
    required=True,
    default_value=None,
    value=CONF_ENTRY_CONTENT_TYPE.default_value,
    expanded_options=True,
)
_OVERLAP_WARNING = ConfigEntry(key="overlap_warning", type=ConfigEntryType.ALERT, required=False)
_OVERLAP_CHOICE = ConfigEntry(
    key="overlap_choice",
    type=ConfigEntryType.STRING,
    options=[ConfigValueOption("use_folder"), ConfigValueOption("choose_another")],
    expanded_options=True,
)


async def run_setup(session: SetupSession) -> None:
    """
    Run the setup flow: collect the content type and folder, then create the provider.

    A new folder must be an existing folder in an available storage location the caller may
    use, and the user confirms it when other music sources read its files too; on reconfigure
    an unchanged folder is accepted without these checks.

    :param session: The setup session driving the flow.
    """
    manages_all_sources = session.context.manages_all_sources
    default_folder = (
        DEFAULT_MEDIA_FOLDER
        if session.mass.storage.can_hold_music_source(DEFAULT_MEDIA_FOLDER, manages_all_sources)
        else None
    )
    entries = (_CONTENT_TYPE_ENTRY, replace(CONF_ENTRY_PATH, default_value=default_folder))
    errors: dict[str, str | SetupFlowError] | None = None
    setup_data = dict(session.context.setup_data)
    while True:
        form_entries = [
            replace(entry, value=setup_data.get(entry.key, entry.value)) for entry in entries
        ]
        submitted = await session.form(form_entries, step_id="user", errors=errors, last_step=True)
        setup_data.update(submitted)
        path = str(setup_data[CONF_ENTRY_PATH.key])
        if not path.strip():
            # the same error the engine gives a required field that was left out
            errors = {CONF_ENTRY_PATH.key: "required"}
            continue
        # a source keeps the folder it already reads from, also one outside every location
        if path != session.context.setup_data.get(CONF_ENTRY_PATH.key):
            try:
                await _check_folder(session, path)
            except SetupFlowError as err:
                errors = {CONF_ENTRY_PATH.key: err}
                continue
            if not await _confirm_overlap(session, path):
                errors = None
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
    # the storage controller answers first, so a share whose server is gone is never touched
    available = await storage.is_available(path)
    location = storage.get_location_for_path(path)
    if not available and location is not None and not location.available:
        raise SetupFlowError(
            f"Storage location {location.path} is not available",
            translation_key="storage_location_unavailable",
            translation_args=[location.path],
        )
    # a symlink must not lead out of the locations the caller may use, nor into the server's
    # own folders; checked before a missing folder is reported, so a link does not tell what
    # exists outside those locations
    real_path = await asyncio.to_thread(os.path.realpath, path)
    if not storage.can_hold_music_source(real_path, manages_all_sources):
        raise _folder_not_allowed(path)
    if not available:
        raise SetupFlowError(
            f"Folder {path} does not exist",
            translation_key="music_directory_not_found",
            translation_args=[path],
        )


async def _confirm_overlap(session: SetupSession, path: str) -> bool:
    """
    Return whether to use a folder, asking the user first when other music sources read it too.

    :param session: The setup session driving the flow.
    :param path: The checked folder.
    """
    context = session.context
    # a caller that does not manage every source only learns of the sources it may use
    sources = session.mass.storage.get_overlapping_sources(
        path, context.instance_id, None if context.manages_all_sources else context.user
    )
    if not sources:
        return True
    warning = replace(_OVERLAP_WARNING, translation_params=[", ".join(sources)])
    submitted = await session.form([warning, _OVERLAP_CHOICE], step_id="overlap")
    return submitted[_OVERLAP_CHOICE.key] == "use_folder"


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
