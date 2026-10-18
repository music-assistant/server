"""Tests for the Tidal setup flow's account check."""

from __future__ import annotations

from typing import cast
from unittest import mock

import pytest

from music_assistant.models.setup_flow import AbortFlow, SetupFlowContext, SetupSession
from music_assistant.providers.tidal import setup_flow as tidal_flow
from music_assistant.providers.tidal.auth_manager import TidalAuthManager
from music_assistant.providers.tidal.constants import CONF_USER_ID, DOMAIN

DEVICE = {"userCode": "ABCDE", "verificationUri": "link.tidal.com/ABCDE", "expiresIn": "300"}


LOW = f"{DOMAIN}--a"
HIGH = f"{DOMAIN}--b"


def _make_session(
    accounts: dict[str, str | None],
    *,
    instance_id: str | None = None,
    disabled: frozenset[str] = frozenset(),
    loaded: frozenset[str] = frozenset(),
) -> tuple[SetupSession, mock.AsyncMock]:
    """Return a session with the given configured Tidal instances and its finish mock."""
    mass = mock.Mock()
    mass.config.get = mock.Mock(
        return_value={
            iid: {
                "type": "music",
                "domain": DOMAIN,
                "instance_id": iid,
                "enabled": iid not in disabled,
            }
            for iid in accounts
        }
    )
    mass.config.get_provider_configs = mock.AsyncMock(side_effect=AssertionError)
    mass.config.get_provider_setup_value = mock.Mock(
        side_effect=lambda iid, _key: accounts.get(iid)
    )
    mass.get_provider = mock.Mock(
        side_effect=lambda iid, *_args, **_kwargs: (
            mock.Mock(instance_id=iid) if iid in loaded else None
        )
    )
    finish = mock.AsyncMock(return_value={"instance_id": f"{DOMAIN}--test"})
    context = SetupFlowContext(
        kind="reconfigure" if instance_id else "setup",
        reason="user",
        domain=DOMAIN,
        instance_id=instance_id,
    )
    session = SetupSession(mass, "flow-test", context, finish)
    return session, finish


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


async def test_an_account_on_another_instance_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An account another instance already serves aborts before anything is stored."""
    session, finish = _make_session({f"{DOMAIN}--other": "42"})
    _sign_in(session, monkeypatch, 42)

    with pytest.raises(AbortFlow, match="account_already_configured"):
        await tidal_flow.run_setup(session)

    finish.assert_not_called()


async def test_a_different_account_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second account alongside an existing instance finishes the setup."""
    session, finish = _make_session({f"{DOMAIN}--other": "41"})
    _sign_in(session, monkeypatch, 42)

    await tidal_flow.run_setup(session)

    finish.assert_awaited_once()
    assert finish.call_args.args[1][CONF_USER_ID] == "42"


async def test_an_instance_without_a_stored_account_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An instance with no stored user id never blocks a sign-in."""
    session, finish = _make_session({f"{DOMAIN}--other": None})
    _sign_in(session, monkeypatch, 42)

    await tidal_flow.run_setup(session)

    finish.assert_awaited_once()


async def test_an_account_on_a_disabled_instance_is_refused_for_a_new_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh setup is refused even when the holder is disabled."""
    session, finish = _make_session({LOW: "42"}, disabled=frozenset({LOW}))
    _sign_in(session, monkeypatch, 42)

    with pytest.raises(AbortFlow, match="account_already_configured"):
        await tidal_flow.run_setup(session)

    finish.assert_not_called()


async def test_a_reconfigure_keeps_its_own_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """The instance being reconfigured may sign in with the account it already holds."""
    session, finish = _make_session({LOW: "42"}, instance_id=LOW)
    _sign_in(session, monkeypatch, 42)

    await tidal_flow.run_setup(session)

    finish.assert_awaited_once()


async def test_the_winning_instance_may_reauthenticate_with_a_shared_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lowest-id instance keeps its account even if a higher-id instance shares it."""
    session, finish = _make_session({LOW: "42", HIGH: "42"}, instance_id=LOW)
    _sign_in(session, monkeypatch, 42)

    await tidal_flow.run_setup(session)

    finish.assert_awaited_once()


async def test_a_disabled_lower_instance_does_not_block_a_reconfigure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disabled lower-id instance does not hold the account against a reconfigure."""
    session, finish = _make_session(
        {LOW: "42", HIGH: "42"}, instance_id=HIGH, disabled=frozenset({LOW})
    )
    _sign_in(session, monkeypatch, 42)

    await tidal_flow.run_setup(session)

    finish.assert_awaited_once()


async def test_a_reconfigure_onto_a_held_account_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconfiguring onto an account a lower-id enabled instance holds aborts."""
    session, finish = _make_session({LOW: "42", HIGH: "41"}, instance_id=HIGH)
    _sign_in(session, monkeypatch, 42)

    with pytest.raises(AbortFlow, match="account_used_by_other_instance"):
        await tidal_flow.run_setup(session)

    finish.assert_not_awaited()


async def test_a_reconfigure_onto_an_account_a_loaded_higher_instance_holds_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A running higher-id instance on the account blocks a lower-id reconfigure."""
    session, finish = _make_session(
        {LOW: "41", HIGH: "42"}, instance_id=LOW, loaded=frozenset({HIGH})
    )
    _sign_in(session, monkeypatch, 42)

    with pytest.raises(AbortFlow, match="account_used_by_other_instance"):
        await tidal_flow.run_setup(session)

    finish.assert_not_awaited()
    get_provider = cast("mock.Mock", session.mass.get_provider)
    get_provider.assert_any_call(HIGH, return_unavailable=True)


async def test_a_higher_instance_that_is_not_loaded_does_not_block_a_reconfigure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A higher-id holder that failed to load at startup is not streaming on the account."""
    session, finish = _make_session({LOW: "41", HIGH: "42"}, instance_id=LOW)
    _sign_in(session, monkeypatch, 42)

    await tidal_flow.run_setup(session)

    finish.assert_awaited_once()


async def test_a_disabled_higher_instance_does_not_block_a_reconfigure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disabled higher-id holder does not block a lower-id reconfigure."""
    session, finish = _make_session(
        {LOW: "41", HIGH: "42"},
        instance_id=LOW,
        disabled=frozenset({HIGH}),
        loaded=frozenset({HIGH}),
    )
    _sign_in(session, monkeypatch, 42)

    await tidal_flow.run_setup(session)

    finish.assert_awaited_once()
