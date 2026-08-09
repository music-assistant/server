"""
Setup flow for the NetEase Cloud Music provider.

Authentication uses NetEase's QR login. The flow first collects the address of the
local NeteaseCloudMusicApi-compatible backend, then shows a QR code the user scans
with the NetEase Cloud Music app and confirms. Whenever the scan window elapses the
QR code is minted afresh and the progress step is re-emitted in place, so the shown
code never silently goes stale. The resulting login cookie and resolved user id are
persisted as setup_data; the QR poll key and page URL that used to be smuggled through
hidden config values now live only as locals for the duration of the flow.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    ResourceTemporarilyUnavailable,
)

from music_assistant.models.setup_flow import SetupFlowError, StepExpiredError

from . import NcmApiClient, _extract_code, _extract_cookie, _extract_data, _resolve_uid
from .constants import CONF_API_BASE_URL, CONF_COOKIE, CONF_UID, DEFAULT_API_BASE_URL

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant.models.setup_flow import SetupSession

# scan window (seconds) a shown QR code is valid for before it is refreshed in place
_QR_EXPIRES_IN = 120.0
# delay (seconds) between poll attempts against the QR check endpoint
_QR_POLL_INTERVAL = 1.5
# NetEase QR check status codes
_QR_CODE_EXPIRED = 800
_QR_CODE_WAITING_SCAN = 801
_QR_CODE_WAITING_CONFIRM = 802
_QR_CODE_CONFIRMED = 803


async def run_setup(session: SetupSession) -> None:
    """
    Run the NetEase QR login flow: pick the backend, scan a QR, store the cookie.

    :param session: The setup session driving the flow.
    """
    prefill = (
        session.context.setup_data.get(CONF_API_BASE_URL)
        or session.context.values.get(CONF_API_BASE_URL)
        or DEFAULT_API_BASE_URL
    )
    errors: dict[str, str] | None = None
    while True:
        values = await session.form(
            [
                ConfigEntry(
                    key=CONF_API_BASE_URL,
                    type=ConfigEntryType.STRING,
                    required=True,
                    default_value=DEFAULT_API_BASE_URL,
                    value=str(prefill),
                ),
            ],
            step_id="user",
            errors=errors,
        )
        api_base_url = str(values[CONF_API_BASE_URL]).strip()
        prefill = api_base_url
        client = NcmApiClient(session.mass.http_session, api_base_url)
        try:
            cookie, uid = await _run_qr_login(session, client)
        except (InvalidDataError, LoginFailed, ResourceTemporarilyUnavailable) as err:
            # most likely a wrong/unreachable backend URL: let the user correct it
            errors = {"base": getattr(err, "translation_key", None) or str(err)}
            continue
        collected: dict[str, ConfigValueType] = {
            CONF_API_BASE_URL: api_base_url,
            CONF_COOKIE: cookie,
            CONF_UID: uid,
        }
        try:
            await session.finish(collected)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}


async def _run_qr_login(session: SetupSession, client: NcmApiClient) -> tuple[str, str]:
    """
    Show a QR code and wait for the user to scan and confirm the login.

    Mints a fresh QR code and re-emits the progress step whenever the scan window
    expires, so the displayed code stays valid until the user completes the login.

    :param session: The setup session driving the flow.
    :param client: API client bound to the chosen backend base URL.
    """
    while True:
        qr_key, qr_image = await _create_qr(client)
        try:
            return await session.progress_until(
                _poll_qr_login(client, qr_key),
                step_id="scan_qr",
                image=qr_image,
                expires_in=_QR_EXPIRES_IN,
            )
        except StepExpiredError:
            # the scan window elapsed (deadline) or the backend reported the code
            # expired: mint a fresh QR and re-emit the progress step in place
            continue


async def _create_qr(client: NcmApiClient) -> tuple[str, str]:
    """
    Create a fresh QR login session and return its poll key and data-URI image.

    :param client: API client bound to the chosen backend base URL.
    """
    key_payload = await client.get("/login/qr/key", params={"timestamp": int(time.time() * 1000)})
    key_data = _extract_data(key_payload)
    qr_key = str(key_data.get("unikey") or key_data.get("key") or "").strip()
    if not qr_key:
        raise LoginFailed("Failed to generate NetEase QR key")
    qr_payload = await client.get(
        "/login/qr/create",
        params={"key": qr_key, "qrimg": "true", "timestamp": int(time.time() * 1000)},
    )
    qr_data = _extract_data(qr_payload)
    # the backend already returns qrimg as a data:image/png;base64,... string; the
    # progress step takes exactly that, so it is emitted verbatim (no re-encoding)
    qr_image = str(qr_data.get("qrimg") or "").strip()
    if not qr_image:
        raise LoginFailed("NetEase QR create did not return a QR image")
    return qr_key, qr_image


async def _poll_qr_login(client: NcmApiClient, qr_key: str) -> tuple[str, str]:
    """
    Poll the QR check endpoint until the login is confirmed, returning (cookie, uid).

    Raises StepExpiredError when the backend reports the code expired, so the caller
    refreshes the QR alongside the progress-step deadline.

    :param client: API client bound to the chosen backend base URL.
    :param qr_key: The QR poll token from _create_qr.
    """
    while True:
        payload = await client.get(
            "/login/qr/check",
            params={"key": qr_key, "timestamp": int(time.time() * 1000)},
            allow_codes={
                _QR_CODE_EXPIRED,
                _QR_CODE_WAITING_SCAN,
                _QR_CODE_WAITING_CONFIRM,
                _QR_CODE_CONFIRMED,
            },
        )
        code = _extract_code(payload)
        if code == _QR_CODE_CONFIRMED:
            cookie = _extract_cookie(payload)
            if not cookie:
                raise LoginFailed("QR login succeeded but API response did not include cookie")
            uid = await _resolve_uid(client, cookie)
            return cookie, uid
        if code == _QR_CODE_EXPIRED:
            raise StepExpiredError
        # 801 (awaiting scan) / 802 (scanned, awaiting confirm) / anything else: keep polling
        await asyncio.sleep(_QR_POLL_INTERVAL)
