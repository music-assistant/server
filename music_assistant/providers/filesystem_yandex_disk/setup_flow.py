"""Guided OAuth Device Flow setup for the Yandex Disk provider."""

from __future__ import annotations

import asyncio
import base64
import time
from html import escape
from typing import TYPE_CHECKING

from music_assistant.models.setup_flow import (
    AbortFlow,
    SetupFlowError,
    StepExpiredError,
)
from music_assistant.providers.filesystem_cloud.base import run_cloud_setup

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

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

# overall budget for one login attempt, across device-code renewals
AUTHORIZATION_TIMEOUT = 15 * 60.0


async def run_setup(session: SetupSession) -> None:
    """Collect cloud settings and authorize Yandex Disk with Device Flow."""
    await run_cloud_setup(session, _authorize)


async def _authorize(session: SetupSession, client_id: str, client_secret: str) -> str:
    """Run Device Flow, refreshing an expired user code until login completes."""
    deadline = time.monotonic() + AUTHORIZATION_TIMEOUT
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            async with asyncio.timeout(remaining):
                grant = await request_device_code(session.mass.http_session, client_id)
            # the step countdown never outlives the overall budget
            remaining = deadline - time.monotonic()
            tokens = await session.progress_until(
                _poll_until_confirmed(session, grant, client_id, client_secret),
                step_id="device_login",
                text="device_login",
                image=_device_image(grant.user_code, grant.verification_url),
                expires_in=max(0.0, min(float(grant.expires_in), remaining)),
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


def _device_image(user_code: str, verification_url: str) -> str:
    """Render the activation code and URL as an inline SVG data URI."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="460" height="180" '
        'viewBox="0 0 460 180" role="img">'
        '<rect width="460" height="180" rx="16" fill="#ffdb4d"/>'
        '<text x="230" y="82" font-family="monospace" font-size="46" font-weight="700" '
        f'text-anchor="middle" fill="#1a1a1a">{escape(user_code)}</text>'
        '<text x="230" y="130" font-family="sans-serif" font-size="16" '
        f'text-anchor="middle" fill="#5a4a00">{escape(verification_url)}</text>'
        "</svg>"
    )
    encoded = base64.b64encode(svg.encode()).decode()
    return f"data:image/svg+xml;base64,{encoded}"
