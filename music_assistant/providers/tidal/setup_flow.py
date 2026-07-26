"""
Setup flow for the Tidal music provider.

Tidal is linked with the OAuth device flow: a short user code plus the
``link.tidal.com`` verification URL are shown as an inline image and completion is
detected by polling, not a browser callback the flow UI cannot drive. A code that
elapses is minted afresh and the progress step re-emitted in place. On success the
tokens are persisted as setup data for the provider to refresh silently.
"""

from __future__ import annotations

import base64
from html import escape
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import LoginFailed

from music_assistant.models.setup_flow import AbortFlow, StepExpiredError

from .auth_manager import TidalAuthManager
from .constants import (
    CONF_AUTH_TOKEN,
    CONF_EXPIRY_TIME,
    CONF_REFRESH_TOKEN,
    CONF_USER_ID,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant.models.setup_flow import SetupSession


async def run_setup(session: SetupSession) -> None:
    """
    Run the Tidal device-flow login: open the link, poll until approved, persist tokens.

    :param session: The setup session driving the flow.
    """
    http_session = session.mass.http_session
    while True:
        device = await TidalAuthManager.start_device_login(http_session)
        # Step 1: a clickable link that opens Tidal with the code pre-filled.
        await session.form(
            [
                ConfigEntry(
                    key="auth_instructions",
                    type=ConfigEntryType.LABEL,
                    help_link=_verification_url(device),
                ),
            ],
            step_id="user",
        )
        # Step 2: wait for approval, showing the code for cross-device sign-in. The step's
        # countdown owns expiry, raising StepExpiredError so we mint and show a fresh code.
        try:
            auth_data = await session.progress_until(
                TidalAuthManager.poll_device_login(http_session, device),
                step_id="device_login",
                text="device_login",
                image=_device_image(str(device["userCode"]), str(device["verificationUri"])),
                expires_in=float(device["expiresIn"]),
            )
        except StepExpiredError:
            continue
        except LoginFailed as err:
            raise AbortFlow("login_failed") from err

        collected: dict[str, ConfigValueType] = {
            CONF_AUTH_TOKEN: auth_data["access_token"],
            CONF_REFRESH_TOKEN: auth_data["refresh_token"],
            CONF_EXPIRY_TIME: auth_data["expires_at"],
            CONF_USER_ID: str(auth_data["userId"]),
        }
        await session.finish(collected)
        return


def _verification_url(device: dict[str, str]) -> str:
    """Return the full (scheme-prefixed) verification URL with the code pre-filled."""
    url = str(device.get("verificationUriComplete") or device["verificationUri"])
    return url if url.startswith("http") else f"https://{url}"


def _device_image(user_code: str, verification_url: str) -> str:
    """Render the device ``user_code`` + verification URL as an SVG data URI."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="460" height="180" '
        'viewBox="0 0 460 180" role="img">'
        '<rect width="460" height="180" rx="16" fill="#000000"/>'
        '<text x="230" y="82" font-family="monospace" font-size="46" font-weight="700" '
        f'text-anchor="middle" fill="#ffffff">{escape(user_code)}</text>'
        '<text x="230" y="130" font-family="sans-serif" font-size="16" '
        f'text-anchor="middle" fill="#b3b3b3">{escape(verification_url)}</text>'
        "</svg>"
    )
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")
