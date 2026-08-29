"""Tests for the MilkDrop visualizer provider's viewer-facing API commands."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, Mock

from music_assistant.providers.milkdrop_visualizer.provider import (
    CAPABILITY_COMMAND,
    CONF_SHOW_ON_DASHBOARDS,
    CONFIG_COMMAND,
    MAX_REPORT_FIELD_LEN,
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
