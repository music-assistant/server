"""Tests for the Tidal setup flow's account check."""

from __future__ import annotations

from unittest import mock

import pytest

from music_assistant.models.setup_flow import AbortFlow, SetupFlowContext, SetupSession
from music_assistant.providers.tidal import setup_flow as tidal_flow
from music_assistant.providers.tidal.auth_manager import TidalAuthManager
from music_assistant.providers.tidal.constants import CONF_USER_ID, DOMAIN

DEVICE = {"userCode": "ABCDE", "verificationUri": "link.tidal.com/ABCDE", "expiresIn": "300"}
OWN = f"{DOMAIN}--own"
OTHER = f"{DOMAIN}--other"


def _make_session(
    accounts: dict[str, str | int | None], *, instance_id: str | None = None
) -> tuple[SetupSession, mock.AsyncMock]:
    """Return a session with the given configured Tidal instances and its finish mock."""
    mass = mock.Mock()
    mass.config.get_provider_configs = mock.AsyncMock(
        return_value=[mock.Mock(instance_id=iid) for iid in accounts]
    )
    mass.config.get_provider_setup_value = mock.Mock(
        side_effect=lambda iid, _key: accounts.get(iid)
    )
    finish = mock.AsyncMock(return_value={"instance_id": OWN})
    context = SetupFlowContext(
        kind="reconfigure" if instance_id else "setup",
        reason="user",
        domain=DOMAIN,
        instance_id=instance_id,
    )
    return SetupSession(mass, "flow-test", context, finish), finish


def _sign_in(session: SetupSession, monkeypatch: pytest.MonkeyPatch, user_id: int | str) -> None:
    """Make the device login succeed for the given Tidal user id."""
    auth_data = {
        "access_token": "at",
        "refresh_token": "rt",
        "expires_at": 1,
        "userId": user_id,
    }
    monkeypatch.setattr(TidalAuthManager, "start_device_login", mock.AsyncMock(return_value=DEVICE))
    monkeypatch.setattr(TidalAuthManager, "poll_device_login", mock.Mock())
    monkeypatch.setattr(session, "external_until", mock.AsyncMock(return_value=auth_data))


@pytest.mark.parametrize("instance_id", [None, OWN], ids=["setup", "reconfigure"])
async def test_an_account_on_another_instance_is_refused(
    monkeypatch: pytest.MonkeyPatch, instance_id: str | None
) -> None:
    """An account another instance already holds aborts before anything is stored."""
    # auth refreshes can store the user id as a number
    session, finish = _make_session({OTHER: 42, OWN: "41"}, instance_id=instance_id)
    _sign_in(session, monkeypatch, 42)

    with pytest.raises(AbortFlow, match="account_already_configured"):
        await tidal_flow.run_setup(session)

    finish.assert_not_called()


async def test_a_different_account_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second account alongside an existing instance finishes the setup."""
    session, finish = _make_session({OTHER: "41"})
    _sign_in(session, monkeypatch, 42)

    await tidal_flow.run_setup(session)

    finish.assert_awaited_once()
    assert finish.call_args.args[1][CONF_USER_ID] == "42"


async def test_a_reconfigure_keeps_its_own_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """The instance being reconfigured may sign in with the account it already holds."""
    session, finish = _make_session({OWN: "42"}, instance_id=OWN)
    _sign_in(session, monkeypatch, 42)

    await tidal_flow.run_setup(session)

    finish.assert_awaited_once()


async def test_an_instance_without_a_stored_account_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An instance with no stored user id never blocks a sign-in."""
    session, finish = _make_session({OTHER: None})
    _sign_in(session, monkeypatch, 42)

    await tidal_flow.run_setup(session)

    finish.assert_awaited_once()
