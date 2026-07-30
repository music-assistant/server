"""
Setup flow for the Yandex Music provider.

The user picks a login method (Yandex Passport OAuth Device Flow or QR) and whether to
remember the session, then signs in with the native ``PassportClient``: the device code
(or QR code) is rendered as an inline image and completion is detected by polling, not a
browser callback the flow UI cannot drive. A shown code that elapses is minted afresh and
the progress step re-emitted in place.

On success the music token is persisted as setup data; when "remember session" is on the
long-lived ``x_token`` (and, Device-Flow only, the ``refresh_token``) are stored too so the
provider can silently refresh expired credentials. The music-token-only path (remember off)
stores no long-lived secrets, exactly as the old action handlers did.
"""

from __future__ import annotations

import base64
from html import escape
from typing import TYPE_CHECKING

import segno
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType
from ya_passport_auth import ClientConfig, PassportClient
from ya_passport_auth.exceptions import (
    DeviceCodeTimeoutError,
    InvalidCredentialsError,
    QRTimeoutError,
    YaPassportError,
)

from music_assistant.models.setup_flow import AbortFlow, SetupFlowError, StepExpiredError

from .constants import (
    CONF_REFRESH_TOKEN,
    CONF_REMEMBER_SESSION,
    CONF_TOKEN,
    CONF_X_TOKEN,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType
    from ya_passport_auth import Credentials

    from music_assistant.models.setup_flow import SetupSession

# login method choices on the first form
CONF_METHOD = "method"
METHOD_DEVICE = "device"
METHOD_QR = "qr"

# device name shown on the Yandex confirmation screen during the login flows
_DEVICE_NAME = "Music Assistant"


async def run_setup(session: SetupSession) -> None:
    """
    Run the Yandex Music login flow: choose a method, sign in, persist the tokens.

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
                    default_value=METHOD_DEVICE,
                    options=[
                        ConfigValueOption(value=METHOD_DEVICE),
                        ConfigValueOption(value=METHOD_QR),
                    ],
                ),
                ConfigEntry(
                    key=CONF_REMEMBER_SESSION,
                    type=ConfigEntryType.BOOLEAN,
                    required=False,
                    default_value=True,
                ),
            ],
            step_id="user",
            errors=errors,
        )
        method = str(values[CONF_METHOD])
        remember = bool(values[CONF_REMEMBER_SESSION])
        try:
            if method == METHOD_QR:
                creds = await _qr_login(session)
            else:
                creds = await _device_login(session)
        except AbortFlow:
            raise
        except YaPassportError as err:
            errors = {"base": str(err)}
            continue
        if creds.music_token is None:
            errors = {"base": "no_music_token"}
            continue
        collected: dict[str, ConfigValueType] = {CONF_TOKEN: creds.music_token.get_secret()}
        if remember:
            collected[CONF_X_TOKEN] = creds.x_token.get_secret()
            collected[CONF_REFRESH_TOKEN] = (
                creds.refresh_token.get_secret() if creds.refresh_token is not None else None
            )
        else:
            collected[CONF_X_TOKEN] = None
            collected[CONF_REFRESH_TOKEN] = None
        try:
            await session.finish(collected)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}


async def _qr_login(session: SetupSession) -> Credentials:
    """
    Run the native Yandex Passport QR login, refreshing the code on expiry.

    Shows the QR code as an inline image and polls until the user scans and confirms it
    in the Yandex app; whenever the scan window elapses a fresh code is minted and the
    progress step re-emitted in place.
    """
    ttl = ClientConfig().qr_poll_total_timeout_seconds
    async with PassportClient.create(config=ClientConfig()) as client:
        while True:
            qr = await client.start_qr_login()
            try:
                return await session.progress_until(
                    client.poll_qr_until_confirmed(qr, total_timeout=ttl),
                    step_id="scan_qr",
                    text="scan_qr",
                    image=_qr_image(qr.qr_url),
                    expires_in=int(ttl),
                )
            except StepExpiredError, QRTimeoutError:
                continue


async def _device_login(session: SetupSession) -> Credentials:
    """
    Run the native Yandex Passport OAuth Device Flow, refreshing the code on expiry.

    Shows the ``user_code`` + verification URL as an inline image and polls until the user
    confirms; a code that elapses is minted afresh and the progress step re-emitted in place.
    """
    async with PassportClient.create(config=ClientConfig()) as client:
        while True:
            device = await client.start_device_login(device_name=_DEVICE_NAME)
            try:
                return await session.progress_until(
                    # give the poll a longer deadline than the step so the step's
                    # countdown (StepExpiredError -> refresh) always wins the race
                    client.poll_device_until_confirmed(
                        device, total_timeout=float(device.expires_in) + 60
                    ),
                    step_id="device_login",
                    text="device_login",
                    image=_device_image(device.user_code, device.verification_url),
                    expires_in=float(device.expires_in),
                )
            except StepExpiredError, DeviceCodeTimeoutError:
                continue
            except InvalidCredentialsError as err:
                raise AbortFlow("login_denied") from err


def _qr_image(qr_url: str) -> str:
    """Render a QR-login URL as an SVG data URI to display in the flow."""
    return segno.make(qr_url, error="m").svg_data_uri(scale=4)


def _device_image(user_code: str, verification_url: str) -> str:
    """Render the device ``user_code`` + verification URL as an SVG data URI."""
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
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")
