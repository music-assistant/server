"""Tests for the WLED setup flow's port auto-suggestion."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from music_assistant.providers.wled.constants import CONF_PORT, DEFAULT_PORT
from music_assistant.providers.wled.setup_flow import _next_free_port


def _fake_sibling(instance_id: str) -> MagicMock:
    """Build a minimal fake ProviderConfig exposing just what _port_from_config needs."""
    sibling = MagicMock()
    sibling.instance_id = instance_id
    return sibling


def _fake_session(
    siblings: list[MagicMock],
    raw_ports: dict[str, int] | None = None,
    setup_ports: dict[str, int] | None = None,
) -> MagicMock:
    """
    Build a minimal fake SetupSession.

    :param siblings: The fake sibling ProviderConfigs the scan should see.
    :param raw_ports: instance_id -> port explicitly stored under "values",
        e.g. after editing the port in the provider's options UI.
    :param setup_ports: instance_id -> port stored in "setup_data" -- what
        the setup flow originally persisted, readable even for an
        unloaded/failed sibling (see _port_from_config).
    """
    raw_ports = raw_ports or {}
    setup_ports = setup_ports or {}
    session = MagicMock()
    session.mass.config.get_provider_configs = AsyncMock(return_value=siblings)
    session.mass.config.get_raw_provider_config_value = MagicMock(
        side_effect=lambda instance_id, key, default=None: (
            raw_ports.get(instance_id, default) if key == CONF_PORT else default
        )
    )
    session.mass.config.get_provider_setup_value = MagicMock(
        side_effect=lambda instance_id, key, default=None: (
            setup_ports.get(instance_id, default) if key == CONF_PORT else default
        )
    )
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
        session = _fake_session([_fake_sibling("a")], setup_ports={"a": DEFAULT_PORT})
        assert await _next_free_port(session) == DEFAULT_PORT + 1

    async def test_skips_multiple_consecutive_taken_ports(self) -> None:
        """Two siblings on consecutive ports bump the suggestion past both."""
        session = _fake_session(
            [_fake_sibling("a"), _fake_sibling("b")],
            setup_ports={"a": DEFAULT_PORT, "b": DEFAULT_PORT + 1},
        )
        assert await _next_free_port(session) == DEFAULT_PORT + 2

    async def test_finds_a_gap_rather_than_always_taking_the_top(self) -> None:
        """A free port below the highest used one is suggested, not just max+1."""
        session = _fake_session(
            [_fake_sibling("a"), _fake_sibling("b")],
            setup_ports={"a": DEFAULT_PORT, "b": DEFAULT_PORT + 2},
        )
        assert await _next_free_port(session) == DEFAULT_PORT + 1

    async def test_queries_the_wled_domain_only(self) -> None:
        """The sibling scan must be scoped to the wled provider domain."""
        session = _fake_session([])
        await _next_free_port(session)
        session.mass.config.get_provider_configs.assert_awaited_once_with(
            provider_domain="wled", include_values=True
        )

    async def test_unloaded_sibling_port_read_from_setup_data(self) -> None:
        """
        An unloaded/failed sibling's port must still be read from setup_data.

        Regression test: get_provider_configs(include_values=True) only
        resolves the server-injected default config entries for a sibling
        that isn't loaded, so it never has a CONF_PORT entry to read a
        "regular" value from -- reading setup_data directly is required.
        """
        session = _fake_session([_fake_sibling("a")], setup_ports={"a": DEFAULT_PORT})
        assert await _next_free_port(session) == DEFAULT_PORT + 1

    async def test_explicit_override_wins_over_stale_setup_data(self) -> None:
        """
        A port edited via the options UI after setup must take precedence.

        save_provider_config only ever updates "values", never setup_data,
        so once a user changes the port, setup_data still holds the
        original suggested port. The raw stored override must win, or the
        sibling would keep reporting its now-stale original port.
        """
        session = _fake_session(
            [_fake_sibling("a")],
            raw_ports={"a": DEFAULT_PORT + 5},
            setup_ports={"a": DEFAULT_PORT},
        )
        # DEFAULT_PORT itself is free -- only DEFAULT_PORT + 5 is actually taken.
        assert await _next_free_port(session) == DEFAULT_PORT
