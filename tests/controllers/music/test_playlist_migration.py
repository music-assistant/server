"""Tests for playlist migration."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, PropertyMock, call, patch

import pytest
from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import (
    InvalidDataError,
    ProviderUnavailableError,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.media_items import Playlist, ProviderMapping, Track

from music_assistant.controllers.music import MusicController
from music_assistant.controllers.music.media.playlists import (
    PlaylistController,
    PlaylistMigrationMatchPolicy,
)
from music_assistant.controllers.music.media.tracks import (
    TrackProviderEnrichment,
    TrackProviderMatch,
    TrackProviderMatchResult,
)
from music_assistant.helpers.compare import TrackMatchConfidence
from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider

from .helpers import create_track


@pytest.fixture
async def music(mass_minimal: MusicAssistant) -> AsyncGenerator[MusicController]:
    """Return a music controller attached to the minimal mass instance."""
    controller = MusicController(mass_minimal)
    mass_minimal.music = controller
    yield controller
    if controller._database:
        await controller._database.close()


def _playlist(
    item_id: str,
    name: str,
    provider_instance: str,
    provider_item_id: str,
) -> Playlist:
    """Build a provider-backed playlist."""
    return Playlist(
        item_id=item_id,
        provider="library",
        name=name,
        provider_mappings={
            ProviderMapping(
                item_id=provider_item_id,
                provider_domain=provider_instance.split("_", maxsplit=1)[0],
                provider_instance=provider_instance,
            )
        },
        owner="Test",
        is_editable=True,
    )


def test_migration_report_renders_substitutions_and_skips() -> None:
    """Migration reports summarize counts and item-level decisions as Markdown."""
    report = PlaylistController._build_migration_report(
        "Source",
        "Migrated",
        "Tidal",
        2,
        {
            "total": 3,
            "exact": 1,
            "same_recording": 1,
            "best_effort": 0,
            "skipped": 1,
            "ambiguous": 0,
            "library_matches": 1,
            "provider_matches": 0,
        },
        [("Artist - Song", "Artist - Song (Remaster)", "Same recording")],
        [("Artist - Missing", "No acceptable match")],
        [],
        completed=True,
        builtin_destination=False,
    )

    assert "## Playlist migration complete" in report
    assert "| Exact release | 1 |" in report
    assert "### Substitutions" in report
    assert "Artist - Missing" in report


def test_migration_report_caps_detail_rows() -> None:
    """Large migrations retain totals without creating unbounded task payloads."""
    skipped_tracks = [(f"Artist - Track {index}", "No acceptable match") for index in range(201)]
    report = PlaylistController._build_migration_report(
        "Source",
        "Migrated",
        "Tidal",
        0,
        {
            "total": 201,
            "exact": 0,
            "same_recording": 0,
            "best_effort": 0,
            "skipped": 201,
            "ambiguous": 0,
            "library_matches": 0,
            "provider_matches": 0,
        },
        [],
        skipped_tracks,
        [],
        completed=True,
        builtin_destination=False,
    )

    assert "_1 additional rows omitted._" in report
    assert "| Artist - Track 199 |" in report
    assert "| Artist - Track 200 |" not in report


async def test_provider_playlist_additions_are_batched_in_order() -> None:
    """Large playlist writes respect common provider request limits."""
    provider = MagicMock(spec=MusicProvider)
    provider.add_playlist_tracks = AsyncMock()
    track_ids = [str(index) for index in range(205)]

    await PlaylistController._add_provider_playlist_tracks(
        provider,
        "playlist",
        track_ids,
    )

    batches = [call.args[1] for call in provider.add_playlist_tracks.await_args_list]
    assert [len(batch) for batch in batches] == [100, 100, 5]
    assert [track_id for batch in batches for track_id in batch] == track_ids


async def test_migrate_playlist_queues_validated_task(
    music: MusicController,
) -> None:
    """The public command validates its destination and queues a managed task."""
    source_playlist = _playlist("1", "Source", "spotify_1", "source")
    target_provider = MagicMock(spec=MusicProvider)
    target_provider.instance_id = "tidal_1"
    target_provider.domain = "tidal"
    target_provider.name = "Tidal"
    target_provider.is_streaming_provider = True
    target_provider.supported_features = {
        ProviderFeature.PLAYLIST_CREATE,
        ProviderFeature.PLAYLIST_TRACKS_EDIT,
    }
    target_provider.supported_media_types = {MediaType.TRACK}
    source_provider = MagicMock(spec=MusicProvider)
    source_provider.instance_id = "spotify_1"
    source_provider.domain = "spotify"
    queued_task = MagicMock()
    tasks = MagicMock()
    tasks.run_background_task.return_value = queued_task

    with (
        patch.object(
            music.playlists,
            "get_library_item",
            AsyncMock(return_value=source_playlist),
        ),
        patch.object(
            music.mass,
            "get_provider",
            return_value=target_provider,
        ) as get_provider,
        patch.object(
            MusicController,
            "providers",
            new_callable=PropertyMock,
            return_value=[source_provider, target_provider],
        ),
        patch.object(
            music.mass,
            "tasks",
            tasks,
            create=True,
        ),
        patch.object(
            music.playlists,
            "_handle_migrate_playlist",
            AsyncMock(),
        ) as handle_migration,
    ):
        result = await music.playlists.migrate_playlist(
            source_playlist.item_id,
            destination_provider=target_provider.domain,
            name="Migrated",
            match_policy=PlaylistMigrationMatchPolicy.BEST_EFFORT,
        )
        await tasks.run_background_task.call_args.kwargs["handler"]()

    assert result is queued_task
    get_provider.assert_not_called()
    assert "task_id" not in tasks.run_background_task.call_args.kwargs
    assert tasks.run_background_task.call_args.kwargs["metadata"]["match_policy"] == "best_effort"
    handle_migration.assert_awaited_once_with(
        "1",
        "source",
        "spotify_1",
        "tidal_1",
        "Migrated",
        PlaylistMigrationMatchPolicy.BEST_EFFORT,
        ("spotify_1", "tidal_1"),
    )


async def test_migrate_playlist_rejects_filtered_source_provider(
    music: MusicController,
) -> None:
    """A deferred migration can not expand the initiating user's provider scope."""
    source_playlist = _playlist("1", "Source", "spotify_1", "source")
    source_provider = MagicMock(spec=MusicProvider)
    source_provider.instance_id = "spotify_1"
    source_provider.domain = "spotify"
    target_provider = MagicMock(spec=MusicProvider)
    target_provider.instance_id = "tidal_1"
    target_provider.domain = "tidal"
    target_provider.name = "Tidal"
    target_provider.is_streaming_provider = True
    target_provider.supported_features = {
        ProviderFeature.PLAYLIST_CREATE,
        ProviderFeature.PLAYLIST_TRACKS_EDIT,
    }
    target_provider.supported_media_types = {MediaType.TRACK}

    with (
        patch.object(
            music.playlists,
            "get_library_item",
            AsyncMock(return_value=source_playlist),
        ),
        patch.object(
            MusicController,
            "providers",
            new_callable=PropertyMock,
            return_value=[target_provider],
        ),
        patch.object(
            music.mass,
            "get_provider",
            return_value=source_provider,
        ),
        pytest.raises(ProviderUnavailableError, match="spotify_1"),
    ):
        await music.playlists.migrate_playlist(
            source_playlist.item_id,
            destination_provider=target_provider.instance_id,
        )


async def test_migrate_playlist_rejects_dynamic_source(
    music: MusicController,
) -> None:
    """A changing dynamic playlist can not be copied as a stable migration."""
    source_playlist = _playlist("1", "Dynamic", "spotify_1", "source")
    source_playlist.is_dynamic = True

    with (
        patch.object(
            music.playlists,
            "get_library_item",
            AsyncMock(return_value=source_playlist),
        ),
        pytest.raises(InvalidDataError, match="Dynamic playlists"),
    ):
        await music.playlists.migrate_playlist(source_playlist.item_id)


@pytest.mark.parametrize(
    (
        "duplicates_supported",
        "expected_target_ids",
        "actual_target_ids",
        "expected_failure_count",
    ),
    [
        (
            True,
            ["tidal-one", "tidal-two", "tidal-one"],
            ["tidal-one", "tidal-two", "tidal-one"],
            1,
        ),
        (
            False,
            ["tidal-one", "tidal-two"],
            ["tidal-one", "tidal-two"],
            2,
        ),
        (
            True,
            ["tidal-one", "tidal-two", "tidal-one"],
            ["tidal-one", "tidal-one"],
            2,
        ),
    ],
)
async def test_streaming_migration_handles_provider_duplicate_policy(
    music: MusicController,
    duplicates_supported: bool,
    expected_target_ids: list[str],
    actual_target_ids: list[str],
    expected_failure_count: int,
) -> None:
    """A provider migration preserves supported duplicates and reports unsupported ones."""
    source_playlist = _playlist("1", "Source", "spotify_1", "source")
    source_one = create_track("spotify_1", "one")
    source_two = create_track("spotify_1", "two")
    source_missing = create_track("spotify_1", "missing")
    target_one = create_track("tidal_1", "tidal-one")
    target_two = create_track("tidal_1", "tidal-two")
    target_playlist = _playlist("2", "Migrated", "tidal_1", "target")
    target_provider = MagicMock(spec=MusicProvider)
    target_provider.instance_id = "tidal_1"
    target_provider.domain = "tidal"
    target_provider.name = "Tidal"
    target_provider.playlist_duplicates_supported = duplicates_supported
    target_provider.add_playlist_tracks = AsyncMock()
    source_provider = MagicMock(spec=MusicProvider)
    source_provider.instance_id = "spotify_1"
    source_provider.domain = "spotify"
    track_requests: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def iter_playlist_tracks(*args: object, **kwargs: object) -> AsyncGenerator[Track]:
        track_requests.append((args, kwargs))
        tracks: tuple[Track, ...]
        if args == ("source", "spotify_1"):
            tracks = (
                source_one,
                source_two,
                source_one,
                source_missing,
                source_missing,
            )
        else:
            target_tracks = {
                "tidal-one": target_one,
                "tidal-two": target_two,
            }
            tracks = tuple(target_tracks[item_id] for item_id in actual_target_ids)
        for track in tracks:
            yield track

    matches = {
        "one": TrackProviderMatchResult(
            match=TrackProviderMatch(
                track=target_one,
                mapping=next(iter(target_one.provider_mappings)),
                confidence=TrackMatchConfidence.EXACT,
            )
        ),
        "two": TrackProviderMatchResult(
            match=TrackProviderMatch(
                track=target_two,
                mapping=next(iter(target_two.provider_mappings)),
                confidence=TrackMatchConfidence.LIKELY,
            )
        ),
        "missing": TrackProviderMatchResult(),
    }
    find_match = AsyncMock(side_effect=lambda track, *_args, **_kwargs: matches[track.item_id])

    with (
        patch.object(music.playlists, "get_library_item", AsyncMock(return_value=source_playlist)),
        patch.object(music.playlists, "tracks", iter_playlist_tracks),
        patch.object(music.tracks, "get_library_match", AsyncMock(return_value=None)),
        patch.object(music.tracks, "find_provider_match", find_match),
        patch.object(
            music.mass,
            "get_provider",
            side_effect=lambda provider_instance_id: {
                "spotify_1": source_provider,
                "tidal_1": target_provider,
            }[provider_instance_id],
        ),
        patch.object(
            music.playlists,
            "create_playlist",
            AsyncMock(return_value=target_playlist),
        ) as create_playlist,
        patch.object(
            music.playlists,
            "_select_provider_id",
            return_value=("tidal_1", "target"),
        ),
        patch.object(music.playlists, "update_item_in_library", AsyncMock()),
        patch(
            "music_assistant.controllers.music.media.playlists.report_current_task_failure"
        ) as report_failure,
        patch(
            "music_assistant.controllers.music.media.playlists.set_current_task_report"
        ) as set_report,
    ):
        await music.playlists._handle_migrate_playlist(
            source_playlist.item_id,
            "source",
            "spotify_1",
            target_provider.instance_id,
            "Migrated",
            PlaylistMigrationMatchPolicy.SAME_RECORDING,
            ("spotify_1", "tidal_1"),
        )

    create_playlist.assert_awaited_once()
    assert track_requests == [
        (("source", "spotify_1"), {"force_refresh": True}),
        (("target", "tidal_1"), {"force_refresh": True}),
    ]
    target_provider.add_playlist_tracks.assert_awaited_once_with(
        "target",
        expected_target_ids,
    )
    assert find_match.await_count == 3
    assert all(
        call.kwargs["allowed_provider_instances"] == {"spotify_1", "tidal_1"}
        for call in find_match.await_args_list
    )
    expected_failures = [
        call("Test Artist - Test Track: no acceptable match"),
    ]
    if not duplicates_supported:
        expected_failures.append(
            call("Test Artist - Test Track: tidal does not support duplicate playlist entries")
        )
    if "tidal-two" not in actual_target_ids:
        expected_failures.append(call("Test Artist - Test Track: tidal did not add this track"))
    assert report_failure.call_args_list == expected_failures
    assert report_failure.call_count == expected_failure_count
    assert set_report.call_count == 2
    assert "### Skipped tracks" in set_report.call_args.args[0]


async def test_builtin_migration_keeps_all_enriched_mappings(
    music: MusicController,
) -> None:
    """A Music Assistant playlist stores every resolved mapping without deduplication."""
    source_playlist = _playlist("1", "Source", "spotify_1", "source")
    source = create_track("spotify_1", "one")
    enriched = deepcopy(source)
    tidal_mapping = ProviderMapping(
        item_id="tidal-one",
        provider_domain="tidal",
        provider_instance="tidal_1",
    )
    enriched.provider_mappings.add(tidal_mapping)
    builtin_provider = MagicMock(spec=MusicProvider)
    builtin_provider.instance_id = "builtin"
    builtin_provider.domain = "builtin"
    builtin_provider.name = "Music Assistant"
    source_provider = MagicMock(spec=MusicProvider)
    source_provider.instance_id = "spotify_1"
    source_provider.domain = "spotify"
    destination_playlist = _playlist("2", "Migrated", "builtin", "migrated")

    async def iter_source_tracks(*_args: object, **_kwargs: object) -> AsyncGenerator[Track]:
        yield source
        yield source

    enrichment = TrackProviderEnrichment(
        track=enriched,
        matches=(
            TrackProviderMatch(
                track=enriched,
                mapping=tidal_mapping,
                confidence=TrackMatchConfidence.LIKELY,
            ),
        ),
        ambiguous_providers=(),
        failed_providers=(),
        used_library_item=True,
    )

    with (
        patch.object(music.playlists, "get_library_item", AsyncMock(return_value=source_playlist)),
        patch.object(music.playlists, "tracks", iter_source_tracks),
        patch.object(
            music.tracks,
            "enrich_provider_mappings",
            AsyncMock(return_value=enrichment),
        ) as enrich,
        patch.object(
            music.mass,
            "get_provider",
            side_effect=lambda provider_instance_id: {
                "builtin": builtin_provider,
                "spotify_1": source_provider,
            }[provider_instance_id],
        ),
        patch.object(
            music.playlists,
            "_create_builtin_migration_playlist",
            AsyncMock(return_value=destination_playlist),
        ) as create_builtin,
    ):
        await music.playlists._handle_migrate_playlist(
            source_playlist.item_id,
            "source",
            "spotify_1",
            builtin_provider.instance_id,
            "Migrated",
            PlaylistMigrationMatchPolicy.BEST_EFFORT,
            ("builtin", "spotify_1"),
        )

    enrich.assert_awaited_once_with(
        source,
        minimum_confidence=TrackMatchConfidence.LOOSE,
        provider_instance_ids={"builtin", "spotify_1"},
        trust_track_mappings=True,
        failed_provider_instances=set(),
    )
    assert create_builtin.await_args is not None
    entries = create_builtin.await_args.args[1]
    assert len(entries) == 2
    assert [{provider.domain for provider in entry.providers} for entry in entries] == [
        {"spotify", "tidal"},
        {"spotify", "tidal"},
    ]


async def test_builtin_resolution_does_not_return_unfiltered_track_on_failure(
    music: MusicController,
) -> None:
    """A failed enrichment can not serialize the original out-of-scope mappings."""
    source = create_track("spotify_1", "source")
    builtin_provider = MagicMock(spec=MusicProvider)
    builtin_provider.instance_id = "builtin"
    builtin_provider.domain = "builtin"
    builtin_provider.name = "Music Assistant"
    failed_provider_instances: set[str] = set()

    with patch.object(
        music.tracks,
        "enrich_provider_mappings",
        AsyncMock(side_effect=ResourceTemporarilyUnavailable("Lookup failed")),
    ):
        result = await music.playlists._resolve_migration_track(
            source,
            builtin_provider,
            TrackMatchConfidence.LIKELY,
            {"builtin"},
            False,
            failed_provider_instances,
        )

    assert result.track is None
    assert result.error == "Lookup failed"
