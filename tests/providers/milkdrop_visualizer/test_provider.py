"""Tests for the MilkDrop visualizer provider's viewer-facing API commands."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, Mock

from music_assistant.providers.milkdrop_visualizer.provider import (
    CAPABILITY_COMMAND,
    CONF_COLOR_TINT,
    CONF_SHOW_ON_DASHBOARDS,
    CONFIG_COMMAND,
    MAX_REPORT_FIELD_LEN,
    PREF_PALETTE_COLORS,
    PREF_PALETTE_RAMP,
    MilkdropVisualizerProvider,
)


def _provider() -> tuple[MilkdropVisualizerProvider, Mock, AsyncMock]:
    """Return a provider instance (with its logger and config-read mocks) without full setup."""
    provider = MilkdropVisualizerProvider.__new__(MilkdropVisualizerProvider)
    logger = Mock()
    config_value = AsyncMock()
    mass = Mock()
    mass.config.get_provider_config_value = config_value
    mass.config.get_raw_provider_config_value = Mock(return_value=None)
    mass.config.remove_provider_config_value = AsyncMock()
    mass.webserver.auth.list_users = AsyncMock(return_value=[])
    mass.webserver.auth.update_user_preferences = AsyncMock()
    mocked = cast("Any", provider)
    mocked.logger = logger
    mocked.config = Mock()
    mocked.mass = mass
    return provider, logger, config_value


async def test_loaded_in_mass_registers_nothing_while_unloading() -> None:
    """A stale instance must not take the live one's route or commands on its way out."""
    provider, _logger, _config_value = _provider()
    mocked = cast("Any", provider)
    mocked.unloading = True
    mocked._relay = Mock()
    mocked._unregister_handles = []

    await provider.loaded_in_mass()

    # the relay route unregisters by name too, and this instance is past its own close()
    mocked._relay.setup.assert_not_called()
    mocked.mass.register_api_command.assert_not_called()
    mocked.mass.config.get_raw_provider_config_value.assert_not_called()
    assert provider._unregister_handles == []


async def test_loaded_in_mass_registers_the_viewer_commands() -> None:
    """A live instance exposes both viewer-facing commands and keeps their unregister handles."""
    provider, _logger, _config_value = _provider()
    mocked = cast("Any", provider)
    mocked.unloading = False
    mocked._relay = Mock()
    mocked._unregister_handles = []

    await provider.loaded_in_mass()

    mocked._relay.setup.assert_called_once_with()
    registered = [call.args[0] for call in mocked.mass.register_api_command.call_args_list]
    assert registered == [CONFIG_COMMAND, CAPABILITY_COMMAND]
    assert len(provider._unregister_handles) == 2


async def test_loaded_in_mass_runs_the_color_tint_migration_before_setup() -> None:
    """The migration must settle before the relay (and thus viewer traffic) comes up."""
    provider, _logger, _config_value = _provider()
    mocked = cast("Any", provider)
    mocked.unloading = False
    mocked._unregister_handles = []
    order: list[str] = []
    mocked._migrate_color_tint = AsyncMock(side_effect=lambda: order.append("migrate"))
    mocked._relay = Mock()
    mocked._relay.setup = Mock(side_effect=lambda: order.append("setup"))

    await provider.loaded_in_mass()

    assert order == ["migrate", "setup"]


async def test_visualizer_config_reflects_dashboard_setting() -> None:
    """The viewer config command reads the live show_on_dashboards setting."""
    provider, _logger, config_value = _provider()
    config_value.return_value = True
    assert await provider.get_visualizer_config() == {CONF_SHOW_ON_DASHBOARDS: True}
    # Read through the config controller (live), not this instance's snapshot.
    assert config_value.call_args.args == (provider.config.instance_id, CONF_SHOW_ON_DASHBOARDS)
    config_value.return_value = False
    assert await provider.get_visualizer_config() == {CONF_SHOW_ON_DASHBOARDS: False}


async def test_capability_report_is_logged_at_debug_with_trimmed_fields() -> None:
    """A report is one debug line, and a viewer cannot forge log lines or flood the log."""
    provider, logger, _config_value = _provider()

    await provider.report_capability(
        webgl2=False,
        renderer="none",
        user_agent="x" * 900,
        gpu="gpu\nforged",
        error="boom\nViewer error: forged",
        dashboard_id="chromecast_abc",
    )

    logger.debug.assert_called_once()
    logged_args = logger.debug.call_args.args[1:]
    assert logged_args == (
        "chromecast_abc",
        False,
        "none",
        "gpu forged",
        "x" * MAX_REPORT_FIELD_LEN,
        "boom Viewer error: forged",
    )


async def test_color_tint_migration_is_a_noop_without_the_old_key() -> None:
    """No stored color_tint means nothing to migrate: no users listed, nothing removed."""
    provider, _logger, _config_value = _provider()
    mocked = cast("Any", provider)

    await provider._migrate_color_tint()

    mocked.mass.webserver.auth.list_users.assert_not_called()
    mocked.mass.config.remove_provider_config_value.assert_not_called()


async def test_color_tint_migration_folds_an_explicit_false_into_user_preferences() -> None:
    """A disabled tint is folded into every user, keeping each user's own existing value."""
    provider, _logger, _config_value = _provider()
    mocked = cast("Any", provider)
    mocked.mass.config.get_raw_provider_config_value = Mock(return_value=False)
    user_without_prefs = Mock(user_id="u1", preferences={})
    user_with_partial_prefs = Mock(user_id="u2", preferences={PREF_PALETTE_COLORS: True})
    mocked.mass.webserver.auth.list_users = AsyncMock(
        return_value=[user_without_prefs, user_with_partial_prefs]
    )

    await provider._migrate_color_tint()

    calls = {
        call.args[0].user_id: call.args[1]
        for call in mocked.mass.webserver.auth.update_user_preferences.call_args_list
    }
    assert calls["u1"] == {PREF_PALETTE_COLORS: False, PREF_PALETTE_RAMP: 0}
    assert calls["u2"] == {PREF_PALETTE_COLORS: True, PREF_PALETTE_RAMP: 0}
    mocked.mass.config.remove_provider_config_value.assert_awaited_once_with(
        provider.instance_id, CONF_COLOR_TINT
    )


async def test_color_tint_migration_skips_users_that_already_carry_both_keys() -> None:
    """A user who already has both palette preferences set is left untouched."""
    provider, _logger, _config_value = _provider()
    mocked = cast("Any", provider)
    mocked.mass.config.get_raw_provider_config_value = Mock(return_value=False)
    user = Mock(user_id="u1", preferences={PREF_PALETTE_COLORS: True, PREF_PALETTE_RAMP: 50})
    mocked.mass.webserver.auth.list_users = AsyncMock(return_value=[user])

    await provider._migrate_color_tint()

    mocked.mass.webserver.auth.update_user_preferences.assert_not_called()
    mocked.mass.config.remove_provider_config_value.assert_awaited_once_with(
        provider.instance_id, CONF_COLOR_TINT
    )


async def test_color_tint_migration_only_removes_the_key_when_tint_was_enabled() -> None:
    """A stored `True` (the old default) needs no per-user migration, only cleanup."""
    provider, _logger, _config_value = _provider()
    mocked = cast("Any", provider)
    mocked.mass.config.get_raw_provider_config_value = Mock(return_value=True)

    await provider._migrate_color_tint()

    mocked.mass.webserver.auth.list_users.assert_not_called()
    mocked.mass.config.remove_provider_config_value.assert_awaited_once_with(
        provider.instance_id, CONF_COLOR_TINT
    )


async def test_color_tint_migration_keeps_the_key_when_a_user_update_fails() -> None:
    """A failed per-user update must not raise, nor drop the key that would retry it."""
    provider, logger, _config_value = _provider()
    mocked = cast("Any", provider)
    mocked.mass.config.get_raw_provider_config_value = Mock(return_value=False)
    user = Mock(user_id="u1", preferences={})
    mocked.mass.webserver.auth.list_users = AsyncMock(return_value=[user])
    mocked.mass.webserver.auth.update_user_preferences = AsyncMock(side_effect=RuntimeError("boom"))

    await provider._migrate_color_tint()

    logger.warning.assert_called_once()
    mocked.mass.config.remove_provider_config_value.assert_not_called()
