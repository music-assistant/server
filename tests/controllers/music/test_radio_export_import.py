"""
Integration tests for the RadioController's M3U export/import round trip.

Uses a booted Music Assistant so the real builtin provider performs the library
writes; that is what makes the stored ``item_id`` assertions meaningful, since
builtin treats a stored radio item_id as a plain stream URL.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import (
    ImageType,
    MediaType,
    ProviderFeature,
    ProviderType,
    TaskStatus,
)
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import (
    ItemMapping,
    MediaItemImage,
    MediaItemType,
    ProviderMapping,
    Radio,
)
from music_assistant_models.provider import ProviderManifest

from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.builtin.constants import CONF_KEY_RADIOS

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant.controllers.tasks import TasksController
    from music_assistant.mass import MusicAssistant

FAKE_DOMAIN = "fake_radio"
FAKE_INSTANCE = "fake_radio--instance"
BUILTIN_STREAM_URL = "http://stream.example.com/jazz"
BUILTIN_IMAGE_URL = "http://img.example.com/jazz.jpg"


class FakeRadioProvider(MusicProvider):
    """Streaming-style provider that owns a single radio station."""

    async def sync_library(self, media_type: MediaType) -> None:
        """No-op sync implementation for tests."""

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Return the station this provider owns."""
        return _make_radio(
            item_id=prov_radio_id,
            provider=FAKE_INSTANCE,
            domain=FAKE_DOMAIN,
            name="Provider Owned Radio",
        )


def _make_radio(
    item_id: str,
    provider: str,
    domain: str,
    name: str,
    favorite: bool = False,
    image_url: str | None = None,
) -> Radio:
    """Build a Radio as a music provider would hand it over."""
    radio = Radio(
        item_id=item_id,
        provider=provider,
        name=name,
        favorite=favorite,
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=domain,
                provider_instance=provider,
            )
        },
    )
    if image_url:
        radio.metadata.add_image(
            MediaItemImage(
                type=ImageType.THUMB,
                path=image_url,
                provider=domain,
                remotely_accessible=True,
            )
        )
    return radio


async def _wait_for_task_status(
    controller: TasksController,
    task_id: str,
    *statuses: TaskStatus,
    timeout: float = 5.0,
) -> None:
    """Wait until a managed task reaches one of the expected statuses."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if controller.get_task(task_id).status in statuses:
            return
        await asyncio.sleep(0.01)
    msg = f"Task {task_id} did not reach {[status.value for status in statuses]} before timeout"
    raise AssertionError(msg)


async def _clear_radio_library(mass: MusicAssistant) -> None:
    """Empty the radio library and builtin's stored stations, mimicking a clean instance."""
    for item in await mass.music.radio.library_items(limit=500, summary=False):
        await mass.music.remove_item_from_library(MediaType.RADIO, item.item_id)
    mass.config.set(CONF_KEY_RADIOS, [])


@pytest.fixture(name="radio_mass")
async def radio_mass_fixture(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[MusicAssistant]:
    """Return a booted instance with a fake radio provider and no metadata scanning."""
    config = ProviderConfig(
        values={},
        type=ProviderType.MUSIC,
        domain=FAKE_DOMAIN,
        instance_id=FAKE_INSTANCE,
        name="Fake Radio",
    )
    monkeypatch.setattr(config, "get_value", lambda *_args, **_kwargs: "GLOBAL")
    provider = FakeRadioProvider(
        mass,
        manifest=ProviderManifest(
            type=ProviderType.MUSIC,
            domain=FAKE_DOMAIN,
            name="Fake Radio",
            description="Fake radio provider",
            codeowners=["@music-assistant"],
        ),
        config=config,
        supported_features={ProviderFeature.LIBRARY_RADIOS},
    )
    provider.available = True
    mass._providers[FAKE_INSTANCE] = provider
    # a full metadata scan is irrelevant here and would reach out to the network
    mass.metadata.update_metadata = AsyncMock()  # type: ignore[method-assign]
    try:
        yield mass
    finally:
        mass._providers.pop(FAKE_INSTANCE, None)


async def test_export_import_round_trip_restores_stations(radio_mass: MusicAssistant) -> None:
    """An untouched export must restore names, favourites, artwork and provider ownership."""
    mass = radio_mass
    jazz_item = await mass.music.add_item_to_library(
        _make_radio(
            item_id=BUILTIN_STREAM_URL,
            provider="builtin",
            domain="builtin",
            name="Jazz FM",
            image_url=BUILTIN_IMAGE_URL,
        )
    )
    owned_item = await mass.music.add_item_to_library(
        _make_radio(
            item_id="station-123",
            provider=FAKE_INSTANCE,
            domain=FAKE_DOMAIN,
            name="Provider Owned Radio",
        )
    )
    # favourite both through the library, the way a user does
    await mass.music.radio.set_favorite(jazz_item.item_id, True)
    await mass.music.radio.set_favorite(owned_item.item_id, True)

    m3u_data = await mass.music.radio.export_radios()
    # the name must travel in an #EXTINF line even though a station has no duration
    assert "#EXTINF:-1,Jazz FM" in m3u_data
    assert f"#EXTIMG:thumb||{BUILTIN_IMAGE_URL}||builtin||true" in m3u_data
    assert f"#EXTPROV:builtin||{BUILTIN_STREAM_URL}||builtin" in m3u_data
    assert m3u_data.count("favorite=true") == 2

    await _clear_radio_library(mass)
    assert await mass.music.radio.library_count() == 0

    task = await mass.music.radio.import_radios(m3u_data)
    await _wait_for_task_status(mass.tasks, task.id, TaskStatus.SUCCESS)
    assert mass.tasks.get_task(task.id).failure_messages == []

    library = {item.name: item for item in await mass.music.radio.library_items(summary=False)}
    assert set(library) == {"Jazz FM", "Provider Owned Radio"}

    jazz = library["Jazz FM"]
    assert jazz.favorite is True
    assert jazz.image is not None
    assert jazz.image.path == BUILTIN_IMAGE_URL
    jazz_mapping = next(iter(jazz.provider_mappings))
    assert jazz_mapping.provider_domain == "builtin"
    # builtin's item_id is the raw stream URL, never the builtin://radio/... MA URI
    assert jazz_mapping.item_id == BUILTIN_STREAM_URL

    # the provider-owned station is restored against its own provider, not as a builtin row
    owned = library["Provider Owned Radio"]
    assert {mapping.provider_domain for mapping in owned.provider_mappings} == {FAKE_DOMAIN}
    assert next(iter(owned.provider_mappings)).item_id == "station-123"
    # a provider-owned item is refetched from its provider, so the favorite flag has to be
    # reapplied to the library item rather than set on the object handed to the library
    assert owned.favorite is True

    stored_radios = mass.config.get(CONF_KEY_RADIOS, [])
    assert [item["item_id"] for item in stored_radios] == [BUILTIN_STREAM_URL]
    assert stored_radios[0]["name"] == "Jazz FM"
    assert stored_radios[0]["image_url"] == BUILTIN_IMAGE_URL


async def test_import_plain_url_m3u_yields_playable_radios(radio_mass: MusicAssistant) -> None:
    """A third-party M3U of bare stream URLs imports as radio with a usable mapping."""
    mass = radio_mass
    await _clear_radio_library(mass)
    m3u_data = "#EXTM3U\n#EXTINF:-1,Plain Station\nhttp://stream.example.com/plain\n"

    task = await mass.music.radio.import_radios(m3u_data)
    await _wait_for_task_status(mass.tasks, task.id, TaskStatus.SUCCESS)

    items = await mass.music.radio.library_items(summary=False)
    assert len(items) == 1
    station = items[0]
    # a bare url carries no #EXTMA, so import_radios supplies the media type itself
    assert isinstance(station, Radio)
    assert station.name == "Plain Station"
    # an item with no provider mappings never reaches library_add and stays unplayable
    assert len(station.provider_mappings) == 1
    mapping = next(iter(station.provider_mappings))
    assert mapping.provider_domain == "builtin"
    assert mapping.item_id == "http://stream.example.com/plain"
    assert [item["item_id"] for item in mass.config.get(CONF_KEY_RADIOS, [])] == [
        "http://stream.example.com/plain"
    ]


async def test_import_reports_unresolvable_station(radio_mass: MusicAssistant) -> None:
    """A station whose provider is not loaded is reported, not silently skipped."""
    mass = radio_mass
    await _clear_radio_library(mass)
    m3u_data = (
        "#EXTM3U\n"
        "#EXTMA:media_type=radio||name=Gone Radio\n"
        "#EXTPROV:absent_provider||station-9||absent_provider--instance\n"
        "#EXTINF:-1,Gone Radio\n"
        "absent_provider://radio/station-9\n"
    )

    task = await mass.music.radio.import_radios(m3u_data)
    await _wait_for_task_status(mass.tasks, task.id, TaskStatus.PARTIAL_SUCCESS)

    failures = mass.tasks.get_task(task.id).failure_messages
    assert len(failures) == 1
    assert "Gone Radio" in failures[0]
    assert await mass.music.radio.library_count() == 0


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        # a radio station is a stream URL, so a bare local path is not one
        ("some/file.mp3", "not a stream URL"),
        # an entry that resolves to another media type must not be stored as a station
        ("spotify://track/abc123", "is a track, not a radio station"),
    ],
)
async def test_import_reports_entry_that_is_not_a_station(
    radio_mass: MusicAssistant, path: str, expected: str
) -> None:
    """An entry that is not a radio station is reported, and nothing is stored for it."""
    mass = radio_mass
    await _clear_radio_library(mass)

    task = await mass.music.radio.import_radios(f"#EXTM3U\n#EXTINF:-1,Some Entry\n{path}\n")
    await _wait_for_task_status(mass.tasks, task.id, TaskStatus.PARTIAL_SUCCESS)

    failures = mass.tasks.get_task(task.id).failure_messages
    assert len(failures) == 1
    assert expected in failures[0]
    assert await mass.music.radio.library_count() == 0


async def test_import_continues_after_unexpected_error(radio_mass: MusicAssistant) -> None:
    """A transient fault on one station is reported and the queue behind it still imports."""
    mass = radio_mass
    await _clear_radio_library(mass)
    m3u_data = "#EXTM3U\n" + "".join(
        f"#EXTMA:media_type=radio||name=Station {name}\n"
        f"#EXTINF:-1,Station {name}\nhttp://stream.example.com/{name.lower()}\n"
        for name in ("One", "Two", "Three")
    )
    add_item_to_library = mass.music.add_item_to_library

    async def flaky(
        item: str | MediaItemType | ItemMapping, overwrite_existing: bool = False
    ) -> MediaItemType:
        if isinstance(item, Radio) and item.name == "Station Two":
            raise TimeoutError("connection timed out")
        return await add_item_to_library(item, overwrite_existing)

    mass.music.add_item_to_library = flaky  # type: ignore[method-assign]

    task = await mass.music.radio.import_radios(m3u_data)
    await _wait_for_task_status(mass.tasks, task.id, TaskStatus.PARTIAL_SUCCESS)

    failures = mass.tasks.get_task(task.id).failure_messages
    assert len(failures) == 1
    assert "Station Two" in failures[0]
    # the stations queued behind the failure must still be imported
    imported = {item.name for item in await mass.music.radio.library_items(summary=False)}
    assert imported == {"Station One", "Station Three"}


async def test_import_radios_rejects_empty_m3u(radio_mass: MusicAssistant) -> None:
    """An M3U without entries is an error, not an empty background task."""
    with pytest.raises(InvalidDataError):
        await radio_mass.music.radio.import_radios("#EXTM3U\n")
