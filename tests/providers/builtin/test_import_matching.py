"""Tests for playlist import track matching against the shared resolver."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Self, cast
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.enums import ExternalID, ImageType, MediaType, PlaylistMatchPolicy
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError
from music_assistant_models.media_items import (
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    MediaItemMetadata,
    Playlist,
    ProviderMapping,
    Track,
    UniqueList,
)

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


def _make_provider(
    loaded_provider_domains: set[str] | None = None,
    unavailable_provider_domains: set[str] | None = None,
    get_provider_item: AsyncMock | None = None,
    provider_entries: list[tuple[str, str, bool]] | None = None,
) -> BuiltinProvider:
    """
    Create a minimal BuiltinProvider with a mocked mass.

    :param loaded_provider_domains: Domains/instances that resolve to a loaded, available
        provider.
    :param unavailable_provider_domains: Domains/instances that resolve to a provider that
        is configured but currently unavailable (only returned when ``return_unavailable``
        is passed).
    :param get_provider_item: Optional stub for the authoritative
        ``mass.music.tracks.get_provider_item`` lookup; defaults to one that always
        succeeds, as if the original track still resolves.
    :param provider_entries: Optional explicit (domain, instance_id, available) entries, for
        scenarios with several distinct instances of the same domain.
    """
    mass = MagicMock()
    loaded = loaded_provider_domains or set()
    unavailable = unavailable_provider_domains or set()
    # a single shared registry so mass.get_provider, mass.providers and
    # mass.get_provider_instances all agree on what is actually loaded
    registry = (
        [MagicMock(domain=pid, instance_id=pid, available=True) for pid in loaded]
        + [MagicMock(domain=pid, instance_id=pid, available=False) for pid in unavailable]
        + [
            MagicMock(domain=domain, instance_id=instance_id, available=available)
            for domain, instance_id, available in (provider_entries or [])
        ]
    )

    def _get_provider(
        pid: str, return_unavailable: bool = False, **_kwargs: Any
    ) -> MagicMock | None:
        for provider in registry:
            if provider.instance_id == pid and (return_unavailable or provider.available):
                return provider
        return None

    def _get_provider_instances(
        domain: str, return_unavailable: bool = False, **_kwargs: Any
    ) -> list[MagicMock]:
        return [
            provider
            for provider in registry
            if provider.domain == domain and (return_unavailable or provider.available)
        ]

    mass.providers = registry
    mass.get_provider = MagicMock(side_effect=_get_provider)
    mass.get_provider_instances = MagicMock(side_effect=_get_provider_instances)
    mass.music.tracks.get_provider_item = get_provider_item or AsyncMock(return_value=MagicMock())
    prov = object.__new__(BuiltinProvider)
    prov.mass = mass
    prov.logger = MagicMock()
    prov._playlist_locks = {}
    prov._playlist_lock = asyncio.Lock()
    return prov


def _allowed(prov: BuiltinProvider, *instance_ids: str) -> tuple[tuple[str, str], ...]:
    """
    Build an allowed-instances snapshot for the given instance ids.

    The domain for each id is taken from the provider's mocked registry when it is
    present there (loaded or unavailable); an id deliberately absent from the registry,
    simulating one that is configured but did not load at all, keeps its own string as
    a stand-in domain, which is fine since none of those tests exercise domain-only
    expansion.
    """
    domains = {provider.instance_id: provider.domain for provider in prov.mass.providers}
    return tuple(
        (instance_id, domains.get(instance_id, instance_id)) for instance_id in instance_ids
    )


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
    # a stable dummy generation: the playlist is the same file throughout the pass
    # unless a test overrides this to simulate a delete-and-recreate race
    prov_any._get_playlist_generation = AsyncMock(return_value=1)
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


class _FakeHttpResponse:
    """Stand-in for an aiohttp HEAD/GET response carrying a fixed status code."""

    def __init__(self, status: int) -> None:
        self.status = status

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


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
            "playlist_1", PlaylistMatchPolicy.BEST_EFFORT, _allowed(prov, "builtin", "qobuz--1")
        )

    prov_any._write_m3u_file.assert_not_awaited()
    report_markdown = set_report.call_args.args[0]
    assert "| Retained | 1 |" in report_markdown


async def test_bare_uri_without_extprov_is_recognized_as_available() -> None:
    """A plain M3U entry with a bare MA URI, but no #EXTPROV metadata, is retained."""
    prov = _make_provider(loaded_provider_domains={"spotify"})
    item = PlaylistItem(path="spotify://track/abc123", title="Test", length="120")
    assert not item.providers
    prov_any = _prepare(prov, generate_m3u("Imported", [item]))

    with patch("music_assistant.providers.builtin.set_current_task_report") as set_report:
        await prov.match_imported_playlist_tracks(
            "playlist_1", PlaylistMatchPolicy.BEST_EFFORT, _allowed(prov, "spotify", "qobuz--1")
        )

    # written back once so the entry keeps resolving to a provider mapping on future
    # loads too, since a bare URI with no #EXTPROV can otherwise never attach one -
    # it is still counted and reported as retained though, not as a substitution
    prov_any._write_m3u_file.assert_awaited_once()
    written_items = prov_any._write_m3u_file.await_args.args[2]
    assert len(written_items) == 1
    assert written_items[0].providers == [
        ProviderMappingInfo(domain="spotify", item_id="abc123", instance_id="spotify")
    ]
    report_markdown = set_report.call_args.args[0]
    assert "| Retained | 1 |" in report_markdown


async def test_share_url_without_extprov_is_persisted_as_provider_mapping() -> None:
    """A public share URL is resolved and persisted, not treated as a raw builtin stream."""
    prov = _make_provider(loaded_provider_domains={"spotify"})
    item = PlaylistItem(path="https://open.spotify.com/track/abc123", title="Test", length="120")
    assert not item.providers
    prov_any = _prepare(prov, generate_m3u("Imported", [item]))

    with patch("music_assistant.providers.builtin.set_current_task_report") as set_report:
        await prov.match_imported_playlist_tracks(
            "playlist_1", PlaylistMatchPolicy.BEST_EFFORT, _allowed(prov, "spotify", "qobuz--1")
        )

    # written back so the entry resolves through Spotify on future loads too, instead
    # of reconstructing as an unplayable raw builtin web URL
    prov_any._write_m3u_file.assert_awaited_once()
    written_items = prov_any._write_m3u_file.await_args.args[2]
    assert len(written_items) == 1
    assert written_items[0].providers == [
        ProviderMappingInfo(domain="spotify", item_id="abc123", instance_id="spotify")
    ]
    report_markdown = set_report.call_args.args[0]
    assert "| Retained | 1 |" in report_markdown


async def test_transient_stream_failure_retains_original_without_confirmation() -> None:
    """A network blip during the ffprobe check must not substitute a live stream."""
    prov = _make_provider(
        loaded_provider_domains={"builtin"},
        get_provider_item=AsyncMock(side_effect=InvalidDataError("ffprobe failed")),
    )
    item = PlaylistItem(path="https://example.com/stream.mp3", title="Live Stream", length=None)
    prov_any = _prepare(prov, generate_m3u("Imported", [item]))
    # a 500 response does not prove the stream is gone - ffprobe wraps both a
    # transient server error and a genuinely dead stream in the same error
    prov_any.mass.http_session.head = MagicMock(return_value=_FakeHttpResponse(500))

    with patch("music_assistant.providers.builtin.set_current_task_report") as set_report:
        await prov.match_imported_playlist_tracks(
            "playlist_1", PlaylistMatchPolicy.BEST_EFFORT, _allowed(prov, "builtin", "qobuz--1")
        )

    prov_any.mass.music.tracks.enrich_provider_mappings.assert_not_called()
    prov_any._write_m3u_file.assert_not_awaited()
    report_markdown = set_report.call_args.args[0]
    assert "| Retained | 1 |" in report_markdown


async def test_confirmed_dead_stream_url_falls_through_to_matching() -> None:
    """A HEAD and corroborating GET both reporting terminal status prove the stream is gone."""
    prov = _make_provider(
        loaded_provider_domains={"builtin"},
        get_provider_item=AsyncMock(side_effect=InvalidDataError("ffprobe failed")),
    )
    item = PlaylistItem(
        path="https://example.com/stream.mp3", title="Artist - Live Stream", length=None
    )
    prov_any = _prepare(prov, generate_m3u("Imported", [item]))
    prov_any.mass.http_session.head = MagicMock(return_value=_FakeHttpResponse(404))
    prov_any.mass.http_session.get = MagicMock(return_value=_FakeHttpResponse(404))
    enrichment = TrackProviderEnrichment(
        track=cast("Any", MagicMock()),
        matches=(),
        ambiguous_providers=(),
        failed_providers=(),
        used_library_item=False,
    )
    prov_any.mass.music.tracks.enrich_provider_mappings = AsyncMock(return_value=enrichment)

    with patch("music_assistant.providers.builtin.set_current_task_report") as set_report:
        await prov.match_imported_playlist_tracks(
            "playlist_1", PlaylistMatchPolicy.BEST_EFFORT, _allowed(prov, "builtin", "qobuz--1")
        )

    prov_any.mass.music.tracks.enrich_provider_mappings.assert_awaited_once()
    report_markdown = set_report.call_args.args[0]
    assert "| Retained | 1 |" not in report_markdown


async def test_head_404_alone_does_not_confirm_a_stream_is_gone() -> None:
    """A server that rejects HEAD but still serves GET must not have its stream substituted."""
    prov = _make_provider(
        loaded_provider_domains={"builtin"},
        get_provider_item=AsyncMock(side_effect=InvalidDataError("ffprobe failed")),
    )
    item = PlaylistItem(path="https://example.com/stream.mp3", title="Live Stream", length=None)
    prov_any = _prepare(prov, generate_m3u("Imported", [item]))
    # some servers reply 404 to HEAD for endpoints they do serve correctly on GET
    prov_any.mass.http_session.head = MagicMock(return_value=_FakeHttpResponse(404))
    prov_any.mass.http_session.get = MagicMock(return_value=_FakeHttpResponse(200))

    with patch("music_assistant.providers.builtin.set_current_task_report") as set_report:
        await prov.match_imported_playlist_tracks(
            "playlist_1", PlaylistMatchPolicy.BEST_EFFORT, _allowed(prov, "builtin", "qobuz--1")
        )

    prov_any.mass.music.tracks.enrich_provider_mappings.assert_not_called()
    prov_any._write_m3u_file.assert_not_awaited()
    report_markdown = set_report.call_args.args[0]
    assert "| Retained | 1 |" in report_markdown


async def test_head_method_not_allowed_still_confirms_via_get() -> None:
    """A server that rejects HEAD outright must still have a dead GET confirm it."""
    prov = _make_provider(
        loaded_provider_domains={"builtin"},
        get_provider_item=AsyncMock(side_effect=InvalidDataError("ffprobe failed")),
    )
    item = PlaylistItem(
        path="https://example.com/stream.mp3", title="Artist - Live Stream", length=None
    )
    prov_any = _prepare(prov, generate_m3u("Imported", [item]))
    # 405 means the server doesn't support HEAD at all, not that the stream is alive -
    # the ranged GET must still be attempted to determine that
    prov_any.mass.http_session.head = MagicMock(return_value=_FakeHttpResponse(405))
    prov_any.mass.http_session.get = MagicMock(return_value=_FakeHttpResponse(404))
    enrichment = TrackProviderEnrichment(
        track=cast("Any", MagicMock()),
        matches=(),
        ambiguous_providers=(),
        failed_providers=(),
        used_library_item=False,
    )
    prov_any.mass.music.tracks.enrich_provider_mappings = AsyncMock(return_value=enrichment)

    with patch("music_assistant.providers.builtin.set_current_task_report") as set_report:
        await prov.match_imported_playlist_tracks(
            "playlist_1", PlaylistMatchPolicy.BEST_EFFORT, _allowed(prov, "builtin", "qobuz--1")
        )

    prov_any.mass.music.tracks.enrich_provider_mappings.assert_awaited_once()
    report_markdown = set_report.call_args.args[0]
    assert "| Retained | 1 |" not in report_markdown


async def test_catalog_item_api_error_retains_original_without_confirmation() -> None:
    """A provider API error must not be treated as proof a catalog id was deleted."""
    prov = _make_provider(
        loaded_provider_domains={"qobuz--1"},
        get_provider_item=AsyncMock(side_effect=InvalidDataError("Error 500 while handling")),
    )
    item = _make_playlist_item(
        path="qobuz://track/12345",
        providers=[
            ProviderMappingInfo(
                domain="qobuz", instance_id="qobuz--1", item_id="12345", content_type=""
            )
        ],
    )
    prov_any = _prepare(prov, generate_m3u("Imported", [item]))

    with patch("music_assistant.providers.builtin.set_current_task_report") as set_report:
        await prov.match_imported_playlist_tracks(
            "playlist_1", PlaylistMatchPolicy.BEST_EFFORT, _allowed(prov, "qobuz--1")
        )

    prov_any.mass.music.tracks.enrich_provider_mappings.assert_not_called()
    prov_any._write_m3u_file.assert_not_awaited()
    report_markdown = set_report.call_args.args[0]
    assert "| Retained | 1 |" in report_markdown


async def test_bare_radio_uri_without_extma_is_not_matched_as_track() -> None:
    """A bare radio:// entry with no #EXTMA metadata must not be searched as a Track."""
    prov = _make_provider(
        loaded_provider_domains={"radiobrowser"},
        get_provider_item=AsyncMock(side_effect=MediaNotFoundError("gone")),
    )
    item = PlaylistItem(path="radiobrowser://radio/xyz123", title="Live Station", length=None)
    assert item.metadata is None
    assert not item.providers
    prov_any = _prepare(prov, generate_m3u("Imported", [item]))

    with patch("music_assistant.providers.builtin.set_current_task_report") as set_report:
        await prov.match_imported_playlist_tracks(
            "playlist_1", PlaylistMatchPolicy.BEST_EFFORT, _allowed(prov, "radiobrowser")
        )

    prov_any._write_m3u_file.assert_not_awaited()
    prov_any.mass.music.tracks.enrich_provider_mappings.assert_not_called()
    report_markdown = set_report.call_args.args[0]
    assert "| Retained | 1 |" in report_markdown


async def test_search_narrowing_does_not_affect_source_validation() -> None:
    """Narrowing the search targets must not make a playable original look unavailable."""
    prov = _make_provider(loaded_provider_domains={"spotify", "qobuz"})
    item = PlaylistItem(path="spotify://track/abc123", title="Test", length="120")
    prov_any = _prepare(prov, generate_m3u("Imported", [item]))

    with patch("music_assistant.providers.builtin.set_current_task_report") as set_report:
        # spotify is allowed (source validation), but the search is narrowed to qobuz only
        await prov.match_imported_playlist_tracks(
            "playlist_1",
            PlaylistMatchPolicy.BEST_EFFORT,
            _allowed(prov, "spotify", "qobuz--1"),
            ("qobuz--1",),
        )

    prov_any.mass.music.tracks.enrich_provider_mappings.assert_not_called()
    # written back once so the entry keeps resolving to a provider mapping on future
    # loads too, but the search narrowing itself must not have been touched
    prov_any._write_m3u_file.assert_awaited_once()
    written_items = prov_any._write_m3u_file.await_args.args[2]
    assert written_items[0].providers == [
        ProviderMappingInfo(domain="spotify", item_id="abc123", instance_id="spotify")
    ]
    report_markdown = set_report.call_args.args[0]
    assert "| Retained | 1 |" in report_markdown


async def test_configured_but_unavailable_provider_is_retained() -> None:
    """A provider that is configured but currently down is not treated as gone."""
    prov = _make_provider(unavailable_provider_domains={"spotify--1"})
    item = _make_playlist_item(
        path="spotify://track/abc123",
        providers=[
            ProviderMappingInfo(
                domain="spotify", instance_id="spotify--1", item_id="abc123", content_type=""
            )
        ],
    )
    prov_any = _prepare(prov, generate_m3u("Imported", [item]))

    with patch("music_assistant.providers.builtin.set_current_task_report") as set_report:
        await prov.match_imported_playlist_tracks(
            "playlist_1", PlaylistMatchPolicy.BEST_EFFORT, _allowed(prov, "spotify--1", "qobuz--1")
        )

    prov_any._write_m3u_file.assert_not_awaited()
    report_markdown = set_report.call_args.args[0]
    assert "| Retained | 1 |" in report_markdown


async def test_configured_but_unloaded_provider_is_retained() -> None:
    """A provider that failed setup entirely (not just down) is still not treated as gone."""
    # spotify--1 is deliberately absent from the registry: this simulates a provider
    # that is configured and allowed for this user, but did not load at all right now
    # (e.g. it failed setup), as opposed to one that loaded but is currently unavailable
    prov = _make_provider(loaded_provider_domains={"qobuz--1"})
    item = _make_playlist_item(
        path="spotify://track/abc123",
        providers=[
            ProviderMappingInfo(
                domain="spotify", instance_id="spotify--1", item_id="abc123", content_type=""
            )
        ],
    )
    prov_any = _prepare(prov, generate_m3u("Imported", [item]))

    with patch("music_assistant.providers.builtin.set_current_task_report") as set_report:
        await prov.match_imported_playlist_tracks(
            "playlist_1", PlaylistMatchPolicy.BEST_EFFORT, _allowed(prov, "spotify--1", "qobuz--1")
        )

    prov_any._write_m3u_file.assert_not_awaited()
    prov_any.mass.music.tracks.get_provider_item.assert_not_awaited()
    report_markdown = set_report.call_args.args[0]
    assert "| Retained | 1 |" in report_markdown


async def test_available_provider_with_dead_item_id_is_matched() -> None:
    """A loaded provider whose item id no longer resolves is substituted, not retained."""
    prov = _make_provider(
        loaded_provider_domains={"spotify--1"},
        get_provider_item=AsyncMock(side_effect=MediaNotFoundError("gone")),
    )
    item = _make_playlist_item(
        path="spotify://track/abc123",
        title="Artist - Song",
        providers=[
            ProviderMappingInfo(
                domain="spotify", instance_id="spotify--1", item_id="abc123", content_type=""
            )
        ],
        artists=[ArtistInfo(name="Artist", provider_domain="", item_id="", provider_instance="")],
    )
    prov_any = _prepare(prov, generate_m3u("Imported", [item]))
    matched_track = _make_track(
        "Song",
        artists=["Artist"],
        provider_mappings={
            ProviderMapping(item_id="xyz789", provider_domain="qobuz", provider_instance="qobuz--1")
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
            "playlist_1",
            PlaylistMatchPolicy.SAME_RECORDING,
            _allowed(prov, "spotify--1", "qobuz--1"),
        )

    # the configured source must actually be probed authoritatively - it must not be
    # skipped as if it were simply out of scope
    prov_any.mass.music.tracks.get_provider_item.assert_awaited_once()
    prov_any._write_m3u_file.assert_awaited_once()
    report_markdown = set_report.call_args.args[0]
    assert "| Retained | 0 |" in report_markdown
    assert "| Exact release | 1 |" in report_markdown


async def test_confirmed_dead_original_is_not_hydrated_again_during_matching() -> None:
    """The just-confirmed-dead original mapping is passed on, not force-refreshed twice."""
    prov = _make_provider(
        loaded_provider_domains={"spotify--1"},
        get_provider_item=AsyncMock(side_effect=MediaNotFoundError("gone")),
    )
    item = _make_playlist_item(
        path="spotify://track/abc123",
        title="Artist - Song",
        providers=[
            ProviderMappingInfo(
                domain="spotify", instance_id="spotify--1", item_id="abc123", content_type=""
            )
        ],
        artists=[ArtistInfo(name="Artist", provider_domain="", item_id="", provider_instance="")],
    )
    prov_any = _prepare(prov, generate_m3u("Imported", [item]))
    matched_track = _make_track(
        "Song",
        artists=["Artist"],
        provider_mappings={
            ProviderMapping(item_id="xyz789", provider_domain="qobuz", provider_instance="qobuz--1")
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
    enrich_provider_mappings = AsyncMock(return_value=enrichment)
    prov_any.mass.music.tracks.enrich_provider_mappings = enrich_provider_mappings

    with patch("music_assistant.providers.builtin.set_current_task_report"):
        await prov.match_imported_playlist_tracks(
            "playlist_1",
            PlaylistMatchPolicy.SAME_RECORDING,
            _allowed(prov, "spotify--1", "qobuz--1"),
        )

    # the liveness check above already force-refreshed and confirmed this exact
    # (instance, item id) pair dead, so it must be handed on rather than probed twice
    assert enrich_provider_mappings.await_args is not None
    assert enrich_provider_mappings.await_args.kwargs["known_dead_mappings"] == frozenset(
        {("spotify--1", "abc123")}
    )


async def test_persisted_mappings_exclude_weaker_compatible_tier() -> None:
    """A weaker, merely-compatible tier does not end up as the persisted substitute."""
    prov = _make_provider(
        loaded_provider_domains={"spotify--1"},
        get_provider_item=AsyncMock(side_effect=MediaNotFoundError("gone")),
    )
    item = _make_playlist_item(
        path="spotify://track/abc123",
        title="Artist - Song",
        providers=[
            ProviderMappingInfo(
                domain="spotify", instance_id="spotify--1", item_id="abc123", content_type=""
            )
        ],
        artists=[ArtistInfo(name="Artist", provider_domain="", item_id="", provider_instance="")],
    )
    prov_any = _prepare(prov, generate_m3u("Imported", [item]))
    exact_mapping = ProviderMapping(
        item_id="exact123", provider_domain="qobuz", provider_instance="qobuz--1"
    )
    likely_mapping = ProviderMapping(
        item_id="likely456", provider_domain="tidal", provider_instance="tidal--1"
    )
    exact_track = _make_track("Song", artists=["Artist"], provider_mappings={exact_mapping})
    likely_track = _make_track("Song", artists=["Artist"], provider_mappings={likely_mapping})
    enrichment = TrackProviderEnrichment(
        track=exact_track,
        # ordered strongest-tier-first, as _resolve_confident_matches guarantees
        matches=(
            TrackProviderMatch(
                track=exact_track, mapping=exact_mapping, confidence=TrackMatchConfidence.EXACT
            ),
            TrackProviderMatch(
                track=likely_track,
                mapping=likely_mapping,
                confidence=TrackMatchConfidence.LIKELY,
            ),
        ),
        ambiguous_providers=(),
        failed_providers=(),
        used_library_item=False,
    )
    prov_any.mass.music.tracks.enrich_provider_mappings = AsyncMock(return_value=enrichment)

    with patch("music_assistant.providers.builtin.set_current_task_report") as set_report:
        await prov.match_imported_playlist_tracks(
            "playlist_1",
            PlaylistMatchPolicy.SAME_RECORDING,
            _allowed(prov, "spotify--1", "qobuz--1", "tidal--1"),
        )

    prov_any._write_m3u_file.assert_awaited_once()
    written_items = prov_any._write_m3u_file.await_args.args[2]
    # the weaker LIKELY tier's own mapping must not become the persisted (and
    # therefore potentially chosen-as-primary-URI) substitute, even though it was
    # compatible enough to be accepted alongside the stronger EXACT tier
    assert written_items[0].providers == [
        ProviderMappingInfo(
            domain="qobuz",
            item_id="exact123",
            instance_id="qobuz--1",
            content_type="?",
            sample_rate=44100,
            bit_depth=16,
        )
    ]
    report_markdown = set_report.call_args.args[0]
    assert "| Exact release | 1 |" in report_markdown


async def test_same_tier_mappings_only_combine_with_the_primary_release() -> None:
    """Two same-tier matches for different releases don't get merged as one exact release."""
    prov = _make_provider(
        loaded_provider_domains={"spotify--1"},
        get_provider_item=AsyncMock(side_effect=MediaNotFoundError("gone")),
    )
    item = _make_playlist_item(
        path="spotify://track/abc123",
        title="Artist - Song",
        providers=[
            ProviderMappingInfo(
                domain="spotify", instance_id="spotify--1", item_id="abc123", content_type=""
            )
        ],
        artists=[ArtistInfo(name="Artist", provider_domain="", item_id="", provider_instance="")],
    )
    prov_any = _prepare(prov, generate_m3u("Imported", [item]))
    # a low-quality mapping that happens to be first in the (unordered) matches tuple
    low_quality_mapping = ProviderMapping(
        item_id="low456",
        provider_domain="tidal",
        provider_instance="tidal--1",
        audio_format=AudioFormat(sample_rate=44100, bit_depth=16),
    )
    low_quality_track = _make_track(
        "Song", artists=["Artist"], provider_mappings={low_quality_mapping}
    )
    # distinct provider identity from the other candidate track, so the two aren't
    # trivially recognised as the same item by shared-identity comparison
    low_quality_track.item_id = "low456"
    low_quality_track.provider = "tidal--1"
    low_quality_track.external_ids = {(ExternalID.MB_TRACK, "low-quality-release-mbid")}
    # a higher-quality mapping for a genuinely different release, at the same
    # (merely recording-level compatible) LIKELY confidence tier
    high_quality_mapping = ProviderMapping(
        item_id="high789",
        provider_domain="qobuz",
        provider_instance="qobuz--1",
        audio_format=AudioFormat(sample_rate=192000, bit_depth=24),
    )
    high_quality_track = _make_track(
        "Song", artists=["Artist"], provider_mappings={high_quality_mapping}
    )
    high_quality_track.item_id = "high789"
    high_quality_track.provider = "qobuz--1"
    high_quality_track.external_ids = {(ExternalID.MB_TRACK, "high-quality-release-mbid")}
    enrichment = TrackProviderEnrichment(
        track=low_quality_track,
        matches=(
            TrackProviderMatch(
                track=low_quality_track,
                mapping=low_quality_mapping,
                confidence=TrackMatchConfidence.LIKELY,
            ),
            TrackProviderMatch(
                track=high_quality_track,
                mapping=high_quality_mapping,
                confidence=TrackMatchConfidence.LIKELY,
            ),
        ),
        ambiguous_providers=(),
        failed_providers=(),
        used_library_item=False,
    )
    prov_any.mass.music.tracks.enrich_provider_mappings = AsyncMock(return_value=enrichment)

    with patch("music_assistant.providers.builtin.set_current_task_report") as set_report:
        await prov.match_imported_playlist_tracks(
            "playlist_1",
            PlaylistMatchPolicy.SAME_RECORDING,
            _allowed(prov, "spotify--1", "qobuz--1", "tidal--1"),
        )

    written_items = prov_any._write_m3u_file.await_args.args[2]
    # media_item_to_playlist_item picks its primary playback URI by mapping quality,
    # so the higher-quality qobuz mapping is what will actually be exported - the
    # entry's release evidence must describe that same release, not the low-quality
    # tidal candidate's unrelated one, and the tidal mapping must not be kept either
    assert written_items[0].providers == [
        ProviderMappingInfo(
            domain="qobuz",
            item_id="high789",
            instance_id="qobuz--1",
            content_type="?",
            sample_rate=192000,
            bit_depth=24,
        )
    ]
    assert written_items[0].metadata is not None
    assert written_items[0].metadata.get("mb_track") == "high-quality-release-mbid"
    report_markdown = set_report.call_args.args[0]
    assert "| Same recording | 1 |" in report_markdown


async def test_dead_url_is_matched_instead_of_retained() -> None:
    """A plain stream URL that no longer resolves is substituted, not silently kept."""
    prov = _make_provider(
        loaded_provider_domains={"builtin"},
        get_provider_item=AsyncMock(side_effect=InvalidDataError("404")),
    )
    item = _make_playlist_item(
        path="https://example.com/dead.mp3",
        title="Artist - Song",
        artists=[ArtistInfo(name="Artist", provider_domain="", item_id="", provider_instance="")],
    )
    prov_any = _prepare(prov, generate_m3u("Imported", [item]))
    matched_track = _make_track(
        "Song",
        artists=["Artist"],
        provider_mappings={
            ProviderMapping(item_id="xyz789", provider_domain="qobuz", provider_instance="qobuz--1")
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
            "playlist_1", PlaylistMatchPolicy.SAME_RECORDING, _allowed(prov, "qobuz--1")
        )

    prov_any._write_m3u_file.assert_awaited_once()
    report_markdown = set_report.call_args.args[0]
    assert "| Retained | 0 |" in report_markdown
    assert "| Exact release | 1 |" in report_markdown


async def test_original_source_outside_allowed_instances_is_not_trusted() -> None:
    """A source provider outside the initiating user's snapshot is never treated as playable."""
    prov = _make_provider(loaded_provider_domains={"spotify--1"})
    item = _make_playlist_item(
        path="spotify://track/abc123",
        providers=[
            ProviderMappingInfo(
                domain="spotify", instance_id="spotify--1", item_id="abc123", content_type=""
            )
        ],
        artists=[ArtistInfo(name="Artist", provider_domain="", item_id="", provider_instance="")],
    )
    prov_any = _prepare(prov, generate_m3u("Imported", [item]))
    matched_track = _make_track(
        "Song",
        artists=["Artist"],
        provider_mappings={
            ProviderMapping(item_id="xyz789", provider_domain="qobuz", provider_instance="qobuz--1")
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

    # spotify--1 is loaded and available, but is not part of this user's allowed snapshot -
    # it must never be trusted (or even probed) just because some account can resolve it
    with patch("music_assistant.providers.builtin.set_current_task_report") as set_report:
        await prov.match_imported_playlist_tracks(
            "playlist_1", PlaylistMatchPolicy.SAME_RECORDING, _allowed(prov, "qobuz--1")
        )

    prov_any._write_m3u_file.assert_awaited_once()
    prov_any.mass.music.tracks.get_provider_item.assert_not_awaited()
    report_markdown = set_report.call_args.args[0]
    assert "| Retained | 0 |" in report_markdown
    assert "| Exact release | 1 |" in report_markdown


async def test_original_source_probe_bypasses_cached_provider_details() -> None:
    """The authoritative liveness probe must not trust a stale cached hit."""
    prov = _make_provider(
        loaded_provider_domains={"spotify--1"},
        get_provider_item=AsyncMock(return_value=MagicMock()),
    )
    item = _make_playlist_item(
        path="spotify://track/abc123",
        providers=[
            ProviderMappingInfo(domain="spotify", instance_id="spotify--1", item_id="abc123"),
        ],
        artists=[ArtistInfo(name="Artist", provider_domain="", item_id="", provider_instance="")],
    )
    prov_any = _prepare(prov, generate_m3u("Imported", [item]))

    with patch("music_assistant.providers.builtin.set_current_task_report"):
        await prov.match_imported_playlist_tracks(
            "playlist_1", PlaylistMatchPolicy.BEST_EFFORT, _allowed(prov, "spotify--1")
        )

    # cached (possibly stale) details are never good enough for this check - a
    # provider hit that only comes from cache would hide a deletion that happened
    # after the cache entry was written
    prov_any.mass.music.tracks.get_provider_item.assert_awaited_once()
    assert prov_any.mass.music.tracks.get_provider_item.await_args.kwargs["force_refresh"] is True


async def test_domain_only_reference_tries_every_allowed_instance() -> None:
    """A domain-only #EXTPROV entry is probed on every allowed instance of that domain."""
    prov = _make_provider(
        provider_entries=[
            ("spotify", "spotify--1", True),
            ("spotify", "spotify--2", True),
        ],
        # the first instance probed does not have the item, the second one does
        get_provider_item=AsyncMock(
            side_effect=[MediaNotFoundError("gone"), MagicMock()],
        ),
    )
    item = _make_playlist_item(
        path="spotify://track/abc123",
        providers=[
            ProviderMappingInfo(domain="spotify", instance_id="", item_id="abc123"),
        ],
    )
    prov_any = _prepare(prov, generate_m3u("Imported", [item]))

    with patch("music_assistant.providers.builtin.set_current_task_report") as set_report:
        await prov.match_imported_playlist_tracks(
            "playlist_1",
            PlaylistMatchPolicy.BEST_EFFORT,
            _allowed(prov, "spotify--1", "spotify--2"),
        )

    # the domain-only reference is normalized to the instance that actually confirmed
    # it, so a later load resolves straight to spotify--2 instead of guessing the
    # first registered account (spotify--1, which just proved this id gone) again
    prov_any._write_m3u_file.assert_awaited_once()
    written_items = prov_any._write_m3u_file.await_args.args[2]
    assert len(written_items) == 1
    assert written_items[0].providers == [
        ProviderMappingInfo(domain="spotify", instance_id="spotify--2", item_id="abc123")
    ]
    assert prov_any.mass.music.tracks.get_provider_item.await_count == 2
    report_markdown = set_report.call_args.args[0]
    assert "| Retained | 1 |" in report_markdown


async def test_exact_instance_reference_never_widens_to_sibling_instance() -> None:
    """An exact #EXTPROV instance reference is never retried against a sibling instance."""
    prov = _make_provider(
        provider_entries=[
            ("spotify", "spotify--1", True),
            ("spotify", "spotify--2", True),
        ],
        # spotify--2 (a different account) does have the item, but must never be tried:
        # the entry names spotify--1 exactly, so only that instance may be probed
        get_provider_item=AsyncMock(side_effect=MediaNotFoundError("gone")),
    )
    item = _make_playlist_item(
        path="spotify://track/abc123",
        providers=[
            ProviderMappingInfo(domain="spotify", instance_id="spotify--1", item_id="abc123"),
        ],
        artists=[ArtistInfo(name="Artist", provider_domain="", item_id="", provider_instance="")],
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

    await prov.match_imported_playlist_tracks(
        "playlist_1", PlaylistMatchPolicy.BEST_EFFORT, _allowed(prov, "spotify--1", "spotify--2")
    )

    prov_any.mass.music.tracks.get_provider_item.assert_awaited_once()
    called_instance = prov_any.mass.music.tracks.get_provider_item.await_args.args[1]
    assert called_instance == "spotify--1"


async def test_extprov_naming_unregistered_instance_falls_back_to_allowed_sibling_domain() -> None:
    """A pinned instance absent locally is retried on allowed siblings of its own domain."""
    # spotify--1 does not exist in this server's registry at all - as if the M3U was
    # exported from a different Music Assistant install, where instance ids are
    # randomly generated per install - but the allowed snapshot's own spotify--2
    # sibling of the same domain does have the item
    prov = _make_provider(
        provider_entries=[("spotify", "spotify--2", True)],
        get_provider_item=AsyncMock(return_value=MagicMock()),
    )
    item = _make_playlist_item(
        path="spotify://track/abc123",
        providers=[
            ProviderMappingInfo(domain="spotify", instance_id="spotify--1", item_id="abc123"),
        ],
    )
    prov_any = _prepare(prov, generate_m3u("Imported", [item]))

    with patch("music_assistant.providers.builtin.set_current_task_report") as set_report:
        await prov.match_imported_playlist_tracks(
            "playlist_1", PlaylistMatchPolicy.BEST_EFFORT, _allowed(prov, "spotify--2")
        )

    # the unregistered pinned instance is normalized to the allowed sibling that
    # actually confirmed it, so a later load resolves straight to spotify--2 instead
    # of repeating this same fallback every pass
    prov_any._write_m3u_file.assert_awaited_once()
    written_items = prov_any._write_m3u_file.await_args.args[2]
    assert len(written_items) == 1
    assert written_items[0].providers == [
        ProviderMappingInfo(domain="spotify", instance_id="spotify--2", item_id="abc123")
    ]
    prov_any.mass.music.tracks.get_provider_item.assert_awaited_once_with(
        "abc123",
        "spotify--2",
        force_refresh=True,
        allow_fallback=False,
        strict_provider_instance=True,
    )
    report_markdown = set_report.call_args.args[0]
    assert "| Retained | 1 |" in report_markdown


async def test_domain_only_reference_to_unloaded_instance_is_retained() -> None:
    """A domain-only reference expands from the configured snapshot, not the live registry."""
    # spotify--1 is deliberately absent from the registry (failed setup), but it is
    # still part of the allowed snapshot together with its configured domain - a
    # domain-only reference must be able to find it there, not just among instances
    # mass.get_provider_instances happens to know about right now
    prov = _make_provider(loaded_provider_domains={"qobuz--1"})
    item = _make_playlist_item(
        path="spotify://track/abc123",
        providers=[
            ProviderMappingInfo(domain="spotify", instance_id="", item_id="abc123"),
        ],
    )
    prov_any = _prepare(prov, generate_m3u("Imported", [item]))

    with patch("music_assistant.providers.builtin.set_current_task_report") as set_report:
        await prov.match_imported_playlist_tracks(
            "playlist_1",
            PlaylistMatchPolicy.BEST_EFFORT,
            (("spotify--1", "spotify"), ("qobuz--1", "qobuz")),
        )

    prov_any._write_m3u_file.assert_not_awaited()
    prov_any.mass.music.tracks.get_provider_item.assert_not_awaited()
    report_markdown = set_report.call_args.args[0]
    assert "| Retained | 1 |" in report_markdown


async def test_duplicate_original_entries_are_resolved_only_once() -> None:
    """A track that repeats in the playlist is probed and searched only once."""
    prov = _make_provider(
        loaded_provider_domains={"spotify--1"},
        get_provider_item=AsyncMock(side_effect=MediaNotFoundError("gone")),
    )
    to_match = _make_playlist_item(
        path="spotify:track:dup",
        title="Artist - Song",
        providers=[
            ProviderMappingInfo(domain="spotify", instance_id="spotify--1", item_id="abc123"),
        ],
        artists=[ArtistInfo(name="Artist", provider_domain="", item_id="", provider_instance="")],
    )
    prov_any = _prepare(prov, generate_m3u("Imported", [to_match, to_match, to_match]))
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
    enrich_mock = AsyncMock(return_value=enrichment)
    prov_any.mass.music.tracks.enrich_provider_mappings = enrich_mock

    with patch("music_assistant.providers.builtin.set_current_task_report") as set_report:
        await prov.match_imported_playlist_tracks(
            "playlist_1",
            PlaylistMatchPolicy.SAME_RECORDING,
            _allowed(prov, "qobuz--1", "spotify--1"),
        )

    enrich_mock.assert_awaited_once()
    written_items = prov_any._write_m3u_file.await_args.args[2]
    assert len(written_items) == 3
    assert all(entry.providers[0].domain == "qobuz" for entry in written_items)
    report_markdown = set_report.call_args.args[0]
    assert "| Exact release | 3 |" in report_markdown


async def test_duplicate_path_with_different_metadata_is_resolved_independently() -> None:
    """Entries sharing a path but differing in title/artists are not conflated by the cache."""
    prov = _make_provider(
        loaded_provider_domains={"builtin"},
        get_provider_item=AsyncMock(side_effect=InvalidDataError("404")),
    )
    first = _make_playlist_item(
        path="https://example.com/shared.mp3",
        title="Artist One - Song One",
        artists=[
            ArtistInfo(name="Artist One", provider_domain="", item_id="", provider_instance="")
        ],
    )
    second = _make_playlist_item(
        path="https://example.com/shared.mp3",
        title="Artist Two - Song Two",
        artists=[
            ArtistInfo(name="Artist Two", provider_domain="", item_id="", provider_instance="")
        ],
    )
    prov_any = _prepare(prov, generate_m3u("Imported", [first, second]))
    track_one = _make_track(
        "Song One",
        artists=["Artist One"],
        provider_mappings={
            ProviderMapping(item_id="one", provider_domain="qobuz", provider_instance="qobuz--1")
        },
    )
    track_two = _make_track(
        "Song Two",
        artists=["Artist Two"],
        provider_mappings={
            ProviderMapping(item_id="two", provider_domain="qobuz", provider_instance="qobuz--1")
        },
    )
    enrichment_one = TrackProviderEnrichment(
        track=track_one,
        matches=(
            TrackProviderMatch(
                track=track_one,
                mapping=next(iter(track_one.provider_mappings)),
                confidence=TrackMatchConfidence.EXACT,
            ),
        ),
        ambiguous_providers=(),
        failed_providers=(),
        used_library_item=False,
    )
    enrichment_two = TrackProviderEnrichment(
        track=track_two,
        matches=(
            TrackProviderMatch(
                track=track_two,
                mapping=next(iter(track_two.provider_mappings)),
                confidence=TrackMatchConfidence.EXACT,
            ),
        ),
        ambiguous_providers=(),
        failed_providers=(),
        used_library_item=False,
    )
    enrich_mock = AsyncMock(side_effect=[enrichment_one, enrichment_two])
    prov_any.mass.music.tracks.enrich_provider_mappings = enrich_mock

    await prov.match_imported_playlist_tracks(
        "playlist_1", PlaylistMatchPolicy.SAME_RECORDING, _allowed(prov, "qobuz--1")
    )

    # a metadata-poor or different first duplicate must not suppress resolution of the
    # second entry - each is matched against its own title/artist evidence
    assert enrich_mock.await_count == 2
    written_items = prov_any._write_m3u_file.await_args.args[2]
    assert written_items[0].providers[0].item_id == "one"
    assert written_items[1].providers[0].item_id == "two"


async def test_unmatched_stale_mapping_does_not_crash_or_get_reused() -> None:
    """A preserved but unverified mapping with no actual match is unmatched, not a crash."""
    prov = _make_provider()
    item = _make_playlist_item(
        artists=[ArtistInfo(name="Artist", provider_domain="", item_id="", provider_instance="")]
    )
    prov_any = _prepare(prov, generate_m3u("Imported", [item]))
    stale_mapping = ProviderMapping(
        item_id="original", provider_domain="spotify", provider_instance="spotify--1"
    )
    enrichment = TrackProviderEnrichment(
        track=_make_track("Song", artists=["Artist"], provider_mappings={stale_mapping}),
        matches=(),
        ambiguous_providers=(),
        failed_providers=(),
        used_library_item=False,
    )
    prov_any.mass.music.tracks.enrich_provider_mappings = AsyncMock(return_value=enrichment)

    with patch("music_assistant.providers.builtin.set_current_task_report") as set_report:
        await prov.match_imported_playlist_tracks(
            "playlist_1", PlaylistMatchPolicy.BEST_EFFORT, _allowed(prov, "qobuz--1")
        )

    prov_any._write_m3u_file.assert_not_awaited()
    report_markdown = set_report.call_args.args[0]
    assert "| Unmatched | 1 |" in report_markdown


async def test_matched_entry_excludes_unmatched_stale_mapping() -> None:
    """The substituted entry only carries mappings produced by an actual match."""
    prov = _make_provider()
    item = _make_playlist_item(
        artists=[ArtistInfo(name="Artist", provider_domain="", item_id="", provider_instance="")]
    )
    prov_any = _prepare(prov, generate_m3u("Imported", [item]))
    stale_mapping = ProviderMapping(
        item_id="original", provider_domain="spotify", provider_instance="spotify--1"
    )
    matched_mapping = ProviderMapping(
        item_id="xyz789", provider_domain="qobuz", provider_instance="qobuz--1"
    )
    enriched_track = _make_track(
        "Song", artists=["Artist"], provider_mappings={stale_mapping, matched_mapping}
    )
    enrichment = TrackProviderEnrichment(
        track=enriched_track,
        matches=(
            TrackProviderMatch(
                track=enriched_track,
                mapping=matched_mapping,
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
            "playlist_1", PlaylistMatchPolicy.SAME_RECORDING, _allowed(prov, "qobuz--1")
        )

    written_items = prov_any._write_m3u_file.await_args.args[2]
    domains = {p.domain for p in written_items[0].providers}
    assert domains == {"qobuz"}
    report_markdown = set_report.call_args.args[0]
    assert "| Exact release | 1 |" in report_markdown


async def test_matched_entry_uses_matched_candidate_metadata_not_source() -> None:
    """The substituted entry reflects the matched candidate, not the dead source's metadata."""
    prov = _make_provider()
    item = _make_playlist_item(
        artists=[ArtistInfo(name="Artist", provider_domain="", item_id="", provider_instance="")]
    )
    prov_any = _prepare(prov, generate_m3u("Imported", [item]))
    # the dead source track carries release evidence (MB_TRACK) for a release that is
    # no longer reachable - it must not leak into the substituted entry, or a later
    # export/re-import could mistake this same-recording match for an exact one
    stale_source_track = _make_track("Song", artists=["Artist"])
    stale_source_track.external_ids = {(ExternalID.MB_TRACK, "stale-dead-release-mbid")}
    matched_mapping = ProviderMapping(
        item_id="xyz789", provider_domain="qobuz", provider_instance="qobuz--1"
    )
    # the actual matched candidate, freshly fetched from qobuz, has its own (different)
    # release evidence that genuinely belongs to it
    matched_candidate_track = _make_track(
        "Song", artists=["Artist"], provider_mappings={matched_mapping}
    )
    matched_candidate_track.external_ids = {(ExternalID.MB_TRACK, "fresh-matched-release-mbid")}
    enrichment = TrackProviderEnrichment(
        track=stale_source_track,
        matches=(
            TrackProviderMatch(
                track=matched_candidate_track,
                mapping=matched_mapping,
                confidence=TrackMatchConfidence.LIKELY,
            ),
        ),
        ambiguous_providers=(),
        failed_providers=(),
        used_library_item=False,
    )
    prov_any.mass.music.tracks.enrich_provider_mappings = AsyncMock(return_value=enrichment)

    with patch("music_assistant.providers.builtin.set_current_task_report") as set_report:
        await prov.match_imported_playlist_tracks(
            "playlist_1", PlaylistMatchPolicy.SAME_RECORDING, _allowed(prov, "qobuz--1")
        )

    written_items = prov_any._write_m3u_file.await_args.args[2]
    assert written_items[0].metadata is not None
    assert written_items[0].metadata.get("mb_track") == "fresh-matched-release-mbid"
    report_markdown = set_report.call_args.args[0]
    assert "| Same recording | 1 |" in report_markdown


async def test_update_playlist_metadata_uses_per_playlist_lock() -> None:
    """Metadata edits share the same per-playlist lock as substitutions and track edits."""
    prov = _make_provider()
    prov_any = _prepare(
        prov, generate_m3u("Imported", [PlaylistItem(path="a", title="A", length=None)])
    )

    await prov._update_playlist_metadata("playlist_1", "New Name", None)

    assert "playlist_1" in prov_any._playlist_locks


async def test_delete_playlist_uses_per_playlist_lock() -> None:
    """Deleting a user playlist shares the same per-playlist lock as other mutations."""
    prov = _make_provider()
    prov_any = cast("Any", prov)
    prov_any._playlists_dir = "stubbed-playlists-dir"

    with (
        patch("music_assistant.providers.builtin.os.path.isfile", return_value=True),
        patch("music_assistant.providers.builtin.os.remove"),
    ):
        await prov.library_remove("playlist_1", MediaType.PLAYLIST)

    assert "playlist_1" in prov_any._playlist_locks


async def test_delete_playlist_waits_for_global_file_lock() -> None:
    """Deleting a playlist file cannot race an in-flight _read_m3u_file/_write_m3u_file call."""
    prov = _make_provider()
    prov_any = cast("Any", prov)
    prov_any._playlists_dir = "stubbed-playlists-dir"

    with (
        patch("music_assistant.providers.builtin.os.path.isfile", return_value=True),
        patch("music_assistant.providers.builtin.os.remove") as remove_mock,
    ):
        # hold the same global file-I/O lock that _read_m3u_file/_write_m3u_file
        # acquire around their actual file access, as if a read or write were
        # already in flight
        await prov_any._playlist_lock.acquire()
        delete_task = asyncio.ensure_future(prov.library_remove("playlist_1", MediaType.PLAYLIST))
        try:
            await asyncio.sleep(0.1)
            # the removal must not proceed while the global lock is held elsewhere
            remove_mock.assert_not_called()
            prov_any._playlist_lock.release()
            await asyncio.wait_for(delete_task, timeout=2)
            remove_mock.assert_called_once()
        finally:
            if not delete_task.done():
                delete_task.cancel()


async def test_concurrent_deletes_of_the_same_playlist_do_not_race() -> None:
    """Two concurrent deletes of the same playlist must not both attempt os.remove."""
    prov = _make_provider()
    prov_any = cast("Any", prov)
    prov_any._playlists_dir = "stubbed-playlists-dir"
    file_state = {"exists": True}

    def fake_isfile(_path: str) -> bool:
        return file_state["exists"]

    def fake_remove(path: str) -> None:
        if not file_state["exists"]:
            # a real os.remove on an already-deleted file raises this
            raise FileNotFoundError(path)
        file_state["exists"] = False

    with (
        patch("music_assistant.providers.builtin.os.path.isfile", side_effect=fake_isfile),
        patch(
            "music_assistant.providers.builtin.os.remove", side_effect=fake_remove
        ) as remove_mock,
    ):
        # the existence check must be re-evaluated under the lock for each caller,
        # rather than both callers trusting a stale check made before either
        # acquired it
        result_a, result_b = await asyncio.gather(
            prov.library_remove("playlist_1", MediaType.PLAYLIST),
            prov.library_remove("playlist_1", MediaType.PLAYLIST),
        )

    assert result_a is True
    assert result_b is True
    remove_mock.assert_called_once()


async def test_read_m3u_file_existence_check_is_atomic_with_delete() -> None:
    """A read's existence check and file open cannot be split by a concurrent delete."""
    prov = _make_provider()
    prov_any = cast("Any", prov)
    prov_any._playlists_dir = "stubbed-playlists-dir"
    file_exists = True

    def fake_isfile(_path: str) -> bool:
        return file_exists

    class _FakeM3uFile:
        """Stand-in for aiofiles' open() that only succeeds while the file still exists."""

        async def __aenter__(self) -> Self:
            if not file_exists:
                raise FileNotFoundError(self)
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def read(self) -> str:
            return "#EXTM3U"

    with (
        patch("music_assistant.providers.builtin.os.path.isfile", side_effect=fake_isfile),
        patch("music_assistant.providers.builtin.aiofiles.open", return_value=_FakeM3uFile()),
    ):
        # hold the global file-I/O lock as if a concurrent operation is already in flight,
        # so the read's existence check cannot yet run ahead of a delete that is about to
        # remove the file
        await prov_any._playlist_lock.acquire()
        read_task = asyncio.ensure_future(prov._read_m3u_file("playlist_1"))
        try:
            await asyncio.sleep(0.1)
            assert not read_task.done()
            # the file is removed while the read is still waiting for the lock
            file_exists = False
            prov_any._playlist_lock.release()
            result = await asyncio.wait_for(read_task, timeout=2)
        finally:
            if not read_task.done():
                read_task.cancel()

    # the existence check must be re-evaluated under the lock rather than trusting a
    # stale result from before the file was removed
    assert result == ""


async def test_startup_migration_waits_for_per_playlist_lock() -> None:
    """The startup repair pass shares the per-playlist lock with delete/edit/import matching."""
    prov = _make_provider()
    prov_any = cast("Any", prov)
    prov_any._playlists_dir = "stubbed-playlists-dir"
    prov_any.manifest = MagicMock(domain="builtin")
    prov_any.mass.config.get = MagicMock(side_effect=lambda _key, default=None: default)
    prov_any._read_m3u_file = AsyncMock(
        return_value=generate_m3u(
            "Imported",
            [
                PlaylistItem(
                    path="builtin://track/1",
                    title="Already Resolved",
                    providers=[ProviderMappingInfo(domain="builtin", item_id="1")],
                    metadata={"media_type": "track"},
                )
            ],
        )
    )
    prov_any.get_playlist = AsyncMock(return_value=_make_playlist("Imported"))
    prov_any._write_m3u_file = AsyncMock()

    with patch(
        "music_assistant.providers.builtin.os.listdir",
        return_value=["playlist_1.m3u"],
    ):
        # hold the same per-playlist lock a concurrent delete/edit/import-matching write
        # would hold, as if one of those was already in flight for this playlist
        async with prov._get_playlist_lock("playlist_1"):
            migration_task = asyncio.ensure_future(prov._migrate_playlists())
            try:
                await asyncio.sleep(0.1)
                # the migration pass must not read/repair this playlist while it is locked
                assert not migration_task.done()
                prov_any._read_m3u_file.assert_not_awaited()
            finally:
                pass
        await asyncio.wait_for(migration_task, timeout=2)

    prov_any._read_m3u_file.assert_awaited_once_with("playlist_1")


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
            "playlist_1", PlaylistMatchPolicy.SAME_RECORDING, _allowed(prov, "opensubsonic--abc123")
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
            "playlist_1", PlaylistMatchPolicy.BEST_EFFORT, _allowed(prov, "qobuz--1")
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
            "playlist_1", PlaylistMatchPolicy.EXACT, _allowed(prov, "qobuz--1")
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
            "playlist_1", PlaylistMatchPolicy.BEST_EFFORT, _allowed(prov, "qobuz--1")
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
        "playlist_1", PlaylistMatchPolicy.BEST_EFFORT, _allowed(prov, "qobuz--1")
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
            "playlist_1", PlaylistMatchPolicy.BEST_EFFORT, _allowed(prov, "qobuz--1")
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
        "playlist_1", PlaylistMatchPolicy.BEST_EFFORT, _allowed(prov, "qobuz--1")
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
        "playlist_1", PlaylistMatchPolicy.EXACT, _allowed(prov, "qobuz--1")
    )

    written_items = prov_any._write_m3u_file.await_args.args[2]
    assert len(written_items) == 2
    assert written_items[0].providers[0].domain == "qobuz"
    assert written_items[1].path == "https://example.com/new.mp3"


async def test_concurrent_deletion_during_matching_is_reflected_in_report() -> None:
    """A track removed from the playlist while matching runs is reported, not double-counted."""
    prov = _make_provider()
    to_match = _make_playlist_item(
        path="spotify:track:original",
        title="Artist - Song",
        artists=[ArtistInfo(name="Artist", provider_domain="", item_id="", provider_instance="")],
    )
    initial_m3u = generate_m3u("Imported", [to_match])
    prov_any = _prepare(prov, initial_m3u)
    # the user removed the only entry while the (possibly long-running) matching pass was
    # still running: the second read - taken under the lock, right before the write - no
    # longer has it, so the resolved substitute must not be written or reported as applied
    edited_m3u = generate_m3u("Imported", [])
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

    with patch("music_assistant.providers.builtin.set_current_task_report") as set_report:
        await prov.match_imported_playlist_tracks(
            "playlist_1", PlaylistMatchPolicy.EXACT, _allowed(prov, "qobuz--1")
        )

    prov_any._write_m3u_file.assert_not_awaited()
    report_markdown = set_report.call_args.args[0]
    assert "| Exact release | 0 |" in report_markdown
    assert "| Skipped (playlist changed during matching) | 1 |" in report_markdown
    assert "Substitutions" not in report_markdown


async def test_playlist_deleted_and_recreated_during_matching_is_not_overwritten() -> None:
    """A stale pass must not write into an unrelated new playlist that reused the same ID."""
    prov = _make_provider()
    to_match = _make_playlist_item(
        path="spotify:track:original",
        title="Artist - Song",
        artists=[ArtistInfo(name="Artist", provider_domain="", item_id="", provider_instance="")],
    )
    m3u_data = generate_m3u("Imported", [to_match])
    prov_any = _prepare(prov, m3u_data)
    # the original playlist was deleted and a new, unrelated playlist was created that
    # reused the same sanitized ID while this (possibly long-running) pass was still
    # searching - even though its content happens to look identical, its creation
    # generation (captured once at the start of the pass) is now different
    prov_any._get_playlist_generation = AsyncMock(side_effect=[1, 2])

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

    with patch("music_assistant.providers.builtin.set_current_task_report") as set_report:
        await prov.match_imported_playlist_tracks(
            "playlist_1", PlaylistMatchPolicy.EXACT, _allowed(prov, "qobuz--1")
        )

    prov_any._write_m3u_file.assert_not_awaited()
    report_markdown = set_report.call_args.args[0]
    assert "| Skipped (playlist changed during matching) | 1 |" in report_markdown
    assert "Substitutions" not in report_markdown


async def test_match_discards_substitution_when_recreated_between_read_and_capture() -> None:
    """
    A recreate landing between the M3U read and generation capture is still caught.

    The initial read and the generation capture happen under the same per-playlist lock
    that ``library_remove`` takes before unlinking a playlist's file, so a concurrent
    recreate can only complete *after* that pair has been captured together - closing the
    narrower race where old content could otherwise be paired with a fresh generation and
    incorrectly accepted by the write-back check.
    """
    prov = _make_provider()
    prov_playlist_id = "playlist_1"
    to_match = _make_playlist_item(
        path="spotify:track:original",
        title="Artist - Song",
        artists=[ArtistInfo(name="Artist", provider_domain="", item_id="", provider_instance="")],
    )
    m3u_data = generate_m3u("Imported", [to_match])
    prov_any = _prepare(prov, m3u_data, playlist_name="Imported")

    generation = 1
    content_read = asyncio.Event()
    recreated = asyncio.Event()
    generation_calls = 0

    async def read_m3u(_playlist_id: str) -> str:
        content_read.set()
        # yield control so the concurrent recreate below gets a chance to attempt the
        # per-playlist lock before this pass's own generation capture happens
        await asyncio.sleep(0)
        return m3u_data

    async def get_generation(_playlist_id: str) -> int:
        nonlocal generation_calls
        generation_calls += 1
        if generation_calls == 2:
            # this is the write-back's final check - wait for the concurrent recreate
            # to actually finish so this reads its bumped value deterministically,
            # rather than depending on incidental task-scheduling order
            await recreated.wait()
        return generation

    async def race_recreate() -> None:
        nonlocal generation
        await content_read.wait()
        # mirrors library_remove, which takes this same per-playlist lock before
        # unlinking the file - it can only proceed once the read+capture pair below
        # releases the lock
        async with prov._get_playlist_lock(prov_playlist_id):
            generation += 1
        recreated.set()

    prov_any._read_m3u_file = AsyncMock(side_effect=read_m3u)
    prov_any._get_playlist_generation = AsyncMock(side_effect=get_generation)

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

    race_task = asyncio.ensure_future(race_recreate())
    with patch("music_assistant.providers.builtin.set_current_task_report") as set_report:
        await prov.match_imported_playlist_tracks(
            prov_playlist_id, PlaylistMatchPolicy.EXACT, _allowed(prov, "qobuz--1")
        )
    await race_task

    # the recreate genuinely raced in and completed - this isn't passing by mere
    # absence of contention
    assert recreated.is_set()
    assert generation == 2
    # the substitution was matched against the pre-recreate content, so it must be
    # discarded rather than written into the recreated playlist
    prov_any._write_m3u_file.assert_not_awaited()
    report_markdown = set_report.call_args.args[0]
    assert "| Skipped (playlist changed during matching) | 1 |" in report_markdown
    assert "Substitutions" not in report_markdown


async def test_create_playlist_generation_survives_inode_reuse(tmp_path: Path) -> None:
    """
    A recreate under the same ID gets a new generation, even with the same inode.

    The generation counter is an in-memory value bumped by ``create_playlist``, not a
    filesystem fingerprint, so it stays reliable even when a delete followed by a
    same-path create happens to have the filesystem reuse the just-freed inode number -
    a scenario this test does not need to force, since the implementation never reads
    inode/device information in the first place.
    """
    prov = _make_provider()
    prov_any = cast("Any", prov)
    prov_any._playlists_dir = str(tmp_path)
    prov_any._playlist_generations = {}
    prov_any.get_playlist = AsyncMock(return_value=_make_playlist("Test"))

    await prov.create_playlist("Test", {MediaType.TRACK})
    playlist_id = prov._sanitize_playlist_id("Test")
    playlist_file = tmp_path / f"{playlist_id}.m3u"
    first_generation = await prov._get_playlist_generation(playlist_id)
    assert first_generation == 1

    playlist_file.unlink()
    assert await prov._get_playlist_generation(playlist_id) is None
    await prov.create_playlist("Test", {MediaType.TRACK})

    second_generation = await prov._get_playlist_generation(playlist_id)
    assert second_generation == 2
    assert second_generation != first_generation

    assert second_generation != first_generation
