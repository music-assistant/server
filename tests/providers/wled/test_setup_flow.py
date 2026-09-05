"""Tests for the WLED setup flow's port auto-suggestion."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from music_assistant.providers.wled.constants import CONF_PORT, DEFAULT_PORT
from music_assistant.providers.wled.setup_flow import _next_free_port


def _fake_sibling(port: int) -> MagicMock:
    """Build a minimal fake ProviderConfig exposing just what _port_from_config needs."""
    sibling = MagicMock()
    sibling.get_value.side_effect = lambda key: port if key == CONF_PORT else None
    return sibling


def _fake_session(siblings: list[MagicMock]) -> MagicMock:
    """Build a minimal fake SetupSession exposing session.mass.config.get_provider_configs."""
    session = MagicMock()
    session.mass.config.get_provider_configs = AsyncMock(return_value=siblings)
    return session


class TestNextFreePort:
    """
    Tests for the auto-suggested zone port shown in the setup form.

    Without this suggestion, every new instance would start at the hardcoded
    default port and get rejected by handle_async_init's duplicate-port
    check before the user ever gets a chance to change it -- see the module
    docstring on setup_flow.py.
    """

    async def test_no_existing_instances_suggests_the_default_port(self) -> None:
        """With no siblings, the suggestion is just DEFAULT_PORT."""
        session = _fake_session([])
        assert await _next_free_port(session) == DEFAULT_PORT

    async def test_default_port_taken_suggests_the_next_one(self) -> None:
        """One sibling on DEFAULT_PORT bumps the suggestion by one."""
        session = _fake_session([_fake_sibling(DEFAULT_PORT)])
        assert await _next_free_port(session) == DEFAULT_PORT + 1

    async def test_skips_multiple_consecutive_taken_ports(self) -> None:
        """Two siblings on consecutive ports bump the suggestion past both."""
        session = _fake_session([_fake_sibling(DEFAULT_PORT), _fake_sibling(DEFAULT_PORT + 1)])
        assert await _next_free_port(session) == DEFAULT_PORT + 2

    async def test_finds_a_gap_rather_than_always_taking_the_top(self) -> None:
        """A free port below the highest used one is suggested, not just max+1."""
        session = _fake_session([_fake_sibling(DEFAULT_PORT), _fake_sibling(DEFAULT_PORT + 2)])
        assert await _next_free_port(session) == DEFAULT_PORT + 1

    async def test_queries_the_wled_domain_only(self) -> None:
        """The sibling scan must be scoped to the wled provider domain."""
        session = _fake_session([])
        await _next_free_port(session)
        session.mass.config.get_provider_configs.assert_awaited_once_with(
            provider_domain="wled", include_values=True
        )
