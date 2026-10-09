"""Unit tests for the QQ Music setup flow helpers."""

# mypy: ignore-errors

from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from qqmusic_api import ApiDataError, GlobalApiError, HTTPError, LoginError, NetworkError
from qqmusic_api.models.login import QR, QRCodeLoginEvents, QRLoginResult, QRLoginType
from qqmusic_api.models.request import Credential
from qqmusic_api.modules.login_utils import QRCodeLoginSession

from music_assistant.models.setup_flow import AbortFlow, SetupFlowError, StepExpiredError
from music_assistant.providers.qqmusic import setup_flow as qqmusic_setup_flow
from music_assistant.providers.qqmusic.setup_flow import _qr_data_uri, _run_qr_login, _wait_qr_login


def test_qr_data_uri_honors_mimetype() -> None:
    """The data URI should carry the QR mimetype and bytes."""
    png_qr = SimpleNamespace(data=b"abc", mimetype="image/png")
    jpeg_qr = SimpleNamespace(data=b"abc", mimetype="image/jpeg")
    assert _qr_data_uri(png_qr) == "data:image/png;base64,YWJj"
    assert _qr_data_uri(jpeg_qr) == "data:image/jpeg;base64,YWJj"


class _FakeLogin:
    """Minimal public LoginApi-compatible QR backend."""

    def __init__(self, event_batches: list[list[QRLoginResult]]) -> None:
        """Initialize the QR event stream."""
        self.event_batches = event_batches
        self.login_types: list[QRLoginType] = []
        self.mobile_checks = 0

    async def get_qrcode(self, login_type: QRLoginType) -> QR:
        """Return a QR code and remember the requested login type."""
        self.login_types.append(login_type)
        return QR(f"qr-{len(self.login_types)}".encode(), login_type, "image/png", "qr-id")

    async def checking_mobile_qrcode(
        self, _qrcode: QR, deadline: float | None = None
    ) -> AsyncGenerator[QRLoginResult]:
        """Yield configured public mobile QR events."""
        assert deadline is not None
        self.mobile_checks += 1
        for event in self.event_batches.pop(0):
            yield event

    async def check_qrcode(self, _qrcode: QR) -> QRLoginResult:
        """Fail if the obsolete web QR polling path is used."""
        raise AssertionError("web QR polling must not be used")


async def _mobile_session(events: list[QRLoginResult]) -> tuple[QRCodeLoginSession, _FakeLogin]:
    """Create a public QQ Music app QR session backed by fake events."""
    login = _FakeLogin([events])
    qr_session = QRCodeLoginSession(login, QRLoginType.MOBILE, timeout_seconds=120)
    await qr_session.get_qrcode()
    return qr_session, login


@pytest.mark.asyncio
async def test_wait_qr_login_returns_mobile_credential_on_done() -> None:
    """A MOBILE DONE event should return its credential without web polling."""
    credential = SimpleNamespace(musicid=123, musickey="mk")
    qr_session, login = await _mobile_session([QRLoginResult(QRCodeLoginEvents.DONE, credential)])

    result = await _wait_qr_login(qr_session)

    assert result is credential
    assert login.login_types == [QRLoginType.MOBILE]
    assert login.mobile_checks == 1


@pytest.mark.asyncio
async def test_wait_qr_login_expired_raises_step_expired() -> None:
    """A MOBILE TIMEOUT event should refresh the displayed QR code."""
    qr_session, _login = await _mobile_session([QRLoginResult(QRCodeLoginEvents.TIMEOUT)])

    with pytest.raises(StepExpiredError):
        await _wait_qr_login(qr_session)


@pytest.mark.asyncio
async def test_wait_qr_login_refused_aborts_flow() -> None:
    """A MOBILE REFUSE event should abort the flow cleanly."""
    qr_session, _login = await _mobile_session([QRLoginResult(QRCodeLoginEvents.REFUSE)])

    with pytest.raises(AbortFlow) as excinfo:
        await _wait_qr_login(qr_session)
    assert excinfo.value.reason == "login_rejected"


class _FakeSetupSession:
    """Minimal setup-flow session that records progress calls."""

    def __init__(self) -> None:
        """Initialize the recorded progress calls."""
        self.progress_calls: list[dict[str, object]] = []

    async def progress_until(self, awaitable, **kwargs):
        """Record the displayed step and await its result."""
        self.progress_calls.append(kwargs)
        return await awaitable


@pytest.mark.asyncio
async def test_run_qr_login_uses_mobile_session_and_progress_image() -> None:
    """The production setup path always requests an app QR code and displays it."""
    credential = SimpleNamespace(musicid=123, musickey="mk")
    login = _FakeLogin([[QRLoginResult(QRCodeLoginEvents.DONE, credential)]])
    session = _FakeSetupSession()

    result = await _run_qr_login(session, SimpleNamespace(login=login))

    assert result is credential
    assert login.login_types == [QRLoginType.MOBILE]
    assert login.mobile_checks == 1
    assert session.progress_calls == [
        {
            "step_id": "scan_qr",
            "image": "data:image/png;base64,cXItMQ==",
            "expires_in": 120.0,
        }
    ]


@pytest.mark.asyncio
async def test_run_qr_login_refreshes_mobile_qr_after_timeout() -> None:
    """A timed-out app QR code is replaced before waiting for a new login result."""
    credential = SimpleNamespace(musicid=123, musickey="mk")
    login = _FakeLogin(
        [
            [QRLoginResult(QRCodeLoginEvents.TIMEOUT)],
            [QRLoginResult(QRCodeLoginEvents.DONE, credential)],
        ]
    )
    session = _FakeSetupSession()

    result = await _run_qr_login(session, SimpleNamespace(login=login))

    assert result is credential
    assert login.login_types == [QRLoginType.MOBILE, QRLoginType.MOBILE]
    assert login.mobile_checks == 2
    assert [call["image"] for call in session.progress_calls] == [
        "data:image/png;base64,cXItMQ==",
        "data:image/png;base64,cXItMg==",
    ]


@pytest.mark.asyncio
async def test_run_qr_login_converts_mobile_network_error() -> None:
    """A public mobile QR transport failure is shown as a setup-flow error."""

    class _NetworkLogin:
        async def get_qrcode(self, _login_type: QRLoginType) -> QR:
            raise NetworkError("offline")

    with pytest.raises(SetupFlowError, match="QQ Music app connection failed"):
        await _run_qr_login(_FakeSetupSession(), SimpleNamespace(login=_NetworkLogin()))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "message"),
    [
        (ApiDataError("malformed QR"), "QQ Music app login failed"),
        (LoginError("rejected", code=-1), "QQ Music app login failed"),
        (HTTPError("gateway unavailable", status_code=502), "QQ Music app login failed"),
        (GlobalApiError(code=1), "QQ Music app login failed"),
    ],
)
async def test_run_qr_login_converts_public_login_errors(error, message) -> None:
    """Malformed or rejected SDK QR responses become setup-flow errors."""

    class _ErrorLogin:
        async def get_qrcode(self, _login_type: QRLoginType) -> QR:
            raise error

    with pytest.raises(SetupFlowError, match=message):
        await _run_qr_login(_FakeSetupSession(), SimpleNamespace(login=_ErrorLogin()))


@pytest.mark.asyncio
async def test_run_qr_login_converts_mobile_credential_validation_error() -> None:
    """Malformed credentials from the mobile QR event stream become a flow error."""

    class _MalformedCredentialLogin:
        async def get_qrcode(self, login_type: QRLoginType) -> QR:
            return QR(b"qr", login_type, "image/png", "qr-id")

        async def checking_mobile_qrcode(
            self, _qrcode: QR, deadline: float | None = None
        ) -> AsyncGenerator[QRLoginResult]:
            assert deadline is not None
            Credential.model_validate({"musicid": "invalid", "musickey": "key"})
            yield QRLoginResult(QRCodeLoginEvents.SCAN)

    with pytest.raises(SetupFlowError, match="QQ Music app login failed"):
        await _run_qr_login(_FakeSetupSession(), SimpleNamespace(login=_MalformedCredentialLogin()))


@pytest.mark.asyncio
async def test_run_setup_rejects_credential_without_encrypt_uin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An incomplete QR credential is rejected before the setup data is persisted."""
    client = SimpleNamespace(close=AsyncMock())
    session = SimpleNamespace(finish=AsyncMock())
    credential = Credential.model_validate(
        {"musicid": 123, "musickey": "key", "str_musicid": "123", "loginType": 2}
    )
    monkeypatch.setattr(qqmusic_setup_flow, "QQClient", Mock(return_value=client))
    monkeypatch.setattr(qqmusic_setup_flow, "_run_qr_login", AsyncMock(return_value=credential))

    with pytest.raises(SetupFlowError, match="incomplete credential"):
        await qqmusic_setup_flow.run_setup(session)

    session.finish.assert_not_awaited()
    client.close.assert_awaited_once()
