"""Tests for the manifest that core controllers expose to the UI."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from music_assistant.controllers.dashboard.controller import DashboardController
from music_assistant.controllers.tasks.controller import TasksController
from music_assistant.models.core_controller import CORE_DOCS_URL, CoreController


class _PlayerQueuesController(CoreController):
    """Core controller with an underscored domain."""

    domain = "player_queues"


@pytest.fixture
def mass() -> MagicMock:
    """Return a mass mock whose core controllers log at the global level."""
    mass = MagicMock()
    mass.config.get_raw_core_config_value.return_value = "GLOBAL"
    return mass


def test_documentation_defaults_to_domain_anchor(mass: MagicMock) -> None:
    """The documentation link points to the docs section named after the domain."""
    controller = _PlayerQueuesController(mass)
    assert controller.manifest.documentation == f"{CORE_DOCS_URL}#player-queues"


def test_documentation_can_be_overridden(mass: MagicMock) -> None:
    """Controllers whose docs section is named differently point to that section."""
    tasks = TasksController(mass)
    assert tasks.manifest.documentation == f"{CORE_DOCS_URL}#background-tasks-configuration"


def test_documentation_without_section_links_to_page(mass: MagicMock) -> None:
    """Controllers without a docs section link to the page itself."""
    dashboard = DashboardController(mass)
    assert dashboard.manifest.documentation == CORE_DOCS_URL
