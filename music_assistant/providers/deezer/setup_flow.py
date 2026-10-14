"""Setup flow for the Deezer provider."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiohttp import ClientError
from deezer_python_gql import (
    DeezerGQLClient,
    GraphQLClientAuthError,
    GraphQLClientError,
)
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.deezer.provider import CONF_ARL_TOKEN, CONF_FAMILY_PROFILE

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant.models.setup_flow import SetupSession

LOGGER = logging.getLogger(__name__)


async def run_setup(session: SetupSession) -> None:
    """Run the setup flow: collect the ARL token, pick a Family profile and create the provider."""
    errors: dict[str, str | SetupFlowError] | None = None
    setup_data = dict(session.context.setup_data)
    while True:
        submitted = await session.form(
            [
                ConfigEntry(
                    key=CONF_ARL_TOKEN,
                    type=ConfigEntryType.SECURE_STRING,
                    # the stored token is never sent to the client, an empty field on a
                    # reconfigure means "keep the current one"
                    required=not setup_data.get(CONF_ARL_TOKEN),
                )
            ],
            step_id="user",
            errors=errors,
        )
        if arl := str(submitted.get(CONF_ARL_TOKEN) or "").strip():
            setup_data[CONF_ARL_TOKEN] = arl
        if not setup_data.get(CONF_ARL_TOKEN):
            errors = {CONF_ARL_TOKEN: "required"}
            continue
        profiles, error = await session.progress_until(
            _get_profiles(session, str(setup_data[CONF_ARL_TOKEN])),
            step_id="loading_profiles",
            text="loading_profiles",
            expires_in=60,
        )
        if error:
            errors = {"base": error}
            continue
        setup_data[CONF_FAMILY_PROFILE] = await _select_profile(session, profiles, setup_data)
        try:
            await session.finish(setup_data)
            return
        except SetupFlowError as err:
            errors = {"base": err}


async def _get_profiles(
    session: SetupSession, arl: str
) -> tuple[list[tuple[str, str]], str | None]:
    """
    Return (id, name) of the account the ARL belongs to and its profiles, plus an error.

    The account itself comes first. Family members with their own login are left out,
    they need their own ARL. The list is only empty together with an error.

    :param session: The setup session.
    :param arl: The ARL token entered by the user.
    """
    client = DeezerGQLClient(arl=arl, session=session.mass.http_session)
    try:
        me = await client.get_family()
    except GraphQLClientAuthError as err:
        LOGGER.warning("Deezer rejected the ARL: %s", err)
        return [], "arl_rejected"
    except (GraphQLClientError, ClientError, TimeoutError) as err:
        # handled here, a timeout reaching progress_until would end the flow as expired
        LOGGER.warning("Could not load the Deezer profiles: %r", err)
        return [], "auth_failed"
    if me is None:
        LOGGER.warning("Deezer returned no user data for this ARL")
        return [], "auth_failed"
    if me.family is None:
        return [(me.id, "")], None
    profiles = [(me.id, me.family.main.name if me.family.main.id == me.id else "")]
    profiles.extend(
        (member.id, member.name)
        for member in (me.family.main, *me.family.linked)
        if member.id != me.id and member.permissions.is_loggable_as
    )
    return profiles, None


async def _select_profile(
    session: SetupSession,
    profiles: list[tuple[str, str]],
    setup_data: dict[str, ConfigValueType],
) -> str:
    """
    Let the user pick a Family profile and return its id, or "" for the account itself.

    A stored profile is never dropped without asking, otherwise a profile source would
    silently show the admin's library again.

    :param session: The setup session.
    :param profiles: The account and its profiles, from _get_profiles.
    :param setup_data: The setup data collected so far, for the current selection.
    """
    stored = str(setup_data.get(CONF_FAMILY_PROFILE) or "")
    if len(profiles) < 2 and not stored:
        return ""
    account_id = profiles[0][0]
    current = stored if stored in (profile_id for profile_id, _ in profiles) else account_id
    values = await session.form(
        [
            ConfigEntry(
                key=CONF_FAMILY_PROFILE,
                type=ConfigEntryType.STRING,
                required=True,
                options=[
                    ConfigValueOption(title=name or profile_id, value=profile_id)
                    for profile_id, name in profiles
                ],
                default_value=account_id,
                value=current,
            )
        ],
        step_id="profile",
        last_step=True,
    )
    selected = str(values[CONF_FAMILY_PROFILE])
    return "" if selected == account_id else selected
