"""Tests for the myNoise interactive setup flow."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp

from music_assistant.providers.mynoise import setup_flow
from music_assistant.providers.mynoise.constants import CONF_PATRON_EMAIL

if TYPE_CHECKING:
    import pytest


def _session(setup_data: dict[str, Any] | None = None) -> MagicMock:
    """Build a SetupSession stand-in with stubbed form/finish steps."""
    session = MagicMock()
    session.context.setup_data = dict(setup_data or {})
    session.form = AsyncMock()
    session.finish = AsyncMock()
    return session


async def test_setup_verifies_email_before_finish(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setup only finishes after myNoise accepted the patron email."""
    session = _session()
    session.form.return_value = {CONF_PATRON_EMAIL: "me@example.com"}
    login = AsyncMock(return_value=True)
    monkeypatch.setattr(setup_flow, "patron_login", login)

    await setup_flow.run_setup(session)

    login.assert_awaited_once_with(session.mass, "me@example.com")
    session.finish.assert_awaited_once_with({CONF_PATRON_EMAIL: "me@example.com"})


async def test_setup_reprompts_rejected_email(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rejected email re-prompts the form instead of finishing."""
    session = _session()
    session.form.side_effect = [
        {CONF_PATRON_EMAIL: "nobody@example.com"},
        {CONF_PATRON_EMAIL: "me@example.com"},
    ]
    login = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr(setup_flow, "patron_login", login)

    await setup_flow.run_setup(session)

    assert session.form.await_count == 2
    # the rejection is shown on the re-prompted form
    assert session.form.await_args_list[1].kwargs["errors"]
    session.finish.assert_awaited_once_with({CONF_PATRON_EMAIL: "me@example.com"})


async def test_setup_reprompts_when_site_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A network hiccup re-prompts (retry) instead of aborting the flow."""
    session = _session()
    session.form.return_value = {CONF_PATRON_EMAIL: "me@example.com"}
    login = AsyncMock(side_effect=[aiohttp.ClientError("offline"), True])
    monkeypatch.setattr(setup_flow, "patron_login", login)

    await setup_flow.run_setup(session)

    assert login.await_count == 2
    session.finish.assert_awaited_once_with({CONF_PATRON_EMAIL: "me@example.com"})


async def test_setup_prefills_known_email(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reconfiguring pre-fills the form with the previously collected email."""
    session = _session({CONF_PATRON_EMAIL: "old@example.com"})
    session.form.return_value = {CONF_PATRON_EMAIL: "old@example.com"}
    monkeypatch.setattr(setup_flow, "patron_login", AsyncMock(return_value=True))

    await setup_flow.run_setup(session)

    entries = session.form.await_args.args[0]
    assert entries[0].value == "old@example.com"
