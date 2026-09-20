"""Collect the music account independently of NAS administrator authentication."""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.setup_flow import SetupFlowError

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

_ENTRIES = (
    ConfigEntry(key="url", type=ConfigEntryType.STRING, required=True),
    ConfigEntry(key="username", type=ConfigEntryType.STRING, required=True),
    ConfigEntry(key="password", type=ConfigEntryType.SECURE_STRING, required=True),
)


async def run_setup(session: SetupSession) -> None:
    """Save a stable, unique device ID for each configured provider instance."""
    data = dict(session.context.setup_data)
    saved_identity = (
        {key: data.get(key) for key in ("url", "username")}
        if session.context.kind == "reconfigure"
        else None
    )
    data.setdefault("device_id", uuid.uuid4().hex)
    errors = None
    while True:
        submitted = await session.form(
            [
                replace(
                    entry,
                    value=None if entry.key == "password" else data.get(entry.key, entry.value),
                    required=not bool(data.get("password"))
                    if entry.key == "password"
                    else entry.required,
                )
                for entry in _ENTRIES
            ],
            step_id="user",
            errors=errors,
            last_step=True,
        )
        if saved_identity is not None and any(
            submitted.get(key, value) != value for key, value in saved_identity.items()
        ):
            errors = {"base": "identity_change_not_supported"}
            continue
        data.update((key, value) for key, value in submitted.items() if key != "password" or value)
        try:
            await session.finish(data)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}
