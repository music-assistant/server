"""Shared helpers for the config controller tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.controllers.config.controller import ConfigController

if TYPE_CHECKING:
    from unittest.mock import MagicMock


def build_bare_config_controller(mock_mass: MagicMock) -> ConfigController:
    """
    Build a bare ConfigController wired to the given mocked mass.

    Mock setup must go through the caller's own ``mock_mass`` (not ``controller.mass``):
    ``ConfigController.mass`` is statically typed as ``MusicAssistant``, so reading it
    back would resolve real (non-mock) attribute/overload types instead of the mock's,
    even though the object assigned at runtime is the mock.

    :param mock_mass: Mocked Music Assistant instance to build the controller on.
    """
    controller = ConfigController.__new__(ConfigController)
    controller.mass = mock_mass
    return controller
