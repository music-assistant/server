"""Tests for the WLED setup flow's port auto-suggestion."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from music_assistant.models.setup_flow import SetupFlowContext
from music_assistant.providers.wled.constants import CONF_PORT, DEFAULT_PORT
from music_assistant.providers.wled.setup_flow import _next_free_port, run_setup


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


def _fake_flow_session(
    context: SetupFlowContext, siblings: list[MagicMock] | None = None
) -> MagicMock:
    """Build a minimal fake SetupSession for driving run_setup() end to end."""
    session = MagicMock()
    session.context = context
    session.mass.config.get_provider_configs = AsyncMock(return_value=siblings or [])
    session.mass.config.get_raw_provider_config_value = MagicMock(return_value=None)
    session.mass.config.get_provider_setup_value = MagicMock(return_value=None)
    session.form = AsyncMock(return_value={})
    session.finish = AsyncMock(return_value={"instance_id": "wled_1"})
    return session


class TestRunSetupPortDefault:
    """
    Tests for which port run_setup suggests, for a fresh setup vs. a reconfigure.

    Regression coverage: a reconfigure flow runs the same run_setup coroutine as a
    fresh setup (see the config controller's provider_setup/provider_reconfigure
    dispatch), so without branching on session.context.kind, reopening an existing
    zone's settings would re-run the free-port scan -- which always finds a
    *different* port than the instance's own (since the scan counts it as taken) --
    and silently move the zone if the user just accepts the form.
    """

    async def test_reconfigure_defaults_to_the_instance_s_current_port(self) -> None:
        """An explicit port override (from the options UI) must be kept as-is."""
        context = SetupFlowContext(
            kind="reconfigure",
            reason="user",
            domain="wled",
            instance_id="wled_1",
            setup_data={CONF_PORT: DEFAULT_PORT},
            values={CONF_PORT: DEFAULT_PORT + 5},
        )
        session = _fake_flow_session(context)

        await run_setup(session)

        entries = session.form.call_args.args[0]
        assert entries[0].default_value == DEFAULT_PORT + 5

    async def test_reconfigure_falls_back_to_setup_data_port(self) -> None:
        """With no options-UI override, the originally chosen setup_data port is kept."""
        context = SetupFlowContext(
            kind="reconfigure",
            reason="user",
            domain="wled",
            instance_id="wled_1",
            setup_data={CONF_PORT: DEFAULT_PORT + 2},
            values={},
        )
        session = _fake_flow_session(context)

        await run_setup(session)

        entries = session.form.call_args.args[0]
        assert entries[0].default_value == DEFAULT_PORT + 2

    async def test_reconfigure_does_not_rescan_sibling_ports(self) -> None:
        """
        Reconfigure must not run the free-port scan at all.

        The scan always finds a *different* port than the instance's own current one.
        """
        context = SetupFlowContext(
            kind="reconfigure",
            reason="user",
            domain="wled",
            instance_id="wled_1",
            setup_data={CONF_PORT: DEFAULT_PORT},
            values={},
        )
        session = _fake_flow_session(context)

        await run_setup(session)

        session.mass.config.get_provider_configs.assert_not_awaited()

    async def test_fresh_setup_still_scans_for_a_free_port(self) -> None:
        """A brand new instance (no instance_id yet) still gets the auto-suggested port."""
        sibling = _fake_sibling("existing")
        context = SetupFlowContext(kind="setup", reason="user", domain="wled")
        session = _fake_flow_session(context, siblings=[sibling])
        session.mass.config.get_provider_setup_value = MagicMock(
            side_effect=lambda instance_id, key, default=None: (
                DEFAULT_PORT if instance_id == "existing" and key == CONF_PORT else default
            )
        )

        await run_setup(session)

        entries = session.form.call_args.args[0]
        assert entries[0].default_value == DEFAULT_PORT + 1


class TestRunSetupSyncsValuesOverrideOnReconfigure:
    """
    A port change submitted through Reconfigure must actually take effect.

    Regression coverage: session.finish() only ever persists the submitted port into
    setup_data (see SetupSession.finish's docstring), but _port_from_config prefers a
    "values" override -- written by the provider's normal settings page, see
    ConfigController.set_raw_provider_config_value -- over setup_data. Without
    explicitly syncing the two, a port change submitted through Reconfigure would be
    silently shadowed forever by a pre-existing values entry.
    """

    async def test_new_port_is_synced_to_values_when_an_override_already_exists(self) -> None:
        """An existing values override must be updated so the new port actually wins."""
        context = SetupFlowContext(
            kind="reconfigure",
            reason="user",
            domain="wled",
            instance_id="wled_1",
            setup_data={CONF_PORT: DEFAULT_PORT},
            values={CONF_PORT: DEFAULT_PORT + 5},
        )
        session = _fake_flow_session(context)
        session.form.return_value = {CONF_PORT: DEFAULT_PORT + 9}

        await run_setup(session)

        session.mass.config.set_raw_provider_config_value.assert_called_once_with(
            "wled_1", CONF_PORT, DEFAULT_PORT + 9
        )

    async def test_no_values_write_when_no_override_previously_existed(self) -> None:
        """Without a pre-existing values override, the setup_data write alone is enough."""
        context = SetupFlowContext(
            kind="reconfigure",
            reason="user",
            domain="wled",
            instance_id="wled_1",
            setup_data={CONF_PORT: DEFAULT_PORT},
            values={},
        )
        session = _fake_flow_session(context)
        session.form.return_value = {CONF_PORT: DEFAULT_PORT + 9}

        await run_setup(session)

        session.mass.config.set_raw_provider_config_value.assert_not_called()

    async def test_fresh_setup_never_writes_to_values(self) -> None:
        """A brand-new instance has nothing to sync -- setup_data is the only store yet."""
        context = SetupFlowContext(kind="setup", reason="user", domain="wled")
        session = _fake_flow_session(context)

        await run_setup(session)

        session.mass.config.set_raw_provider_config_value.assert_not_called()
