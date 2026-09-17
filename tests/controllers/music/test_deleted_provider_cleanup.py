"""Tests for draining the deleted-provider queue once the analysis database is attached."""

from __future__ import annotations

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Artist, ProviderMapping, Track, UniqueList

from music_assistant.constants import DB_TABLE_PROVIDER_MAPPINGS
from music_assistant.controllers.music.constants import CONF_DELETED_PROVIDERS
from music_assistant.controllers.streams.audio_analysis import AA_TABLE_ANALYSIS
from music_assistant.mass import MusicAssistant

FS_DOMAIN = "filesystem_local"
FS_INSTANCE = "filesystem_local--AbCd"


async def _add_track(mass: MusicAssistant, item_id: str, name: str) -> int:
    """Add a single-provider track to the library and return its library id."""
    artist = Artist(
        item_id=f"{item_id}-artist",
        provider=FS_INSTANCE,
        name=f"{name} Artist",
        provider_mappings={
            ProviderMapping(
                item_id=f"{item_id}-artist",
                provider_domain=FS_DOMAIN,
                provider_instance=FS_INSTANCE,
            )
        },
    )
    db_track = await mass.music.tracks.add_item_to_library(
        Track(
            item_id=item_id,
            provider=FS_INSTANCE,
            name=name,
            artists=UniqueList([artist]),
            provider_mappings={
                ProviderMapping(
                    item_id=item_id,
                    provider_domain=FS_DOMAIN,
                    provider_instance=FS_INSTANCE,
                )
            },
        )
    )
    return int(db_track.item_id)


async def _add_analysis_row(mass: MusicAssistant, item_id: str) -> None:
    """Insert an analysis row for the given provider item id."""
    await mass.music.database.insert(
        AA_TABLE_ANALYSIS,
        {
            "media_type": MediaType.TRACK.value,
            "item_id": item_id,
            "provider": FS_INSTANCE,
            "aa_provider_domain": "loudness_analysis",
            "analysis_data": "{}",
            "analysis_version": 1,
        },
    )


async def test_post_setup_drains_deleted_providers(mass: MusicAssistant) -> None:
    """A queued provider removal completes at post_setup, analysis rows included."""
    db_id = await _add_track(mass, "fs-track", "Queued Removal Track")
    await _add_analysis_row(mass, "fs-track")
    mass.config.set_raw_core_config_value(mass.music.domain, CONF_DELETED_PROVIDERS, [FS_INSTANCE])

    await mass.music.post_setup()

    with pytest.raises(MediaNotFoundError):
        await mass.music.tracks.get_library_item(db_id)
    assert not await mass.music.database.get_rows(
        DB_TABLE_PROVIDER_MAPPINGS, {"provider_instance": FS_INSTANCE}
    )
    assert not await mass.music.database.get_rows(AA_TABLE_ANALYSIS, {"item_id": "fs-track"})
    assert (
        mass.config.get_raw_core_config_value(mass.music.domain, CONF_DELETED_PROVIDERS, []) == []
    )


async def test_remove_item_from_library_deletes_analysis(mass: MusicAssistant) -> None:
    """Removing a library item through the public API also drops its analysis rows."""
    db_id = await _add_track(mass, "fs-removed", "Removed Track")
    await _add_analysis_row(mass, "fs-removed")

    await mass.music.remove_item_from_library(MediaType.TRACK, str(db_id))

    assert not await mass.music.database.get_rows(AA_TABLE_ANALYSIS, {"item_id": "fs-removed"})


@pytest.mark.parametrize("queued", [False, True])
async def test_cleanup_waits_for_analysis_database(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch, queued: bool
) -> None:
    """Startup and runtime removal preserve the existing retry queue and provider mappings."""
    db_id = await _add_track(mass, "fs-deferred", "Deferred Removal Track")
    await _add_analysis_row(mass, "fs-deferred")
    monkeypatch.setattr(mass.streams.audio_analysis, "_database_ready", False)
    if queued:
        mass.config.set_raw_core_config_value(
            mass.music.domain, CONF_DELETED_PROVIDERS, [FS_INSTANCE]
        )
        await mass.music.post_setup()
    else:
        await mass.music.cleanup_provider(FS_INSTANCE)

    assert await mass.music.tracks.get_library_item(db_id)
    assert await mass.music.database.get_rows(
        DB_TABLE_PROVIDER_MAPPINGS, {"provider_instance": FS_INSTANCE}
    )
    assert await mass.music.database.get_rows(AA_TABLE_ANALYSIS, {"item_id": "fs-deferred"})
    assert mass.config.get_raw_core_config_value(mass.music.domain, CONF_DELETED_PROVIDERS, []) == [
        FS_INSTANCE
    ]

    await mass.streams.audio_analysis.setup_database()
    await mass.music.post_setup()

    with pytest.raises(MediaNotFoundError):
        await mass.music.tracks.get_library_item(db_id)
    assert not await mass.music.database.get_rows(
        DB_TABLE_PROVIDER_MAPPINGS, {"provider_instance": FS_INSTANCE}
    )
    assert not await mass.music.database.get_rows(AA_TABLE_ANALYSIS, {"item_id": "fs-deferred"})
    assert (
        mass.config.get_raw_core_config_value(mass.music.domain, CONF_DELETED_PROVIDERS, []) == []
    )
