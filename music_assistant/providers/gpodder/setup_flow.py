"""
Setup flow for the gPodder provider.

Two connection methods are offered:

- ``gpodder`` — a classic gpodder/opodsync server: URL + username + password + device id,
  validated (Basic auth) when the provider loads on finish.
- ``nextcloud`` — the Nextcloud "Login Flow v2": Music Assistant opens the Nextcloud
  login URL for the user to approve and then polls the server for the generated app
  password (there is no callback to us, so this is a poll, not an external step).

Credentials are persisted as setup data; the max-episodes limit stays a provider option.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import LoginFailed

from music_assistant.models.setup_flow import SetupFlowError, StepExpiredError

from . import (
    CONF_DEVICE_ID,
    CONF_PASSWORD,
    CONF_TOKEN_NC,
    CONF_URL,
    CONF_URL_NC,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)

if TYPE_CHECKING:
    from aiohttp import ClientSession
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant.models.setup_flow import SetupSession

CONF_METHOD = "method"
METHOD_GPODDER = "gpodder"
METHOD_NEXTCLOUD = "nextcloud"

# deadline (seconds) for the user to approve the Nextcloud login in their browser
_NC_LOGIN_TIMEOUT = 180.0


async def run_setup(session: SetupSession) -> None:
    """
    Run the gPodder setup flow: choose a method, then collect/verify its credentials.

    :param session: The setup session driving the flow.
    """
    values = await session.form(
        [
            ConfigEntry(
                key=CONF_METHOD,
                type=ConfigEntryType.STRING,
                required=True,
                default_value=METHOD_GPODDER,
                options=[
                    ConfigValueOption(value=METHOD_GPODDER),
                    ConfigValueOption(value=METHOD_NEXTCLOUD),
                ],
            )
        ],
        step_id="user",
    )
    if str(values[CONF_METHOD]) == METHOD_NEXTCLOUD:
        await _run_nextcloud(session)
    else:
        await _run_gpodder(session)


async def _run_gpodder(session: SetupSession) -> None:
    """Collect classic gpodder/opodsync credentials; finish validates them by loading."""
    prefill = session.context.setup_data
    errors: dict[str, str] | None = None
    while True:
        values = await session.form(
            [
                ConfigEntry(
                    key=CONF_URL,
                    type=ConfigEntryType.STRING,
                    required=True,
                    value=str(prefill.get(CONF_URL) or "") or None,
                ),
                ConfigEntry(key=CONF_USERNAME, type=ConfigEntryType.STRING, required=True),
                ConfigEntry(key=CONF_PASSWORD, type=ConfigEntryType.SECURE_STRING, required=True),
                ConfigEntry(
                    key=CONF_DEVICE_ID,
                    type=ConfigEntryType.STRING,
                    required=False,
                    value=str(prefill.get(CONF_DEVICE_ID) or "") or None,
                ),
                ConfigEntry(
                    key=CONF_VERIFY_SSL,
                    type=ConfigEntryType.BOOLEAN,
                    required=False,
                    default_value=True,
                    value=prefill.get(CONF_VERIFY_SSL),
                ),
            ],
            step_id="gpodder",
            errors=errors,
            last_step=True,
        )
        collected: dict[str, ConfigValueType] = {
            CONF_URL: str(values[CONF_URL]),
            CONF_USERNAME: str(values[CONF_USERNAME]),
            CONF_PASSWORD: str(values[CONF_PASSWORD]),
            CONF_DEVICE_ID: str(values[CONF_DEVICE_ID]),
            CONF_VERIFY_SSL: bool(values[CONF_VERIFY_SSL]),
        }
        try:
            await session.finish(collected)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}


async def _run_nextcloud(session: SetupSession) -> None:
    """Run the Nextcloud Login Flow v2 (open URL + poll for the app password)."""
    prefill = session.context.setup_data
    errors: dict[str, str] | None = None
    while True:
        values = await session.form(
            [
                ConfigEntry(
                    key=CONF_URL_NC,
                    type=ConfigEntryType.STRING,
                    required=True,
                    value=str(prefill.get(CONF_URL_NC) or "") or None,
                ),
                ConfigEntry(
                    key=CONF_VERIFY_SSL,
                    type=ConfigEntryType.BOOLEAN,
                    required=False,
                    default_value=True,
                    value=prefill.get(CONF_VERIFY_SSL),
                ),
            ],
            step_id="nextcloud",
            errors=errors,
        )
        url_nc = str(values[CONF_URL_NC])
        verify_ssl = bool(values[CONF_VERIFY_SSL])
        try:
            login_url, poll_endpoint, poll_token = await _nc_start(
                session.mass.http_session, url_nc, verify_ssl
            )
        except Exception:
            errors = {"base": "not_a_nextcloud_instance"}
            continue
        try:
            app_password = await session.external_until(
                _nc_poll(session.mass.http_session, poll_endpoint, poll_token, verify_ssl),
                url=login_url,
                step_id="nc_approve",
                expires_in=_NC_LOGIN_TIMEOUT,
            )
        except StepExpiredError:
            errors = {"base": "nextcloud_login_timeout"}
            continue
        except LoginFailed as err:
            errors = {"base": err.translation_key or str(err)}
            continue
        collected: dict[str, ConfigValueType] = {
            CONF_URL_NC: url_nc,
            CONF_TOKEN_NC: app_password,
            CONF_VERIFY_SSL: verify_ssl,
        }
        try:
            await session.finish(collected)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}


async def _nc_start(session: ClientSession, url_nc: str, verify_ssl: bool) -> tuple[str, str, str]:
    """Start the Nextcloud Login Flow v2, returning (login_url, poll_endpoint, poll_token)."""
    async with session.post(
        url_nc.rstrip("/") + "/index.php/login/v2",
        headers={"User-Agent": "Music Assistant"},
        ssl=verify_ssl,
    ) as response:
        data = await response.json()
    return data["login"], data["poll"]["endpoint"], data["poll"]["token"]


async def _nc_poll(
    session: ClientSession, poll_endpoint: str, poll_token: str, verify_ssl: bool
) -> str:
    """Poll the Nextcloud login endpoint until the user approves, returning the app password."""
    while True:
        async with session.post(
            poll_endpoint, data={"token": poll_token}, ssl=verify_ssl
        ) as response:
            if response.status not in (200, 404):
                raise LoginFailed("The specified url seems not to belong to a nextcloud instance.")
            if response.status == 200:
                data = await response.json()
                return str(data["appPassword"])
        await asyncio.sleep(1)
