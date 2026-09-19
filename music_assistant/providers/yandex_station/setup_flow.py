"""
Setup flow for the Yandex Station player provider.

First the user picks the Yandex account source: borrow the account of a linked Yandex
Music provider (single login, one token family) or use this provider's own credentials.
Borrowing finishes immediately with just the linked instance id — the linked provider
owns authentication. For own credentials the user then picks a login method (Yandex
Passport OAuth Device Flow, QR, or browser cookies) and signs in; device/QR use the
native ``PassportClient`` with the code rendered as an inline image, cookies reuse the
provider's popup-free ``login_with_cookies`` wrapper.

The resulting music token is persisted as setup data; when "remember session" is on the
long-lived ``x_token`` (and, Device-Flow only, the ``refresh_token``) are stored too so
the shared credential cascade can silently refresh expired credentials.
"""

from __future__ import annotations

import base64
from html import escape
from typing import TYPE_CHECKING

import segno
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    ResourceTemporarilyUnavailable,
)
from ya_passport_auth import ClientConfig, PassportClient
from ya_passport_auth.exceptions import (
    DeviceCodeTimeoutError,
    InvalidCredentialsError,
    QRTimeoutError,
    YaPassportError,
)
from ya_passport_auth.ma import BORROW_SOURCE_OWN, list_yandex_music_instances

from music_assistant.models.setup_flow import AbortFlow, SetupFlowError, StepExpiredError

from .auth import login_with_cookies
from .constants import (
    CONF_COOKIES,
    CONF_MUSIC_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_REMEMBER_SESSION,
    CONF_X_TOKEN,
    CONF_YM_INSTANCE,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType
    from ya_passport_auth import Credentials

    from music_assistant.models.setup_flow import SetupSession

# login method choices for the own-credentials path
CONF_METHOD = "method"
METHOD_DEVICE = "device"
METHOD_QR = "qr"
METHOD_COOKIES = "cookies"

# device name shown on the Yandex confirmation screen during the login flows
_DEVICE_NAME = "Music Assistant"


async def run_setup(session: SetupSession) -> None:
    """
    Run the Yandex Station setup flow: pick the account source, then borrow or log in.

    :param session: The setup session driving the flow.
    """
    ym_instances = list_yandex_music_instances(session.mass)
    valid_sources = {inst_id for inst_id, _ in ym_instances}
    default_source = str(session.context.setup_data.get(CONF_YM_INSTANCE) or BORROW_SOURCE_OWN)
    if default_source != BORROW_SOURCE_OWN and default_source not in valid_sources:
        default_source = BORROW_SOURCE_OWN

    errors: dict[str, str] | None = None
    while True:
        values = await session.form(
            [_source_entry(default_source, ym_instances)],
            step_id="user",
            errors=errors,
        )
        source = str(values[CONF_YM_INSTANCE])
        if source != BORROW_SOURCE_OWN:
            # borrow mode: the linked Yandex Music instance owns authentication
            try:
                await session.finish({CONF_YM_INSTANCE: source})
                return
            except SetupFlowError as err:
                errors = {"base": err.translation_key or str(err)}
                default_source = source
                continue
        # own credentials: collect a login method and sign in
        await _run_own(session)
        return


async def _run_own(session: SetupSession) -> None:
    """Collect a login method for the provider's own credentials and sign in."""
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
                        ConfigValueOption(value=METHOD_COOKIES),
                    ],
                ),
                ConfigEntry(
                    key=CONF_REMEMBER_SESSION,
                    type=ConfigEntryType.BOOLEAN,
                    required=False,
                    default_value=True,
                ),
            ],
            step_id="method",
            errors=errors,
        )
        method = str(values[CONF_METHOD])
        remember = bool(values[CONF_REMEMBER_SESSION])
        try:
            if method == METHOD_COOKIES:
                music_token, x_token, refresh_token = await _cookie_login(session)
            elif method == METHOD_QR:
                music_token, x_token, refresh_token = _unpack(await _qr_login(session))
            else:
                music_token, x_token, refresh_token = _unpack(await _device_login(session))
        except AbortFlow:
            raise
        except YaPassportError as err:
            errors = {"base": str(err)}
            continue
        if music_token is None:
            errors = {"base": "no_music_token"}
            continue
        collected: dict[str, ConfigValueType] = {
            CONF_YM_INSTANCE: BORROW_SOURCE_OWN,
            CONF_MUSIC_TOKEN: music_token,
        }
        if remember:
            collected[CONF_X_TOKEN] = x_token
            collected[CONF_REFRESH_TOKEN] = refresh_token
        else:
            collected[CONF_X_TOKEN] = None
            collected[CONF_REFRESH_TOKEN] = None
        try:
            await session.finish(collected)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}


async def _cookie_login(session: SetupSession) -> tuple[str | None, str, str | None]:
    """Collect a pasted cookie string and exchange it for (music_token, x_token, None)."""
    errors: dict[str, str] | None = None
    while True:
        values = await session.form(
            [ConfigEntry(key=CONF_COOKIES, type=ConfigEntryType.SECURE_STRING, required=True)],
            step_id="cookies",
            errors=errors,
        )
        try:
            x_token, music_token = await login_with_cookies(str(values[CONF_COOKIES]))
        except (InvalidDataError, LoginFailed, ResourceTemporarilyUnavailable) as err:
            errors = {"base": getattr(err, "translation_key", None) or str(err)}
            continue
        return music_token, x_token, None


async def _qr_login(session: SetupSession) -> Credentials:
    """
    Run the native Yandex Passport QR login, refreshing the code on expiry.

    Shows the QR code as an inline image and polls until the user scans and confirms it
    in the Yandex app; whenever the scan window elapses a fresh code is minted in place.
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


def _unpack(creds: Credentials) -> tuple[str | None, str, str | None]:
    """Unpack Credentials into plain (music_token, x_token, refresh_token) strings."""
    music_token = creds.music_token.get_secret() if creds.music_token is not None else None
    refresh_token = creds.refresh_token.get_secret() if creds.refresh_token is not None else None
    return music_token, creds.x_token.get_secret(), refresh_token


def _source_entry(default_source: str, ym_instances: list[tuple[str, str]]) -> ConfigEntry:
    """Build the account-source select (linked Yandex Music instances + own credentials)."""
    return ConfigEntry(
        key=CONF_YM_INSTANCE,
        type=ConfigEntryType.STRING,
        required=True,
        default_value=default_source,
        options=[
            *(
                ConfigValueOption(value=inst_id, title=f"Yandex Music: {name}")
                for inst_id, name in ym_instances
            ),
            ConfigValueOption(value=BORROW_SOURCE_OWN),
        ],
    )


def _qr_image(qr_url: str) -> str:
    """Render a QR-login URL as an SVG data URI to display in the flow."""
    return str(segno.make(qr_url, error="m").svg_data_uri(scale=4))


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
