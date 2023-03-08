"""Model/base for a Plugin Provider implementation."""
from __future__ import annotations

from typing import TYPE_CHECKING

from .provider import Provider

if TYPE_CHECKING:
    pass

# ruff: noqa: ARG001, ARG002


class PluginProvider(Provider):
    """
    Base representation of a Plugin for Music Assistant.

    Plugin Provider implementations should inherit from this base model.
    """
