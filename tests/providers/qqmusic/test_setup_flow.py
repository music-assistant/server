"""Unit tests for the QQ Music setup flow helpers."""

# mypy: ignore-errors

from __future__ import annotations

from types import SimpleNamespace

import pytest
from qqmusic_api.models.login import QRCodeLoginEvents

from music_assistant.models.setup_flow import AbortFlow, StepExpiredError
from music_assistant.providers.qqmusic.setup_flow import _poll_qr_login, _qr_data_uri


def test_qr_data_uri_honors_mimetype() -> None:
    """The data-URI should carry the QR's own mimetype (QQ png vs WeChat jpeg) and bytes."""
    png_qr = SimpleNamespace(data=b"abc", mimetype="image/png")
    jpeg_qr = SimpleNamespace(data=b"abc", mimetype="image/jpeg")
    assert _qr_data_uri(png_qr) == "data:image/png;base64,YWJj"
    assert _qr_data_uri(jpeg_qr) == "data:image/jpeg;base64,YWJj"


def _client_returning(
    event: QRCodeLoginEvents, credential: object | None = None
) -> SimpleNamespace:
    """Build a fake QQ client whose check_qrcode yields the given single result."""

    async def check_qrcode(_qr: object) -> SimpleNamespace:
        return SimpleNamespace(event=event, credential=credential)

    return SimpleNamespace(login=SimpleNamespace(check_qrcode=check_qrcode))


@pytest.mark.asyncio
async def test_poll_qr_login_returns_credential_on_done() -> None:
    """A DONE event carrying a credential should be returned to the caller."""
    credential = SimpleNamespace(musicid=123, musickey="mk")
    client = _client_returning(QRCodeLoginEvents.DONE, credential)
    result = await _poll_qr_login(client, SimpleNamespace())
    assert result is credential


@pytest.mark.asyncio
async def test_poll_qr_login_expired_raises_step_expired() -> None:
    """A TIMEOUT event should surface as StepExpiredError so the caller refreshes the QR."""
    client = _client_returning(QRCodeLoginEvents.TIMEOUT)
    with pytest.raises(StepExpiredError):
        await _poll_qr_login(client, SimpleNamespace())


@pytest.mark.asyncio
async def test_poll_qr_login_refused_aborts_flow() -> None:
    """A REFUSE event should abort the flow with the login_rejected reason."""
    client = _client_returning(QRCodeLoginEvents.REFUSE)
    with pytest.raises(AbortFlow) as excinfo:
        await _poll_qr_login(client, SimpleNamespace())
    assert excinfo.value.reason == "login_rejected"
