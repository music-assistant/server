"""Stand-in for the provider event hook on the ai_radio test harnesses."""

from __future__ import annotations

from typing import Any


class ProviderEventRecorder:
    """Records every payload a harness hands to signal_provider_event."""

    provider_events: list[Any]

    def signal_provider_event(self, data: Any, sub_scope: str | None = None) -> None:
        """Record the payload instead of signalling the event bus."""
        self.provider_events = getattr(self, "provider_events", [])
        self.provider_events.append(data)
