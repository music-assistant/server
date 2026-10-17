"""Regression: sync-details SQL must round-trip provider mapping ``details``."""

from __future__ import annotations

import pytest
from music_assistant_models.media_items import Audiobook, ProviderMapping

from music_assistant.mass import MusicAssistant

INSTANCE_ID = "test--sync-details"


@pytest.fixture(name="mass")
def mass_fixture(music_mass: MusicAssistant) -> MusicAssistant:
    """Run on a library-only instance."""
    return music_mass


async def test_sync_details_preserves_provider_mapping_details(mass: MusicAssistant) -> None:
    """``get_library_item_sync_details`` must restore stored mapping fingerprints."""
    fingerprint = "2026-10-15T12:00:00.000Z"
    mapping = ProviderMapping(
        item_id="book_1",
        provider_domain="test",
        provider_instance=INSTANCE_ID,
        in_library=True,
        details=fingerprint,
    )
    book = Audiobook(
        item_id="book_1",
        provider=INSTANCE_ID,
        name="Sync Details Book",
        duration=100,
        provider_mappings={mapping},
    )
    await mass.music.audiobooks.add_item_to_library(book)

    details = await mass.music.audiobooks.get_library_item_sync_details({mapping})
    assert details is not None
    stored = next(iter(details.provider_mappings))
    assert stored.details == fingerprint
