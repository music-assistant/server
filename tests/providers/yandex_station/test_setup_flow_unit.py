"""Tests for the Yandex Station interactive setup flow."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Self
from unittest import mock

import pytest
from music_assistant_models.errors import InvalidDataError
from ya_passport_auth import Credentials, DeviceCodeSession, QrSession, SecretStr
from ya_passport_auth.exceptions import InvalidCredentialsError
from ya_passport_auth.ma import BORROW_SOURCE_OWN

from music_assistant.models.setup_flow import AbortFlow, StepExpiredError
from music_assistant.providers.yandex_station import setup_flow as station_flow
from music_assistant.providers.yandex_station.constants import (
    CONF_COOKIES,
    CONF_MUSIC_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_REMEMBER_SESSION,
    CONF_X_TOKEN,
    CONF_YM_INSTANCE,
)


class _FakeClient:
    """Canned Passport client that confirms QR and device logins."""

    def __init__(self, credentials: Credentials) -> None:
        self._credentials = credentials

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def start_qr_login(self) -> QrSession:
        return QrSession(track_id="t", csrf_token="c", qr_url="https://ya.ru/qr")

    async def poll_qr_until_confirmed(self, _qr: QrSession, **_kwargs: Any) -> Credentials:
        return self._credentials

    async def start_device_login(self, **_kwargs: Any) -> DeviceCodeSession:
        return DeviceCodeSession(
            device_code=SecretStr("dc"),
            user_code="ABCD-1234",
            verification_url="https://ya.ru/device",
            expires_in=300,
            interval=5,
        )

    async def poll_device_until_confirmed(
        self, _session: DeviceCodeSession, **_kwargs: Any
    ) -> Credentials:
        return self._credentials


class _FakeSession:
    """Scripted setup session that records the provider's observable result."""

    def __init__(
        self,
        responses: list[tuple[str, dict[str, Any]]],
        providers: dict[str, Any] | None = None,
        progress_errors: list[Exception] | None = None,
    ) -> None:
        self.mass = mock.MagicMock()
        self.mass.config.get.return_value = providers or {}
        self.context = SimpleNamespace(setup_data={})
        self._responses = responses
        self._progress_errors = progress_errors or []
        self.steps: list[tuple[str, dict[str, str] | None]] = []
        self.finished: dict[str, Any] | None = None

    async def form(
        self,
        _entries: list[Any],
        *,
        step_id: str,
        errors: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self.steps.append((step_id, errors))
        expected_step, response = self._responses.pop(0)
        assert step_id == expected_step
        return response

    async def progress_until(self, awaitable: Any, **_kwargs: Any) -> Any:
        if self._progress_errors:
            awaitable.close()
            raise self._progress_errors.pop(0)
        return await awaitable

    async def finish(self, values: dict[str, Any]) -> None:
        self.finished = values


async def test_borrow_mode_finishes_with_instance_only() -> None:
    """Selecting a linked instance persists no provider-owned credentials."""
    session = _FakeSession(
        [("user", {CONF_YM_INSTANCE: "ym-a"})],
        providers={"ym-a": {"domain": "yandex_music", "name": "Main"}},
    )

    await station_flow.run_setup(session)  # type: ignore[arg-type]

    assert session.finished == {CONF_YM_INSTANCE: "ym-a"}


async def test_own_device_login_persists_full_triple() -> None:
    """Remembered device login persists music, x, and refresh tokens."""
    credentials = Credentials(
        x_token=SecretStr("XT"),
        music_token=SecretStr("MT"),
        refresh_token=SecretStr("RT"),
        display_login="alice",
    )
    session = _FakeSession(
        [
            ("user", {CONF_YM_INSTANCE: BORROW_SOURCE_OWN}),
            (
                "method",
                {
                    station_flow.CONF_METHOD: station_flow.METHOD_DEVICE,
                    CONF_REMEMBER_SESSION: True,
                },
            ),
        ]
    )

    with mock.patch.object(station_flow, "PassportClient") as passport_client:
        passport_client.create.return_value = _FakeClient(credentials)
        await station_flow.run_setup(session)  # type: ignore[arg-type]

    assert session.finished == {
        CONF_YM_INSTANCE: BORROW_SOURCE_OWN,
        CONF_MUSIC_TOKEN: "MT",
        CONF_X_TOKEN: "XT",
        CONF_REFRESH_TOKEN: "RT",
    }


async def test_own_cookie_login_persists_tokens() -> None:
    """Remembered cookie login persists its music and x tokens."""
    session = _FakeSession(
        [
            ("user", {CONF_YM_INSTANCE: BORROW_SOURCE_OWN}),
            (
                "method",
                {
                    station_flow.CONF_METHOD: station_flow.METHOD_COOKIES,
                    CONF_REMEMBER_SESSION: True,
                },
            ),
            ("cookies", {CONF_COOKIES: "Session_id=abc; yandexuid=1"}),
        ]
    )

    with mock.patch.object(
        station_flow,
        "login_with_cookies",
        new=mock.AsyncMock(return_value=("XT", "MT")),
    ):
        await station_flow.run_setup(session)  # type: ignore[arg-type]

    assert session.finished == {
        CONF_YM_INSTANCE: BORROW_SOURCE_OWN,
        CONF_MUSIC_TOKEN: "MT",
        CONF_X_TOKEN: "XT",
        CONF_REFRESH_TOKEN: None,
    }


async def test_own_qr_without_remember_clears_long_lived_tokens() -> None:
    """Unremembered QR login stores only the immediately usable music token."""
    credentials = Credentials(x_token=SecretStr("XT"), music_token=SecretStr("MT"))
    session = _FakeSession(
        [
            ("user", {CONF_YM_INSTANCE: BORROW_SOURCE_OWN}),
            (
                "method",
                {
                    station_flow.CONF_METHOD: station_flow.METHOD_QR,
                    CONF_REMEMBER_SESSION: False,
                },
            ),
        ]
    )

    with mock.patch.object(station_flow, "PassportClient") as passport_client:
        passport_client.create.return_value = _FakeClient(credentials)
        await station_flow.run_setup(session)  # type: ignore[arg-type]

    assert session.finished == {
        CONF_YM_INSTANCE: BORROW_SOURCE_OWN,
        CONF_MUSIC_TOKEN: "MT",
        CONF_X_TOKEN: None,
        CONF_REFRESH_TOKEN: None,
    }


async def test_missing_music_token_redisplays_method_error() -> None:
    """A login result without a music token returns to method selection."""
    no_music = Credentials(x_token=SecretStr("XT"), music_token=None)
    session = _FakeSession(
        [
            ("user", {CONF_YM_INSTANCE: BORROW_SOURCE_OWN}),
            (
                "method",
                {
                    station_flow.CONF_METHOD: station_flow.METHOD_DEVICE,
                    CONF_REMEMBER_SESSION: True,
                },
            ),
            (
                "method",
                {
                    station_flow.CONF_METHOD: station_flow.METHOD_COOKIES,
                    CONF_REMEMBER_SESSION: True,
                },
            ),
            ("cookies", {CONF_COOKIES: "Session_id=abc"}),
        ]
    )
    with (
        mock.patch.object(station_flow, "_device_login", new=mock.AsyncMock(return_value=no_music)),
        mock.patch.object(
            station_flow,
            "login_with_cookies",
            new=mock.AsyncMock(return_value=("XT2", "MT2")),
        ),
    ):
        await station_flow.run_setup(session)  # type: ignore[arg-type]

    assert session.steps[2] == ("method", {"base": "no_music_token"})
    assert session.finished is not None
    assert session.finished[CONF_MUSIC_TOKEN] == "MT2"


async def test_cookie_error_redisplays_cookie_form() -> None:
    """Invalid pasted cookies stay on the cookie form with a base error."""
    session = _FakeSession(
        [
            ("user", {CONF_YM_INSTANCE: BORROW_SOURCE_OWN}),
            (
                "method",
                {
                    station_flow.CONF_METHOD: station_flow.METHOD_COOKIES,
                    CONF_REMEMBER_SESSION: True,
                },
            ),
            ("cookies", {CONF_COOKIES: "invalid"}),
            ("cookies", {CONF_COOKIES: "Session_id=abc"}),
        ]
    )
    with mock.patch.object(
        station_flow,
        "login_with_cookies",
        new=mock.AsyncMock(side_effect=[InvalidDataError("invalid cookies"), ("XT", "MT")]),
    ):
        await station_flow.run_setup(session)  # type: ignore[arg-type]

    assert session.steps[3] == ("cookies", {"base": "invalid_data"})
    assert session.finished is not None
    assert session.finished[CONF_MUSIC_TOKEN] == "MT"


async def test_expired_qr_step_mints_a_fresh_session() -> None:
    """An expired progress step starts a second QR session before finishing."""
    credentials = Credentials(x_token=SecretStr("XT"), music_token=SecretStr("MT"))
    session = _FakeSession([], progress_errors=[StepExpiredError()])
    client = _FakeClient(credentials)
    client.start_qr_login = mock.AsyncMock(wraps=client.start_qr_login)  # type: ignore[method-assign]

    with mock.patch.object(station_flow, "PassportClient") as passport_client:
        passport_client.create.return_value = client
        result = await station_flow._qr_login(session)  # type: ignore[arg-type]

    assert result is credentials
    assert client.start_qr_login.await_count == 2


async def test_denied_device_login_aborts_flow() -> None:
    """Explicit Passport rejection aborts setup with the translated reason."""
    credentials = Credentials(x_token=SecretStr("XT"), music_token=SecretStr("MT"))
    client = _FakeClient(credentials)
    client.poll_device_until_confirmed = mock.AsyncMock(  # type: ignore[method-assign]
        side_effect=InvalidCredentialsError("denied")
    )
    session = _FakeSession([])

    with mock.patch.object(station_flow, "PassportClient") as passport_client:
        passport_client.create.return_value = client
        with pytest.raises(AbortFlow) as error:
            await station_flow._device_login(session)  # type: ignore[arg-type]

    assert error.value.reason == "login_denied"
