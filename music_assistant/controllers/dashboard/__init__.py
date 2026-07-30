"""
MusicAssistant DashboardController.

Provides the core-level API for casting Music Assistant dashboards (e.g. Party
mode, Now Playing) to display devices, delegating the actual casting transport
to whichever player provider owns the display device.
"""

from __future__ import annotations

from .controller import DashboardController

__all__ = ["DashboardController"]
