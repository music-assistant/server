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
    mass.config.get_provider_configs = mock.AsyncMock(return_value=[])
    mass.config.get_provider_setup_value = mock.Mock(return_value=None)
    mass.get_provider = mock.Mock(return_value=None)

    async def finish(_session: SetupSession, _submitted: dict[str, Any]) -> dict[str, str]:
        return {"instance_id": "spotify--test"}

    context = SetupFlowContext(
        kind="reconfigure" if instance_id else "setup",
        reason="user",
        domain="spotify",
        instance_id=instance_id,
    )
    return SetupSession(mass, "flow-test", context, finish)


def _stub_configs(session: SetupSession, accounts: dict[str, str | None]) -> None:
    """Point the session at the given configured Spotify instances and their stored accounts."""
    session.mass.config.get_provider_configs = mock.AsyncMock(  # type: ignore[method-assign]
        return_value=[mock.Mock(instance_id=instance_id) for instance_id in accounts]
    )
    session.mass.config.get_provider_setup_value = mock.Mock(  # type: ignore[method-assign]
        side_effect=lambda instance_id, _key: accounts.get(instance_id)
    )


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
    _stub_configs(session, {other_instance_id: "u1"})
    _stub_me(session, payload={"id": "u1", "product": "premium"})

    if aborts:
        with pytest.raises(AbortFlow, match="account_already_configured"):
            await spotify_flow._verify_account(session, "at-test")
    else:
        assert await spotify_flow._verify_account(session, "at-test") == "u1"


async def test_a_disabled_instance_still_holds_its_account() -> None:
    """An instance that is not running is still found through its stored account id."""
    session = _make_session()
    # configured but absent from mass.providers, as a disabled or failed instance is
    _stub_configs(session, {"spotify--disabled": "u1"})
    _stub_me(session, payload={"id": "u1", "product": "premium"})

    with pytest.raises(AbortFlow, match="account_already_configured"):
        await spotify_flow._verify_account(session, "at-test")


async def test_a_config_without_a_stored_account_falls_back_to_the_instance() -> None:
    """A configuration predating the stored account id is compared via its running instance."""
    session = _make_session()
    _stub_configs(session, {"spotify--legacy": None})
    legacy = mock.MagicMock(spec=SpotifyProvider)
    legacy.account_id = "u1"
    session.mass.get_provider = mock.Mock(return_value=legacy)  # type: ignore[method-assign]
    _stub_me(session, payload={"id": "u1", "product": "premium"})

    with pytest.raises(AbortFlow, match="account_already_configured"):
        await spotify_flow._verify_account(session, "at-test")


async def test_a_different_account_is_accepted() -> None:
    """A second account alongside an existing instance is allowed and returned."""
    session = _make_session()
    _stub_configs(session, {"spotify--other": "u1"})
    _stub_me(session, payload={"id": "u2", "product": "premium"})

    assert await spotify_flow._verify_account(session, "at-test") == "u2"


@pytest.mark.parametrize("payload", [None, [], "nope"])
async def test_a_malformed_account_response_does_not_block_the_setup(payload: Any) -> None:
    """A 200 whose body is not an object must fail open like any other bad lookup."""
    session = _make_session()
    _stub_me(session, payload=payload)

    assert await spotify_flow._verify_account(session, "at-test") is None
