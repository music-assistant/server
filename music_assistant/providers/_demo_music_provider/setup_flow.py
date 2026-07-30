"""
DEMO/TEMPLATE interactive setup flow for a Music Assistant provider.

A provider ships a ``setup_flow.py`` module ONLY when adding an instance requires user
input (credentials, a token, an OAuth/QR login, picking a device on the network, ...).
When present, the server imports it and drives its ``run_setup`` coroutine when the user
adds the provider via ``config/providers/setup``; when absent, the instance is created
immediately with no input.

The flow talks to the user through the ``SetupSession``:

* ``await session.form(entries, step_id=..., errors=..., last_step=...)`` shows a form of
  ``ConfigEntry`` objects and returns the submitted ``{key: value}`` dict. Call it as many
  times as you need (multi-step wizards, re-prompts on error, ...).
* ``await session.finish(setup_data)`` persists ``setup_data`` (secrets are encrypted at
  rest by the server) and loads the provider. The collected values are read back in the
  provider via ``self.get_setup_value(<key>)`` - NOT via ``get_config_entries``, which now
  only describes the (runtime) options of an already set-up instance.

Refused/failed setup: raise ``AbortFlow(reason)`` to end the flow, or let a
``SetupFlowError`` from ``finish()`` bubble up to re-prompt with an error.

This demo collects nothing and finishes immediately; delete this file for a provider that
needs no setup input, or replace the body with a real form (see e.g. the opensubsonic or
deezer providers).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession


async def run_setup(session: SetupSession) -> None:
    """
    Drive the interactive setup flow for a new provider instance.

    :param session: The setup flow session used to interact with the user.
    """
    # Example of a single-step form (uncomment and adapt for a real provider):
    #
    # from music_assistant_models.config_entries import ConfigEntry
    # from music_assistant_models.enums import ConfigEntryType
    # from music_assistant.constants import CONF_USERNAME, CONF_PASSWORD
    #
    # values = await session.form(
    #     [
    #         ConfigEntry(key=CONF_USERNAME, type=ConfigEntryType.STRING, required=True),
    #         ConfigEntry(key=CONF_PASSWORD, type=ConfigEntryType.SECURE_STRING, required=True),
    #     ],
    #     step_id="user",
    #     last_step=True,
    # )
    # await session.finish(values)  # validate + persist (as setup_data) + load the provider
    #
    # This demo collects nothing and finishes right away.
    await session.finish({})
