"""Tests for draining the deleted-provider queue once the analysis database is attached."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError
from music_assistant_models.media_items import Artist, ProviderMapping, Track, UniqueList

from music_assistant.constants import DB_TABLE_PROVIDER_MAPPINGS
from music_assistant.controllers.music.constants import CONF_DELETED_PROVIDERS
from music_assistant.controllers.streams.constants import AA_TABLE_ANALYSIS, AA_TABLE_FAILURES
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


async def _add_analysis_row(
    mass: MusicAssistant, item_id: str, provider_key: str = FS_INSTANCE
) -> None:
    """Insert an analysis row for the given provider item id."""
    await mass.music.database.insert(
        AA_TABLE_ANALYSIS,
        {
            "media_type": MediaType.TRACK.value,
            "item_id": item_id,
            "provider": provider_key,
            "aa_provider_domain": "loudness_analysis",
            "header": "{}",
            "payload": b"",
            "analysis_version": 1,
        },
    )


async def _add_failure_row(mass: MusicAssistant, item_id: str, provider_key: str) -> None:
    """Insert a never-retry failure for the given provider item key."""
    await mass.music.database.insert(
        AA_TABLE_FAILURES,
        {
            "media_type": MediaType.TRACK.value,
            "item_id": item_id,
            "provider": provider_key,
            "aa_provider_domain": "loudness_analysis",
            "reason": "never retry",
            "next_retry": None,
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
    await _add_failure_row(mass, "fs-removed", FS_INSTANCE)

    await mass.music.remove_item_from_library(MediaType.TRACK, str(db_id))

    assert not await mass.music.database.get_rows(AA_TABLE_ANALYSIS, {"item_id": "fs-removed"})
    assert not await mass.music.database.get_rows(AA_TABLE_FAILURES, {"item_id": "fs-removed"})


@pytest.mark.parametrize("removal", ["single", "all", "provider"])
async def test_mapping_removal_cleans_analysis_for_kept_track(
    mass: MusicAssistant, removal: str
) -> None:
    """Removing one source clears its analysis and failures without removing the kept track."""
    db_id = await _add_track(mass, "fs-kept", "Kept Track")
    spotify_instance = "spotify--EfGh"
    await mass.music.tracks.add_provider_mapping(
        db_id,
        ProviderMapping(
            item_id="sp-kept", provider_domain="spotify", provider_instance=spotify_instance
        ),
    )
    track = await mass.music.tracks.get_library_item(db_id)
    await mass.music.artists.add_provider_mapping(
        track.artists[0].item_id,
        ProviderMapping(
            item_id="sp-artist", provider_domain="spotify", provider_instance=spotify_instance
        ),
    )
    for item_id, provider_key in (("fs-kept", FS_INSTANCE), ("sp-kept", "spotify")):
        await _add_analysis_row(mass, item_id, provider_key)
        await _add_failure_row(mass, item_id, provider_key)

    if removal == "single":
        await mass.music.tracks.remove_provider_mapping(db_id, FS_INSTANCE, "fs-kept")
    elif removal == "all":
        await mass.music.tracks.remove_provider_mappings(db_id, FS_INSTANCE)
    else:
        await mass.music.cleanup_provider(FS_INSTANCE)

    updated = await mass.music.tracks.get_library_item(db_id)
    assert {mapping.provider_instance for mapping in updated.provider_mappings} == {
        spotify_instance
    }
    for table in (AA_TABLE_ANALYSIS, AA_TABLE_FAILURES):
        assert not await mass.music.database.get_rows(table, {"provider": FS_INSTANCE})
        assert await mass.music.database.get_rows(
            table, {"item_id": "sp-kept", "provider": "spotify"}
        )


@pytest.mark.parametrize("removal", ["single", "all"])
async def test_shared_domain_analysis_survives_other_account_removal(
    mass: MusicAssistant, removal: str
) -> None:
    """Domain-keyed analysis is kept until the last account mapping to that item is removed."""
    db_id = await _add_track(mass, "fs-shared", "Shared Track")
    instances = ("spotify--first", "spotify--second")
    await mass.music.tracks.add_provider_mappings(
        db_id,
        {
            ProviderMapping(item_id="sp-shared", provider_domain="spotify", provider_instance=inst)
            for inst in instances
        },
    )
    await _add_analysis_row(mass, "sp-shared", "spotify")
    await _add_failure_row(mass, "sp-shared", "spotify")
    match = {"item_id": "sp-shared", "provider": "spotify"}

    for index, instance in enumerate(instances):
        if removal == "single":
            await mass.music.tracks.remove_provider_mapping(db_id, instance, "sp-shared")
        else:
            await mass.music.tracks.remove_provider_mappings(db_id, instance)
        for table in (AA_TABLE_ANALYSIS, AA_TABLE_FAILURES):
            assert bool(await mass.music.database.get_rows(table, match)) == (index == 0)


@pytest.mark.parametrize("removal", ["item", "single", "all"])
async def test_shared_analysis_survives_removal_from_another_library_item(
    mass: MusicAssistant, removal: str
) -> None:
    """Domain analysis remains while another library item references it through another account."""
    instances = ("spotify--first", "spotify--second")
    library_ids = []
    for index, instance in enumerate(instances):
        db_id = await _add_track(mass, f"fs-shared-{index}", f"Shared Track {index}")
        library_ids.append(db_id)
        await mass.music.tracks.add_provider_mapping(
            db_id,
            ProviderMapping(
                item_id="sp-shared", provider_domain="spotify", provider_instance=instance
            ),
        )
    assert library_ids[0] != library_ids[1]
    for provider_key in ("spotify", *instances):
        await _add_analysis_row(mass, "sp-shared", provider_key)
        await _add_failure_row(mass, "sp-shared", provider_key)

    for index, (db_id, instance) in enumerate(zip(library_ids, instances, strict=True)):
        if removal == "item":
            await mass.music.tracks.remove_item_from_library(db_id)
        elif removal == "single":
            await mass.music.tracks.remove_provider_mapping(db_id, instance, "sp-shared")
        else:
            await mass.music.tracks.remove_provider_mappings(db_id, instance)
        for table in (AA_TABLE_ANALYSIS, AA_TABLE_FAILURES):
            assert not await mass.music.database.get_rows(
                table, {"item_id": "sp-shared", "provider": instance}
            )
            assert bool(
                await mass.music.database.get_rows(
                    table, {"item_id": "sp-shared", "provider": "spotify"}
                )
            ) == (index == 0)


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


@pytest.mark.parametrize("removal", ["item", "single", "all"])
@pytest.mark.parametrize("failure", ["unavailable", "storage"])
async def test_failed_analysis_cleanup_preserves_removal_keys(
    mass: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
    removal: str,
    failure: str,
) -> None:
    """An unavailable database or failed cleanup leaves the item and mapping keys retryable."""
    db_id = await _add_track(mass, "fs-retry", "Retry Removal Track")
    await _add_analysis_row(mass, "fs-retry")
    await _add_failure_row(mass, "fs-retry", FS_INSTANCE)
    if removal != "item":
        await mass.music.tracks.add_provider_mapping(
            db_id,
            ProviderMapping(
                item_id="sp-retry", provider_domain="spotify", provider_instance="spotify--EfGh"
            ),
        )
    original = await mass.music.tracks.get_library_item(db_id)
    real_delete = mass.music.database.delete

    async def failing_delete(
        table: str, match: dict[str, Any] | None = None, query: str | None = None
    ) -> None:
        if table == AA_TABLE_FAILURES:
            raise sqlite3.OperationalError("analysis storage unavailable")
        await real_delete(table, match, query)

    async def remove() -> None:
        if removal == "item":
            await mass.music.tracks.remove_item_from_library(db_id)
        elif removal == "single":
            await mass.music.tracks.remove_provider_mapping(db_id, FS_INSTANCE, "fs-retry")
        else:
            await mass.music.tracks.remove_provider_mappings(db_id, FS_INSTANCE)

    with monkeypatch.context() as failing:
        if failure == "unavailable":
            failing.setattr(mass.streams.audio_analysis, "_database_ready", False)
        else:
            failing.setattr(mass.music.database, "delete", failing_delete)
        expected_error = (
            ProviderUnavailableError if failure == "unavailable" else sqlite3.OperationalError
        )
        with pytest.raises(expected_error, match="unavailable"):
            await remove()
        retained = await mass.music.tracks.get_library_item(db_id)
        assert retained.provider_mappings == original.provider_mappings
        assert retained.artists == original.artists
        assert await mass.music.database.get_rows(
            DB_TABLE_PROVIDER_MAPPINGS,
            {
                "media_type": MediaType.TRACK.value,
                "item_id": db_id,
                "provider_instance": FS_INSTANCE,
            },
        )
        assert await mass.music.database.get_rows(
            AA_TABLE_FAILURES, {"item_id": "fs-retry", "provider": FS_INSTANCE}
        )

    await remove()

    assert not await mass.music.database.get_rows(
        DB_TABLE_PROVIDER_MAPPINGS,
        {
            "media_type": MediaType.TRACK.value,
            "item_id": db_id,
            "provider_instance": FS_INSTANCE,
        },
    )
    for table in (AA_TABLE_ANALYSIS, AA_TABLE_FAILURES):
        assert not await mass.music.database.get_rows(
            table, {"item_id": "fs-retry", "provider": FS_INSTANCE}
        )
