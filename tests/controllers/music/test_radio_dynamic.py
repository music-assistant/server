"""
Integration tests for dynamic radio stations on the RadioController.

Uses a fully booted MusicAssistant instance (mirrors test_radio_export_import.py) with a
small fake music provider that owns one dynamic and one non-dynamic radio station, to
verify the guards and track-feed wiring introduced for dynamic radio stations (mirrors
the existing dynamic-playlist wiring on PlaylistController/media_resolver).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import pytest
from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import MediaType, ProviderFeature, ProviderType
from music_assistant_models.errors import UnsupportedFeaturedException
from music_assistant_models.media_items import ProviderMapping, Radio, SearchResults, Track
from music_assistant_models.provider import ProviderManifest

from music_assistant.controllers.music.media.radio import RadioController
from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

FAKE_DOMAIN = "fake_dynamic_radio"
FAKE_INSTANCE = "fake_dynamic_radio--instance"
DYNAMIC_STATION_ID = "dynamic-1"
STATIC_STATION_ID = "static-1"
TOGGLE_STATION_ID = "toggle-1"


class FakeDynamicRadioProvider(MusicProvider):
    """Streaming-style provider owning one dynamic and one static radio station."""

    # controls the is_dynamic value get_library_radios reports for TOGGLE_STATION_ID
    toggle_station_is_dynamic: bool = False
    toggle_station_date_added: datetime | None = None

    async def sync_library(self, media_type: MediaType) -> None:
        """No-op sync implementation for tests."""

    async def get_library_radios(self) -> AsyncGenerator[Radio]:
        """Yield the single toggle station, honoring the current is_dynamic flag."""
        yield Radio(
            item_id=TOGGLE_STATION_ID,
            provider=self.instance_id,
            name="Toggle Station",
            is_dynamic=self.toggle_station_is_dynamic,
            date_added=self.toggle_station_date_added,
            provider_mappings={
                ProviderMapping(
                    item_id=TOGGLE_STATION_ID,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Return the requested fake station."""
        is_dynamic = prov_radio_id == DYNAMIC_STATION_ID
        return Radio(
            item_id=prov_radio_id,
            provider=self.instance_id,
            name="Dynamic Station" if is_dynamic else "Static Station",
            is_dynamic=is_dynamic,
            provider_mappings={
                ProviderMapping(
                    item_id=prov_radio_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

    async def get_dynamic_radio_tracks(self, prov_radio_id: str) -> list[Track]:
        """Return a fixed batch of fake tracks for the dynamic station."""
        return [
            Track(
                item_id=f"{prov_radio_id}-t{i}",
                provider=self.instance_id,
                name=f"Track {i}",
                provider_mappings={
                    ProviderMapping(
                        item_id=f"{prov_radio_id}-t{i}",
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                    )
                },
            )
            for i in range(3)
        ]

    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """Return a same-named station, so a name-only match would find something to report."""
        return SearchResults(
            radio=[
                Radio(
                    item_id="same-name-elsewhere",
                    provider=self.instance_id,
                    name=search_query,
                    provider_mappings={
                        ProviderMapping(
                            item_id="same-name-elsewhere",
                            provider_domain=self.domain,
                            provider_instance=self.instance_id,
                        )
                    },
                )
            ]
        )


@pytest.fixture(name="radio_mass")
async def radio_mass_fixture(mass: MusicAssistant) -> AsyncGenerator[MusicAssistant]:
    """Return a booted instance with the fake dynamic-radio provider registered."""
    config = ProviderConfig(
        values={},
        type=ProviderType.MUSIC,
        domain=FAKE_DOMAIN,
        instance_id=FAKE_INSTANCE,
        name="Fake Dynamic Radio",
    )
    provider = FakeDynamicRadioProvider(
        mass,
        manifest=ProviderManifest(
            type=ProviderType.MUSIC,
            domain=FAKE_DOMAIN,
            name="Fake Dynamic Radio",
            description="Fake dynamic radio provider",
            codeowners=["@music-assistant"],
        ),
        config=config,
        supported_features={ProviderFeature.LIBRARY_RADIOS, ProviderFeature.SEARCH},
    )
    provider.available = True
    mass._providers[FAKE_INSTANCE] = provider
    try:
        yield mass
    finally:
        mass._providers.pop(FAKE_INSTANCE, None)


@pytest.fixture(name="radio_ctrl")
def radio_ctrl_fixture(radio_mass: MusicAssistant) -> RadioController:
    """Get the radio controller from the booted Music Assistant instance."""
    return radio_mass.music.radio


class TestRadioTracks:
    """Tests for RadioController.radio_tracks."""

    async def test_raises_for_non_dynamic_station(self, radio_ctrl: RadioController) -> None:
        """A non-dynamic (live-stream) station has no track feed."""
        with pytest.raises(UnsupportedFeaturedException):
            await radio_ctrl.radio_tracks(STATIC_STATION_ID, FAKE_INSTANCE)

    async def test_returns_provider_tracks_for_provider_item(
        self, radio_ctrl: RadioController
    ) -> None:
        """A dynamic station's tracks are fetched straight from its provider."""
        tracks = await radio_ctrl.radio_tracks(DYNAMIC_STATION_ID, FAKE_INSTANCE)
        assert [track.name for track in tracks] == ["Track 0", "Track 1", "Track 2"]

    async def test_returns_provider_tracks_for_library_item(
        self, radio_mass: MusicAssistant, radio_ctrl: RadioController
    ) -> None:
        """A library-resolved dynamic station still fetches its tracks from its own provider."""
        provider = cast("MusicProvider", radio_mass.get_provider(FAKE_INSTANCE))
        station = await provider.get_radio(DYNAMIC_STATION_ID)
        library_item = await radio_ctrl.add_item_to_library(station)
        tracks = await radio_ctrl.radio_tracks(str(library_item.item_id), "library")
        assert len(tracks) == 3


class TestAddToLibraryDynamicGuard:
    """Tests that adding a dynamic station never folds it into an existing station."""

    async def test_same_named_live_station_is_left_alone(
        self, radio_mass: MusicAssistant, radio_ctrl: RadioController
    ) -> None:
        """A dynamic station gets its own library item instead of joining a same-named stream."""
        live_station = Radio(
            item_id="live-1",
            provider="some_radio_directory--instance",
            name="Chill Vibes",
            provider_mappings={
                ProviderMapping(
                    item_id="live-1",
                    provider_domain="some_radio_directory",
                    provider_instance="some_radio_directory--instance",
                )
            },
        )
        live_item = await radio_ctrl.add_item_to_library(live_station)
        station = Radio(
            item_id=DYNAMIC_STATION_ID,
            provider=FAKE_INSTANCE,
            name="Chill Vibes",
            is_dynamic=True,
            provider_mappings={
                ProviderMapping(
                    item_id=DYNAMIC_STATION_ID,
                    provider_domain=FAKE_DOMAIN,
                    provider_instance=FAKE_INSTANCE,
                )
            },
        )

        dynamic_item = await radio_ctrl.add_item_to_library(station)

        assert dynamic_item.item_id != live_item.item_id
        assert {mapping.provider_domain for mapping in dynamic_item.provider_mappings} == {
            FAKE_DOMAIN
        }


class TestVersionsDynamicGuard:
    """Tests for the dynamic-station guard on RadioController.versions."""

    async def test_dynamic_station_has_no_other_versions(self, radio_ctrl: RadioController) -> None:
        """A dynamic station reports no other versions, and searches nothing to find that out."""
        assert await radio_ctrl.versions(DYNAMIC_STATION_ID, FAKE_INSTANCE) == []


class TestMatchProvidersDynamicGuard:
    """Tests for the dynamic-station guard on RadioController.match_providers."""

    async def test_dynamic_station_returns_before_searching(
        self,
        radio_mass: MusicAssistant,
        radio_ctrl: RadioController,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A dynamic station returns immediately, without inspecting any other provider."""
        dynamic_radio = Radio(
            item_id="1",
            provider="library",
            name="Dynamic",
            is_dynamic=True,
            provider_mappings={
                ProviderMapping(
                    item_id=DYNAMIC_STATION_ID,
                    provider_domain=FAKE_DOMAIN,
                    provider_instance=FAKE_INSTANCE,
                )
            },
        )

        def _boom(_self: object) -> list[MusicProvider]:
            raise AssertionError("must not access other providers for a dynamic station")

        monkeypatch.setattr(type(radio_mass.music), "providers", property(_boom))
        # must not raise: the guard returns before the (patched-to-explode) providers property
        await radio_ctrl.match_providers(dynamic_radio)

    async def test_non_dynamic_station_still_searches(
        self,
        radio_mass: MusicAssistant,
        radio_ctrl: RadioController,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A non-dynamic station is unaffected by the guard and still triggers matching."""
        static_radio = Radio(
            item_id="2",
            provider="library",
            name="Static",
            is_dynamic=False,
            provider_mappings={
                ProviderMapping(
                    item_id=STATIC_STATION_ID,
                    provider_domain=FAKE_DOMAIN,
                    provider_instance=FAKE_INSTANCE,
                )
            },
        )
        accessed = False
        real_providers = list(radio_mass.music.providers)

        def _track_access(_self: object) -> list[MusicProvider]:
            nonlocal accessed
            accessed = True
            return real_providers

        monkeypatch.setattr(type(radio_mass.music), "providers", property(_track_access))
        await radio_ctrl.match_providers(static_radio)
        assert accessed is True


class TestSyncLibraryRadiosDynamicFlag:
    """Tests for the is_dynamic branch of MusicProvider._sync_library_radios."""

    @staticmethod
    def _toggle_mapping() -> ProviderMapping:
        return ProviderMapping(
            item_id=TOGGLE_STATION_ID,
            provider_domain=FAKE_DOMAIN,
            provider_instance=FAKE_INSTANCE,
        )

    async def test_switching_to_dynamic_updates_library_item(
        self, radio_mass: MusicAssistant, radio_ctrl: RadioController
    ) -> None:
        """A provider reporting the same station as dynamic updates the stored flag."""
        provider = cast("FakeDynamicRadioProvider", radio_mass.get_provider(FAKE_INSTANCE))
        provider.toggle_station_is_dynamic = False
        await provider._sync_library_radios()
        library_item = await radio_ctrl.get_library_item_by_prov_mappings([self._toggle_mapping()])
        assert library_item is not None
        assert library_item.is_dynamic is False

        provider.toggle_station_is_dynamic = True
        await provider._sync_library_radios()
        library_item = await radio_ctrl.get_library_item_by_prov_mappings([self._toggle_mapping()])
        assert library_item is not None
        assert library_item.is_dynamic is True

    async def test_switching_to_dynamic_keeps_the_station_listed(
        self, radio_mass: MusicAssistant, radio_ctrl: RadioController
    ) -> None:
        """A station that becomes dynamic stays in the library listing."""
        provider = cast("FakeDynamicRadioProvider", radio_mass.get_provider(FAKE_INSTANCE))
        provider.toggle_station_is_dynamic = False
        await provider._sync_library_radios()
        assert "Toggle Station" in [item.name for item in await radio_ctrl.library_items()]

        provider.toggle_station_is_dynamic = True
        await provider._sync_library_radios()

        assert "Toggle Station" in [item.name for item in await radio_ctrl.library_items()]

    async def test_switching_to_dynamic_drops_name_matched_mappings(
        self, radio_mass: MusicAssistant, radio_ctrl: RadioController
    ) -> None:
        """Only the owning provider is left to serve a station's tracks once it becomes dynamic."""
        provider = cast("FakeDynamicRadioProvider", radio_mass.get_provider(FAKE_INSTANCE))
        provider.toggle_station_is_dynamic = False
        await provider._sync_library_radios()
        library_item = await radio_ctrl.get_library_item_by_prov_mappings([self._toggle_mapping()])
        assert library_item is not None
        # a same-named station on another provider, as cross-provider matching would have added
        await radio_ctrl.add_provider_mappings(
            library_item.item_id,
            [
                ProviderMapping(
                    item_id="unrelated-stream",
                    provider_domain="some_radio_directory",
                    provider_instance="some_radio_directory--instance",
                )
            ],
        )

        provider.toggle_station_is_dynamic = True
        await provider._sync_library_radios()

        library_item = await radio_ctrl.get_library_item_by_prov_mappings([self._toggle_mapping()])
        assert library_item is not None
        assert {mapping.provider_domain for mapping in library_item.provider_mappings} == {
            FAKE_DOMAIN
        }

    async def test_dynamic_switch_wins_over_a_coinciding_generic_update(
        self, radio_mass: MusicAssistant, radio_ctrl: RadioController
    ) -> None:
        """A station going dynamic is overwritten even when the same sync has a generic update."""
        provider = cast("FakeDynamicRadioProvider", radio_mass.get_provider(FAKE_INSTANCE))
        provider.toggle_station_is_dynamic = False
        provider.toggle_station_date_added = datetime(2026, 1, 1, tzinfo=UTC)
        await provider._sync_library_radios()
        library_item = await radio_ctrl.get_library_item_by_prov_mappings([self._toggle_mapping()])
        assert library_item is not None
        await radio_ctrl.add_provider_mappings(
            library_item.item_id,
            [
                ProviderMapping(
                    item_id="unrelated-stream",
                    provider_domain="some_radio_directory",
                    provider_instance="some_radio_directory--instance",
                )
            ],
        )

        # a changed date_added makes _library_item_needs_update true alongside the dynamic switch
        provider.toggle_station_is_dynamic = True
        provider.toggle_station_date_added = datetime(2026, 6, 1, tzinfo=UTC)
        await provider._sync_library_radios()

        library_item = await radio_ctrl.get_library_item_by_prov_mappings([self._toggle_mapping()])
        assert library_item is not None
        assert library_item.is_dynamic is True
        assert {mapping.provider_domain for mapping in library_item.provider_mappings} == {
            FAKE_DOMAIN
        }

    async def test_switching_to_non_dynamic_updates_library_item(
        self, radio_mass: MusicAssistant, radio_ctrl: RadioController
    ) -> None:
        """A provider reporting the same station as no longer dynamic updates the stored flag."""
        provider = cast("FakeDynamicRadioProvider", radio_mass.get_provider(FAKE_INSTANCE))
        provider.toggle_station_is_dynamic = True
        await provider._sync_library_radios()
        library_item = await radio_ctrl.get_library_item_by_prov_mappings([self._toggle_mapping()])
        assert library_item is not None
        assert library_item.is_dynamic is True

        provider.toggle_station_is_dynamic = False
        await provider._sync_library_radios()
        library_item = await radio_ctrl.get_library_item_by_prov_mappings([self._toggle_mapping()])
        assert library_item is not None
        assert library_item.is_dynamic is False
