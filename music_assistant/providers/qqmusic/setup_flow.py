"""
Setup flow for the QQ Music provider.

QQ Music has no official OAuth. Authentication uses a QR code scanned in the QQ Music app.
The public qqmusic-api QR session drives the app's MQTT event stream. Whenever the scan window
elapses, a fresh QR code is shown automatically. The resulting credential is persisted as setup
data.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

from pydantic import ValidationError
from qqmusic_api import ApiDataError, CgiApiException, NetworkError
from qqmusic_api import Client as QQClient
from qqmusic_api.models.login import QRCodeLoginEvents, QRLoginType
from qqmusic_api.modules.login_utils import QRCodeLoginSession

from music_assistant.models.setup_flow import AbortFlow, SetupFlowError, StepExpiredError

from . import _store_credential

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType
    from qqmusic_api import Credential
    from qqmusic_api.models.login import QR

    from music_assistant.models.setup_flow import SetupSession

_QR_EXPIRES_IN = 120.0


async def run_setup(session: SetupSession) -> None:
    """
    Run the QQ Music app QR login flow and store the credential.

    :param session: The setup session driving the flow.
    """
    client = QQClient()
    try:
        credential = await _run_qr_login(session, client)
        collected: dict[str, ConfigValueType] = {}
        _store_credential(collected, credential)
        await session.finish(collected)
    finally:
        await client.close()


async def _run_qr_login(session: SetupSession, client: QQClient) -> Credential:
    """
    Show a QQ Music app QR code and wait for the user to confirm the login.

    :param session: The setup session driving the flow.
    :param client: The QQ Music API client bound to this flow.
    """
    while True:
        qr_session = QRCodeLoginSession(
            client.login,
            QRLoginType.MOBILE,
            timeout_seconds=_QR_EXPIRES_IN,
        )
        try:
            qr = await qr_session.get_qrcode()
            return await session.progress_until(
                _wait_qr_login(qr_session),
                step_id="scan_qr",
                image=_qr_data_uri(qr),
                expires_in=_QR_EXPIRES_IN,
            )
        except StepExpiredError:
            continue
        except NetworkError as err:
            raise SetupFlowError(f"QQ Music app connection failed: {err}") from err
        except (ApiDataError, CgiApiException, ValidationError) as err:
            raise SetupFlowError(f"QQ Music app login failed: {err}") from err


async def _wait_qr_login(qr_session: QRCodeLoginSession) -> Credential:
    """
    Wait for the QQ Music app QR event stream to return the credential.

    :param qr_session: The public qqmusic-api QR login session.
    """
    async for result in qr_session:
        if result.event == QRCodeLoginEvents.DONE and result.credential:
            return result.credential
        if result.event == QRCodeLoginEvents.TIMEOUT:
            raise StepExpiredError
        if result.event == QRCodeLoginEvents.REFUSE:
            raise AbortFlow("login_rejected")
    raise SetupFlowError("QQ Music app login ended without a credential")


def _qr_data_uri(qr: QR) -> str:
    """
    Build a base64 data URI from a QR object.

    :param qr: The QR object returned by get_qrcode.
    """
    encoded = base64.b64encode(qr.data).decode("ascii")
    return f"data:{qr.mimetype};base64,{encoded}"
