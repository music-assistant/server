"""Tests for provider-agnostic share links."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncGenerator
from dataclasses import replace
from types import SimpleNamespace
from typing import Literal, cast
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from music_assistant_models.auth import Scope
from music_assistant_models.enums import ExternalID, MediaType, PlaylistMatchPolicy, ProviderType
from music_assistant_models.errors import (
    InvalidDataError,
    ProviderUnavailableError,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    ItemMapping,
    Playlist,
    ProviderMapping,
    Radio,
    Track,
    UniqueList,
)

from music_assistant.helpers.compare import (
    TrackMatchConfidence,
    compare_track,
    compare_track_evidence,
)
from music_assistant.helpers.playlists import (
    AlbumInfo,
    ArtistInfo,
    PlaylistItem,
    construct_media_item_from_playlist_item,
    parse_m3u,
)
from music_assistant.providers.share_provider import (
    SharePayload,
    ShareProvider,
    TrackStub,
)
from music_assistant.providers.share_provider.links import (
    build_provider_links,
    build_provider_refs,
    compact_artwork_ref,
    expand_artwork_ref,
    expand_provider_ref,
)

ISRC = "USRC17607839"
BARCODE = "00724354283857"


def _provider() -> ShareProvider:
    """Build a share provider with mocked server services."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "share_provider"
    config = MagicMock()
    config.instance_id = "share_provider--test"
    config.name = "Share Links"
    config.get_value.return_value = "GLOBAL"
    provider = ShareProvider(mass, manifest, config)
    mass.config.get_provider_configs = AsyncMock(return_value=[])
    mass.music.tracks.enrich_provider_mappings = AsyncMock(return_value=SimpleNamespace(matches=()))
    return provider


def _mass(provider: ShareProvider) -> MagicMock:
    return cast("MagicMock", provider.mass)


def _core_match(track: Track, confidence: TrackMatchConfidence) -> SimpleNamespace:
    """Build a minimal core enrichment match for a provider track."""
    mapping = ProviderMapping(
        item_id=track.item_id,
        provider_domain=track.provider.split("--", maxsplit=1)[0],
        provider_instance=track.provider,
    )
    return SimpleNamespace(track=track, confidence=confidence, mapping=mapping)


def _track(
    item_id: str = "track",
    name: str = "Track",
    artist: str = "Artist",
    version: str = "",
    duration: int = 218,
    external_ids: set[tuple[ExternalID, str]] | None = None,
    provider_mappings: set[ProviderMapping] | None = None,
) -> Track:
    """Build a full-enough Track for exercising core comparison behavior."""
    return Track(
        item_id=item_id,
        provider="catalog--1",
        name=name,
        version=version,
        duration=duration,
        provider_mappings=provider_mappings
        if provider_mappings is not None
        else {
            ProviderMapping(
                item_id=item_id,
                provider_domain="catalog",
                provider_instance="catalog--1",
                available=True,
            )
        },
        external_ids=external_ids or set(),
        artists=UniqueList(
            [
                Artist(
                    item_id="artist",
                    provider="catalog--1",
                    name=artist,
                    provider_mappings=set(),
                )
            ]
            if artist
            else []
        ),
    )


@pytest.mark.parametrize(
    ("media_type", "external_ids", "expected_type", "expected_id"),
    [
        (
            MediaType.TRACK,
            {"isrc": ISRC, "mbid": "recording-mbid"},
            ExternalID.ISRC,
            ISRC,
        ),
        (
            MediaType.TRACK,
            {"mb_track": "release-track-mbid"},
            ExternalID.MB_TRACK,
            "release-track-mbid",
        ),
        (
            MediaType.ALBUM,
            {"barcode": BARCODE, "mbid": "album-mbid"},
            ExternalID.BARCODE,
            BARCODE,
        ),
        (
            MediaType.ARTIST,
            {"mbid": "artist-mbid"},
            ExternalID.MB_ARTIST,
            "artist-mbid",
        ),
    ],
)
async def test_resolve_uses_controller_external_id_before_search(
    media_type: MediaType,
    external_ids: dict[str, str],
    expected_type: ExternalID,
    expected_id: str,
) -> None:
    """Resolve matching media types through the shared controller contract."""
    provider = _provider()
    controller = _mass(provider).music.get_controller.return_value
    item: Album | Artist | Track | MagicMock
    if media_type == MediaType.ALBUM:
        item = Album(
            item_id="item",
            provider="library",
            name="Item",
            provider_mappings=set(),
            external_ids={(expected_type, expected_id)},
        )
    elif media_type == MediaType.TRACK:
        item = _track(
            item_id="item",
            name="Item",
            external_ids={(expected_type, expected_id)},
        )
    else:
        item = Artist(
            item_id="item",
            provider="library",
            name="Item",
            provider_mappings=set(),
            external_ids={(expected_type, expected_id)},
        )
    controller.get_item_by_external_id = AsyncMock(return_value=item)
    _mass(provider).music.tracks.get = AsyncMock(return_value=item)
    encoded = SharePayload(
        type=cast("Literal['track', 'album', 'artist', 'playlist']", media_type.value),
        title="Item",
        isrc=external_ids.get("isrc"),
        mbid=external_ids.get("mbid"),
        mb_track=external_ids.get("mb_track"),
        barcode=external_ids.get("barcode"),
        artist="Artist" if media_type == MediaType.TRACK else None,
        duration=218 if media_type == MediaType.TRACK else None,
    ).to_base64()

    result = await provider.resolve_share_link(encoded, allow_search=False)

    assert result is item
    controller.get_item_by_external_id.assert_awaited_once_with(expected_id, expected_type)
    _mass(provider).music.search.assert_not_called()


@pytest.mark.parametrize("lookup_failure", [False, True])
async def test_resolve_without_search_returns_none_after_id_miss(lookup_failure: bool) -> None:
    """ID-only resolution never falls back to a text-search candidate."""
    provider = _provider()
    mass = _mass(provider)
    controller = mass.music.get_controller.return_value
    controller.get_item_by_external_id = AsyncMock(
        return_value=None,
        side_effect=ProviderUnavailableError("provider offline") if lookup_failure else None,
    )
    encoded = SharePayload(
        type="track", title="Track", artist="Artist", isrc=ISRC, duration=218
    ).to_base64()

    assert await provider.resolve_share_link(encoded, allow_search=False) is None
    mass.music.search.assert_not_called()


async def test_resolve_skips_unrelated_barcode_album_and_tries_mbid() -> None:
    """A barcode collision does not mask an album matching the shared metadata."""
    provider = _provider()
    mass = _mass(provider)
    unrelated = Album(
        item_id="wrong-album",
        provider="library",
        name="Different Album",
        provider_mappings=set(),
    )
    matching = Album(
        item_id="right-album",
        provider="library",
        name="Shared Album",
        provider_mappings=set(),
    )
    controller = mass.music.get_controller.return_value
    controller.get_item_by_external_id = AsyncMock(side_effect=[unrelated, matching])
    payload = SharePayload(type="album", title="Shared Album", barcode=BARCODE, mbid="album-mbid")

    result = await provider._resolve_media_item(payload, allow_search=False)

    assert result is matching
    assert controller.get_item_by_external_id.await_count == 2
    mass.music.search.assert_not_called()


@pytest.mark.parametrize(
    ("candidate_title", "candidate_artist"),
    [("Different song", "Artist"), ("Shared song", "Other artist")],
)
async def test_resolve_rejects_unrelated_isrc_match(
    candidate_title: str, candidate_artist: str
) -> None:
    """A matching ISRC and duration still require title and artist corroboration."""
    provider = _provider()
    mass = _mass(provider)
    unrelated = _track(
        item_id="wrong-track",
        name=candidate_title,
        artist=candidate_artist,
        duration=218,
        external_ids={(ExternalID.ISRC, ISRC)},
    )
    mass.music.get_controller.return_value.get_item_by_external_id = AsyncMock(
        return_value=unrelated
    )
    mass.music.tracks.get = AsyncMock()
    payload = SharePayload(
        type="track", title="Shared song", artist="Artist", isrc=ISRC, duration=218
    )

    result = await provider._resolve_media_item(payload, allow_search=False)

    assert result is None
    mass.music.tracks.get.assert_not_called()


async def test_legacy_isrc_share_without_duration_is_not_resolved() -> None:
    """A legacy ISRC without duration is insufficient identity evidence."""
    provider = _provider()
    mass = _mass(provider)
    candidate = _track(external_ids={(ExternalID.ISRC, ISRC)})
    mass.music.get_controller.return_value.get_item_by_external_id = AsyncMock(
        return_value=candidate
    )
    result = await provider._resolve_media_item(
        SharePayload(type="track", title="Track", artist="Artist", isrc=ISRC),
        allow_search=False,
    )

    assert result is None
    mass.music.tracks.get.assert_not_called()


async def test_resolve_skips_conflicting_isrc_recording_id_and_tries_mbid() -> None:
    """A conflicting recording ID on an ISRC hit does not hide a valid MBID match."""
    provider = _provider()
    mass = _mass(provider)
    conflicting = _track(
        item_id="conflicting-track",
        name="Shared song",
        external_ids={(ExternalID.ISRC, ISRC), (ExternalID.MB_RECORDING, "recording-other")},
    )
    matching = _track(
        item_id="matching-track",
        name="Shared song",
        external_ids={(ExternalID.MB_RECORDING, "recording-shared")},
    )
    controller = mass.music.get_controller.return_value
    controller.get_item_by_external_id = AsyncMock(side_effect=[conflicting, matching])
    mass.music.tracks.get = AsyncMock(return_value=matching)
    payload = SharePayload(
        type="track",
        title="Shared song",
        artist="Artist",
        isrc=ISRC,
        mbid="recording-shared",
        duration=218,
    )

    result = await provider._resolve_media_item(payload, allow_search=False)

    assert result is matching
    assert controller.get_item_by_external_id.await_count == 2
    mass.music.tracks.get.assert_awaited_once_with(
        "matching-track", "catalog--1", allow_update_metadata=False
    )


async def test_resolve_without_search_returns_id_match() -> None:
    """ID matches include full track and album details without text search."""
    provider = _provider()
    mass = _mass(provider)
    item = _track(item_id="source-id")
    full_item = MagicMock()
    mass.music.tracks.get = AsyncMock(return_value=full_item)
    mass.music.get_controller.return_value.get_item_by_external_id = AsyncMock(return_value=item)
    encoded = SharePayload(
        type="track", title="Track", artist="Artist", isrc=ISRC, duration=218
    ).to_base64()

    assert await provider.resolve_share_link(encoded, allow_search=False) is full_item
    mass.music.tracks.get.assert_awaited_once_with(
        item.item_id, item.provider, allow_update_metadata=False
    )
    mass.music.search.assert_not_called()


async def test_generate_share_link_rejects_oversized_playlist() -> None:
    """Reject playlist payloads that cannot fit in a single API DataChannel message."""
    provider = _provider()
    mass = _mass(provider)
    mass.music.get_item_by_uri = AsyncMock(return_value=MagicMock())
    payload = SharePayload(
        type="playlist",
        title="Playlist",
        tracks=[TrackStub(title="x" * (50 * 1024))],
    )

    with (
        patch.object(provider, "_build_payload", new=AsyncMock(return_value=payload)),
        pytest.raises(InvalidDataError) as err,
    ):
        await provider.generate_share_link("library://playlist/1")

    assert err.value.translation_key == "share_playlist_too_large"


async def test_resolve_keeps_release_track_id_match_despite_version_label_drift() -> None:
    """A release-specific MusicBrainz ID is stronger than provider version labels."""
    provider = _provider()
    mass = _mass(provider)
    id_match = _track(
        item_id="release-track",
        version="Studio",
        external_ids={(ExternalID.MB_TRACK, "release-track-mbid")},
    )
    full_track = _track(item_id="release-track", version="Studio")
    mass.music.get_controller.return_value.get_item_by_external_id = AsyncMock(
        return_value=id_match
    )
    mass.music.tracks.get = AsyncMock(return_value=full_track)
    payload = SharePayload(
        type="track",
        title="Track",
        artist="Artist",
        version="Live",
        mb_track="release-track-mbid",
    )

    assert await provider._resolve_media_item(payload, allow_search=False) is full_track
    mass.music.tracks.get.assert_awaited_once_with(
        "release-track", "catalog--1", allow_update_metadata=False
    )


async def test_resolve_rejects_version_mismatch_for_recording_id() -> None:
    """A recording ID must not resolve to a different explicit version."""
    provider = _provider()
    mass = _mass(provider)
    mass.music.get_controller.return_value.get_item_by_external_id = AsyncMock(
        return_value=_track(
            item_id="recording",
            version="Studio",
            external_ids={(ExternalID.MB_RECORDING, "recording-mbid")},
        )
    )
    payload = SharePayload(
        type="track",
        title="Track",
        artist="Artist",
        version="Live",
        mbid="recording-mbid",
    )

    assert await provider._resolve_media_item(payload, allow_search=False) is None
    mass.music.tracks.get.assert_not_called()


async def test_resolve_unversioned_track_rejects_versioned_recording_id() -> None:
    """An unversioned share must not resolve to a version-specific recording ID."""
    provider = _provider()
    mass = _mass(provider)
    mass.music.get_controller.return_value.get_item_by_external_id = AsyncMock(
        return_value=_track(
            item_id="live-recording",
            version="Live",
            external_ids={(ExternalID.ISRC, ISRC)},
        )
    )

    assert (
        await provider._resolve_media_item(
            SharePayload(type="track", title="Track", artist="Artist", isrc=ISRC),
            allow_search=False,
        )
        is None
    )
    mass.music.tracks.get.assert_not_called()


async def test_resolve_skips_id_match_when_full_track_details_are_unavailable() -> None:
    """An ID hit whose detail fetch fails remains a miss for preview fallback."""
    provider = _provider()
    mass = _mass(provider)
    mass.music.get_controller.return_value.get_item_by_external_id = AsyncMock(
        return_value=_track(item_id="source-id", external_ids={(ExternalID.ISRC, ISRC)})
    )
    mass.music.tracks.get = AsyncMock(side_effect=ProviderUnavailableError("offline"))
    encoded = SharePayload(
        type="track", title="Track", artist="Artist", isrc=ISRC, duration=218
    ).to_base64()

    assert await provider.resolve_share_link(encoded, allow_search=False) is None
    mass.music.tracks.get.assert_awaited_once()
    mass.music.search.assert_not_called()


async def test_track_resolution_uses_core_matcher_and_full_share_metadata() -> None:
    """Core matching receives the complete metadata available in the share payload."""
    provider = _provider()
    mass = _mass(provider)
    matched = _track(item_id="matched", version="Live")
    mass.music.get_controller.return_value.get_item_by_external_id = AsyncMock(return_value=None)
    mass.music.tracks.enrich_provider_mappings = AsyncMock(
        return_value=SimpleNamespace(matches=(_core_match(matched, TrackMatchConfidence.LIKELY),))
    )
    payload = SharePayload(
        type="track",
        title="Track",
        artist="Artist",
        version="Live",
        album="Album",
        isrc=ISRC,
        duration=218,
    )

    assert await provider._resolve_media_item(payload) is matched
    core_track = mass.music.tracks.enrich_provider_mappings.await_args.args[0]
    assert core_track.name == "Track"
    assert core_track.version == "Live"
    assert core_track.duration == 218
    assert core_track.album is None
    assert core_track.external_ids == {(ExternalID.ISRC, ISRC)}
    assert core_track.artists[0].name == "Artist"
    mass.music.search.assert_not_called()
    enrichment_call = mass.music.tracks.enrich_provider_mappings.await_args
    assert enrichment_call.kwargs["minimum_confidence"] == TrackMatchConfidence.LIKELY
    assert enrichment_call.kwargs["provider_instance_ids"] == set()
    assert enrichment_call.kwargs["search_non_streaming_without_mapping"] is True


async def test_core_search_accepts_matching_artist_with_featured_credit_drift() -> None:
    """Core ISRC matches tolerate services that omit a featured artist credit."""
    provider = _provider()
    mass = _mass(provider)
    candidate = _track(
        item_id="reused-isrc",
        artist="Artist",
        duration=218,
        external_ids={(ExternalID.ISRC, ISRC)},
    )
    mass.music.get_controller.return_value.get_item_by_external_id = AsyncMock(return_value=None)
    mass.music.tracks.enrich_provider_mappings = AsyncMock(
        return_value=SimpleNamespace(matches=(_core_match(candidate, TrackMatchConfidence.LIKELY),))
    )

    result = await provider._resolve_media_item(
        SharePayload(
            type="track",
            title="Track",
            artist="Artist, Featured Artist",
            artists=["Artist", "Featured Artist"],
            isrc=ISRC,
            duration=218,
        )
    )

    assert result is candidate


async def test_core_search_rejects_reused_isrc_with_different_lead_artist() -> None:
    """A reused ISRC cannot override a different primary artist in Core search."""
    provider = _provider()
    mass = _mass(provider)
    candidate = _track(
        item_id="reused-isrc",
        artist="Other Artist",
        duration=218,
        external_ids={(ExternalID.ISRC, ISRC)},
    )
    mass.music.get_controller.return_value.get_item_by_external_id = AsyncMock(return_value=None)
    mass.music.tracks.enrich_provider_mappings = AsyncMock(
        return_value=SimpleNamespace(matches=(_core_match(candidate, TrackMatchConfidence.LIKELY),))
    )

    result = await provider._resolve_media_item(
        SharePayload(
            type="track",
            title="Track",
            artist="Artist",
            isrc=ISRC,
            duration=218,
        )
    )

    assert result is None


async def test_core_search_rejects_conflicting_featured_artist_credit() -> None:
    """A shared ISRC cannot override a conflicting secondary artist credit."""
    provider = _provider()
    mass = _mass(provider)
    candidate = Track(
        item_id="reused-isrc",
        provider="catalog--1",
        name="Song",
        duration=218,
        external_ids={(ExternalID.ISRC, ISRC)},
        provider_mappings={
            ProviderMapping(
                item_id="reused-isrc",
                provider_domain="catalog",
                provider_instance="catalog--1",
                available=True,
            )
        },
        artists=UniqueList(
            [
                Artist(
                    item_id="first", provider="catalog--1", name="First", provider_mappings=set()
                ),
                Artist(
                    item_id="other", provider="catalog--1", name="Other", provider_mappings=set()
                ),
            ]
        ),
    )
    mass.music.get_controller.return_value.get_item_by_external_id = AsyncMock(return_value=None)
    mass.music.tracks.enrich_provider_mappings = AsyncMock(
        return_value=SimpleNamespace(matches=(_core_match(candidate, TrackMatchConfidence.LIKELY),))
    )

    result = await provider._resolve_media_item(
        SharePayload(
            type="track",
            title="Song",
            artist="First, Second",
            artists=["First", "Second"],
            isrc=ISRC,
            duration=218,
        )
    )

    assert result is None


@pytest.mark.parametrize(("second_artist", "expected_match"), [("Second", True), ("Other", False)])
async def test_isrc_match_requires_all_artist_credits(
    second_artist: str, expected_match: bool
) -> None:
    """An ISRC hit is accepted only when every credited artist matches."""
    provider = _provider()
    mass = _mass(provider)
    candidate = Track(
        item_id="candidate",
        provider="catalog--1",
        name="Song",
        duration=218,
        external_ids={(ExternalID.ISRC, ISRC)},
        provider_mappings={
            ProviderMapping(
                item_id="candidate",
                provider_domain="catalog",
                provider_instance="catalog--1",
                available=True,
            )
        },
        artists=UniqueList(
            [
                Artist(item_id="one", provider="catalog--1", name="First", provider_mappings=set()),
                Artist(
                    item_id="two",
                    provider="catalog--1",
                    name=second_artist,
                    provider_mappings=set(),
                ),
            ]
        ),
    )
    mass.music.get_controller.return_value.get_item_by_external_id = AsyncMock(
        return_value=candidate
    )
    mass.music.tracks.get = AsyncMock(return_value=candidate)
    payload = SharePayload(
        type="track",
        title="Song",
        artist="First, Second",
        artists=["First", "Second"],
        duration=218,
        isrc=ISRC,
    )

    result = await provider._resolve_media_item(payload, allow_search=False)

    assert (result is candidate) is expected_match
    assert mass.music.tracks.get.await_count == int(expected_match)


async def test_track_resolution_returns_none_without_core_match() -> None:
    """A failed or ambiguous core enrichment never opens an arbitrary candidate."""
    provider = _provider()
    mass = _mass(provider)
    payload = SharePayload(type="track", title="Track", artist="Artist")

    assert await provider._resolve_media_item(payload) is None
    mass.music.tracks.enrich_provider_mappings.assert_awaited_once()


async def test_track_resolution_rejects_core_loose_match() -> None:
    """Core loose title matches are insufficient to open a shared track."""
    provider = _provider()
    mass = _mass(provider)
    candidate = _track(item_id="loose")
    mass.music.get_controller.return_value.get_item_by_external_id = AsyncMock(return_value=None)
    mass.music.tracks.enrich_provider_mappings = AsyncMock(
        return_value=SimpleNamespace(matches=(_core_match(candidate, TrackMatchConfidence.LOOSE),))
    )

    assert (
        await provider._resolve_media_item(
            SharePayload(type="track", title="Track", artist="Artist")
        )
        is None
    )
    assert (
        mass.music.tracks.enrich_provider_mappings.await_args.kwargs["minimum_confidence"]
        == TrackMatchConfidence.LIKELY
    )


async def test_track_resolution_selects_highest_core_confidence() -> None:
    """The best confidence tier wins, with deterministic mapping selection."""
    provider = _provider()
    mass = _mass(provider)
    lower = _track(item_id="lower", artist="Artist")
    higher = _track(item_id="higher", artist="Artist")
    mass.music.tracks.enrich_provider_mappings = AsyncMock(
        return_value=SimpleNamespace(
            matches=(
                _core_match(lower, TrackMatchConfidence.LIKELY),
                _core_match(higher, TrackMatchConfidence.EXACT),
            )
        )
    )

    assert (
        await provider._resolve_media_item(
            SharePayload(type="track", title="Track", artist="Artist")
        )
        is higher
    )


async def test_playlist_preview_uses_core_track_matcher() -> None:
    """Preview matching uses the same Core evidence path as share resolution."""
    provider = _provider()
    mass = _mass(provider)
    matched = _track(item_id="matched", external_ids={(ExternalID.ISRC, ISRC)})
    mass.music.get_controller.return_value.get_item_by_external_id = AsyncMock(return_value=None)
    mass.music.tracks.enrich_provider_mappings = AsyncMock(
        return_value=SimpleNamespace(matches=(_core_match(matched, TrackMatchConfidence.LIKELY),))
    )
    encoded = SharePayload(
        type="playlist",
        title="Playlist",
        tracks=[TrackStub(title="Track", artist="Artist", isrc=ISRC, duration=218)],
    ).to_base64()

    preview = await provider.preview_shared_playlist(encoded)

    assert len(preview) == 1
    assert preview[0].item_id == "matched"
    assert preview[0].position == 1
    core_track = mass.music.tracks.enrich_provider_mappings.await_args.args[0]
    assert core_track.duration == 218
    assert core_track.get_external_id(ExternalID.ISRC) == ISRC
    mass.config.get_provider_configs.assert_awaited_once_with(provider_type=ProviderType.MUSIC)
    enrichment_call = mass.music.tracks.enrich_provider_mappings.await_args
    assert enrichment_call.kwargs["provider_instance_ids"] == set()
    assert enrichment_call.kwargs["search_non_streaming_without_mapping"] is True


async def test_search_provider_scope_filters_disabled_builtin_and_hidden_sources() -> None:
    """Only enabled music providers visible to the caller enter Core search."""
    provider = _provider()
    mass = _mass(provider)
    user = object()
    mass.config.get_provider_configs = AsyncMock(
        return_value=[
            SimpleNamespace(instance_id="tidal--visible", domain="tidal", enabled=True),
            SimpleNamespace(instance_id="jellyfin--visible", domain="jellyfin", enabled=True),
            SimpleNamespace(instance_id="tidal--disabled", domain="tidal", enabled=False),
            SimpleNamespace(instance_id="builtin", domain="builtin", enabled=True),
        ]
    )
    with (
        patch(
            "music_assistant.providers.share_provider.resolver.get_current_user",
            return_value=user,
        ),
        patch(
            "music_assistant.providers.share_provider.resolver.visible_music_sources",
            return_value=["tidal--visible", "jellyfin--visible"],
        ),
    ):
        provider_instances = await provider._searchable_music_provider_instances()

    assert provider_instances == {"tidal--visible", "jellyfin--visible"}
    mass.config.get_provider_configs.assert_awaited_once_with(provider_type=ProviderType.MUSIC)


async def test_build_album_payload_includes_barcode_mbid_and_provider_link() -> None:
    """Album shares carry the identifiers and provider links available to the sender."""
    provider = _provider()
    album = MagicMock()
    album.media_type = MediaType.ALBUM
    album.name = "Album"
    album.version = "Deluxe"
    album.artist_str = "Artist"
    album.image = None
    album.provider_mappings = [
        MagicMock(
            provider_domain="spotify",
            item_id="album/123",
            url="https://open.spotify.com/album/album%2F123",
        )
    ]
    album.get_external_id.side_effect = {
        ExternalID.MB_ALBUM: "album-mbid",
        ExternalID.BARCODE: BARCODE,
    }.__getitem__

    payload = await provider._build_payload(album)

    assert payload.type == "album"
    assert payload.version == "Deluxe"
    assert payload.mbid == "album-mbid"
    assert payload.barcode == BARCODE
    assert payload.provider_refs == {"spotify": "/album/album%2F123"}


async def test_build_track_payload_includes_duration_for_core_matching() -> None:
    """New individual-track shares include duration as matching evidence."""
    provider = _provider()
    track = _track(item_id="track", duration=218)

    payload = await provider._build_payload(track)

    assert payload.duration == 218


async def test_track_payload_preserves_separate_artist_credits() -> None:
    """New individual-track shares preserve structured credits for Core matching."""
    provider = _provider()
    track = Track(
        item_id="multi-artist",
        provider="catalog--1",
        name="Song",
        duration=218,
        provider_mappings=set(),
        artists=UniqueList(
            [
                Artist(item_id="one", provider="catalog--1", name="First", provider_mappings=set()),
                Artist(
                    item_id="two", provider="catalog--1", name="Second", provider_mappings=set()
                ),
            ]
        ),
    )

    payload = await provider._build_payload(track)
    reconstructed = provider._track_from_payload(payload)

    assert payload.artist == "First, Second"
    assert payload.artists == ["First", "Second"]
    assert [artist.name for artist in reconstructed.artists] == ["First", "Second"]


async def test_album_payload_preserves_separate_artist_credits() -> None:
    """Album payloads preserve artist credits as separate structured values."""
    provider = _provider()
    album = Album(
        item_id="album",
        provider="catalog--1",
        name="Album",
        version="Deluxe",
        provider_mappings=set(),
        artists=UniqueList(
            [
                Artist(item_id="one", provider="catalog--1", name="First", provider_mappings=set()),
                Artist(
                    item_id="two", provider="catalog--1", name="Second", provider_mappings=set()
                ),
            ]
        ),
    )

    payload = await provider._build_payload(album)
    reference = provider._album_reference(payload)

    assert payload.artists == ["First", "Second"]
    assert provider._matches_album_reference(payload, reference, album)


def test_name_only_album_is_not_reconstructed_as_a_builtin_mapping() -> None:
    """An album label stays share metadata rather than a fetchable builtin identity."""
    provider = _provider()
    payload = SharePayload(type="track", title="Track", artist="Artist", album="Album")

    item = provider._playlist_item_from_payload(payload)
    reconstructed = provider._track_from_payload(payload)

    assert item.album is None
    assert reconstructed.album is None
    assert SharePayload.from_base64(item.path.removeprefix("share://track/")).album == "Album"


async def test_album_search_requires_matching_edition() -> None:
    """Album search uses core comparison and rejects unknown or different editions."""
    provider = _provider()
    standard = Album(
        item_id="standard",
        provider="catalog--1",
        name="Album",
        version="Standard",
        provider_mappings=set(),
        artists=UniqueList(
            [
                Artist(
                    item_id="artist", provider="catalog--1", name="Artist", provider_mappings=set()
                )
            ]
        ),
    )
    deluxe = replace(standard, item_id="deluxe", version="Deluxe")
    results = MagicMock(tracks=[], albums=[standard, deluxe], artists=[])
    _mass(provider).music.search = AsyncMock(return_value=results)
    payload = SharePayload(type="album", title="Album", artist="Artist", version="Deluxe")

    assert await provider._resolve_media_item(payload) is deluxe

    results.albums = [replace(deluxe, version="")]
    assert await provider._resolve_media_item(payload) is None


async def test_album_search_does_not_choose_between_duplicate_core_matches() -> None:
    """Duplicate albums with equal core evidence remain unresolved."""
    provider = _provider()
    album = Album(
        item_id="album-one",
        provider="catalog--1",
        name="Album",
        provider_mappings=set(),
        artists=UniqueList(
            [
                Artist(
                    item_id="artist", provider="catalog--1", name="Artist", provider_mappings=set()
                )
            ]
        ),
    )
    duplicate = replace(album, item_id="album-two")
    _mass(provider).music.search = AsyncMock(
        return_value=MagicMock(albums=[album, duplicate], tracks=[], artists=[])
    )

    assert (
        await provider._resolve_media_item(
            SharePayload(type="album", title="Album", artist="Artist")
        )
        is None
    )


async def test_artist_search_does_not_choose_between_homonyms() -> None:
    """Artists with the same name but no matching ID remain unresolved."""
    provider = _provider()
    artist = Artist(
        item_id="artist-one",
        provider="catalog--1",
        name="Artist",
        provider_mappings=set(),
    )
    homonym = replace(artist, item_id="artist-two")
    _mass(provider).music.search = AsyncMock(
        return_value=MagicMock(artists=[artist, homonym], albums=[], tracks=[])
    )

    assert await provider._resolve_media_item(SharePayload(type="artist", title="Artist")) is None


async def test_album_search_collapses_matches_with_the_same_mbid() -> None:
    """Cross-provider album results with the same MusicBrainz ID are one match."""
    provider = _provider()
    album = Album(
        item_id="album-one",
        provider="catalog-one",
        name="Album",
        external_ids={(ExternalID.MB_ALBUM, "shared-album-mbid")},
        provider_mappings=set(),
        artists=UniqueList(
            [
                Artist(
                    item_id="artist-one",
                    provider="catalog-one",
                    name="Artist",
                    provider_mappings=set(),
                )
            ]
        ),
    )
    duplicate = replace(album, item_id="album-two", provider="catalog-two")
    _mass(provider).music.search = AsyncMock(
        return_value=MagicMock(albums=[album, duplicate], tracks=[], artists=[])
    )

    result = await provider._resolve_media_item(SharePayload(type="album", title="Album"))

    assert result is album


async def test_artist_search_collapses_matches_with_the_same_mbid() -> None:
    """Cross-provider artist results with the same MusicBrainz ID are one match."""
    provider = _provider()
    artist = Artist(
        item_id="artist-one",
        provider="catalog-one",
        name="Artist",
        external_ids={(ExternalID.MB_ARTIST, "shared-artist-mbid")},
        provider_mappings=set(),
    )
    duplicate = replace(artist, item_id="artist-two", provider="catalog-two")
    _mass(provider).music.search = AsyncMock(
        return_value=MagicMock(artists=[artist, duplicate], albums=[], tracks=[])
    )

    result = await provider._resolve_media_item(SharePayload(type="artist", title="Artist"))

    assert result is artist


@pytest.mark.parametrize("use_barcode", [False, True])
async def test_album_resolution_accepts_ep_title_variant_across_providers(
    use_barcode: bool,
) -> None:
    """Album titles with an EP suffix match across providers in search and barcode paths."""
    provider = _provider()
    mass = _mass(provider)
    candidate = Album(
        item_id="album",
        provider="qobuz--1",
        name="Album A - EP",
        artists=UniqueList(
            [
                Artist(
                    item_id="artist",
                    provider="qobuz--1",
                    name="Artist",
                    provider_mappings=set(),
                )
            ]
        ),
        provider_mappings=set(),
    )
    controller = mass.music.get_controller.return_value
    controller.get_item_by_external_id = AsyncMock(return_value=candidate)
    mass.music.search = AsyncMock(return_value=MagicMock(albums=[candidate], tracks=[], artists=[]))
    payload = SharePayload(
        type="album",
        title="Album A",
        artist="Artist",
        barcode=BARCODE if use_barcode else None,
    )

    result = await provider._resolve_media_item(payload)

    assert result is candidate
    if use_barcode:
        mass.music.search.assert_not_called()
    else:
        mass.music.search.assert_awaited_once()


async def test_playlist_export_includes_all_tracks_duplicates_and_mapped_album_names() -> None:
    """Large playlist shares preserve every entry and compact album metadata."""
    provider = _provider()
    album = ItemMapping(item_id="album", provider="test", name="Album", media_type=MediaType.ALBUM)
    tracks = [
        Track(
            item_id=str(index),
            provider="test",
            name=f"Track {index}",
            album=album,
            provider_mappings=set(),
        )
        for index in range(501)
    ]
    tracks.append(tracks[0])

    async def iter_playlist_tracks(
        item_id: str, provider_instance_id_or_domain: str
    ) -> AsyncGenerator[Track]:
        assert item_id == "playlist"
        assert provider_instance_id_or_domain == "test"
        for track in tracks:
            yield track

    _mass(provider).music.playlists.iter_tracks = MagicMock(side_effect=iter_playlist_tracks)
    playlist = MagicMock(item_id="playlist", provider="test")

    exported = await provider._build_playlist_tracks(playlist)

    assert len(exported) == 502
    assert exported[-1].title == exported[0].title
    assert exported[500].title == "Track 500"
    assert all(track.album == "Album" for track in exported)
    _mass(provider).music.playlists.iter_tracks.assert_called_once_with("playlist", "test")


async def test_playlist_export_fails_when_a_later_page_fails() -> None:
    """A failed later playlist page cannot produce a truncated share."""
    provider = _provider()
    first_track = _track(item_id="first-track")

    async def iter_playlist_tracks(
        item_id: str, provider_instance_id_or_domain: str
    ) -> AsyncGenerator[Track]:
        assert item_id == "playlist"
        assert provider_instance_id_or_domain == "catalog--1"
        yield first_track
        raise ProviderUnavailableError("later page failed")

    _mass(provider).music.playlists.iter_tracks = MagicMock(side_effect=iter_playlist_tracks)
    playlist = MagicMock(item_id="playlist", provider="catalog--1")

    with pytest.raises(ProviderUnavailableError, match="later page failed"):
        await provider._build_playlist_tracks(playlist)


@pytest.mark.parametrize("include_track", [False, True])
async def test_playlist_export_rejects_unsupported_media_entries(include_track: bool) -> None:
    """A playlist with unsupported entries is rejected rather than truncated."""
    provider = _provider()
    playlist_tracks: list[Track | Radio] = [_track(item_id="track")] if include_track else []
    playlist_tracks.append(
        Radio(item_id="radio", provider="builtin", name="Radio", provider_mappings=set())
    )

    async def iter_playlist_tracks(
        item_id: str, provider_instance_id_or_domain: str
    ) -> AsyncGenerator[Track | Radio]:
        assert item_id == "playlist"
        assert provider_instance_id_or_domain == "builtin"
        for item in playlist_tracks:
            yield item

    _mass(provider).music.playlists.iter_tracks = MagicMock(side_effect=iter_playlist_tracks)
    playlist = Playlist(
        item_id="playlist", provider="builtin", name="Playlist", provider_mappings=set()
    )

    with pytest.raises(InvalidDataError, match="radio entries"):
        await provider._build_payload(playlist)


async def test_share_base_url_is_an_instance_config_option() -> None:
    """The config controller can discover the share URL option on the provider."""
    entries = await _provider().get_config_entries()
    assert len(entries) == 1
    assert entries[0].key == "share_base_url"
    assert entries[0].default_value == "https://share.music-assistant.io"


@pytest.mark.parametrize(
    "url",
    [
        "https://archive.org/download/album/cover:front.jpg",
        "https://archive.org/download/album/cover.jpg?part=front:2",
        "https://ia800.archive.org/download/album/cover:front.jpg?format=full:2",
    ],
)
def test_artwork_refs_roundtrip_colons_in_path_and_query(url: str) -> None:
    """Artwork paths and queries retain colons when compacted and expanded."""
    ref = compact_artwork_ref(url)

    assert ref is not None
    assert expand_artwork_ref(ref) == url


def test_build_provider_links_keeps_apple_music_canonical_url() -> None:
    """Apple mappings carry a canonical storefront URL that must not be rebuilt."""
    item = MagicMock()
    item.provider_mappings = [
        MagicMock(
            provider_domain="apple_music",
            item_id="123456789",
            url="https://music.apple.com/us/album/album-name/987654321?i=123456789",
        )
    ]

    assert build_provider_links(item) == {
        "apple_music": "https://music.apple.com/us/album/album-name/987654321?i=123456789"
    }


def test_build_provider_refs_omits_provider_hosts() -> None:
    """Share payload references keep localized paths without full URLs."""
    item = MagicMock()
    item.provider_mappings = [
        MagicMock(
            provider_domain="apple_music",
            item_id="123456789",
            url="https://music.apple.com/at/album/a-little-bit-off/1487244856?i=1487244862",
        ),
        MagicMock(
            provider_domain="spotify",
            item_id="track-id",
            url="https://open.spotify.com/track/track-id",
        ),
    ]

    assert build_provider_refs(item) == {
        "apple_music": "/at/album/a-little-bit-off/1487244856?i=1487244862",
        "spotify": "/track/track-id",
    }


def test_build_provider_links_uses_only_mapping_urls() -> None:
    """Mappings without a provider URL do not get a guessed link."""
    item = MagicMock()
    item.provider_mappings = [
        MagicMock(provider_domain="qobuz", item_id="Example Performer", url=None),
        MagicMock(provider_domain="qobuz", item_id="12345", url=None),
    ]
    assert build_provider_links(item) == {}

    item.provider_mappings = [
        MagicMock(
            provider_domain="qobuz",
            item_id="Artist Name",
            url="https://open.qobuz.com/artist/54321",
        ),
    ]
    assert build_provider_links(item) == {"qobuz": "https://open.qobuz.com/artist/54321"}


def test_payload_uses_compact_provider_and_artwork_refs_without_version_field() -> None:
    """Share metadata is encoded without a redundant protocol-version field."""
    apple_url = "https://music.apple.com/at/album/123?i=456"
    art_url = "https://is1-ssl.mzstatic.com/image/thumb/cover/1000x1000bb.jpg"
    art_ref = compact_artwork_ref(art_url)
    assert art_ref is not None
    payload = SharePayload(
        type="track",
        title="Track",
        version="Live",
        art_ref=art_ref,
        provider_refs={
            "apple_music": build_provider_refs(
                MagicMock(
                    provider_mappings=[
                        MagicMock(
                            provider_domain="apple_music",
                            item_id="456",
                            url=apple_url,
                        )
                    ]
                )
            )["apple_music"]
        },
    )

    encoded = payload.to_base64()
    decoded = SharePayload.from_base64(encoded)
    decoded_json = base64.urlsafe_b64decode(encoded).decode()

    assert decoded.art_ref == art_ref
    assert decoded.version == "Live"
    assert decoded.provider_refs == {
        "apple_music": "/at/album/123?i=456",
    }
    assert expand_artwork_ref(art_ref) == art_url
    assert expand_provider_ref("apple_music", decoded.provider_refs["apple_music"]) == apple_url
    assert "https://" not in decoded_json
    assert '"v"' not in decoded_json


def test_playlist_payload_omits_null_track_fields() -> None:
    """Nested playlist stubs omit null fields from their serialized payload."""
    payload = SharePayload(
        type="playlist",
        title="Playlist",
        tracks=[TrackStub(title="Unversioned track")],
    )

    decoded = json.loads(base64.urlsafe_b64decode(payload.to_base64()).decode())

    assert decoded["tracks"] == [{"title": "Unversioned track"}]


def test_build_provider_links_ignores_invalid_mapping_urls() -> None:
    """Provider mappings only expose URLs on their approved HTTPS host."""
    item = MagicMock()
    item.provider_mappings = [
        MagicMock(
            provider_domain="apple_music",
            item_id="123456789",
            url="javascript:alert(1)",
        )
    ]

    assert build_provider_links(item) == {}


async def test_playlist_resolution_is_read_only() -> None:
    """Resolving a playlist preview never creates or edits a library playlist."""
    provider = _provider()
    encoded = SharePayload(
        type="playlist",
        title="Playlist",
        tracks=[TrackStub(title="Track", artist="Artist", isrc=ISRC)],
    ).to_base64()

    assert await provider.resolve_share_link(encoded) is None
    _mass(provider).music.playlists.create_playlist.assert_not_called()
    _mass(provider).music.playlists.add_playlist_tracks.assert_not_called()


async def test_import_shared_playlist_uses_existing_matching_importer() -> None:
    """A confirmed playlist import preserves metadata for the built-in matcher."""
    provider = _provider()
    playlist = MagicMock()
    mass = _mass(provider)
    mass.music.playlists.import_playlist = AsyncMock(return_value=playlist)
    encoded = SharePayload(
        type="playlist",
        title="Shared Mix",
        tracks=[
            TrackStub(
                title="Track",
                artist="Artist",
                version="Live",
                isrc=ISRC,
                mbid="recording-mbid",
                mb_track="release-track-mbid",
                album="Album",
                duration=218,
            )
        ],
    ).to_base64()

    result = await provider.import_shared_playlist(encoded)

    assert result is playlist
    mass.music.playlists.import_playlist.assert_awaited_once()
    await_args = mass.music.playlists.import_playlist.await_args
    assert await_args is not None
    m3u_data = await_args.args[0]
    imported_items = parse_m3u(m3u_data)
    assert len(imported_items) == 1
    imported_track = SharePayload.from_base64(imported_items[0].path.removeprefix("share://track/"))
    assert imported_track.type == "track"
    assert imported_track.title == "Track"
    assert imported_track.artist == "Artist"
    assert imported_track.version == "Live"
    assert imported_track.album == "Album"
    assert imported_track.isrc == ISRC
    assert imported_track.mbid == "recording-mbid"
    assert imported_track.mb_track == "release-track-mbid"
    assert imported_items[0].length == "218"
    decoded_playlist = SharePayload.from_base64(encoded)
    assert decoded_playlist.tracks is not None
    assert decoded_playlist.tracks[0].duration == 218
    assert imported_track.isrc == ISRC
    assert imported_items[0].metadata == {
        "media_type": "track",
        "name": "Track",
        "version": "Live",
        "mbid": "recording-mbid",
        "mb_track": "release-track-mbid",
    }
    assert imported_items[0].artists[0].name == "Artist"
    assert imported_items[0].album is None
    assert imported_track.album == "Album"
    shared_track = construct_media_item_from_playlist_item(imported_items[0], mass)
    shared_track_without_duration = construct_media_item_from_playlist_item(
        PlaylistItem(
            path="share://track/no-duration",
            title="Track",
            metadata={"media_type": "track", "name": "Track", "isrc": ISRC},
            artists=[ArtistInfo("Artist", "", "", "")],
            album=AlbumInfo("Album", "", "", ""),
        ),
        mass,
    )
    catalog_track = Track(
        item_id="catalog-track",
        provider="catalog--1",
        name="Track",
        version="Live",
        duration=218,
        external_ids={(ExternalID.ISRC, ISRC)},
        provider_mappings=set(),
        artists=UniqueList(
            [
                Artist(
                    item_id="artist", provider="catalog--1", name="Artist", provider_mappings=set()
                )
            ]
        ),
    )
    assert isinstance(shared_track, Track)
    assert compare_track(catalog_track, shared_track)
    assert isinstance(shared_track_without_duration, Track)
    assert not compare_track(catalog_track, shared_track_without_duration)
    mass.music.playlists.import_playlist.assert_awaited_once_with(
        m3u_data,
        library_matching=True,
        match_policy=PlaylistMatchPolicy.SAME_RECORDING,
    )


async def test_import_shared_playlist_preserves_separate_artist_credits() -> None:
    """M3U import preserves the separate artist list in share metadata and items."""
    provider = _provider()
    mass = _mass(provider)
    mass.music.playlists.import_playlist = AsyncMock(return_value=MagicMock())
    payload = SharePayload(
        type="playlist",
        title="Shared Mix",
        tracks=[
            TrackStub(
                title="Song",
                artist="First, Second",
                artists=["First", "Second"],
                duration=218,
            )
        ],
    )

    await provider.import_shared_playlist(payload.to_base64())

    m3u_data = mass.music.playlists.import_playlist.await_args.args[0]
    imported_item = parse_m3u(m3u_data)[0]
    shared_payload = SharePayload.from_base64(imported_item.path.removeprefix("share://track/"))
    assert shared_payload.artists == ["First", "Second"]
    assert [artist.name for artist in imported_item.artists] == ["First", "Second"]


async def test_import_shared_playlist_rejects_m3u_extension_delimiter() -> None:
    """A share title cannot inject structured metadata through the M3U separator."""
    provider = _provider()
    mass = _mass(provider)
    encoded = SharePayload(
        type="playlist",
        title="Shared Mix",
        tracks=[TrackStub(title="Song||isrc=USRC17607839", artist="Artist")],
    ).to_base64()

    with pytest.raises(InvalidDataError, match="unsupported M3U delimiter"):
        await provider.import_shared_playlist(encoded)

    mass.music.playlists.import_playlist.assert_not_called()


async def test_playlist_payload_preserves_track_version_and_artist_credits() -> None:
    """Playlist stubs retain version metadata and the track's artist credit string."""
    provider = _provider()
    track = Track(
        item_id="track",
        provider="catalog--1",
        name="Song",
        version="Acoustic",
        duration=180,
        provider_mappings=set(),
        artists=UniqueList(
            [
                Artist(
                    item_id="artist-one",
                    provider="catalog--1",
                    name="First Artist",
                    provider_mappings=set(),
                ),
                Artist(
                    item_id="artist-two",
                    provider="catalog--1",
                    name="Second Artist",
                    provider_mappings=set(),
                ),
            ]
        ),
    )
    mass = _mass(provider)

    async def iter_playlist_tracks(
        item_id: str, provider_instance_id_or_domain: str
    ) -> AsyncGenerator[Track]:
        assert item_id == "playlist"
        assert provider_instance_id_or_domain == "catalog--1"
        yield track

    mass.music.playlists.iter_tracks = MagicMock(side_effect=iter_playlist_tracks)
    playlist = MagicMock(item_id="playlist", provider="catalog--1")

    stubs = await provider._build_playlist_tracks(playlist)

    assert len(stubs) == 1
    assert stubs[0].artist == "First Artist, Second Artist"
    assert stubs[0].artists == ["First Artist", "Second Artist"]
    assert stubs[0].version == "Acoustic"


async def test_import_isrc_only_track_requires_title_and_artist_corroboration() -> None:
    """Share import keeps the ISRC in its URI but not the lossy matcher metadata."""
    provider = _provider()
    mass = _mass(provider)
    mass.music.playlists.import_playlist = AsyncMock(return_value=MagicMock())
    encoded = SharePayload(
        type="playlist",
        title="Shared Mix",
        tracks=[TrackStub(title="Track", artist="Artist", isrc=ISRC, duration=218)],
    ).to_base64()

    await provider.import_shared_playlist(encoded)
    imported_m3u = mass.music.playlists.import_playlist.await_args.args[0]
    imported_item = parse_m3u(imported_m3u)[0]
    shared_track = construct_media_item_from_playlist_item(imported_item, mass)
    shared_item_without_duration = construct_media_item_from_playlist_item(
        replace(imported_item, length=None), mass
    )
    catalog_track = Track(
        item_id="catalog-track",
        provider="catalog--1",
        name="Track",
        duration=218,
        external_ids={(ExternalID.ISRC, ISRC)},
        provider_mappings=set(),
        artists=UniqueList(
            [
                Artist(
                    item_id="artist", provider="catalog--1", name="Artist", provider_mappings=set()
                )
            ]
        ),
    )

    assert isinstance(shared_track, Track)
    assert isinstance(shared_item_without_duration, Track)
    assert not shared_track.external_ids
    assert compare_track_evidence(catalog_track, shared_track) >= TrackMatchConfidence.LIKELY
    assert (
        compare_track_evidence(catalog_track, shared_item_without_duration)
        < TrackMatchConfidence.LIKELY
    )

    wrong_artist_track = replace(
        catalog_track,
        artists=UniqueList(
            [
                Artist(
                    item_id="other-artist",
                    provider="catalog--1",
                    name="Different Artist",
                    provider_mappings=set(),
                )
            ]
        ),
    )
    wrong_artist_track.external_ids.add((ExternalID.ISRC, ISRC))
    assert compare_track_evidence(wrong_artist_track, shared_track) == TrackMatchConfidence.NO_MATCH


async def test_import_shared_playlist_rejects_non_playlist_payload() -> None:
    """Only an explicit playlist payload can reach the write-capable importer."""
    provider = _provider()
    encoded = SharePayload(type="track", title="Track").to_base64()

    with pytest.raises(InvalidDataError):
        await provider.import_shared_playlist(encoded)

    _mass(provider).music.playlists.import_playlist.assert_not_called()


async def test_api_commands_require_library_read_and_unregister_on_unload() -> None:
    """Share commands are authenticated read operations and unregister cleanly."""
    provider = _provider()
    unregister_generate = MagicMock()
    unregister_resolve = MagicMock()
    unregister_import = MagicMock()
    unregister_preview = MagicMock()
    mass = _mass(provider)
    mass.register_api_command.side_effect = [
        unregister_generate,
        unregister_resolve,
        unregister_import,
        unregister_preview,
    ]

    await provider.handle_async_init()
    await provider.unload()

    assert mass.register_api_command.call_args_list == [
        call("share/generate", provider.generate_share_link, required_scope=Scope.LIBRARY_READ),
        call("share/resolve", provider.resolve_share_link, required_scope=Scope.LIBRARY_READ),
        call(
            "share/import_playlist",
            provider.import_shared_playlist,
            required_scope=Scope.LIBRARY_WRITE,
        ),
        call(
            "share/preview_playlist",
            provider.preview_shared_playlist,
            required_scope=Scope.LIBRARY_READ,
        ),
    ]
    unregister_generate.assert_called_once_with()
    unregister_resolve.assert_called_once_with()
    unregister_import.assert_called_once_with()
    unregister_preview.assert_called_once_with()
    mass.webserver.register_dynamic_route.assert_not_called()


async def test_preview_keeps_unmatched_and_duplicate_tracks_in_original_order() -> None:
    """Preview entries preserve identity and position without library writes."""
    provider = _provider()
    known = Track(item_id="42", provider="library", name="Known", provider_mappings=set())
    encoded = SharePayload(
        type="playlist",
        title="Playlist",
        tracks=[
            TrackStub(title="Known"),
            TrackStub(title="Missing", artist="Artist", album="Album"),
            TrackStub(title="Known"),
        ],
    ).to_base64()
    resolver = AsyncMock(side_effect=[known, None, known])
    with patch.object(ShareProvider, "_resolve_media_item", resolver):
        tracks = await provider.preview_shared_playlist(encoded)

    assert [track.name for track in tracks] == ["Known", "Missing", "Known"]
    assert [track.position for track in tracks] == [1, 2, 3]
    assert tracks[0] is not tracks[2]
    assert known.position is None
    assert not tracks[1].available
    assert tracks[1].provider_mappings == set()
    assert tracks[1].item_id.startswith("share-preview://track/")
    assert tracks[1].artists[0].name == "Artist"
    assert tracks[1].album is not None
    assert tracks[1].album.name == "Album"
    assert all(
        kwargs == {"hydrate_track": False, "search_provider_instance_ids": set()}
        for _, kwargs in resolver.call_args_list
    )
    assert resolver.await_count == 2
    _mass(provider).music.playlists.import_playlist.assert_not_called()


async def test_preview_skips_full_track_hydration_for_matched_tracks() -> None:
    """A preview returns the matched Track without per-track audio-analysis hydration."""
    provider = _provider()
    mass = _mass(provider)
    matched = Track(
        item_id="42",
        provider="library",
        name="Known",
        duration=218,
        external_ids={(ExternalID.ISRC, ISRC)},
        provider_mappings={
            ProviderMapping(
                item_id="42",
                provider_domain="catalog",
                provider_instance="catalog--1",
                available=True,
            )
        },
        artists=UniqueList(
            [
                Artist(
                    item_id="artist",
                    provider="catalog--1",
                    name="Artist",
                    provider_mappings=set(),
                )
            ]
        ),
    )
    controller = mass.music.get_controller.return_value
    controller.get_item_by_external_id = AsyncMock(return_value=matched)
    mass.music.tracks.get = AsyncMock()
    encoded = SharePayload(
        type="playlist",
        title="Playlist",
        tracks=[
            TrackStub(title="Known", artist="Artist", artists=["Artist"], isrc=ISRC, duration=218)
        ],
    ).to_base64()

    tracks = await provider.preview_shared_playlist(encoded)

    assert len(tracks) == 1
    assert tracks[0].item_id == matched.item_id
    assert tracks[0].name == "Known"
    assert tracks[0].position == 1
    mass.music.tracks.get.assert_not_called()


async def test_external_id_resolution_skips_unavailable_track_for_available_fallback() -> None:
    """An unavailable library ID hit does not block a playable MBID result."""
    provider = _provider()
    mass = _mass(provider)
    artist = Artist(
        item_id="artist",
        provider="catalog--1",
        name="Artist",
        provider_mappings=set(),
    )
    unavailable = Track(
        item_id="unavailable-track",
        provider="library",
        name="Song",
        duration=218,
        external_ids={(ExternalID.ISRC, ISRC)},
        provider_mappings={
            ProviderMapping(
                item_id="unavailable-track",
                provider_domain="catalog",
                provider_instance="catalog--unavailable",
                available=False,
            )
        },
        artists=UniqueList([artist]),
    )
    available = Track(
        item_id="available-track",
        provider="catalog--1",
        name="Song",
        duration=218,
        external_ids={
            (ExternalID.ISRC, ISRC),
            (ExternalID.MB_RECORDING, "recording-mbid"),
        },
        provider_mappings={
            ProviderMapping(
                item_id="available-track",
                provider_domain="catalog",
                provider_instance="catalog--1",
                available=True,
            )
        },
        artists=UniqueList([artist]),
    )
    controller = mass.music.get_controller.return_value
    controller.get_item_by_external_id = AsyncMock(side_effect=[unavailable, available])
    mass.music.tracks.get = AsyncMock(return_value=available)
    payload = SharePayload(
        type="track",
        title="Song",
        artist="Artist",
        artists=["Artist"],
        isrc=ISRC,
        mbid="recording-mbid",
        duration=218,
    )

    result = await provider.resolve_share_link(payload.to_base64())

    assert result is available
    assert controller.get_item_by_external_id.await_count == 2
    mass.music.tracks.get.assert_awaited_once_with(
        available.item_id, available.provider, allow_update_metadata=False
    )


async def test_preview_only_resolves_the_requested_block() -> None:
    """Scrolling resolves only the requested portion of a shared playlist."""
    provider = _provider()
    encoded = SharePayload(
        type="playlist",
        title="Playlist",
        tracks=[TrackStub(title=f"Track {index}") for index in range(55)],
    ).to_base64()
    resolver = AsyncMock(return_value=None)
    with patch.object(ShareProvider, "_resolve_media_item", resolver):
        tracks = await provider.preview_shared_playlist(encoded, offset=50, limit=50)
    assert [track.position for track in tracks] == [51, 52, 53, 54, 55]
    assert resolver.await_count == 5


@pytest.mark.parametrize(("offset", "limit"), [(-1, 50), (0, 0)])
async def test_preview_rejects_invalid_ranges(offset: int, limit: int) -> None:
    """Invalid preview ranges are rejected before resolving any tracks."""
    provider = _provider()
    with pytest.raises(InvalidDataError):
        await provider.preview_shared_playlist("", offset=offset, limit=limit)


async def test_api_registration_failure_unregisters_prior_commands() -> None:
    """A failed later registration removes commands already registered."""
    provider = _provider()
    unregister_generate = MagicMock()
    _mass(provider).register_api_command.side_effect = [
        unregister_generate,
        RuntimeError("command already registered"),
    ]

    with pytest.raises(RuntimeError, match="command already registered"):
        await provider.handle_async_init()

    unregister_generate.assert_called_once_with()


async def test_payload_rejects_unsupported_media_types() -> None:
    """Reject unknown media types before dispatching a share payload."""
    encoded = base64.urlsafe_b64encode(
        json.dumps({"type": "podcast", "title": "Podcast"}).encode()
    ).decode()

    with pytest.raises(ValueError, match="Unsupported share payload"):
        SharePayload.from_base64(encoded)


def test_payload_does_not_require_a_version_field() -> None:
    """The share payload contains no protocol-version marker."""
    encoded = base64.urlsafe_b64encode(
        json.dumps({"type": "track", "title": "Track"}).encode()
    ).decode()

    assert SharePayload.from_base64(encoded).title == "Track"


def test_track_payload_roundtrips_optional_duration() -> None:
    """Individual-track payloads preserve their optional duration."""
    payload = SharePayload(type="track", title="Track", duration=218)

    assert SharePayload.from_base64(payload.to_base64()).duration == 218


@pytest.mark.parametrize("duration", [True, -1, "218", 1.5])
def test_payload_rejects_invalid_track_duration(duration: object) -> None:
    """Individual-track duration must be a nonnegative integer."""
    encoded = base64.urlsafe_b64encode(
        json.dumps({"type": "track", "title": "Track", "duration": duration}).encode()
    ).decode()

    with pytest.raises(ValueError, match="Invalid share track duration"):
        SharePayload.from_base64(encoded)


@pytest.mark.parametrize("duration", [True, -1, "218", 1.5])
def test_payload_rejects_invalid_playlist_track_duration(duration: object) -> None:
    """Playlist durations are nonnegative integer seconds when present."""
    encoded = base64.urlsafe_b64encode(
        json.dumps(
            {
                "type": "playlist",
                "title": "Mix",
                "tracks": [{"title": "Track", "duration": duration}],
            }
        ).encode()
    ).decode()

    with pytest.raises(ValueError, match="Invalid share playlist track duration"):
        SharePayload.from_base64(encoded)


def test_payload_drops_invalid_optional_links_and_artwork() -> None:
    """A malformed optional ref does not invalidate otherwise usable share data."""
    encoded = base64.urlsafe_b64encode(
        json.dumps(
            {
                "type": "playlist",
                "title": "Playlist",
                "art_ref": "x:evil.example:/art.jpg",
                "provider_refs": {
                    "spotify": "/playlist/123",
                    "unknown": "/playlist/456",
                },
                "tracks": [
                    {
                        "title": "Track",
                        "provider_refs": {
                            "tidal": "/track/123",
                            "unknown": "/track/456",
                        },
                    }
                ],
            }
        ).encode()
    ).decode()

    payload = SharePayload.from_base64(encoded)

    assert payload.art_ref is None
    assert payload.provider_refs == {"spotify": "/playlist/123"}
    assert payload.tracks is not None
    assert payload.tracks[0].provider_refs == {"tidal": "/track/123"}
