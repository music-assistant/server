"""Tests for the MilkDrop visualizer provider's viewer-facing API commands."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, Mock

from music_assistant.providers.milkdrop_visualizer.provider import (
    CAPABILITY_COMMAND,
    CONF_SHOW_ON_DASHBOARDS,
    CONFIG_COMMAND,
    MAX_REPORT_FIELD_LEN,
    MilkdropVisualizerProvider,
)


def _provider() -> tuple[MilkdropVisualizerProvider, Mock, AsyncMock]:
    """Return a provider instance (with its logger and config-read mocks) without full setup."""
    provider = MilkdropVisualizerProvider.__new__(MilkdropVisualizerProvider)
    logger = Mock()
    config_value = AsyncMock()
    mass = Mock()
    mass.config.get_provider_config_value = config_value
    mocked = cast("Any", provider)
    mocked.logger = logger
    mocked.config = Mock()
    mocked.mass = mass
    return provider, logger, config_value


async def test_loaded_in_mass_registers_nothing_while_unloading() -> None:
    """A stale instance must not take the live one's commands on its way out."""
    provider, _logger, _config_value = _provider()
    mocked = cast("Any", provider)
    mocked.unloading = True
    mocked._relay = Mock()
    mocked._unregister_handles = []

    await provider.loaded_in_mass()

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
    assert logged_args[1] == "boom Viewer error: forged"
    assert logged_args[2] == "x" * MAX_REPORT_FIELD_LEN
