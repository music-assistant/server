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

from .accounts import get_account_holders, get_loaded_account_holders
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
                # shown on the step so the code can also be typed at link.tidal.com
                # from a phone, rather than only carried by the Open button's url
                translation_params=[str(device["userCode"])],
            )
        except StepExpiredError:
            continue
        except LoginFailed as err:
            raise AbortFlow("login_failed") from err

        user_id = str(auth_data["userId"])
        if await _account_refused(session, user_id):
            raise AbortFlow(
                "account_used_by_other_instance"
                if session.context.kind == "reconfigure"
                else "account_already_configured"
            )
        collected: dict[str, ConfigValueType] = {
            CONF_AUTH_TOKEN: auth_data["access_token"],
            CONF_REFRESH_TOKEN: auth_data["refresh_token"],
            CONF_EXPIRY_TIME: auth_data["expires_at"],
            CONF_USER_ID: user_id,
        }
        await session.finish(collected)
        return


def _verification_url(device: dict[str, str]) -> str:
    """Return the full (scheme-prefixed) verification URL with the code pre-filled."""
    url = str(device.get("verificationUriComplete") or device["verificationUri"])
    return url if url.startswith("http") else f"https://{url}"


async def _account_refused(session: SetupSession, user_id: str) -> bool:
    """
    Return whether the account that just signed in may not be used by this flow.

    A new setup, or a reconfigure that moves onto another account, is refused if any other
    instance holds the account. Re-linking the account an instance already has is refused if
    a lower-id instance holds it, even a disabled one since it takes the account over once
    enabled, or if another instance is running on it right now.

    :param session: The setup session driving the flow.
    :param user_id: The Tidal user id that just signed in.
    """
    mass = session.mass
    instance_id = session.context.instance_id
    holders = get_account_holders(mass, user_id, instance_id)
    if session.context.kind != "reconfigure" or not instance_id:
        return bool(holders)
    stored = mass.config.get_provider_setup_value(instance_id, CONF_USER_ID)
    if not stored or str(stored) != user_id:
        return bool(holders)
    return any(holder.instance_id < instance_id for holder in holders) or bool(
        get_loaded_account_holders(mass, user_id, instance_id)
    )
