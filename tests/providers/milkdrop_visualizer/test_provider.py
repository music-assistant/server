"""Tests for the MilkDrop visualizer provider's viewer-facing API commands."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, Mock

from music_assistant.providers.milkdrop_visualizer.provider import (
    CONF_SHOW_ON_DASHBOARDS,
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
