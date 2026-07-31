"""
Setup flow for the Tidal music provider.

Tidal is linked with the OAuth device flow. It has no browser callback (Tidal's
authorize page cannot redirect back to Music Assistant), so completion is detected by
polling the token endpoint. The single setup step shows an "Open" button for the
``link.tidal.com`` verification URL (code pre-filled) plus a waiting spinner, and
auto-completes the moment the poll reports approval. A code that expires before
approval is minted afresh and the step re-shown.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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
        # a single "Open link.tidal.com" step (code pre-filled) that auto-completes when
        # the poll reports approval. The step's countdown owns expiry, raising
        # StepExpiredError so we mint and show a fresh code.
        try:
            auth_data = await session.external_until(
                TidalAuthManager.poll_device_login(http_session, device),
                url=_verification_url(device),
                step_id="device_login",
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
