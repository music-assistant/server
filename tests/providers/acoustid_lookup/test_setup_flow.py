"""Tests for the AcoustID Lookup setup flow."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest import mock

import pytest
from music_assistant_models.errors import (
    InvalidDataError,
    RateLimited,
    ResourceTemporarilyUnavailable,
)

from music_assistant.models.setup_flow import (
    SetupFlowContext,
    SetupFlowError,
    SetupSession,
    StepExpiredError,
)
from music_assistant.providers.acoustid_lookup import setup_flow as acoustid_flow
from music_assistant.providers.acoustid_lookup.link import LinkRequest, LinkState
from music_assistant.providers.acoustid_lookup.provider import CONF_USER_KEY
from music_assistant.providers.acoustid_lookup.setup_flow import CONF_CONNECT, CONF_RECONNECT

LINK = LinkRequest(
    user_key="NEWKEY",
    code="WT14-9E3S",
    url="https://acoustid.org/link?code=WT14-9E3S",
    expires_in=900.0,
)


@pytest.fixture(autouse=True)
def sleep(monkeypatch: pytest.MonkeyPatch) -> mock.AsyncMock:
    """Skip the waits between two checks of a link."""
    sleep = mock.AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)
    return sleep


def _make_session(
    monkeypatch: pytest.MonkeyPatch,
    answers: list[dict[str, bool]],
    *,
    stored_key: str | None = None,
    reconfigure: bool = False,
) -> tuple[SetupSession, mock.AsyncMock, mock.AsyncMock]:
    """Return a session answering the forms in turn, with its finish and form mocks."""
    finish = mock.AsyncMock(return_value={"instance_id": "acoustid_lookup"})
    context = SetupFlowContext(
        kind="reconfigure" if stored_key or reconfigure else "setup",
        reason="user",
        domain="acoustid_lookup",
        setup_data={CONF_USER_KEY: stored_key} if stored_key else {},
    )
    mass = mock.Mock()
    mass.webserver.server_name = "Living room server"
    session = SetupSession(mass, "flow-test", context, finish)
    form = mock.AsyncMock(side_effect=answers)
    monkeypatch.setattr(session, "form", form)
    return session, finish, form


def _acoustid(
    monkeypatch: pytest.MonkeyPatch, *statuses: Any, link: Any = LINK
) -> tuple[mock.AsyncMock, mock.AsyncMock]:
    """Make AcoustID hand out the given link and report the given states in turn."""
    request_link = mock.AsyncMock(side_effect=link if isinstance(link, list) else [link])
    link_status = mock.AsyncMock(side_effect=list(statuses))
    monkeypatch.setattr(acoustid_flow, "request_link", request_link)
    monkeypatch.setattr(acoustid_flow, "link_status", link_status)
    return request_link, link_status


def _form_error(form: mock.AsyncMock) -> str | None:
    """Return the translation key of the error on the last form shown."""
    errors = form.call_args.kwargs.get("errors")
    if not errors:
        return None
    error = errors["base"]
    assert isinstance(error, SetupFlowError)
    return error.translation_key


async def test_skipping_finishes_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not connecting creates the provider without a user key and never calls AcoustID."""
    session, finish, form = _make_session(monkeypatch, [{CONF_CONNECT: False}])
    request_link, link_status = _acoustid(monkeypatch)

    await acoustid_flow.run_setup(session)

    assert form.call_args.kwargs["step_id"] == "user"
    assert form.call_args.args[0][0].default_value is True
    assert finish.call_args.args[1] == {CONF_USER_KEY: None}
    request_link.assert_not_awaited()
    link_status.assert_not_awaited()


async def test_approving_stores_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The link is shown until the user approves, and only then is the key stored."""
    session, finish, _ = _make_session(monkeypatch, [{CONF_CONNECT: True}])
    # a dropped connection while waiting is tried again
    request_link, link_status = _acoustid(
        monkeypatch,
        LinkState.PENDING,
        ResourceTemporarilyUnavailable("dropped"),
        LinkState.APPROVED,
    )
    external_until = mock.AsyncMock(wraps=session.external_until)
    monkeypatch.setattr(session, "external_until", external_until)

    await acoustid_flow.run_setup(session)

    request_link.assert_awaited_once_with(session.mass.http_session, "Living room server")
    step = external_until.call_args.kwargs
    assert step["url"] == LINK.url
    assert step["step_id"] == "approve"
    assert step["expires_in"] == 900.0
    assert step["copy_text"] == "WT14-9E3S"
    assert step["translation_params"] == ["WT14-9E3S"]
    assert link_status.await_count == 3
    assert link_status.call_args.args == (session.mass.http_session, "NEWKEY")
    finish.assert_awaited_once()
    assert finish.call_args.args[1] == {CONF_USER_KEY: "NEWKEY"}


async def test_reconfiguring_without_a_key_goes_straight_to_the_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconfiguring a provider that is not connected asks for a link without a form first."""
    session, finish, form = _make_session(monkeypatch, [], reconfigure=True)
    request_link, _ = _acoustid(monkeypatch, LinkState.APPROVED)

    await acoustid_flow.run_setup(session)

    form.assert_not_awaited()
    request_link.assert_awaited_once()
    assert finish.call_args.args[1] == {CONF_USER_KEY: "NEWKEY"}


async def test_reconfiguring_without_a_key_asks_before_a_second_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A declined link on that path shows the form, where the user can still skip."""
    session, finish, form = _make_session(monkeypatch, [{CONF_CONNECT: False}], reconfigure=True)
    request_link, _ = _acoustid(monkeypatch, LinkState.DECLINED)

    await acoustid_flow.run_setup(session)

    assert _form_error(form) == "link_declined"
    request_link.assert_awaited_once()
    assert finish.call_args.args[1] == {CONF_USER_KEY: None}


@pytest.mark.parametrize(
    ("state", "expected_error"),
    [
        (LinkState.DECLINED, "link_declined"),
        (LinkState.EXPIRED, "link_expired"),
        (LinkState.REVOKED, "link_revoked"),
        (LinkState.UNKNOWN, "link_unknown"),
    ],
)
async def test_a_link_that_is_not_approved_returns_to_the_form(
    monkeypatch: pytest.MonkeyPatch, state: LinkState, expected_error: str
) -> None:
    """A link the user did not approve shows the reason and waits for a new submit."""
    session, finish, form = _make_session(monkeypatch, [{CONF_CONNECT: True}])
    request_link, _ = _acoustid(monkeypatch, state)

    # the second form is never answered: no new link is requested without a submit
    with pytest.raises(StopAsyncIteration):
        await acoustid_flow.run_setup(session)

    assert form.await_count == 2
    assert _form_error(form) == expected_error
    request_link.assert_awaited_once()
    finish.assert_not_awaited()


async def test_an_expired_step_returns_to_the_form(monkeypatch: pytest.MonkeyPatch) -> None:
    """A link left unapproved until its deadline shows the form again without a new link."""
    session, finish, form = _make_session(monkeypatch, [{CONF_CONNECT: True}])
    request_link, _ = _acoustid(monkeypatch)
    monkeypatch.setattr(acoustid_flow, "_wait_for_decision", mock.Mock())
    monkeypatch.setattr(session, "external_until", mock.AsyncMock(side_effect=StepExpiredError()))

    with pytest.raises(StopAsyncIteration):
        await acoustid_flow.run_setup(session)

    assert _form_error(form) == "link_expired"
    request_link.assert_awaited_once()
    finish.assert_not_awaited()


async def test_connecting_again_after_a_declined_link(monkeypatch: pytest.MonkeyPatch) -> None:
    """A new submit after a declined link requests a fresh link and stores its key."""
    session, finish, _ = _make_session(monkeypatch, [{CONF_CONNECT: True}, {CONF_CONNECT: True}])
    second = LinkRequest(
        user_key="SECONDKEY", code="C2", url="https://acoustid.org/link?code=C2", expires_in=900.0
    )
    request_link, _ = _acoustid(
        monkeypatch, LinkState.DECLINED, LinkState.APPROVED, link=[LINK, second]
    )

    await acoustid_flow.run_setup(session)

    assert request_link.await_count == 2
    assert finish.call_args.args[1] == {CONF_USER_KEY: "SECONDKEY"}


@pytest.mark.parametrize(
    ("failure", "expected_error"),
    [
        (RateLimited("slow down"), "acoustid_rate_limited"),
        (ResourceTemporarilyUnavailable("down"), "acoustid_unreachable"),
        (InvalidDataError("odd reply"), "acoustid_unreachable"),
    ],
)
async def test_a_failed_link_request_shows_a_form_error(
    monkeypatch: pytest.MonkeyPatch, failure: Exception, expected_error: str
) -> None:
    """When AcoustID hands out no link the form returns with the reason, and can be skipped."""
    session, finish, form = _make_session(
        monkeypatch, [{CONF_CONNECT: True}, {CONF_CONNECT: False}]
    )
    request_link, _ = _acoustid(monkeypatch, link=failure)

    await acoustid_flow.run_setup(session)

    assert _form_error(form) == expected_error
    request_link.assert_awaited_once()
    assert finish.call_args.args[1] == {CONF_USER_KEY: None}


async def test_an_approved_key_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reconfiguring a connected provider shows it as connected and changes nothing."""
    session, finish, form = _make_session(
        monkeypatch, [{CONF_RECONNECT: False}], stored_key="OLDKEY"
    )
    request_link, link_status = _acoustid(monkeypatch, LinkState.APPROVED)

    await acoustid_flow.run_setup(session)

    link_status.assert_awaited_once_with(session.mass.http_session, "OLDKEY")
    assert form.call_args.kwargs["step_id"] == "connected"
    assert form.call_args.args[0][0].default_value is False
    request_link.assert_not_awaited()
    assert finish.call_args.args[1] == {CONF_USER_KEY: "OLDKEY"}


async def test_connecting_again_replaces_an_approved_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Choosing to connect again goes straight to a new link and stores its key."""
    session, finish, form = _make_session(
        monkeypatch, [{CONF_RECONNECT: True}], stored_key="OLDKEY"
    )
    request_link, _ = _acoustid(monkeypatch, LinkState.APPROVED, LinkState.APPROVED)

    await acoustid_flow.run_setup(session)

    form.assert_awaited_once()
    request_link.assert_awaited_once()
    assert finish.call_args.args[1] == {CONF_USER_KEY: "NEWKEY"}


async def test_a_declined_reconnect_keeps_the_approved_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backing out after a declined new link leaves the key that still works in place."""
    session, finish, form = _make_session(
        monkeypatch, [{CONF_RECONNECT: True}, {CONF_CONNECT: False}], stored_key="OLDKEY"
    )
    _acoustid(monkeypatch, LinkState.APPROVED, LinkState.DECLINED)

    await acoustid_flow.run_setup(session)

    assert _form_error(form) == "link_declined"
    assert finish.call_args.args[1] == {CONF_USER_KEY: "OLDKEY"}


@pytest.mark.parametrize(
    ("state", "expected_error"),
    [
        (LinkState.REVOKED, "link_revoked"),
        (LinkState.DECLINED, "link_declined"),
        (LinkState.EXPIRED, "link_expired"),
        (LinkState.UNKNOWN, "link_unknown"),
        (LinkState.PENDING, None),
    ],
)
async def test_a_key_that_no_longer_works_shows_the_connect_form(
    monkeypatch: pytest.MonkeyPatch, state: LinkState, expected_error: str | None
) -> None:
    """A stored key AcoustID no longer accepts asks to connect, and is dropped on skipping."""
    session, finish, form = _make_session(monkeypatch, [{CONF_CONNECT: False}], stored_key="OLDKEY")
    request_link, _ = _acoustid(monkeypatch, state)

    await acoustid_flow.run_setup(session)

    assert form.call_args.kwargs["step_id"] == "user"
    assert _form_error(form) == expected_error
    request_link.assert_not_awaited()
    assert finish.call_args.args[1] == {CONF_USER_KEY: None}


async def test_an_unanswered_check_keeps_the_stored_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """When AcoustID can not be asked about the stored key, skipping leaves it in place."""
    session, finish, form = _make_session(monkeypatch, [{CONF_CONNECT: False}], stored_key="OLDKEY")
    _acoustid(monkeypatch, ResourceTemporarilyUnavailable("down"))

    await acoustid_flow.run_setup(session)

    assert _form_error(form) == "acoustid_unreachable"
    assert finish.call_args.args[1] == {CONF_USER_KEY: "OLDKEY"}


async def test_a_rate_limited_check_waits_as_long_as_asked(
    monkeypatch: pytest.MonkeyPatch, sleep: mock.AsyncMock
) -> None:
    """While waiting for approval, a rate limit reply delays the next check."""
    _acoustid(monkeypatch, RateLimited("slow down", backoff_time=30), LinkState.APPROVED)

    assert await acoustid_flow._wait_for_decision(mock.Mock(), "NEWKEY") is LinkState.APPROVED

    assert [call.args[0] for call in sleep.call_args_list] == [3, 30]


async def test_checks_slow_down_while_acoustid_keeps_failing(
    monkeypatch: pytest.MonkeyPatch, sleep: mock.AsyncMock
) -> None:
    """Failed checks are spaced further apart each time, and a good reply resets the pace."""
    _acoustid(
        monkeypatch,
        *[RateLimited("slow down")] * 6,
        LinkState.PENDING,
        LinkState.APPROVED,
    )

    assert await acoustid_flow._wait_for_decision(mock.Mock(), "NEWKEY") is LinkState.APPROVED

    assert [call.args[0] for call in sleep.call_args_list] == [3, 6, 12, 24, 48, 60, 60, 3]
