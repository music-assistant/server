"""
Setup flow for the Yandex Music provider.

The user picks a login method (Yandex Passport OAuth Device Flow, QR, or a manually
supplied music token). Passport flows can remember the session, while manual login stores
only the supplied music token. The device code (or QR code) is rendered as an inline image
and completion is detected by polling, not a browser callback the flow UI cannot drive. A
shown code that elapses is minted afresh and the progress step re-emitted in place.

On success the music token is persisted as setup data; when "remember session" is on the
long-lived ``x_token`` (and, Device-Flow only, the ``refresh_token``) are stored too so the
provider can silently refresh expired credentials. The music-token-only path (remember off)
stores no long-lived secrets, exactly as the old action handlers did.
"""

from __future__ import annotations

import asyncio
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

CONF_METHOD = "method"
METHOD_DEVICE = "device"
METHOD_QR = "qr"
METHOD_TOKEN = "token"

_DEVICE_NAME = "Music Assistant"
_AUTH_FLOW_TIMEOUT_SECONDS = 15 * 60


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
                    default_value=METHOD_QR,
                    options=[
                        ConfigValueOption(value=METHOD_QR),
                        ConfigValueOption(value=METHOD_DEVICE),
                        ConfigValueOption(value=METHOD_TOKEN),
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
        if method == METHOD_TOKEN:
            token_errors: dict[str, str] | None = None
            while True:
                token_values = await session.form(
                    [
                        ConfigEntry(
                            key=CONF_TOKEN,
                            type=ConfigEntryType.SECURE_STRING,
                            required=True,
                        )
                    ],
                    step_id="token_login",
                    errors=token_errors,
                    last_step=True,
                )
                if token_values[CONF_TOKEN]:
                    break
                token_errors = {CONF_TOKEN: "required"}
            collected: dict[str, ConfigValueType] = {
                CONF_TOKEN: str(token_values[CONF_TOKEN]),
                CONF_X_TOKEN: None,
                CONF_REFRESH_TOKEN: None,
            }
        else:
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
            collected = {CONF_TOKEN: creds.music_token.get_secret()}
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
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _AUTH_FLOW_TIMEOUT_SECONDS
    try:
        async with asyncio.timeout_at(deadline):
            async with PassportClient.create(config=ClientConfig()) as client:
                while True:
                    qr = await client.start_qr_login()
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise TimeoutError
                    code_timeout = min(float(ttl), remaining)
                    try:
                        return await session.progress_until(
                            client.poll_qr_until_confirmed(qr, total_timeout=code_timeout),
                            step_id="scan_qr",
                            text="scan_qr",
                            image=_qr_image(qr.qr_url),
                            expires_in=code_timeout,
                        )
                    except StepExpiredError, QRTimeoutError:
                        continue
    except TimeoutError as err:
        raise StepExpiredError from err


async def _device_login(session: SetupSession) -> Credentials:
    """
    Run the native Yandex Passport OAuth Device Flow, refreshing the code on expiry.

    Shows the ``user_code`` + verification URL as an inline image and polls until the user
    confirms; a code that elapses is minted afresh and the progress step re-emitted in place.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _AUTH_FLOW_TIMEOUT_SECONDS
    try:
        async with asyncio.timeout_at(deadline):
            async with PassportClient.create(config=ClientConfig()) as client:
                while True:
                    device = await client.start_device_login(device_name=_DEVICE_NAME)
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise TimeoutError
                    code_timeout = min(float(device.expires_in), remaining)
                    poll_timeout = min(float(device.expires_in) + 60, remaining)
                    try:
                        return await session.progress_until(
                            client.poll_device_until_confirmed(device, total_timeout=poll_timeout),
                            step_id="device_login",
                            text="device_login",
                            image=_device_image(device.user_code, device.verification_url),
                            expires_in=code_timeout,
                        )
                    except StepExpiredError, DeviceCodeTimeoutError:
                        continue
                    except InvalidCredentialsError as err:
                        raise AbortFlow("login_denied") from err
    except TimeoutError as err:
        raise StepExpiredError from err


def _qr_image(qr_url: str) -> str:
    """Render a high-contrast QR-login URL as an SVG data URI."""
    return segno.make(qr_url, error="m").svg_data_uri(
        scale=4,
        dark="#000",
        light="#fff",
        border=4,
    )


def _device_image(user_code: str, verification_url: str) -> str:
    """Render the device ``user_code`` + verification URL as an SVG data URI."""
    display_url = verification_url.removeprefix("https://").removeprefix("http://")
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="420" height="260" '
        'viewBox="0 0 420 260" role="img">'
        '<rect width="420" height="260" rx="18" fill="#ffdb4d"/>'
        '<text x="210" y="40" font-family="sans-serif" font-size="18" font-weight="600" '
        'text-anchor="middle" fill="#5a4a00">Open this address in a browser</text>'
        '<rect x="20" y="55" width="380" height="58" rx="10" fill="#fff"/>'
        '<text x="210" y="93" font-family="sans-serif" font-size="24" font-weight="700" '
        f'text-anchor="middle" fill="#1a1a1a">{escape(display_url)}</text>'
        '<text x="210" y="154" font-family="sans-serif" font-size="18" font-weight="600" '
        'text-anchor="middle" fill="#5a4a00">Then enter this code</text>'
        '<text x="210" y="220" font-family="monospace" font-size="52" font-weight="700" '
        f'text-anchor="middle" fill="#1a1a1a">{escape(user_code)}</text>'
        "</svg>"
    )
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")
