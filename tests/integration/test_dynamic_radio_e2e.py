"""
End-to-end test for enqueueing a dynamic radio station through the real queue controller.

Boots the hermetic `e2e_mass` fixture (fake `test` provider + demo players) plus a small
fake provider exposing one dynamic ("Pandora-style") station, and exercises the same
dynamic-mode transition covered for dynamic playlists in test_dynamic_mode_e2e.py: enqueueing
the station records it as a dynamic source, puts the queue into dynamic mode, and fills the
managed pool from the provider's ``get_dynamic_radio_tracks``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import MediaType, ProviderType, QueueOption
from music_assistant_models.media_items import ProviderMapping, Radio, Track
from music_assistant_models.provider import ProviderManifest

from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider

from .conftest import demo_players, wait_for

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

FAKE_RADIO_DOMAIN = "fake_dynamic_radio_e2e"
FAKE_RADIO_INSTANCE = "fake_dynamic_radio_e2e--instance"
DYNAMIC_STATION_ID = "station-1"
STATION_BATCH_SIZE = 10


class FakeDynamicRadioProvider(MusicProvider):
    """Provider owning a single dynamic ("Pandora-style") radio station."""

    async def sync_library(self, media_type: MediaType) -> None:
        """No-op sync implementation for tests."""

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Return the fake dynamic station."""
        return Radio(
            item_id=prov_radio_id,
            provider=self.instance_id,
            name="Dynamic Station",
            is_dynamic=True,
            provider_mappings={
                ProviderMapping(
                    item_id=prov_radio_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

    async def get_dynamic_radio_tracks(
        self, prov_radio_id: str, *, sample: bool = False
    ) -> list[Track]:
        """Return a fixed batch of fake tracks for the dynamic station."""
        return [
            Track(
                item_id=f"{prov_radio_id}-t{i}",
                provider=self.instance_id,
                name=f"Station Track {i}",
                duration=60,
                provider_mappings={
                    ProviderMapping(
                        item_id=f"{prov_radio_id}-t{i}",
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                    )
                },
            )
            for i in range(STATION_BATCH_SIZE)
        ]


@pytest.fixture
async def e2e_mass_with_dynamic_radio(
    e2e_mass: MusicAssistant,
) -> AsyncGenerator[MusicAssistant]:
    """Register the fake dynamic-radio provider on top of the hermetic e2e instance."""
    config = ProviderConfig(
        values={},
        type=ProviderType.MUSIC,
        domain=FAKE_RADIO_DOMAIN,
        instance_id=FAKE_RADIO_INSTANCE,
        name="Fake Dynamic Radio",
    )
    provider = FakeDynamicRadioProvider(
        e2e_mass,
        manifest=ProviderManifest(
            type=ProviderType.MUSIC,
            domain=FAKE_RADIO_DOMAIN,
            name="Fake Dynamic Radio",
            description="Fake dynamic radio provider",
            codeowners=["@music-assistant"],
        ),
        config=config,
        supported_features=set(),
    )
    provider.available = True
    e2e_mass._providers[FAKE_RADIO_INSTANCE] = provider
    # the global "available providers" cache drives MediaItem.available; refresh it so the
    # fake station's tracks are not filtered out of the managed pool as unavailable
    await e2e_mass._update_available_providers_cache()
    try:
        yield e2e_mass
    finally:
        e2e_mass._providers.pop(FAKE_RADIO_INSTANCE, None)
        await e2e_mass._update_available_providers_cache()


@pytest.mark.asyncio
async def test_enqueue_dynamic_radio_enters_dynamic_mode(
    e2e_mass_with_dynamic_radio: MusicAssistant,
) -> None:
    """Enqueueing a dynamic radio station puts the queue in dynamic mode and fills the pool."""
    mass = e2e_mass_with_dynamic_radio
    queue_id = demo_players(mass)[0].player_id
    provider = cast("MusicProvider", mass.get_provider(FAKE_RADIO_INSTANCE))
    assert provider is not None
    station = await provider.get_radio(DYNAMIC_STATION_ID)

    # ADD keeps this off the playback path (no streamdetails needed for the fake tracks),
    # mirroring how test_dynamic_mode_e2e.py adds a dynamic playlist without starting playback.
    await mass.player_queues.play_media(queue_id, station, option=QueueOption.ADD)

    assert await wait_for(lambda: len(mass.player_queues.items(queue_id)) > 0), (
        "dynamic radio pool never populated"
    )
    queue = mass.player_queues.get(queue_id)
    assert queue is not None
    assert queue.is_dynamic is True

    data = mass.player_queues._queue_data[queue_id]
    assert any(
        isinstance(item, Radio) and item.is_dynamic and item.item_id == DYNAMIC_STATION_ID
        for item in data.source_items
    )

    # the pool was filled straight from the provider's get_dynamic_radio_tracks batch
    items = mass.player_queues.items(queue_id)
    item_ids = {item.media_item.item_id for item in items if item.media_item}
    assert item_ids <= {f"{DYNAMIC_STATION_ID}-t{i}" for i in range(STATION_BATCH_SIZE)}
    assert item_ids
