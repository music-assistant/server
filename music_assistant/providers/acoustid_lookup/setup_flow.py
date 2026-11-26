"""
Setup flow for the AcoustID Lookup provider.

Connecting to the user's AcoustID account is optional. The account is connected with a
link: AcoustID hands out a user key and a page on which the user approves it, and the
flow checks with AcoustID until the user decided. The key is only stored once approved.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import (
    InvalidDataError,
    RateLimited,
    ResourceTemporarilyUnavailable,
)

from music_assistant.models.setup_flow import SetupFlowError, StepExpiredError
from music_assistant.providers.acoustid_lookup.link import LinkState, link_status, request_link
from music_assistant.providers.acoustid_lookup.provider import CONF_USER_KEY

if TYPE_CHECKING:
    from aiohttp import ClientSession

    from music_assistant.models.setup_flow import SetupSession

CONF_CONNECT = "connect"
CONF_RECONNECT = "reconnect"
POLL_INTERVAL = 3
# the longest wait between checks while AcoustID keeps failing to answer one
MAX_POLL_INTERVAL = 60

LOGGER = logging.getLogger(__name__)

# form errors (translation key, English message) for a link the user can not use
_STATE_ERRORS = {
    LinkState.DECLINED: ("link_declined", "The connection was declined on AcoustID."),
    LinkState.EXPIRED: ("link_expired", "The AcoustID link has expired."),
    LinkState.REVOKED: ("link_revoked", "The connection was removed on AcoustID."),
    LinkState.UNKNOWN: ("link_unknown", "This connection no longer exists on AcoustID."),
}


async def run_setup(session: SetupSession) -> None:
    """
    Run the setup flow: optionally connect an AcoustID account, then save the provider.

    :param session: The setup session driving the flow.
    """
    http_session = session.mass.http_session
    stored_key = session.context.setup_data.get(CONF_USER_KEY)
    # the key to store when the user does not connect (again)
    kept_key: str | None = None
    errors: dict[str, str | SetupFlowError] | None = None
    connect_now = False
    if isinstance(stored_key, str) and stored_key:
        try:
            state = await link_status(http_session, stored_key)
        except (ResourceTemporarilyUnavailable, InvalidDataError) as err:
            # an unanswered check says nothing about the key, so it stays
            kept_key = stored_key
            errors = _request_error(err)
        else:
            if state is LinkState.APPROVED:
                kept_key = stored_key
                values = await session.form(
                    [
                        ConfigEntry(
                            key=CONF_RECONNECT,
                            type=ConfigEntryType.BOOLEAN,
                            required=False,
                            default_value=False,
                        )
                    ],
                    step_id="connected",
                )
                connect_now = bool(values[CONF_RECONNECT])
                if not connect_now:
                    await session.finish({CONF_USER_KEY: kept_key})
                    return
            else:
                errors = _state_error(state)
    elif session.context.kind == "reconfigure":
        # reconfiguring a provider that is not connected can only mean connecting it
        connect_now = True
    while True:
        # AcoustID limits link requests, so a new link is only requested when the user asked
        if not connect_now:
            values = await session.form(
                [
                    ConfigEntry(
                        key=CONF_CONNECT,
                        type=ConfigEntryType.BOOLEAN,
                        required=False,
                        default_value=True,
                    )
                ],
                step_id="user",
                errors=errors,
            )
            if not values[CONF_CONNECT]:
                await session.finish({CONF_USER_KEY: kept_key})
                return
        connect_now = False
        try:
            state, user_key = await _request_approval(session, http_session)
        except (ResourceTemporarilyUnavailable, InvalidDataError) as err:
            errors = _request_error(err)
            continue
        if state is not LinkState.APPROVED:
            errors = _state_error(state)
            continue
        await session.finish({CONF_USER_KEY: user_key})
        return


async def _request_approval(
    session: SetupSession, http_session: ClientSession
) -> tuple[LinkState, str]:
    """Request a link, show it and return the user's decision with the user key it is about."""
    link = await request_link(http_session, session.mass.webserver.server_name)
    try:
        state = await session.external_until(
            _wait_for_decision(http_session, link.user_key),
            url=link.url,
            step_id="approve",
            expires_in=link.expires_in,
            copy_text=link.code,
            translation_params=[link.code],
        )
    except StepExpiredError:
        state = LinkState.EXPIRED
    return state, link.user_key


async def _wait_for_decision(http_session: ClientSession, user_key: str) -> LinkState:
    """Check with AcoustID until the link is no longer waiting for the user."""
    wait = POLL_INTERVAL
    failures = 0
    while True:
        await asyncio.sleep(wait)
        try:
            state = await link_status(http_session, user_key)
        except ResourceTemporarilyUnavailable as err:
            # a dropped connection should not end an approval the user is part way through.
            # The step deadline still bounds the retries, as it arrives as a cancellation
            LOGGER.debug("AcoustID did not answer the link check: %s", err)
            # back off further with each failed check, and at least as long as AcoustID asks
            failures += 1
            wait = max(min(POLL_INTERVAL * 2**failures, MAX_POLL_INTERVAL), err.backoff_time)
            continue
        if state is not LinkState.PENDING:
            return state
        wait = POLL_INTERVAL
        failures = 0


def _state_error(state: LinkState) -> dict[str, str | SetupFlowError] | None:
    """Return the form error for a link in the given state, if that state needs one."""
    if state not in _STATE_ERRORS:
        return None
    translation_key, message = _STATE_ERRORS[state]
    return {"base": SetupFlowError(message, translation_key=translation_key)}


def _request_error(err: Exception) -> dict[str, str | SetupFlowError]:
    """Return the form error for a failed call to AcoustID, logging its reason."""
    LOGGER.warning("AcoustID link request failed: %s", err)
    # the reason stays in the log, the form shows a message that can be translated
    if isinstance(err, RateLimited):
        return {
            "base": SetupFlowError(
                "AcoustID received too many requests.", translation_key="acoustid_rate_limited"
            )
        }
    return {
        "base": SetupFlowError(
            "AcoustID could not complete the request.", translation_key="acoustid_unreachable"
        )
    }
