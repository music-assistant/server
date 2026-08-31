"""
Setup flow for the Overcast provider.

Most Overcast accounts are created in the app and carry no email address or password,
so the account is linked by approving a token in the Overcast app for iPhone, either by
scanning a QR code or by following the same link on the phone itself. Accounts that do
have an email address and password can still sign in with those.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

import aiohttp
import segno
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType
from yarl import URL

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.helpers.aiohttp_client import create_clientsession
from music_assistant.models.setup_flow import AbortFlow, SetupFlowError, StepExpiredError

from .constants import (
    BASE_URL,
    CONF_METHOD,
    CONF_SESSION_COOKIE,
    LOGIN_URL,
    METHOD_PASSWORD,
    METHOD_QR,
    QR_AUTH_URL,
    QR_EXPIRES_IN,
    QR_FLOW_TIMEOUT,
    QR_POLL_BACKOFF,
    QR_POLL_MAX_INTERVAL,
    QR_TOKEN_PATTERN,
    QR_VERIFY_URL,
    SESSION_COOKIE_NAME,
    UNAPPROVED_BODY_LENGTH,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant.models.setup_flow import SetupSession

_CREDENTIAL_ENTRIES = (
    ConfigEntry(
        key=CONF_USERNAME,
        type=ConfigEntryType.STRING,
        required=True,
    ),
    ConfigEntry(
        key=CONF_PASSWORD,
        type=ConfigEntryType.SECURE_STRING,
        required=True,
    ),
)


async def run_setup(session: SetupSession) -> None:
    """
    Run the setup flow, linking the account through the app or with credentials.

    :param session: The setup session driving the flow.
    """
    errors: dict[str, str] | None = None
    while True:
        values = await session.form(
            [
                ConfigEntry(
                    key=CONF_METHOD,
                    type=ConfigEntryType.STRING,
                    required=True,
                    default_value=METHOD_QR,
                    options=[
                        ConfigValueOption(value=METHOD_QR),
                        ConfigValueOption(value=METHOD_PASSWORD),
                    ],
                ),
            ],
            step_id="user",
            errors=errors,
        )
        collected: dict[str, ConfigValueType]
        if str(values[CONF_METHOD]) == METHOD_QR:
            collected = {CONF_SESSION_COOKIE: await _qr_login(session)}
        else:
            collected = await _collect_credentials(session)
        try:
            await session.finish(collected)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}


async def _collect_credentials(session: SetupSession) -> dict[str, ConfigValueType]:
    """
    Collect the email address and password of an Overcast account that has them.

    :param session: The setup session driving the flow.
    """
    errors: dict[str, str] | None = None
    setup_data: dict[str, ConfigValueType] = dict(session.context.setup_data)
    while True:
        entries = [
            replace(entry, value=setup_data.get(entry.key, entry.value))
            for entry in _CREDENTIAL_ENTRIES
        ]
        submitted = await session.form(
            entries, step_id="credentials", errors=errors, last_step=True
        )
        if submitted[CONF_USERNAME] and submitted[CONF_PASSWORD]:
            return dict(submitted)
        setup_data.update(submitted)
        errors = {"base": "credentials_required"}


async def _qr_login(session: SetupSession) -> str:
    """
    Show a code to approve in the Overcast app and return the session it grants.

    :param session: The setup session driving the flow.
    """
    http_session = create_clientsession(session.mass, cookie_jar=aiohttp.CookieJar())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + QR_FLOW_TIMEOUT
    try:
        async with asyncio.timeout_at(deadline):
            while True:
                token, target = await _mint_login_token(http_session)
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError
                try:
                    return await session.progress_until(
                        _poll_until_approved(http_session, token, target),
                        step_id="scan_qr",
                        text="scan_qr",
                        image=_qr_image(QR_AUTH_URL.format(token=token)),
                        expires_in=min(QR_EXPIRES_IN, remaining),
                    )
                except StepExpiredError:
                    # the code has been on screen long enough, replace it with a fresh
                    # one in place so the user never scans a stale code
                    continue
    except TimeoutError as err:
        raise AbortFlow("login_timeout") from err
    finally:
        await http_session.close()


async def _mint_login_token(http_session: aiohttp.ClientSession) -> tuple[str, str]:
    """
    Return a token to approve and the page Overcast continues to once it is.

    :param http_session: The session the login runs on.
    """
    try:
        async with http_session.get(LOGIN_URL) as response:
            response.raise_for_status()
            body = await response.text()
    except (TimeoutError, aiohttp.ClientError) as err:
        raise AbortFlow("overcast_unreachable") from err
    if not (match := QR_TOKEN_PATTERN.search(body)):
        raise AbortFlow("no_login_token")
    return match["token"], match["then"]


async def _poll_until_approved(http_session: aiohttp.ClientSession, token: str, target: str) -> str:
    """
    Wait until the token is approved and return the session it grants.

    :param http_session: The session the login runs on.
    :param token: The token shown in the code, approved from the Overcast app.
    :param target: The page Overcast continues to once the token is approved.
    """
    attempts = 0
    while True:
        try:
            async with http_session.post(
                QR_VERIFY_URL, data={"token": token, "then": target}
            ) as response:
                body = await response.text()
        except (TimeoutError, aiohttp.ClientError) as err:
            raise AbortFlow("overcast_unreachable") from err
        if len(body) > UNAPPROVED_BODY_LENGTH:
            # a longer body is the page to continue to, where the session gets set
            return await _claim_session_cookie(http_session, body.strip())
        attempts += 1
        await asyncio.sleep(_poll_interval(attempts))


async def _claim_session_cookie(http_session: aiohttp.ClientSession, target: str) -> str:
    """
    Return the session cookie the approved login leaves behind.

    :param http_session: The session the login runs on.
    :param target: The page Overcast pointed at once the token was approved.
    """
    try:
        async with http_session.get(URL(BASE_URL).join(URL(target))):
            pass
    except (TimeoutError, aiohttp.ClientError) as err:
        raise AbortFlow("overcast_unreachable") from err
    for cookie in http_session.cookie_jar:
        if cookie.key == SESSION_COOKIE_NAME and cookie.value:
            return cookie.value
    raise AbortFlow("no_session_cookie")


def _poll_interval(attempts: int) -> float:
    """
    Return how long to wait before checking the token again.

    :param attempts: How many times the token has been checked so far.
    """
    for limit, interval in QR_POLL_BACKOFF:
        if attempts <= limit:
            return interval
    return QR_POLL_MAX_INTERVAL


def _qr_image(auth_url: str) -> str:
    """
    Render the app deep link as a high contrast SVG data URI.

    :param auth_url: The deep link the Overcast app opens to approve the login.
    """
    return segno.make(auth_url, error="m").svg_data_uri(
        scale=4,
        dark="#000",
        light="#fff",
        border=4,
    )
