"""
Setup flow for the QQ Music provider.

QQ Music has no official OAuth: authentication is a QR code the user scans with either
the QQ or the WeChat mobile app. The flow first asks which app to use, then shows a QR
code the user scans and confirms. Whenever the scan window elapses the QR code is minted
afresh and the progress step is re-emitted in place, so the shown code never silently
goes stale. The resulting credential is persisted as setup_data; the QR identifier/type
and page URL that used to be smuggled through hidden config values now live only as
locals for the duration of the flow.
"""

from __future__ import annotations

import asyncio
import base64
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType
from qqmusic_api import Client as QQClient
from qqmusic_api.models.login import QRCodeLoginEvents, QRLoginType

from music_assistant.models.setup_flow import AbortFlow, SetupFlowError, StepExpiredError

from . import _store_credential

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType
    from qqmusic_api import Credential
    from qqmusic_api.models.login import QR

    from music_assistant.models.setup_flow import SetupSession

# flow-local key for the QQ vs WeChat login method select (never persisted)
CONF_METHOD = "method"
METHOD_QQ = "qq"
METHOD_WX = "wx"
# scan window (seconds) a shown QR code is valid for before it is refreshed in place
_QR_EXPIRES_IN = 120.0
# delay (seconds) between poll attempts against the QR check endpoint
_QR_POLL_INTERVAL = 1.5


async def run_setup(session: SetupSession) -> None:
    """
    Run the QQ Music QR login flow: pick QQ/WeChat, scan a QR, store the credential.

    :param session: The setup session driving the flow.
    """
    method_default = str(
        session.context.setup_data.get(CONF_METHOD)
        or session.context.values.get(CONF_METHOD)
        or METHOD_QQ
    )
    client = QQClient()
    try:
        errors: dict[str, str] | None = None
        while True:
            values = await session.form(
                [
                    ConfigEntry(
                        key=CONF_METHOD,
                        type=ConfigEntryType.STRING,
                        required=True,
                        default_value=METHOD_QQ,
                        value=method_default,
                        options=[
                            ConfigValueOption(METHOD_QQ),
                            ConfigValueOption(METHOD_WX),
                        ],
                    ),
                ],
                step_id="user",
                errors=errors,
            )
            method = str(values[CONF_METHOD])
            method_default = method
            login_type = QRLoginType.WX if method == METHOD_WX else QRLoginType.QQ
            credential = await _run_qr_login(session, client, login_type)
            collected: dict[str, ConfigValueType] = {}
            _store_credential(collected, credential)
            try:
                await session.finish(collected)
                return
            except SetupFlowError as err:
                errors = {"base": err.translation_key or str(err)}
    finally:
        await client.close()


async def _run_qr_login(
    session: SetupSession, client: QQClient, login_type: QRLoginType
) -> Credential:
    """
    Show a QR code and wait for the user to scan and confirm the login.

    Mints a fresh QR code and re-emits the progress step whenever the scan window
    expires, so the displayed code stays valid until the user completes the login.

    :param session: The setup session driving the flow.
    :param client: The QQ Music API client bound to this flow.
    :param login_type: Whether to request a QQ or a WeChat QR login.
    """
    while True:
        qr = await client.login.get_qrcode(login_type)
        try:
            return await session.progress_until(
                _poll_qr_login(client, qr),
                step_id="scan_qr",
                image=_qr_data_uri(qr),
                expires_in=_QR_EXPIRES_IN,
            )
        except StepExpiredError:
            # the scan window elapsed (deadline) or the app reported the code expired:
            # mint a fresh QR and re-emit the progress step in place
            continue


async def _poll_qr_login(client: QQClient, qr: QR) -> Credential:
    """
    Poll the QR check endpoint until the login is confirmed, returning the credential.

    Raises StepExpiredError when the app reports the code expired, so the caller
    refreshes the QR alongside the progress-step deadline; aborts the flow when the
    user rejects the login in the app.

    :param client: The QQ Music API client bound to this flow.
    :param qr: The QR object returned by get_qrcode.
    """
    while True:
        result = await client.login.check_qrcode(qr)
        if result.event == QRCodeLoginEvents.DONE and result.credential:
            return result.credential
        if result.event == QRCodeLoginEvents.TIMEOUT:
            raise StepExpiredError
        if result.event == QRCodeLoginEvents.REFUSE:
            raise AbortFlow("login_rejected")
        # SCAN (awaiting scan) / CONF (scanned, awaiting confirm): keep polling
        await asyncio.sleep(_QR_POLL_INTERVAL)


def _qr_data_uri(qr: QR) -> str:
    """
    Build a base64 data-URI from a QR object, honoring its (per-method) mimetype.

    :param qr: The QR object returned by get_qrcode.
    """
    encoded = base64.b64encode(bytes(qr.data)).decode("ascii")
    return f"data:{qr.mimetype};base64,{encoded}"
