"""Guided OAuth Device Flow setup for the Yandex Disk provider."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.setup_flow import (
    AbortFlow,
    SetupFlowError,
    StepExpiredError,
)
from music_assistant.providers.filesystem_cloud.base import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_FOLDER_ID,
    CONF_REFRESH_TOKEN,
)
from music_assistant.providers.filesystem_local.constants import (
    CONF_CONTENT_TYPE,
    CONF_ENTRY_CONTENT_TYPE,
)

from .auth import (
    DeviceCodeDenied,
    DeviceCodeExpired,
    DeviceCodeGrant,
    DevicePollState,
    OAuthProtocolError,
    OAuthTokens,
    OAuthTransportError,
    poll_device_token,
    request_device_code,
)
from .constants import SUPPORTED_CONTENT_TYPES

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

# overall budget for one login attempt, across device-code renewals
AUTHORIZATION_TIMEOUT = 15 * 60.0


async def run_setup(session: SetupSession) -> None:
    """Collect cloud settings and authorize Yandex Disk with Device Flow."""
    # the shared cloud form includes sound effects, whose handlers require local files
    setup_data = dict(session.context.setup_data)
    if setup_data.get(CONF_CONTENT_TYPE) not in SUPPORTED_CONTENT_TYPES:
        setup_data[CONF_CONTENT_TYPE] = "music"
    stored_secret = str(setup_data.get(CONF_CLIENT_SECRET) or "")
    errors: dict[str, str | SetupFlowError] | None = None
    while True:
        entries = [
            replace(entry, value=setup_data.get(entry.key, entry.value))
            for entry in _setup_entries(bool(stored_secret))
        ]
        setup_data.update(await session.form(entries, step_id="user", errors=errors))
        client_id = str(setup_data.get(CONF_CLIENT_ID) or "")
        client_secret = str(setup_data.get(CONF_CLIENT_SECRET) or "") or stored_secret
        setup_data[CONF_CLIENT_SECRET] = client_secret
        stored_secret = client_secret
        try:
            if not client_secret:
                raise SetupFlowError("A client secret is required", translation_key="required")
            setup_data[CONF_REFRESH_TOKEN] = await _authorize(session, client_id, client_secret)
            await session.finish(setup_data)
            return
        except SetupFlowError as err:
            errors = {"base": err}


def _setup_entries(has_stored_secret: bool) -> tuple[ConfigEntry, ...]:
    """Return setup fields for the supported Yandex Disk content types."""
    return (
        replace(
            CONF_ENTRY_CONTENT_TYPE,
            options=[ConfigValueOption(value) for value in SUPPORTED_CONTENT_TYPES],
            validate=lambda value: value in SUPPORTED_CONTENT_TYPES,
        ),
        ConfigEntry(key=CONF_CLIENT_ID, type=ConfigEntryType.STRING, required=True),
        ConfigEntry(
            key=CONF_CLIENT_SECRET,
            type=ConfigEntryType.SECURE_STRING,
            required=not has_stored_secret,
        ),
        ConfigEntry(
            key=CONF_FOLDER_ID, type=ConfigEntryType.STRING, required=False, default_value="root"
        ),
    )


async def _authorize(session: SetupSession, client_id: str, client_secret: str) -> str:
    """Run Device Flow, refreshing an expired user code until login completes."""
    deadline = time.monotonic() + AUTHORIZATION_TIMEOUT
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            async with asyncio.timeout(remaining):
                grant = await request_device_code(session.mass.http_session, client_id)
            # the step countdown never outlives the overall budget
            remaining = deadline - time.monotonic()
            # the code is step text (not only an image) so screen readers can announce it
            tokens = await session.external_until(
                _poll_until_confirmed(session, grant, client_id, client_secret),
                url=grant.verification_url,
                step_id="device_login",
                expires_in=max(0.0, min(float(grant.expires_in), remaining)),
                translation_params=[grant.user_code],
            )
            return tokens.refresh_token
        except StepExpiredError, DeviceCodeExpired, TimeoutError:
            continue
        except DeviceCodeDenied as err:
            raise AbortFlow("login_denied") from err
        except OAuthTransportError as err:
            raise SetupFlowError(
                "Yandex OAuth is temporarily unavailable",
                translation_key="connection_error",
            ) from err
        except OAuthProtocolError as err:
            raise SetupFlowError(
                "Yandex rejected the OAuth request",
                translation_key="oauth_error",
            ) from err
    raise StepExpiredError


async def _poll_until_confirmed(
    session: SetupSession,
    grant: DeviceCodeGrant,
    client_id: str,
    client_secret: str,
) -> OAuthTokens:
    """Poll until Yandex returns tokens, respecting its requested interval."""
    interval = grant.interval
    while True:
        await asyncio.sleep(interval)
        result = await poll_device_token(
            session.mass.http_session,
            grant,
            client_id,
            client_secret,
        )
        if isinstance(result, OAuthTokens):
            return result
        if result is DevicePollState.SLOW_DOWN:
            interval += 5
