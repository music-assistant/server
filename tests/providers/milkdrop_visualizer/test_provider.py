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
    UNKNOWN_DISPLAY,
    MilkdropVisualizerProvider,
)


def _provider(
    sessions: list[Any] | None = None,
) -> tuple[MilkdropVisualizerProvider, Mock, AsyncMock]:
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
    mass.dashboard.get_dashboard_sessions = AsyncMock(return_value=sessions or [])
    mocked = cast("Any", provider)
    mocked.logger = logger
    mocked.config = Mock()
    mocked.mass = mass
    mocked._last_report = {}
    return provider, logger, config_value


def _session(dashboard_id: str) -> Mock:
    """Return a dashboard-session-shaped mock for the given endpoint."""
    session = Mock()
    session.dashboard_id = dashboard_id
    return session


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


async def test_capability_report_is_logged() -> None:
    """A capability report is logged with all its fields."""
    provider, logger, _config_value = _provider()
    await provider.report_capability(webgl2=False, renderer="none", user_agent="CrKey/1.56")
    assert logger.info.called
    logged_args = logger.info.call_args.args
    assert False in logged_args
    assert "none" in logged_args
    assert "CrKey/1.56" in logged_args


async def test_capability_report_tolerates_missing_fields() -> None:
    """A capability report with no fields at all must not raise."""
    provider, logger, _config_value = _provider()
    await provider.report_capability()
    assert logger.info.called


async def test_capability_report_tolerates_malformed_render_fields() -> None:
    """A render report with a non-numeric late_ratio is still logged, not raised."""
    provider, logger, _config_value = _provider()
    await provider.report_capability(render={"note": "steady", "late_ratio": "not-a-number"})
    assert logger.info.called
    assert 0 in logger.info.call_args.args


async def test_capability_report_zeroes_malformed_ratios_instead_of_raising() -> None:
    """An infinite, NaN, boolean or string ratio is malformed and logs as 0, not a raise."""
    # blocked_ratio is pinned to a distinct value so late_pct=0 is unambiguous in the log line
    for late_ratio in (float("inf"), float("nan"), True, "0.5"):
        provider, logger, _config_value = _provider()
        await provider.report_capability(
            render={"note": "steady", "late_ratio": late_ratio, "blocked_ratio": 0.5}
        )
        assert logger.info.called, late_ratio
        assert 0 in logger.info.call_args.args, late_ratio


async def test_capability_report_clamps_out_of_range_ratios_to_a_hundred_percent() -> None:
    """A ratio above 1, finite-but-huge (1e308) or otherwise, clamps to 100 without raising."""
    for late_ratio in (1.5, 1e308):
        provider, logger, _config_value = _provider()
        await provider.report_capability(
            render={"note": "steady", "late_ratio": late_ratio, "blocked_ratio": 0.5}
        )
        assert logger.info.called, late_ratio
        assert 100 in logger.info.call_args.args, late_ratio


async def test_capability_report_converts_a_well_formed_ratio_to_a_percentage() -> None:
    """A well-formed ratio converts straightforwardly to a whole percentage."""
    provider, logger, _config_value = _provider()
    await provider.report_capability(
        render={"note": "steady", "late_ratio": 0.25, "blocked_ratio": 0.5}
    )
    assert 25 in logger.info.call_args.args


async def test_capability_report_flattens_and_caps_viewer_strings() -> None:
    """A viewer cannot forge log lines or flood the log through the fields it reports."""
    provider, logger, _config_value = _provider()
    await provider.report_capability(error="boom\nViewer error: forged", user_agent="x" * 900)
    logged_args = logger.warning.call_args.args
    assert logged_args[2] == "boom Viewer error: forged"
    assert logged_args[3] == "x" * MAX_REPORT_FIELD_LEN


async def test_render_report_flattens_and_caps_every_viewer_field() -> None:
    """Every render field is viewer-supplied, so none of them may forge a log line."""
    provider, logger, _config_value = _provider()

    await provider.report_capability(
        render={
            "note": "steady\nViewer render forged",
            "level": "x" * 900,
            "gpu_warp": "1\n2",
            "preset": "a\nb",
        }
    )

    logged_args = logger.info.call_args.args
    assert "steady Viewer render forged" in logged_args
    assert "x" * MAX_REPORT_FIELD_LEN in logged_args
    assert "\n" not in "".join(str(arg) for arg in logged_args)


async def test_render_report_names_the_reporting_display() -> None:
    """A dashboard id with a live session identifies the display it reported for."""
    provider, logger, _config_value = _provider(sessions=[_session("chromecast_abc")])

    await provider.report_capability(webgl2=True, dashboard_id="chromecast_abc")

    assert "chromecast_abc" in logger.info.call_args.args


async def test_report_from_an_unknown_display_shares_one_bucket() -> None:
    """Minting fresh ids must not buy fresh cooldowns: an unknown id is never its own key."""
    provider, logger, _config_value = _provider(sessions=[_session("chromecast_abc")])

    await provider.report_capability(webgl2=True, dashboard_id="made-up")

    assert "made-up" not in logger.info.call_args.args
    assert UNKNOWN_DISPLAY in logger.info.call_args.args

    # a second, different made-up id lands in the same bucket, so it is already spent
    await provider.report_capability(webgl2=True, dashboard_id="also-made-up")

    assert logger.info.call_count == 1
    assert logger.debug.call_count == 1


async def test_an_error_is_not_buried_by_a_chatty_renderer() -> None:
    """Errors are the only evidence these displays produce: render reports must not spend them."""
    provider, logger, _config_value = _provider(sessions=[_session("chromecast_abc")])

    await provider.report_capability(webgl2=True, dashboard_id="chromecast_abc")
    await provider.report_capability(error="boom", dashboard_id="chromecast_abc")

    logger.warning.assert_called_once()


async def test_repeat_reports_within_the_cooldown_drop_to_debug() -> None:
    """A display cannot flood the log: only the first report in a cooldown window is logged."""
    provider, logger, _config_value = _provider(sessions=[_session("chromecast_abc")])

    for _ in range(3):
        await provider.report_capability(webgl2=True, dashboard_id="chromecast_abc")

    assert logger.info.call_count == 1
    assert logger.debug.call_count == 2


async def test_the_cooldown_is_kept_per_display() -> None:
    """One noisy display must not silence another one's first report."""
    provider, logger, _config_value = _provider(
        sessions=[_session("chromecast_abc"), _session("kiosk_1")]
    )

    await provider.report_capability(webgl2=True, dashboard_id="chromecast_abc")
    await provider.report_capability(webgl2=True, dashboard_id="kiosk_1")

    assert logger.info.call_count == 2
    logger.debug.assert_not_called()


async def test_unload_clears_the_report_cooldown_cache() -> None:
    """Unload must not leave rate-limit state behind for a later load of the same instance."""
    provider, _logger, _config_value = _provider()
    mocked = cast("Any", provider)
    mocked._relay = Mock()
    mocked._relay.close = AsyncMock()
    handle1, handle2 = Mock(), Mock()
    mocked._unregister_handles = [handle1, handle2]
    mocked._last_report = {("chromecast_abc", "render"): 123.0}

    await provider.unload()

    handle1.assert_called_once_with()
    handle2.assert_called_once_with()
    assert mocked._unregister_handles == []
    assert mocked._last_report == {}


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
