"""Tests for the providers that are set up by default at startup."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

from music_assistant.constants import DEFAULT_PROVIDERS

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


async def test_found_speakers_are_not_set_up_by_default(mass: MusicAssistant) -> None:
    """A speaker found on the network does not get its provider set up without asking."""
    with (
        patch("music_assistant.mass.DEFAULT_PROVIDERS", DEFAULT_PROVIDERS),
        patch.object(mass.discovery, "is_seen_on_mdns", return_value=True),
        # only the configs matter here, no provider may actually reach the network
        patch.object(mass, "load_provider", new=AsyncMock()),
    ):
        await mass._load_providers()

    domains = {conf.domain for conf in await mass.config.get_provider_configs()}
    assert "airplay" in domains
    assert not domains & {"sonos", "bluesound", "heos", "wiim"}
