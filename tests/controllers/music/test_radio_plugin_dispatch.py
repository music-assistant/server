"""Tests that radio dispatch reaches plugin providers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.media_items import ProviderMapping, Radio, Track

from music_assistant.controllers.music.media.radio import RadioController


def _radio() -> Radio:
    """Build a minimal dynamic radio owned by a plugin provider."""
    return Radio(
        item_id="morning_show",
        provider="ai_radio",
        name="Morning Show",
        is_dynamic=True,
        provider_mappings={
            ProviderMapping(
                item_id="morning_show", provider_domain="ai_radio", provider_instance="ai_radio"
            )
        },
    )


async def test_dynamic_tracks_dispatches_to_plugin_provider() -> None:
    """Test that dynamic_tracks dispatches to plugin providers."""
    provider = MagicMock()  # duck-typed plugin provider
    track = Track(
        item_id="t1",
        provider="library",
        name="t1",
        provider_mappings={
            ProviderMapping(item_id="t1", provider_domain="library", provider_instance="library")
        },
    )
    provider.get_dynamic_radio_tracks = AsyncMock(return_value=[track])
    ctrl = RadioController.__new__(RadioController)
    ctrl.mass = MagicMock()
    ctrl.mass.get_provider = MagicMock(return_value=provider)
    result = await ctrl.dynamic_tracks(_radio())
    assert [t.item_id for t in result] == ["t1"]
    provider.get_dynamic_radio_tracks.assert_awaited_once_with("morning_show")
