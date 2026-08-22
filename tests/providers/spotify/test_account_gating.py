"""
Tests for the Spotify setup flow's account checks.

Right after the sign-in the flow refuses accounts that cannot work: one without
Spotify Premium (librespot refuses to stream for a free account) and one that is
already set up on another provider instance.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest
from aiohttp import ClientError

from music_assistant.models.setup_flow import AbortFlow, SetupFlowContext, SetupSession
from music_assistant.providers.spotify import setup_flow as spotify_flow
from music_assistant.providers.spotify.provider import SpotifyProvider


def _make_session(*, instance_id: str | None = None) -> SetupSession:
    """Return a setup session for a fresh setup (or a reconfigure of the given instance)."""
    mass = mock.Mock()
    mass.providers = []

    async def finish(_session: SetupSession, _submitted: dict[str, Any]) -> dict[str, str]:
        return {"instance_id": "spotify--test"}

    context = SetupFlowContext(
        kind="reconfigure" if instance_id else "setup",
        reason="user",
        domain="spotify",
        instance_id=instance_id,
    )
    return SetupSession(mass, "flow-test", context, finish)


def _stub_me(session: SetupSession, *, status: int = 200, payload: Any = None) -> None:
    """Point the session's http_session at a canned GET /me response."""
    response = mock.MagicMock()
    response.status = status
    response.json = mock.AsyncMock(return_value=payload)
    session.mass.http_session.get = mock.MagicMock(  # type: ignore[method-assign]
        return_value=mock.MagicMock(
            __aenter__=mock.AsyncMock(return_value=response), __aexit__=mock.AsyncMock()
        )
    )


@pytest.mark.parametrize(
    ("product", "aborts"),
    [("premium", False), ("free", True), ("open", True), ("", False), (None, False)],
)
async def test_non_premium_accounts_are_turned_away(product: str | None, aborts: bool) -> None:
    """Only a non-Premium answer aborts; an absent product field is not held against the user."""
    session = _make_session()
    payload = {"id": "u1"} if product is None else {"id": "u1", "product": product}
    _stub_me(session, payload=payload)

    if aborts:
        with pytest.raises(AbortFlow, match="premium_required"):
            await spotify_flow._verify_account(session, "at-test")
    else:
        await spotify_flow._verify_account(session, "at-test")


async def test_a_failing_lookup_does_not_block_the_setup() -> None:
    """A lookup Spotify answers with an error must not stop the user from setting up."""
    session = _make_session()
    _stub_me(session, status=503)

    await spotify_flow._verify_account(session, "at-test")


async def test_an_unreachable_lookup_does_not_block_the_setup() -> None:
    """A lookup that never completes (transport error/timeout) must not stop the setup."""
    session = _make_session()
    session.mass.http_session.get = mock.MagicMock(  # type: ignore[method-assign]
        side_effect=ClientError("boom")
    )

    await spotify_flow._verify_account(session, "at-test")


@pytest.mark.parametrize(
    ("setup_instance_id", "other_instance_id", "aborts"),
    [
        # a fresh setup adding an account another instance already serves
        (None, "spotify--other", True),
        # a reconfigure of a different instance
        ("spotify--test", "spotify--other", True),
        # a reconfigure of the very instance that owns the account
        ("spotify--test", "spotify--test", False),
    ],
)
async def test_an_already_configured_account_is_refused(
    setup_instance_id: str | None, other_instance_id: str, aborts: bool
) -> None:
    """An account another instance already serves is refused; a reconfigure keeps its own."""
    session = _make_session(instance_id=setup_instance_id)
    existing = mock.MagicMock(spec=SpotifyProvider)
    existing.instance_id = other_instance_id
    existing.account_id = "u1"
    session.mass.providers = [existing]  # type: ignore[misc]
    _stub_me(session, payload={"id": "u1", "product": "premium"})

    if aborts:
        with pytest.raises(AbortFlow, match="account_already_configured"):
            await spotify_flow._verify_account(session, "at-test")
    else:
        await spotify_flow._verify_account(session, "at-test")
