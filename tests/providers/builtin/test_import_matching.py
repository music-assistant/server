"""Tests for playlist import track matching against the shared resolver."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.enums import ImageType, MediaType
from music_assistant_models.media_items import (
    ItemMapping,
    MediaItemImage,
    MediaItemMetadata,
    Playlist,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.controllers.music.media.playlists import PlaylistMatchPolicy
from music_assistant.controllers.music.media.tracks import (
    TrackProviderEnrichment,
    TrackProviderMatch,
)
from music_assistant.helpers.compare import TrackMatchConfidence
from music_assistant.helpers.playlists import (
    ArtistInfo,
    PlaylistItem,
    ProviderMappingInfo,
    generate_m3u,
)
from music_assistant.providers.builtin import BuiltinProvider


def _make_provider(loaded_provider_domains: set[str] | None = None) -> BuiltinProvider:
    """Create a minimal BuiltinProvider with a mocked mass."""
    mass = MagicMock()
    loaded = loaded_provider_domains or set()
    mass.get_provider = MagicMock(
        side_effect=lambda pid, **_kwargs: MagicMock(domain=pid) if pid in loaded else None
    )
    prov = object.__new__(BuiltinProvider)
    prov.mass = mass
    prov.logger = MagicMock()
    prov._playlist_locks = {}
    return prov


def _make_track(
    name: str,
    artists: list[str] | None = None,
    provider_mappings: set[ProviderMapping] | None = None,
) -> Track:
    """Build a Track for enrichment stubbing."""
    artist_list: UniqueList[Any] = UniqueList()
    for a in artists or []:
        artist_list.append(
            ItemMapping(item_id=a, provider="test", name=a, media_type=MediaType.ARTIST)
        )
    return Track(
        item_id="matched123",
        provider="opensubsonic--abc123",
        name=name,
        artists=artist_list,
        provider_mappings=provider_mappings or set(),
    )


def _make_playlist_item(
    path: str = "spotify:track:original",
    title: str | None = "Artist - Song",
    length: str | None = "294",
    metadata: dict[str, str] | None = None,
    providers: list[ProviderMappingInfo] | None = None,
    artists: list[ArtistInfo] | None = None,
) -> PlaylistItem:
    """Build a PlaylistItem representing one parsed M3U entry."""
    return PlaylistItem(
        path=path,
        title=title,
        length=length,
        metadata=metadata,
        providers=providers or [],
        artists=artists or [],
    )


def _make_playlist(name: str, image_url: str | None = None) -> Playlist:
    """Build a Playlist for builtin provider tests."""
    metadata = MediaItemMetadata()
    if image_url:
        metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=image_url,
                    provider="builtin",
                    remotely_accessible=True,
                )
            ]
        )
    return Playlist(
        item_id="playlist_1",
        provider="builtin",
        name=name,
        metadata=metadata,
        provider_mappings={
            ProviderMapping(
                item_id="playlist_1", provider_domain="builtin", provider_instance="builtin"
            )
        },
    )


def _prepare(prov: BuiltinProvider, m3u_data: str, playlist_name: str = "Imported") -> Any:
    """Wire up the read/write/get_playlist mocks shared by every test."""
    prov_any = cast("Any", prov)
    prov_any._read_m3u_file = AsyncMock(return_value=m3u_data)
    prov_any.get_playlist = AsyncMock(return_value=_make_playlist(playlist_name))
    prov_any._write_m3u_file = AsyncMock()
    return prov_any


async def test_import_playlist_preserves_playlist_image() -> None:
    """Test that importing an M3U keeps the playlist-level image."""
    prov = _make_provider()
    prov_any = cast("Any", prov)
    prov_any.create_playlist = AsyncMock(return_value=_make_playlist("Imported Playlist"))
    prov_any.get_playlist = AsyncMock(
        return_value=_make_playlist("Imported Playlist", "https://img.example.com/cover.jpg")
    )
    prov_any._write_m3u_file = AsyncMock()

    m3u_data = generate_m3u(
        "Imported Playlist",
        [PlaylistItem(path="spotify://track/abc123", title="Test", length="120")],
        "https://img.example.com/cover.jpg",
    )

    result = await prov.import_playlist(m3u_data)

    assert prov_any._write_m3u_file.await_args is not None
    args = prov_any._write_m3u_file.await_args.args
    assert args[0] == "playlist_1"
    assert args[1] == "Imported Playlist"
    assert args[3] == "https://img.example.com/cover.jpg"
    assert result.image is not None
    assert result.image.path == "https://img.example.com/cover.jpg"


async def test_remove_playlist_tracks_preserves_playlist_image() -> None:
    """Test that rewriting a playlist after track removal keeps the playlist image."""
    prov = _make_provider()
    prov._playlist_locks = {}
    prov_any = cast("Any", prov)
    prov_any._read_m3u_file = AsyncMock(
        return_value=generate_m3u(
            "My Playlist",
            [
                PlaylistItem(path="spotify://track/one", title="One", length="120"),
                PlaylistItem(path="spotify://track/two", title="Two", length="180"),
            ],
            "https://img.example.com/cover.jpg",
        )
    )
    prov_any.get_playlist = AsyncMock(
        return_value=_make_playlist("My Playlist", "https://img.example.com/cover.jpg")
    )
    prov_any._write_m3u_file = AsyncMock()

    await prov.remove_playlist_tracks("playlist_1", (1,))

    assert prov_any._write_m3u_file.await_args is not None
    args = prov_any._write_m3u_file.await_args.args
    assert args[0] == "playlist_1"
    assert args[1] == "My Playlist"
    assert len(args[2]) == 1
    assert args[3] == "https://img.example.com/cover.jpg"


async def test_available_original_is_retained_without_search() -> None:
    """An entry whose original provider is still loaded is left untouched."""
    prov = _make_provider(loaded_provider_domains={"builtin"})
    m3u_data = generate_m3u(
        "Imported",
        [PlaylistItem(path="https://example.com/stream.mp3", title="Live Stream", length=None)],
    )
    prov_any = _prepare(prov, m3u_data)

    with patch("music_assistant.providers.builtin.set_current_task_report") as set_report:
        await prov.match_imported_playlist_tracks(
            "playlist_1", PlaylistMatchPolicy.BEST_EFFORT, ("qobuz--1",)
        )

    prov_any._write_m3u_file.assert_not_awaited()
    report_markdown = set_report.call_args.args[0]
    assert "| Retained | 1 |" in report_markdown


async def test_matched_entry_is_enriched_and_reported_as_exact() -> None:
    """A structured-artist entry matched at EXACT confidence is substituted and reported."""
    prov = _make_provider()
    item = _make_playlist_item(
        path="spotify:track:original",
        title="Artist - Song",
        artists=[ArtistInfo(name="Artist", provider_domain="", item_id="", provider_instance="")],
    )
    m3u_data = generate_m3u("Imported", [item])
    prov_any = _prepare(prov, m3u_data)

    matched_track = _make_track(
        "Song",
        artists=["Artist"],
        provider_mappings={
            ProviderMapping(
                item_id="xyz789",
                provider_domain="opensubsonic",
                provider_instance="opensubsonic--abc123",
            )
        },
    )
    enrichment = TrackProviderEnrichment(
        track=matched_track,
        matches=(
            TrackProviderMatch(
                track=matched_track,
                mapping=next(iter(matched_track.provider_mappings)),
                confidence=TrackMatchConfidence.EXACT,
            ),
        ),
        ambiguous_providers=(),
        failed_providers=(),
        used_library_item=False,
    )
    prov_any.mass.music.tracks.enrich_provider_mappings = AsyncMock(return_value=enrichment)

    with patch("music_assistant.providers.builtin.set_current_task_report") as set_report:
        await prov.match_imported_playlist_tracks(
            "playlist_1", PlaylistMatchPolicy.SAME_RECORDING, ("opensubsonic--abc123",)
        )

    prov_any._write_m3u_file.assert_awaited_once()
    written_items = prov_any._write_m3u_file.await_args.args[2]
    assert len(written_items) == 1
    assert written_items[0].providers
    assert written_items[0].providers[0].domain == "opensubsonic"
    report_markdown = set_report.call_args.args[0]
    assert "| Exact release | 1 |" in report_markdown
    assert "Substitutions" in report_markdown


async def test_ambiguous_match_is_reported_and_not_substituted() -> None:
    """An ambiguous provider match leaves the entry unmatched and notes the ambiguity."""
    prov = _make_provider()
    item = _make_playlist_item(
        artists=[ArtistInfo(name="Artist", provider_domain="", item_id="", provider_instance="")]
    )
    prov_any = _prepare(prov, generate_m3u("Imported", [item]))
    enrichment = TrackProviderEnrichment(
        track=_make_track("Song", artists=["Artist"]),
        matches=(),
        ambiguous_providers=("Qobuz",),
        failed_providers=(),
        used_library_item=False,
    )
    prov_any.mass.music.tracks.enrich_provider_mappings = AsyncMock(return_value=enrichment)

    with patch("music_assistant.providers.builtin.set_current_task_report") as set_report:
        await prov.match_imported_playlist_tracks(
            "playlist_1", PlaylistMatchPolicy.BEST_EFFORT, ("qobuz--1",)
        )

    prov_any._write_m3u_file.assert_not_awaited()
    report_markdown = set_report.call_args.args[0]
    assert "| Ambiguous | 1 |" in report_markdown
    assert "| Unmatched | 0 |" in report_markdown
    assert "Ambiguous match on Qobuz" in report_markdown


async def test_no_acceptable_match_is_reported_as_unmatched() -> None:
    """A search that yields nothing above the policy threshold is reported as unmatched."""
    prov = _make_provider()
    item = _make_playlist_item(
        artists=[ArtistInfo(name="Artist", provider_domain="", item_id="", provider_instance="")]
    )
    prov_any = _prepare(prov, generate_m3u("Imported", [item]))
    enrichment = TrackProviderEnrichment(
        track=_make_track("Song", artists=["Artist"]),
        matches=(),
        ambiguous_providers=(),
        failed_providers=(),
        used_library_item=False,
    )
    prov_any.mass.music.tracks.enrich_provider_mappings = AsyncMock(return_value=enrichment)

    with patch("music_assistant.providers.builtin.set_current_task_report") as set_report:
        await prov.match_imported_playlist_tracks(
            "playlist_1", PlaylistMatchPolicy.EXACT, ("qobuz--1",)
        )

    prov_any._write_m3u_file.assert_not_awaited()
    report_markdown = set_report.call_args.args[0]
    assert "| Unmatched | 1 |" in report_markdown
    assert "No acceptable match" in report_markdown


async def test_provider_error_is_reported_as_unmatched_with_issue() -> None:
    """A transient provider failure during matching is surfaced as a provider issue."""
    prov = _make_provider()
    item = _make_playlist_item(
        artists=[ArtistInfo(name="Artist", provider_domain="", item_id="", provider_instance="")]
    )
    prov_any = _prepare(prov, generate_m3u("Imported", [item]))
    prov_any.mass.music.tracks.enrich_provider_mappings = AsyncMock(
        side_effect=TimeoutError("provider timed out")
    )

    with patch("music_assistant.providers.builtin.set_current_task_report") as set_report:
        await prov.match_imported_playlist_tracks(
            "playlist_1", PlaylistMatchPolicy.BEST_EFFORT, ("qobuz--1",)
        )

    report_markdown = set_report.call_args.args[0]
    assert "| Unmatched | 1 |" in report_markdown
    assert "Provider lookup issues" in report_markdown
    assert "provider timed out" in report_markdown


async def test_extinf_title_without_structured_artist_is_split_for_matching() -> None:
    """A foreign M3U8 entry with only a combined EXTINF title still gets matched."""
    prov = _make_provider()
    # no #EXTARTIST tag: only a combined "Artist - Title" EXTINF string is available
    item = _make_playlist_item(
        path="/music/song.mp3", title="Radiohead - Everything In Its Right Place"
    )
    prov_any = _prepare(prov, generate_m3u("Imported", [item]))
    matched_track = _make_track(
        "Everything In Its Right Place",
        artists=["Radiohead"],
        provider_mappings={
            ProviderMapping(item_id="xyz", provider_domain="qobuz", provider_instance="qobuz--1")
        },
    )
    enrichment = TrackProviderEnrichment(
        track=matched_track,
        matches=(
            TrackProviderMatch(
                track=matched_track,
                mapping=next(iter(matched_track.provider_mappings)),
                confidence=TrackMatchConfidence.LIKELY,
            ),
        ),
        ambiguous_providers=(),
        failed_providers=(),
        used_library_item=False,
    )
    enrich_mock = AsyncMock(return_value=enrichment)
    prov_any.mass.music.tracks.enrich_provider_mappings = enrich_mock

    await prov.match_imported_playlist_tracks(
        "playlist_1", PlaylistMatchPolicy.BEST_EFFORT, ("qobuz--1",)
    )

    enrich_mock.assert_awaited_once()
    assert enrich_mock.await_args is not None
    resolved_track = enrich_mock.await_args.args[0]
    assert resolved_track.artists
    assert resolved_track.artists[0].name == "Radiohead"
    assert resolved_track.name == "Everything In Its Right Place"
    prov_any._write_m3u_file.assert_awaited_once()


async def test_missing_artist_metadata_is_unmatched_without_search() -> None:
    """An entry with no title at all cannot be searched and is reported as unmatched."""
    prov = _make_provider()
    item = _make_playlist_item(path="/music/track01.flac", title=None, length=None)
    prov_any = _prepare(prov, generate_m3u("Imported", [item]))
    enrich_mock = AsyncMock()
    prov_any.mass.music.tracks.enrich_provider_mappings = enrich_mock

    with patch("music_assistant.providers.builtin.set_current_task_report") as set_report:
        await prov.match_imported_playlist_tracks(
            "playlist_1", PlaylistMatchPolicy.BEST_EFFORT, ("qobuz--1",)
        )

    enrich_mock.assert_not_awaited()
    prov_any._write_m3u_file.assert_not_awaited()
    report_markdown = set_report.call_args.args[0]
    assert "| Unmatched | 1 |" in report_markdown
    assert "No artist metadata" in report_markdown


async def test_order_and_duplicates_preserved_across_mixed_results() -> None:
    """Retained, matched and unmatched entries keep their original position and duplicates."""
    prov = _make_provider(loaded_provider_domains={"builtin"})
    retained = PlaylistItem(path="https://example.com/a.mp3", title="Retained", length=None)
    to_match = _make_playlist_item(
        path="spotify:track:dup",
        title="Artist - Song",
        artists=[ArtistInfo(name="Artist", provider_domain="", item_id="", provider_instance="")],
    )
    unmatched = _make_playlist_item(path="/music/none.flac", title=None, length=None)
    items = [retained, to_match, to_match, unmatched]
    prov_any = _prepare(prov, generate_m3u("Imported", items))

    matched_track = _make_track(
        "Song",
        artists=["Artist"],
        provider_mappings={
            ProviderMapping(item_id="xyz", provider_domain="qobuz", provider_instance="qobuz--1")
        },
    )
    enrichment = TrackProviderEnrichment(
        track=matched_track,
        matches=(
            TrackProviderMatch(
                track=matched_track,
                mapping=next(iter(matched_track.provider_mappings)),
                confidence=TrackMatchConfidence.LOOSE,
            ),
        ),
        ambiguous_providers=(),
        failed_providers=(),
        used_library_item=False,
    )
    prov_any.mass.music.tracks.enrich_provider_mappings = AsyncMock(return_value=enrichment)

    await prov.match_imported_playlist_tracks(
        "playlist_1", PlaylistMatchPolicy.BEST_EFFORT, ("qobuz--1",)
    )

    written_items = prov_any._write_m3u_file.await_args.args[2]
    assert len(written_items) == 4
    assert written_items[0].path == "https://example.com/a.mp3"
    assert written_items[1].providers[0].domain == "qobuz"
    assert written_items[2].providers[0].domain == "qobuz"
    assert written_items[3].path == "/music/none.flac"


async def test_concurrent_edit_during_matching_is_preserved() -> None:
    """A track added to the playlist while matching runs is not discarded by the write-back."""
    prov = _make_provider()
    to_match = _make_playlist_item(
        path="spotify:track:original",
        title="Artist - Song",
        artists=[ArtistInfo(name="Artist", provider_domain="", item_id="", provider_instance="")],
    )
    initial_m3u = generate_m3u("Imported", [to_match])
    prov_any = _prepare(prov, initial_m3u)
    # simulate a track added by the user while the (possibly long-running) matching pass
    # above was still running: the second read - taken under the lock, right before the
    # write - reflects that concurrent edit
    concurrently_added = PlaylistItem(path="https://example.com/new.mp3", title="New", length=None)
    edited_m3u = generate_m3u("Imported", [to_match, concurrently_added])
    prov_any._read_m3u_file = AsyncMock(side_effect=[initial_m3u, edited_m3u])

    matched_track = _make_track(
        "Song",
        artists=["Artist"],
        provider_mappings={
            ProviderMapping(item_id="xyz", provider_domain="qobuz", provider_instance="qobuz--1")
        },
    )
    enrichment = TrackProviderEnrichment(
        track=matched_track,
        matches=(
            TrackProviderMatch(
                track=matched_track,
                mapping=next(iter(matched_track.provider_mappings)),
                confidence=TrackMatchConfidence.EXACT,
            ),
        ),
        ambiguous_providers=(),
        failed_providers=(),
        used_library_item=False,
    )
    prov_any.mass.music.tracks.enrich_provider_mappings = AsyncMock(return_value=enrichment)

    await prov.match_imported_playlist_tracks(
        "playlist_1", PlaylistMatchPolicy.EXACT, ("qobuz--1",)
    )

    written_items = prov_any._write_m3u_file.await_args.args[2]
    assert len(written_items) == 2
    assert written_items[0].providers[0].domain == "qobuz"
    assert written_items[1].path == "https://example.com/new.mp3"
